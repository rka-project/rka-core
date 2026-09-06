"""Local FastEmbed backend (extracted from the historical
`rka/infra/embeddings.py:EmbeddingService`).

Preserves the prior production semantics exactly:

  - `nomic-ai/nomic-embed-text-v1.5` default model (768-dim)
  - `search_query: ` / `search_document: ` prefixes on Nomic-family models
  - first-use download (~520 MB for the non-quantized default) is performed
    lazily inside the model
    accessor; the FastEmbed library handles caching
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

from rka.infra.embedding_backends.base import (
    ConnectionTestResult,
    reconcile_dim,
)
from rka.infra.embedding_resources import EmbeddingResourceLimits, run_native

logger = logging.getLogger(__name__)


_NOMIC_PREFIX_MODELS = ("nomic-ai/nomic-embed-text",)

# v2.7.0.1: bound onnxruntime intra-op parallelism. The default behavior
# (intra_op_num_threads = CPU count) spawns N threads per inference, each
# allocating its own memory arena. On a 10-core Apple Silicon under Docker,
# this hit unbounded growth — peak rose 7.17→7.87 GiB when the ceiling went
# 7.75→9.21 GiB, signature of "consume whatever you give it". Capping at
# 2 keeps the working set bounded with acceptable per-call latency for the
# background worker. Override via RKA_EMBEDDING_THREADS env or the
# `threads=` constructor kwarg.
_DEFAULT_THREADS = 2


def _needs_nomic_prefix(model_name: str) -> bool:
    return any(model_name.startswith(p) for p in _NOMIC_PREFIX_MODELS)


class FastEmbedBackend:
    """In-process ONNX inference via the `fastembed` package."""

    def __init__(
        self,
        model_name: str = "nomic-ai/nomic-embed-text-v1.5",
        *,
        dim: int | None = None,
        threads: int | None = None,
        cache_dir: str | None = None,
        resource_limits: dict | None = None,
    ) -> None:
        self._model_name = model_name
        self.resource_limits = EmbeddingResourceLimits.from_config(resource_limits)
        self._model: Any = None
        # If `dim` is provided, that becomes the strict expectation enforced
        # by `reconcile_dim`. If omitted (None), default to nomic-v1.5's 768
        # for back-compat with pre-T2.5 callers; the first inference still
        # cross-checks via reconcile_dim and raises on real drift.
        self._dim: int = 768 if dim is None else dim
        self._uses_prefix = _needs_nomic_prefix(model_name)
        # v2.7.0.1: bound onnxruntime threading. Param > env > default.
        if threads is None:
            env_threads = os.getenv("RKA_EMBEDDING_THREADS")
            threads = int(env_threads) if env_threads else _DEFAULT_THREADS
        self._threads = max(1, threads)
        # v2.7.0.1: persistent model cache. When unset, fastembed defaults to
        # ~/.cache/fastembed which doesn't survive container recreate, causing
        # repeated model downloads (and HF rate-limiting under load). Setting
        # cache_dir to a volume-mounted path eliminates this.
        if cache_dir is None:
            cache_dir = os.getenv("RKA_EMBEDDING_CACHE_DIR") or None
        self._cache_dir = cache_dir

    @property
    def dim(self) -> int:
        return self._dim

    @property
    def model_name(self) -> str:
        return self._model_name

    def _get_model(self) -> Any:
        if self._model is None:
            from fastembed import TextEmbedding

            kwargs: dict[str, Any] = {
                "model_name": self._model_name,
                "threads": self._threads,
            }
            if self._cache_dir:
                kwargs["cache_dir"] = self._cache_dir

            logger.info(
                "Loading FastEmbed model: %s (threads=%d, cache_dir=%s; "
                "first uncached load downloads ~520MB for the default model)",
                self._model_name,
                self._threads,
                self._cache_dir or "<fastembed default>",
            )
            self._model = TextEmbedding(**kwargs)
        return self._model

    def _prefix(self, text: str, *, is_query: bool) -> str:
        if not self._uses_prefix:
            return text
        marker = "search_query: " if is_query else "search_document: "
        return f"{marker}{text}"

    def _sync_embed(self, texts: list[str], *, is_query: bool) -> list[list[float]]:
        batches = self.resource_limits.plan(texts, lambda t: self._prefix(t, is_query=is_query))
        return self._sync_embed_batches(batches)

    def _sync_embed_batches(self, batches: list[list[str]]) -> list[list[float]]:
        model = self._get_model()
        result = []
        for batch in batches:
            vectors = [v.tolist() for v in model.embed(batch, batch_size=len(batch))]
            if len(vectors) != len(batch):
                raise ValueError("fastembed returned a different number of vectors than inputs")
            for vector in vectors:
                self._dim = reconcile_dim(self._dim, len(vector))
            result.extend(vectors)
        return result

    def validate_input(self, text: str, *, is_query: bool = False) -> None:
        self.resource_limits.plan([text], lambda t: self._prefix(t, is_query=is_query))

    async def embed(self, text: str, *, is_query: bool = False) -> list[float]:
        out = await self.embed_batch([text], is_query=is_query)
        return out[0]

    async def embed_batch(
        self, texts: list[str], *, is_query: bool = False
    ) -> list[list[float]]:
        if not texts:
            return []
        batches = self.resource_limits.plan(texts, lambda t: self._prefix(t, is_query=is_query))
        return await run_native(
            lambda: self._sync_embed_batches(batches),
            timeout=self.resource_limits.call_timeout_seconds,
        )

    async def test_connection(self) -> ConnectionTestResult:
        # FastEmbed runs in-process; "test" means: can we load the model
        # + run a single embed?
        t0 = time.perf_counter()
        try:
            vec = await self.embed("rka test", is_query=True)
        except Exception as exc:  # noqa: BLE001
            return ConnectionTestResult(
                ok=False,
                detail=f"fastembed load/inference failed: {exc!s}",
            )
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        return ConnectionTestResult(
            ok=True,
            detail=f"fastembed model {self._model_name} ready (dim={len(vec)})",
            detected_dim=len(vec),
            latency_ms=elapsed_ms,
        )
