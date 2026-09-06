"""Unit tests for the pluggable embedding backends (Mission D T1).

Each backend is exercised against a mocked `httpx` transport (for the
HTTP backends) so the suite runs offline. The FastEmbed backend is
covered by a Protocol-conformance test that doesn't touch the model
loader (we don't download model weights at test time).
"""

from __future__ import annotations

import json

import httpx
import pytest

from rka.infra.embedding_backends import (
    ConnectionTestResult,
    EmbeddingBackend,
    EmbeddingConfigError,
    make_backend,
)
from rka.infra.embedding_backends.fastembed import FastEmbedBackend
from rka.infra.embedding_backends.ollama import OllamaBackend
from rka.infra.embedding_backends.openai_compat import OpenAICompatBackend


# ---------------------------------------------------------------------------
# httpx mock transports
# ---------------------------------------------------------------------------


def _openai_responder(dim: int = 4):
    """Return an httpx.MockTransport that mimics OpenAI's /v1/embeddings."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/embeddings"
        body = json.loads(request.content) if request.content else {}
        inputs = body.get("input", [])
        if isinstance(inputs, str):
            inputs = [inputs]
        return httpx.Response(
            200,
            json={
                "data": [
                    {"embedding": [float(i)] * dim, "index": i}
                    for i in range(len(inputs))
                ],
                "model": body.get("model", "unknown"),
            },
        )

    return httpx.MockTransport(handler)


def _ollama_responder(dim: int = 4):
    """Return an httpx.MockTransport that mimics Ollama's /api/embeddings."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/embeddings"
        body = json.loads(request.content) if request.content else {}
        # Ollama returns SINGULAR `embedding`, NOT list-wrapped data.
        return httpx.Response(
            200,
            json={"embedding": [0.1] * dim, "model": body.get("model")},
        )

    return httpx.MockTransport(handler)


def _flaky_responder(failures_first: int, dim: int = 4):
    """Return 503 a few times then succeed; for retry testing."""
    state = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["count"] += 1
        if state["count"] <= failures_first:
            return httpx.Response(503, json={"error": "transient"})
        return httpx.Response(
            200,
            json={"data": [{"embedding": [0.1] * dim, "index": 0}]},
        )

    return httpx.MockTransport(handler)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def test_make_backend_fastembed_dispatch():
    b = make_backend({"backend": "fastembed", "config": {"model_name": "test-model"}})
    assert isinstance(b, FastEmbedBackend)
    assert b.model_name == "test-model"


def test_make_backend_openai_compat_dispatch():
    b = make_backend(
        {
            "backend": "openai_compat",
            "config": {"base_url": "http://x", "model": "m", "dim": 4},
        }
    )
    assert isinstance(b, OpenAICompatBackend)
    assert b.dim == 4
    assert b.model_name == "m"


def test_make_backend_ollama_dispatch():
    b = make_backend(
        {"backend": "ollama", "config": {"base_url": "http://x", "model": "m"}}
    )
    assert isinstance(b, OllamaBackend)
    assert b.model_name == "m"


def test_make_backend_unknown_raises_value_error():
    with pytest.raises(ValueError, match="unknown embedding backend"):
        make_backend({"backend": "magic", "config": {}})


def test_make_backend_missing_backend_raises():
    with pytest.raises(ValueError, match="unknown embedding backend"):
        make_backend({"config": {}})


# ---------------------------------------------------------------------------
# OpenAI-compat backend
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_openai_compat_embed_returns_vector():
    client = httpx.AsyncClient(transport=_openai_responder(dim=4), base_url="http://x")
    b = OpenAICompatBackend(
        base_url="http://x", model="m", api_key=None, dim=4, http_client=client
    )
    vec = await b.embed("hello")
    assert len(vec) == 4


@pytest.mark.asyncio
async def test_openai_compat_embed_batch_preserves_order():
    client = httpx.AsyncClient(transport=_openai_responder(dim=3), base_url="http://x")
    b = OpenAICompatBackend(
        base_url="http://x", model="m", dim=3, http_client=client
    )
    out = await b.embed_batch(["a", "b", "c"])
    assert len(out) == 3
    # The mock returns distinct indices; assert per-input vectors come back
    assert out[0] != out[1]


