"""Pluggable embedding backends.

Three concrete implementations land here:

  - `fastembed.py`     — local ONNX inference via the `fastembed` package
  - `openai_compat.py` — HTTP to any OpenAI-compatible `/v1/embeddings`
                         endpoint (OpenAI API, LM Studio, vLLM, Together,
                         Anthropic-via-shim, etc.)
  - `ollama.py`        — HTTP to Ollama's native `/api/embeddings` (singular
                         `{"embedding": [...]}` response shape, NOT the
                         list-wrapped OpenAI shape)

Every backend honors the `EmbeddingBackend` Protocol declared in
`base.py`. Callers (chiefly `rka/infra/embeddings.py:EmbeddingService`)
go through `make_backend(config)` rather than instantiating concrete
classes directly, so swapping backends is a config-file change, not a
code change.
"""

from __future__ import annotations

from typing import Any

from rka.infra.embedding_backends.base import (
    ConnectionTestResult,
    EmbeddingBackend,
    EmbeddingConfigError,
    reconcile_dim,
)

__all__ = [
    "ConnectionTestResult",
    "EmbeddingBackend",
    "EmbeddingConfigError",
    "make_backend",
    "reconcile_dim",
]


def make_backend(config: dict[str, Any]) -> EmbeddingBackend:
    """Construct an `EmbeddingBackend` from a config dict.

    The config dict shape mirrors `/data/embedding_config.json`:

        {"backend": "openai_compat" | "ollama" | "fastembed",
         "config": {backend-specific fields...}}

    Raises `ValueError` if `backend` is missing or unknown so callers can
    map it to a 422 `embedding_config_invalid` response (Affordance-G
    pattern).
    """
    backend_kind = config.get("backend")
    sub = config.get("config") or {}
    if backend_kind == "fastembed":
        from rka.infra.embedding_backends.fastembed import FastEmbedBackend

        return FastEmbedBackend(
            model_name=sub.get("model_name", "nomic-ai/nomic-embed-text-v1.5"),
            dim=sub.get("dim"),
            threads=sub.get("threads"),
            cache_dir=sub.get("cache_dir"),
            resource_limits=sub.get("resource_limits"),
        )
    if backend_kind == "openai_compat":
        from rka.infra.embedding_backends.openai_compat import OpenAICompatBackend

        return OpenAICompatBackend(
            base_url=sub.get("base_url") or "",
            model=sub.get("model") or "",
            api_key=sub.get("api_key"),
            dim=sub.get("dim"),
            timeout_seconds=sub.get("timeout_seconds", 600.0),
            query_template=sub.get("query_template", "{text}"),
            document_template=sub.get("document_template", "{text}"),
            embedding_space_id=sub.get("embedding_space_id"),
            resource_limits=sub.get("resource_limits"),
        )
    if backend_kind == "ollama":
        from rka.infra.embedding_backends.ollama import OllamaBackend

        return OllamaBackend(
            base_url=sub.get("base_url") or "http://host.docker.internal:11434",
            model=sub.get("model") or "",
            dim=sub.get("dim"),
            timeout_seconds=sub.get("timeout_seconds", 600.0),
            resource_limits=sub.get("resource_limits"),
        )
    raise ValueError(
        f"unknown embedding backend: {backend_kind!r} "
        "(expected: fastembed | openai_compat | ollama)"
    )
