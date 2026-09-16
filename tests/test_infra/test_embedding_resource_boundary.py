"""E1a: provider-free resource-boundary regressions, using real backend adapters."""

import asyncio
import json
import threading

import httpx
import pytest

from rka.infra.embedding_backends.fastembed import FastEmbedBackend
from rka.infra.embedding_backends.ollama import OllamaBackend
from rka.infra.embedding_backends.openai_compat import OpenAICompatBackend
from rka.infra.embedding_backends import make_backend
from rka.infra.embedding_resources import (
    EmbeddingBusy,
    EmbeddingCallTimeout,
    EmbeddingResourceLimits,
    HTTPEmbeddingResourceLimits,
)


class Vector(list):
    def tolist(self):
        return list(self)


def vector_for(text):
    return [float(text.split(" ")[0]), 0.0]


@pytest.fixture(params=["fastembed", "openai_compat", "ollama"])
def captured_backend(request):
    calls = []

    class SyntheticModel:
        def embed(self, texts, **kwargs):
            calls.append(list(texts))
            return [Vector(vector_for(text)) for text in texts]

    def handler(request):
        body = json.loads(request.content)
        texts = body.get("input", [body.get("prompt")])
        calls.append(texts)
        if "prompt" in body:
            return httpx.Response(200, json={"embedding": vector_for(texts[0])})
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": i, "embedding": vector_for(text)} for i, text in enumerate(texts)
                ]
            },
        )

    if request.param == "fastembed":
        backend = FastEmbedBackend(model_name="synthetic", dim=2)
        backend._model = SyntheticModel()
    else:
        cls = OpenAICompatBackend if request.param == "openai_compat" else OllamaBackend
        backend = cls(
            base_url="http://isolated.test",
            model="synthetic",
            dim=2,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
    return backend, calls


@pytest.mark.asyncio
@pytest.mark.parametrize("is_query", [False, True])
async def test_reported_long_input_never_reaches_provider(captured_backend, is_query):
    backend, calls = captured_backend
    with pytest.raises(RuntimeError, match="embedding_input_limit"):
        await backend.embed("0 " + "x" * 20_500, is_query=is_query)
    assert calls == []


@pytest.mark.asyncio
async def test_connection_probe_cannot_bypass_input_policy(captured_backend):
    backend, calls = captured_backend
    backend.resource_limits = EmbeddingResourceLimits(max_input_bytes=1)
    result = await backend.test_connection()
    assert not result.ok and "embedding_input_limit" in result.detail
    assert calls == []


@pytest.mark.asyncio
async def test_multibyte_limit_is_bytes_not_characters(captured_backend):
    backend, calls = captured_backend
    with pytest.raises(RuntimeError, match="embedding_input_limit"):
        await backend.embed("0 " + "界" * (backend.resource_limits.max_input_bytes // 3 + 1))
    assert calls == []


@pytest.mark.asyncio
async def test_invalid_later_input_prevents_partial_inference(captured_backend):
    backend, calls = captured_backend
    with pytest.raises(RuntimeError, match="embedding_input_limit"):
        await backend.embed_batch(["0 valid", "1 " + "x" * 20_500])
    assert calls == []


@pytest.mark.asyncio
async def test_logical_call_count_is_bounded(captured_backend):
    backend, calls = captured_backend
    with pytest.raises(RuntimeError, match="embedding_input_limit"):
        await backend.embed_batch(["0 tiny"] * 129)
    assert calls == []


@pytest.mark.asyncio
async def test_batches_are_bounded_and_preserve_output_order(captured_backend):
    backend, calls = captured_backend
    size = min(4000, backend.resource_limits.max_input_bytes - 64)
    texts = [f"{i} " + "x" * size for i in range(17)]
    assert await backend.embed_batch(texts) == [[float(i), 0.0] for i in range(17)]
    assert [text for batch in calls for text in batch] == texts
    for batch in calls:
        sizes = [len(text.encode("utf-8")) for text in batch]
        assert len(batch) <= 8
        assert sum(sizes) <= backend.resource_limits.max_batch_bytes
        assert max(sizes) * len(batch) <= backend.resource_limits.max_padding_bytes


@pytest.mark.asyncio
async def test_template_expansion_is_inside_the_limit():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.0]}]})

    backend = OpenAICompatBackend(
        base_url="http://isolated.test",
        model="synthetic",
        dim=1,
        document_template="p" * 8000 + "{text}",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(RuntimeError, match="embedding_input_limit"):
        await backend.embed("0 " + "x" * (backend.resource_limits.max_input_bytes - 8000))
    assert calls == []


@pytest.mark.asyncio
async def test_exact_byte_boundary_is_accepted(captured_backend):
    backend, calls = captured_backend
    text = "0 " + "x" * (backend.resource_limits.max_input_bytes - 2)
    assert await backend.embed(text) == [0.0, 0.0]
    assert calls == [[text]]


@pytest.mark.asyncio
async def test_total_call_bytes_are_checked_before_inference(captured_backend):
    from dataclasses import replace
    backend, calls = captured_backend
    backend.resource_limits = replace(backend.resource_limits, max_call_bytes=32768)
    size = min(5000, backend.resource_limits.max_input_bytes - 2)
    with pytest.raises(RuntimeError, match="max_call_bytes"):
        await backend.embed_batch(["0 " + "x" * size] * 64)
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("is_query", [True, False])
async def test_nomic_prefix_counts_toward_input_budget(is_query):
    backend = FastEmbedBackend()
    with pytest.raises(RuntimeError, match="embedding_input_limit"):
        await backend.embed("x" * backend.resource_limits.max_input_bytes, is_query=is_query)
    assert backend._model is None


@pytest.mark.asyncio
async def test_native_ceiling_cannot_be_raised_by_saved_generic_limits():
    backend = FastEmbedBackend(resource_limits={"max_input_bytes": 8192, "max_batch_bytes": 16384})
    assert backend.resource_limits.max_input_bytes == 2048
    assert backend.resource_limits.max_batch_bytes == 4096
    assert backend.resource_limits.max_padding_bytes == 4096
    with pytest.raises(RuntimeError, match="max_input_bytes=2048"):
        await backend.embed("a " * 4085)
    assert backend._model is None


def test_padding_proxy_splits_even_when_total_bytes_fit():
    limits = EmbeddingResourceLimits()
    assert limits.plan(["x" * 8000, "y", "z"]) == [["x" * 8000, "y"], ["z"]]


@pytest.mark.parametrize(
    "bad",
    [
        {"max_input_bytes": 0},
        {"max_input_bytes": 8193},
        {"max_input_bytes": True},
        {"max_input_bytes": 1.5},
        {"max_batch_bytes": 100},
        {"max_call_inputs": 1},
        {"call_timeout_seconds": float("inf")},
        {"call_timeout_seconds": float("nan")},
        {"call_timeout_seconds": 10**1000},
        {"call_timeout_seconds": "1"},
        {"unknown": 1},
        [],
        False,
    ],
)
def test_resource_limits_reject_invalid_or_unbounded_configuration(bad):
    with pytest.raises(ValueError, match="resource_limits"):
        EmbeddingResourceLimits.from_config(bad)


@pytest.mark.parametrize("kind", ["openai_compat", "ollama"])
def test_http_16k_is_default_and_lower_saved_limits_preserve_space_identity(kind):
    from dataclasses import asdict
    from rka.services.embedding_index import embedding_space_signature

    config = {"backend": kind, "config": {
        "base_url": "http://isolated.test", "model": "synthetic", "dim": 2,
    }}
    defaults = make_backend(config).resource_limits
    assert asdict(defaults) == {**asdict(EmbeddingResourceLimits()), "max_input_bytes": 16384}
    signature = embedding_space_signature(config)
    config["config"]["resource_limits"] = {"max_input_bytes": 8192}
    limits = make_backend(config).resource_limits
    assert isinstance(limits, EmbeddingResourceLimits)  # backfill fetch budgeting
    assert asdict(limits) == {**asdict(defaults), "max_input_bytes": 8192}
    assert embedding_space_signature(config) == signature
    assert HTTPEmbeddingResourceLimits.from_config(asdict(limits)) == limits


@pytest.mark.parametrize("bad", [
    {"max_input_bytes": 16385}, {"max_input_bytes": True},
    {"max_input_bytes": 0}, {"max_input_bytes": 16384.0},
    {"max_input_bytes": "16384"}, {"max_input_bytes": float("inf")},
    {"max_input_bytes": 16384, "max_batch_bytes": 8192},
    {"max_input_bytes": 16384, "max_padding_bytes": 8192},
    {"max_batch_bytes": 16385}, {"max_padding_bytes": 16385},
    {"max_call_bytes": 262145}, {"max_batch_inputs": 9},
    {"max_call_inputs": 129}, {"call_timeout_seconds": 121},
    {"_input_ceiling": 1000000}, {"unknown": 1}, [], False,
])
def test_http_default_cannot_bypass_other_bounds(bad):
    with pytest.raises(ValueError, match="resource_limits"):
        HTTPEmbeddingResourceLimits.from_config(bad)


def test_native_does_not_accept_http_only_ceiling():
    with pytest.raises(ValueError, match="max_input_bytes"):
        FastEmbedBackend(resource_limits={"max_input_bytes": 16384})


@pytest.mark.parametrize("kind", ["openai_compat", "ollama"])
@pytest.mark.parametrize("saved", [
    {}, {"call_timeout_seconds": 60},
    {"max_batch_bytes": 8192}, {"max_padding_bytes": 8192},
    {"max_batch_bytes": 8192, "max_padding_bytes": 12000, "max_call_bytes": 8192},
    {"max_input_bytes": 4096, "max_batch_bytes": 8192},
])
def test_http_defaults_respect_existing_partial_config_budgets(kind, saved):
    original = dict(saved)
    backend = make_backend({"backend": kind, "config": {
        "base_url": "http://isolated.test", "model": "synthetic", "resource_limits": saved,
    }})
    expected = saved.get("max_input_bytes", min(16384, saved.get("max_batch_bytes", 16384),
                                              saved.get("max_padding_bytes", 16384)))
    assert backend.resource_limits.max_input_bytes == expected
    assert saved == original  # do not rewrite the caller's persisted config


@pytest.mark.asyncio
@pytest.mark.parametrize("cls", [OpenAICompatBackend, OllamaBackend])
@pytest.mark.parametrize("is_query", [False, True])
async def test_http_16k_exact_multibyte_input_and_aggregate_guards(cls, is_query):
    calls = []

    def handler(request):
        body = json.loads(request.content)
        texts = body.get("input", [body.get("prompt")])
        calls.append(texts)
        if "prompt" in body:
            return httpx.Response(200, json={"embedding": [0.5, 0.5]})
        return httpx.Response(200, json={"data": [
            {"index": i, "embedding": [0.5, 0.5]} for i in range(len(texts))
        ]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        backend = cls(base_url="http://isolated.test", model="synthetic", dim=2,
                      http_client=client)
        exact = "界" * 5461 + "x"
        assert len(exact.encode()) == 16384
        assert await backend.embed(exact, is_query=is_query) == [0.5, 0.5]
        assert calls == [[exact]]
        calls.clear()
        assert await backend.embed_batch([exact, "short", exact]) == [[0.5, 0.5]] * 3
        assert calls == [[exact], ["short"], [exact]]
        calls.clear()
        with pytest.raises(RuntimeError, match="max_input_bytes=16384"):
            await backend.embed_batch(["valid", exact + "x"])
        assert calls == []
        with pytest.raises(RuntimeError, match="max_call_bytes"):
            await backend.embed_batch([exact] * 17)
        assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("is_query", [False, True])
async def test_http_16k_template_expansion_still_counts(is_query):
    backend = OpenAICompatBackend(
        base_url="http://isolated.test", model="synthetic", dim=2,
        query_template="q: {text}", document_template="d: {text}",
    )
    backend.validate_input("x" * 16381, is_query=is_query)
    with pytest.raises(RuntimeError, match="max_input_bytes=16384"):
        await backend.embed("x" * 16382, is_query=is_query)
    assert backend._http is None


@pytest.mark.parametrize("kind", ["fastembed", "openai_compat", "ollama"])
def test_factory_threads_limits_without_changing_space_identity(kind):
    from rka.services.embedding_index import embedding_space_signature

    config = {
        "backend": kind,
        "config": {"base_url": "http://isolated.test", "model": "synthetic", "dim": 2},
    }
    before = embedding_space_signature(config)
    config["config"]["resource_limits"] = {"max_input_bytes": 1024}
    assert make_backend(config).resource_limits.max_input_bytes == 1024
    assert embedding_space_signature(config) == before
    config["config"]["resource_limits"] = {"max_input_bytes": False}
    with pytest.raises(ValueError, match="resource_limits"):
        make_backend(config)


@pytest.mark.parametrize("kind", ["openai_compat", "ollama"])
@pytest.mark.parametrize("bad", [True, 0, -1, float("inf"), float("nan"), "invalid"])
def test_factory_does_not_coerce_invalid_timeouts(kind, bad):
    with pytest.raises(ValueError, match="timeout_seconds"):
        make_backend(
            {
                "backend": kind,
                "config": {
                    "base_url": "http://isolated.test",
                    "model": "synthetic",
                    "timeout_seconds": bad,
                },
            }
        )


async def wait_for_thread_event(event):
    async with asyncio.timeout(2):
        while not event.is_set():
            await asyncio.sleep(0.001)


@pytest.mark.asyncio
@pytest.mark.parametrize("interruption", ["cancel", "timeout"])
async def test_native_slot_outlives_cancelled_or_timed_out_caller(interruption):
    from rka.infra.embedding_resources import _NATIVE_EXECUTOR

    entered, release = threading.Event(), threading.Event()

    class BlockingModel:
        def embed(self, texts, **kwargs):
            entered.set()
            assert release.wait(3), "test cleanup must release native inference"
            return [Vector([0.0, 0.0]) for _ in texts]

    first = FastEmbedBackend(
        model_name="synthetic", dim=2, resource_limits={"call_timeout_seconds": 0.05}
    )
    first._model = BlockingModel()
    second = FastEmbedBackend(model_name="other-synthetic", dim=2)
    task = asyncio.create_task(first.embed("0 first"))
    try:
        await wait_for_thread_event(entered)
        if interruption == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(EmbeddingCallTimeout):
                await task
        with pytest.raises(EmbeddingBusy):
            await second.embed("0 must not load another model")
        assert second._model is None
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        # Test-only executor barrier proves the native future and its admission
        # callback finished before another test starts. It performs no inference.
        await asyncio.wrap_future(_NATIVE_EXECUTOR.submit(lambda: None))
    assert await first.embed("0 after native completion") == [0.0, 0.0]


@pytest.mark.asyncio
@pytest.mark.parametrize("cls", [OpenAICompatBackend, OllamaBackend])
async def test_http_deadline_includes_retry_backoff(cls):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(503)

    backend = cls(
        base_url="http://isolated.test",
        model="synthetic",
        dim=2,
        resource_limits={"call_timeout_seconds": 0.02},
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(EmbeddingCallTimeout):
        await backend.embed("0 synthetic")
    assert len(calls) == 1
    await asyncio.sleep(0)  # allow cancelled transport/backoff cleanup


@pytest.mark.asyncio
async def test_http_slot_is_held_until_cancel_cleanup_finishes():
    entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def handler(request):
        entered.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            cleaning.set()
            await release.wait()
            raise

    backend = OpenAICompatBackend(
        base_url="http://isolated.test",
        model="synthetic",
        dim=2,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    task = asyncio.create_task(backend.embed("0 synthetic"))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(cleaning.wait(), 1)
        with pytest.raises(EmbeddingBusy):
            await backend.embed("0 no overlap")
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0)
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_input_rejection_does_not_mark_provider_unavailable(captured_backend):
    from rka.infra.embeddings import EmbeddingService

    backend, calls = captured_backend
    service = EmbeddingService(backend=backend)
    assert await service.embed("0 healthy") == [0.0, 0.0]
    for method in (service.embed, service.embed_document):
        with pytest.raises(RuntimeError, match="embedding_input_limit"):
            await method("x" * 20_500)
        assert service.runtime_available is True
        assert service.runtime_error_code == "embedding_input_limit"
    assert len(calls) == 1