@pytest.mark.asyncio
async def test_openai_compat_orders_batch_by_response_index():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [
                    {"embedding": [2.0, 2.0], "index": 1},
                    {"embedding": [1.0, 1.0], "index": 0},
                ]
            },
        )

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://x"
    )
    backend = OpenAICompatBackend(
        base_url="http://x", model="m", dim=2, http_client=client
    )

    assert await backend.embed_batch(["first", "second"]) == [
        [1.0, 1.0],
        [2.0, 2.0],
    ]


@pytest.mark.asyncio
async def test_openai_compat_rejects_duplicate_or_missing_batch_indices():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [
                    {"embedding": [1.0, 1.0], "index": 0},
                    {"embedding": [2.0, 2.0], "index": 0},
                ]
            },
        )

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://x"
    )
    backend = OpenAICompatBackend(
        base_url="http://x", model="m", dim=2, http_client=client
    )

    with pytest.raises(ValueError, match="invalid embedding index"):
        await backend.embed_batch(["first", "second"])


@pytest.mark.asyncio
async def test_openai_compat_applies_query_and_document_templates():
    captured: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        captured.append(body["input"])
        return httpx.Response(
            200,
            json={"data": [{"embedding": [0.1] * 3, "index": 0}]},
        )

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://x"
    )
    b = OpenAICompatBackend(
        base_url="http://x",
        model="transport-alias",
        dim=3,
        query_template="Instruct: retrieve research context.\nQuery: {text}",
        document_template="{text}",
        embedding_space_id="qwen-space-v1",
        http_client=client,
    )

    await b.embed("why map nine types to three?", is_query=True)
    await b.embed("decision record", is_query=False)

    assert captured == [
        [
            "Instruct: retrieve research context.\n"
            "Query: why map nine types to three?"
        ],
        ["decision record"],
    ]
    # Provenance uses the vector-space identity; HTTP requests still use the
    # transport alias (asserted separately by the request-capture tests).
    assert b.model_name == "qwen-space-v1"


@pytest.mark.asyncio
async def test_openai_compat_templates_apply_to_batches():
    captured: dict[str, list[str]] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        captured["inputs"] = body["input"]
        return httpx.Response(
            200,
            json={
                "data": [
                    {"embedding": [float(i)] * 2, "index": i}
                    for i in range(len(body["input"]))
                ]
            },
        )

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://x"
    )
    b = OpenAICompatBackend(
        base_url="http://x",
        model="m",
        dim=2,
        query_template="Query: {text}",
        http_client=client,
    )
    await b.embed_batch(["alpha", "beta"], is_query=True)
    assert captured["inputs"] == ["Query: alpha", "Query: beta"]


def test_openai_compat_rejects_invalid_templates():
    with pytest.raises(ValueError, match="exactly one"):
        OpenAICompatBackend(
            base_url="http://x", model="m", query_template="missing placeholder"
        )
    with pytest.raises(ValueError, match="exactly one"):
        OpenAICompatBackend(
            base_url="http://x", model="m", document_template="{text} {text}"
        )


