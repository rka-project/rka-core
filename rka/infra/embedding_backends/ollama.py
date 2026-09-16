"""Ollama-native embedding backend.

Talks to Ollama's `/api/embeddings` endpoint. Critically distinct from
the OpenAI-compat backend in two ways:

  - Request body uses `prompt` (singular) not `input` (array):
        POST {base_url}/api/embeddings
        {"prompt": "text", "model": "<model>"}

  - Response body uses `embedding` (singular) not `data: [{embedding}]`:
        {"embedding": [...]}

That's why this is its own class — a `base_url` toggle on the OpenAI-compat
backend would be brittle since both the request AND response shapes differ.

Batching: Ollama's legacy `/api/embeddings` is single-prompt. We loop
sequentially; if performance matters, Phase 2 can swap to the newer
`/api/embed` (array support) without changing the Protocol.

Retry policy: same as OpenAI-compat — exponential backoff on 429/5xx.
"""

from __future__ import annotations

import asyncio
import logging
import time

import httpx

from rka.infra.embedding_backends.base import (
    ConnectionTestResult,
    EmbeddingConfigError as EmbeddingConfigError,
    reconcile_dim,
)
from rka.infra.embedding_resources import HTTPEmbeddingResourceLimits, request_timeout, run_http

logger = logging.getLogger(__name__)

_RETRY_SLEEPS_SECONDS: tuple[float, ...] = (0.5, 1.0, 2.0)


class OllamaBackend:
    """Ollama `/api/embeddings` HTTP client (singular-prompt, singular-response)."""

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        dim: int | None = None,
        timeout_seconds: float = 600.0,
        http_client: httpx.AsyncClient | None = None,
        resource_limits: dict | None = None,
    ) -> None:
        if not base_url:
            raise ValueError("ollama backend requires base_url")
        if not model:
            raise ValueError("ollama backend requires model")
        self._base_url = base_url.rstrip("/")
        self.resource_limits = HTTPEmbeddingResourceLimits.from_config(resource_limits)
        self._model = model
        self._dim: int = dim or 0
        self._timeout = request_timeout(timeout_seconds)
        self._http = http_client

    @property
    def dim(self) -> int:
        return self._dim

    @property
    def model_name(self) -> str:
        return self._model

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=self._timeout)
        return self._http

    async def _post_one(self, text: str) -> list[float]:
        url = f"{self._base_url}/api/embeddings"
        body = {"prompt": text, "model": self._model}
        client = self._client()
        last_exc: Exception | None = None
        for attempt, sleep_s in enumerate(_RETRY_SLEEPS_SECONDS, start=1):
            try:
                resp = await client.post(url, json=body)
            except httpx.HTTPError as exc:
                last_exc = exc
                if attempt == len(_RETRY_SLEEPS_SECONDS):
                    raise
                await asyncio.sleep(sleep_s)
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt == len(_RETRY_SLEEPS_SECONDS):
                    resp.raise_for_status()
                await asyncio.sleep(sleep_s)
                continue
            resp.raise_for_status()
            payload = resp.json()
            # Ollama's response: {"embedding": [...]}, NOT {"data": [{"embedding": [...]}]}
            vec = payload.get("embedding")
            if not isinstance(vec, list):
                raise RuntimeError(
                    f"ollama /api/embeddings: unexpected response shape "
                    f"(missing 'embedding' field): {payload!r}"
                )
            # T2.5 calibration: drift-check rather than silent mutate.
            self._dim = reconcile_dim(self._dim, len(vec))
            return vec
        if last_exc:
            raise last_exc
        raise RuntimeError("ollama embed: exhausted retries")

    async def embed(self, text: str, *, is_query: bool = False) -> list[float]:  # noqa: ARG002
        return (await self.embed_batch([text], is_query=is_query))[0]

    def validate_input(self, text: str, *, is_query: bool = False) -> None:
        self.resource_limits.plan([text])

    async def embed_batch(
        self, texts: list[str], *, is_query: bool = False  # noqa: ARG002
    ) -> list[list[float]]:
        if not texts:
            return []
        batches = self.resource_limits.plan(texts)

        async def infer():
            # /api/embeddings is single-prompt; one logical-call deadline spans
            # every request and retry, not a fresh deadline for each prompt.
            return [await self._post_one(text) for batch in batches for text in batch]

        return await run_http(
            infer, timeout=min(self._timeout, self.resource_limits.call_timeout_seconds)
        )

    async def test_connection(self) -> ConnectionTestResult:
        t0 = time.perf_counter()
        try:
            vec = await self.embed("rka test", is_query=True)
        except httpx.ConnectError as exc:
            return ConnectionTestResult(
                ok=False, detail=f"connection refused to {self._base_url}: {exc!s}"
            )
        except httpx.HTTPStatusError as exc:
            return ConnectionTestResult(
                ok=False,
                detail=(
                    f"HTTP {exc.response.status_code} from {self._base_url}: "
                    f"{exc.response.text[:200]}"
                ),
            )
        except Exception as exc:  # noqa: BLE001
            return ConnectionTestResult(ok=False, detail=f"unexpected error: {exc!s}")
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        return ConnectionTestResult(
            ok=True,
            detail=f"reached ollama at {self._base_url}; model={self._model}; dim={len(vec)}",
            detected_dim=len(vec),
            latency_ms=elapsed_ms,
        )