@pytest.mark.asyncio
async def test_openai_compat_sends_authorization_header_when_api_key_present():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("Authorization")
        return httpx.Response(
            200,
            json={"data": [{"embedding": [0.1] * 3, "index": 0}]},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://x")
    b = OpenAICompatBackend(
        base_url="http://x", model="m", api_key="sk-secret", dim=3, http_client=client
    )
    await b.embed("x")
    assert captured["auth"] == "Bearer sk-secret"


@pytest.mark.asyncio
async def test_openai_compat_omits_authorization_when_api_key_absent():
    # LM Studio + several local-proxy backends don't require auth.
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("Authorization")
        return httpx.Response(
            200,
            json={"data": [{"embedding": [0.1] * 3, "index": 0}]},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://x")
    b = OpenAICompatBackend(
        base_url="http://x", model="m", api_key=None, dim=3, http_client=client
    )
    await b.embed("x")
    assert captured["auth"] is None


@pytest.mark.asyncio
async def test_openai_compat_test_connection_reports_dim_on_success():
    client = httpx.AsyncClient(transport=_openai_responder(dim=8), base_url="http://x")
    b = OpenAICompatBackend(
        base_url="http://x", model="m", dim=0, http_client=client
    )
    result = await b.test_connection()
    assert result.ok is True
    assert result.detected_dim == 8
    # After a successful test, backend remembers the detected dim.
    assert b.dim == 8


@pytest.mark.asyncio
async def test_openai_compat_test_connection_returns_not_ok_on_connect_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("nope")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://x")
    b = OpenAICompatBackend(
        base_url="http://x", model="m", http_client=client
    )
    result = await b.test_connection()
    assert result.ok is False
    assert "connection refused" in result.detail


@pytest.mark.asyncio
async def test_openai_compat_retries_5xx(monkeypatch):
    # Patch sleep to be no-op so the test stays fast.
    import rka.infra.embedding_backends.openai_compat as oc

    async def _no_sleep(_):
        pass

    monkeypatch.setattr(oc.asyncio, "sleep", _no_sleep)
    client = httpx.AsyncClient(transport=_flaky_responder(failures_first=2, dim=3), base_url="http://x")
    b = OpenAICompatBackend(
        base_url="http://x", model="m", dim=0, http_client=client
    )
    vec = await b.embed("x")
    assert len(vec) == 3


def test_openai_compat_requires_base_url():
    with pytest.raises(ValueError, match="base_url"):
        OpenAICompatBackend(base_url="", model="m")


def test_openai_compat_requires_model():
    with pytest.raises(ValueError, match="model"):
        OpenAICompatBackend(base_url="http://x", model="")


# ---------------------------------------------------------------------------
# Ollama backend
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ollama_parses_singular_embedding_field():
    client = httpx.AsyncClient(transport=_ollama_responder(dim=5), base_url="http://x")
    b = OllamaBackend(base_url="http://x", model="m", http_client=client)
    vec = await b.embed("hi")
    assert len(vec) == 5
    assert b.dim == 5


@pytest.mark.asyncio
async def test_ollama_rejects_unexpected_shape():
    # Ollama uses {"embedding": [...]} not {"data": [{"embedding": [...]}]}
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"embedding": [0.1, 0.2]}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://x")
    b = OllamaBackend(base_url="http://x", model="m", http_client=client)
    with pytest.raises(RuntimeError, match="missing 'embedding'"):
        await b.embed("hi")


@pytest.mark.asyncio
async def test_ollama_embed_batch_loops_single_prompt_calls():
    state = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["count"] += 1
        body = json.loads(request.content)
        assert "prompt" in body  # Ollama uses prompt (singular), not input
        return httpx.Response(200, json={"embedding": [0.1] * 4})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://x")
    b = OllamaBackend(base_url="http://x", model="m", http_client=client)
    out = await b.embed_batch(["a", "b", "c"])
    assert len(out) == 3
    assert state["count"] == 3


@pytest.mark.asyncio
async def test_ollama_test_connection_reports_dim():
    client = httpx.AsyncClient(transport=_ollama_responder(dim=7), base_url="http://x")
    b = OllamaBackend(base_url="http://x", model="m", http_client=client)
    result = await b.test_connection()
    assert result.ok is True
    assert result.detected_dim == 7


@pytest.mark.asyncio
async def test_ollama_test_connection_handles_connect_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("ollama down")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://x")
    b = OllamaBackend(base_url="http://x", model="m", http_client=client)
    result = await b.test_connection()
    assert result.ok is False
    assert "connection refused" in result.detail


def test_ollama_requires_base_url():
    with pytest.raises(ValueError, match="base_url"):
        OllamaBackend(base_url="", model="m")


def test_ollama_requires_model():
    with pytest.raises(ValueError, match="model"):
        OllamaBackend(base_url="http://x", model="")


# ---------------------------------------------------------------------------
# FastEmbed backend (Protocol conformance + lazy load)
# ---------------------------------------------------------------------------


def test_fastembed_backend_implements_protocol():
    b = FastEmbedBackend(model_name="nomic-ai/nomic-embed-text-v1.5")
    assert isinstance(b, EmbeddingBackend)
    # Don't actually load the model; just exercise attributes.
    assert b.model_name == "nomic-ai/nomic-embed-text-v1.5"
    assert b.dim == 768  # Nomic default before first inference


def test_fastembed_backend_uses_nomic_prefix_for_query():
    # Spot-check the prefix-builder; running the model is too heavy for unit tests.
    b = FastEmbedBackend(model_name="nomic-ai/nomic-embed-text-v1.5")
    assert b._prefix("hello", is_query=True) == "search_query: hello"
    assert b._prefix("hello", is_query=False) == "search_document: hello"


def test_fastembed_backend_skips_prefix_for_non_nomic_model():
    b = FastEmbedBackend(model_name="other-vendor/some-model")
    assert b._prefix("hello", is_query=True) == "hello"


# ---------------------------------------------------------------------------
# v2.7.0.1: bounded threads + persistent cache_dir (OOM regression)
#
# Pre-v2.7.0.1, FastEmbedBackend constructed TextEmbedding(model_name=...) with
# no threads or cache_dir, leaving onnxruntime to default intra_op_num_threads
# to container CPU count (10 on Apple Silicon under Docker) and writing the
# model to an ephemeral cache that didn't survive container recreate.
# Empirical signature (2026-06-03): peak worker memory rose 7.17 → 7.87 GiB
# when the VM ceiling went 7.75 → 9.21 GiB — unbounded growth, not undersized
# VM. The fix caps threads at 2 (configurable) and persists cache to /data.
# ---------------------------------------------------------------------------


def test_fastembed_default_threads_is_2(monkeypatch):
    """OOM regression: default threads must be 2, not CPU count."""
    monkeypatch.delenv("RKA_EMBEDDING_THREADS", raising=False)
    b = FastEmbedBackend()
    assert b._threads == 2


def test_fastembed_threads_env_override(monkeypatch):
    monkeypatch.setenv("RKA_EMBEDDING_THREADS", "4")
    b = FastEmbedBackend()
    assert b._threads == 4


def test_fastembed_threads_param_overrides_env(monkeypatch):
    monkeypatch.setenv("RKA_EMBEDDING_THREADS", "4")
    b = FastEmbedBackend(threads=8)
    assert b._threads == 8


def test_fastembed_threads_zero_clamped_to_1():
    """Defensive: zero/negative threads must clamp to 1 (zero would let
    onnxruntime fall back to CPU-count, defeating the bound)."""
    assert FastEmbedBackend(threads=0)._threads == 1
    assert FastEmbedBackend(threads=-5)._threads == 1


def test_fastembed_default_cache_dir_is_none(monkeypatch):
    """When neither env nor param set, cache_dir stays None so fastembed
    uses its default. (Tests + local dev shouldn't be forced onto /data.)"""
    monkeypatch.delenv("RKA_EMBEDDING_CACHE_DIR", raising=False)
    b = FastEmbedBackend()
    assert b._cache_dir is None


def test_fastembed_cache_dir_env_override(monkeypatch):
    monkeypatch.setenv("RKA_EMBEDDING_CACHE_DIR", "/data/fastembed_cache")
    b = FastEmbedBackend()
    assert b._cache_dir == "/data/fastembed_cache"


def test_fastembed_cache_dir_param_overrides_env(monkeypatch):
    monkeypatch.setenv("RKA_EMBEDDING_CACHE_DIR", "/data/fastembed_cache")
    b = FastEmbedBackend(cache_dir="/tmp/custom_cache")
    assert b._cache_dir == "/tmp/custom_cache"


def test_fastembed_textembedding_called_with_bounded_threads(monkeypatch):
    """End-to-end: _get_model must pass threads= to TextEmbedding(). This
    is the call the v2.7.0.1 fix actually changed — pre-fix it received
    only model_name, leaving onnxruntime to default to CPU count."""
    monkeypatch.setenv("RKA_EMBEDDING_THREADS", "2")
    monkeypatch.delenv("RKA_EMBEDDING_CACHE_DIR", raising=False)
    b = FastEmbedBackend()

    captured: dict = {}

    class _StubTextEmbedding:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    import sys
    import types

    stub_module = types.ModuleType("fastembed")
    stub_module.TextEmbedding = _StubTextEmbedding
    monkeypatch.setitem(sys.modules, "fastembed", stub_module)

    b._get_model()
    assert captured["threads"] == 2
    assert captured["model_name"] == "nomic-ai/nomic-embed-text-v1.5"
    # cache_dir omitted when not configured — fastembed falls back to its default
    assert "cache_dir" not in captured


def test_fastembed_textembedding_called_with_cache_dir_when_set(monkeypatch):
    monkeypatch.setenv("RKA_EMBEDDING_CACHE_DIR", "/data/fastembed_cache")
    b = FastEmbedBackend()

    captured: dict = {}

    class _StubTextEmbedding:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    import sys
    import types

    stub_module = types.ModuleType("fastembed")
    stub_module.TextEmbedding = _StubTextEmbedding
    monkeypatch.setitem(sys.modules, "fastembed", stub_module)

    b._get_model()
    assert captured["cache_dir"] == "/data/fastembed_cache"


def test_make_backend_fastembed_threads_passthrough():
    """Factory must forward threads + cache_dir from config dict."""
    b = make_backend(
        {
            "backend": "fastembed",
            "config": {
                "model_name": "test-model",
                "threads": 3,
                "cache_dir": "/tmp/x",
            },
        }
    )
    assert isinstance(b, FastEmbedBackend)
    assert b._threads == 3
    assert b._cache_dir == "/tmp/x"


# ---------------------------------------------------------------------------
# Round-trip via factory + Protocol
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_factory_built_openai_compat_backend_round_trips():
    cfg = {
        "backend": "openai_compat",
        "config": {"base_url": "http://x", "model": "m", "dim": 4},
    }
    b = make_backend(cfg)
    # Swap the client in so we can mock — using the real httpx client
    # would try to reach http://x.
    b._http = httpx.AsyncClient(transport=_openai_responder(dim=4), base_url="http://x")
    assert isinstance(b, EmbeddingBackend)
    vec = await b.embed("hi")
    assert len(vec) == 4


# ---------------------------------------------------------------------------
# T2.5 calibration — dim drift in production embed paths raises rather
# than silently mutates self._dim (Brain greenlight dec_01KRP0WFMXAF0TQN6RDXY65WEX
# redirect of upfront-Backbrief ask 5)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_openai_compat_embed_raises_on_dim_drift_after_construction():
    """After non-zero-dim construction, an embed() that observes a
    differently-sized vector must raise rather than silently update
    self._dim. Real-world scenario: user mistypes dim in advanced UI."""
    client = httpx.AsyncClient(transport=_openai_responder(dim=8), base_url="http://x")
    b = OpenAICompatBackend(
        base_url="http://x", model="m", dim=4, http_client=client  # configured 4, server 8
    )
    with pytest.raises(EmbeddingConfigError, match="dim mismatch"):
        await b.embed("hello")
    # Dim stays at the configured value on drift (not silently corrected).
    assert b.dim == 4


@pytest.mark.asyncio
async def test_ollama_embed_raises_on_dim_drift_after_construction():
    client = httpx.AsyncClient(transport=_ollama_responder(dim=5), base_url="http://x")
    b = OllamaBackend(base_url="http://x", model="m", dim=3, http_client=client)
    with pytest.raises(EmbeddingConfigError, match="dim mismatch"):
        await b.embed("hi")
    assert b.dim == 3


@pytest.mark.asyncio
async def test_fastembed_embed_raises_on_dim_drift_after_construction():
    """FastEmbed's drift case: user picks a non-Nomic model whose actual
    dim differs from the configured one; embed must raise."""

    class _FakeVec:
        def __init__(self, data: list[float]) -> None:
            self._data = data

        def tolist(self) -> list[float]:
            return self._data

    class _FakeModel:
        def __init__(self, dim: int) -> None:
            self._dim = dim

        def embed(self, texts: list[str], *, batch_size: int) -> list[_FakeVec]:
            assert batch_size == len(texts) <= 8
            return [_FakeVec([0.1] * self._dim) for _ in texts]

    b = FastEmbedBackend(model_name="custom-model", dim=4)
    # Bypass the real model loader; inject a fake that returns 768-dim vectors.
    b._model = _FakeModel(dim=768)

    with pytest.raises(EmbeddingConfigError, match="dim mismatch"):
        await b.embed("hi")
    assert b.dim == 4


@pytest.mark.asyncio
async def test_openai_compat_zero_dim_at_construction_populates_on_first_embed():
    """The flip side: when constructed with dim=0 (advanced empty-config
    or post-test_connection-from-zero path), the first embed populates."""
    client = httpx.AsyncClient(transport=_openai_responder(dim=4), base_url="http://x")
    b = OpenAICompatBackend(base_url="http://x", model="m", dim=0, http_client=client)
    assert b.dim == 0
    await b.embed("hi")
    assert b.dim == 4  # populated from observed (legitimate case)


@pytest.mark.asyncio
async def test_connection_test_result_dataclass_shape():
    # `ConnectionTestResult` is the structured return type T3 will surface
    # over the REST API. Lock its field set.
    result = ConnectionTestResult(ok=True, detail="ready", detected_dim=4, latency_ms=12.3)
    assert result.ok is True
    assert result.detected_dim == 4
    assert result.latency_ms == 12.3
    # And the not-ok variant lets detected_dim/latency_ms be None.
    err = ConnectionTestResult(ok=False, detail="oops")
    assert err.detected_dim is None
    assert err.latency_ms is None
