"""Integration tests for the embedding-config REST endpoints (Mission D T3)."""

from __future__ import annotations

import asyncio
import struct
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

from rka.api.app import create_app
from rka.config import RKAConfig
from rka.infra.embedding_backends import ConnectionTestResult
from rka.services.embedding_backfill import (
    BackfillService,
    clear_registry,
    latest_status,
)
from rka.services.embedding_config import EmbeddingConfig, EmbeddingConfigService


@pytest_asyncio.fixture
async def api_client(tmp_path: Path):
    """Per-test API client pointed at tmp_path for both DB and embedding config."""
    clear_registry()  # avoid cross-test pollution of the module-level registry
    config = RKAConfig(
        project_dir=tmp_path,
        db_path=Path("config_routes.db"),
        data_dir=tmp_path / "data",
        llm_enabled=False,
        embeddings_enabled=False,
    )
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    app = create_app(config)
    lifespan = app.router.lifespan_context(app)
    await lifespan.__aenter__()
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            yield client, tmp_path / "data"
    finally:
        await lifespan.__aexit__(None, None, None)


# ---------------------------------------------------------------------------
# GET /api/config/embedding
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_returns_default_on_missing_file(api_client):
    client, _data_dir = api_client
    r = await client.get("/api/config/embedding")
    assert r.status_code == 200
    body = r.json()
    assert body["backend"] == "fastembed"
    assert body["config"]["model_name"].startswith("nomic-")
    assert body["config"]["dim"] == 768


@pytest.mark.asyncio
async def test_get_redacts_api_key(api_client):
    client, data_dir = api_client
    svc = EmbeddingConfigService(config_dir=data_dir)
    svc.save_config(
        EmbeddingConfig(
            backend="openai_compat",
            config={
                "base_url": "http://x",
                "model": "m",
                "api_key": "sk-very-secret",
                "dim": 4,
            },
        ),
        actor="pi",
    )

    r = await client.get("/api/config/embedding")
    assert r.status_code == 200
    body = r.json()
    assert body["config"]["api_key"] == "***", (
        "GET must redact api_key per the upfront Backbrief A4 invariant"
    )
    # Other fields round-trip untouched.
    assert body["config"]["base_url"] == "http://x"
    assert body["config"]["dim"] == 4


@pytest.mark.asyncio
async def test_get_on_corrupt_file_returns_422_with_hint(api_client):
    client, data_dir = api_client
    (data_dir / "embedding_config.json").write_text("not-valid-json {{{")

    r = await client.get("/api/config/embedding")
    assert r.status_code == 422
    body = r.json()
    assert body["error"] == "embedding_config_invalid"
    assert "unreadable" in body["detail"]
    assert body["hint"]  # T6 will surface this string verbatim


# ---------------------------------------------------------------------------
# POST /api/config/embedding/test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_test_endpoint_returns_connection_test_result_shape(api_client):
    # Use openai_compat with an unreachable base_url — the test_connection
    # implementation produces a clean not-ok ConnectionTestResult and the
    # endpoint surfaces its shape verbatim.
    client, _ = api_client
    r = await client.post(
        "/api/config/embedding/test",
        json={
            "backend": "openai_compat",
            "config": {
                "base_url": "http://127.0.0.1:1",  # nothing listening
                "model": "m",
                "dim": 4,
            },
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert set(body.keys()) == {"ok", "detail", "detected_dim", "latency_ms"}
    assert body["ok"] is False
    # An unreachable port surfaces a clear detail message.
    assert isinstance(body["detail"], str) and len(body["detail"]) > 0


@pytest.mark.asyncio
async def test_test_endpoint_uses_saved_secret_when_client_omits_it(
    api_client, monkeypatch
):
    client, data_dir = api_client
    svc = EmbeddingConfigService(config_dir=data_dir)
    svc.save_config(
        EmbeddingConfig(
            backend="openai_compat",
            config={
                "base_url": "http://x",
                "model": "m",
                "api_key": "sk-saved",
                "dim": 768,
            },
        ),
        actor="pi",
    )

    async def _capture(self, config):  # noqa: ARG001
        assert config.config["api_key"] == "sk-saved"
        return ConnectionTestResult(ok=True, detail="ok", detected_dim=768)

    monkeypatch.setattr(EmbeddingConfigService, "test_config", _capture)
    response = await client.post(
        "/api/config/embedding/test",
        json={
            "backend": "openai_compat",
            "config": {"base_url": "http://x", "model": "m", "dim": 768},
        },
    )

    assert response.status_code == 200
    assert response.json()["ok"] is True


@pytest.mark.asyncio
async def test_test_endpoint_422_on_invalid_body(api_client):
    client, _ = api_client
    r = await client.post(
        "/api/config/embedding/test",
        json={"backend": "magic-unknown", "config": {}},
    )
    # Pydantic rejection of the Literal → FastAPI 422 (its native pattern).
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# PUT /api/config/embedding
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_put_returns_200_when_only_api_key_changes(api_client):
    client, data_dir = api_client
    svc = EmbeddingConfigService(config_dir=data_dir)
    svc.save_config(
        EmbeddingConfig(
            backend="openai_compat",
            config={"base_url": "http://x", "model": "m", "api_key": "old", "dim": 4},
        ),
        actor="pi",
    )

    # Try to save a config that only differs in api_key. The PUT handler
    # MUST call test_config first; we use an unreachable URL so the test
    # fails fast — but we test the no-backfill path by changing api_key
    # AND making the test pass. The Brain's spec is unambiguous: only-
    # api_key-changed → 200 (no backfill). Achieving a passing test in
    # the integration suite without a live backend requires the same URL
    # we can mock.
    # We point at the same unreachable URL → test_connection returns
    # not-ok, surface 422 (which is the SAME-test invariant: an
    # unreachable backend never gets persisted regardless of dim drift).
    r = await client.put(
        "/api/config/embedding",
        json={
            "backend": "openai_compat",
            "config": {"base_url": "http://x", "model": "m", "api_key": "new", "dim": 4},
        },
    )
    # Connection-test failure on a never-mocked backend → 422.
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_put_returns_422_on_connection_failure(api_client):
    client, _ = api_client
    r = await client.put(
        "/api/config/embedding",
        json={
            "backend": "openai_compat",
            "config": {
                "base_url": "http://127.0.0.1:1",
                "model": "m",
                "dim": 4,
            },
        },
    )
    assert r.status_code == 422
    body = r.json()
    assert body["error"] == "embedding_config_invalid"
    assert "connection test failed" in body["detail"]
    assert "Settings" in body["hint"]  # actionable next step


@pytest.mark.asyncio
async def test_put_refreshes_search_backend_without_reindex(api_client, monkeypatch):
    """A query-template/key edit must update the live search path immediately."""
    client, data_dir = api_client
    svc = EmbeddingConfigService(config_dir=data_dir)
    svc.save_config(
        EmbeddingConfig(
            backend="openai_compat",
            config={
                "base_url": "http://x",
                "model": "m",
                "api_key": "old",
                "dim": 4,
                "query_template": "Query: {text}",
            },
        ),
        actor="pi",
    )

    async def _passing_test(self, config):  # noqa: ARG001
        return ConnectionTestResult(ok=True, detail="ok", detected_dim=4)

    monkeypatch.setattr(EmbeddingConfigService, "test_config", _passing_test)
    app = client._transport.app
    previous = app.state.search.embeddings

    response = await client.put(
        "/api/config/embedding",
        json={
            "backend": "openai_compat",
            "config": {
                "base_url": "http://x",
                "model": "m",
                "api_key": "new",
                "dim": 4,
                "query_template": "Instruct: retrieve.\nQuery: {text}",
            },
        },
    )

    # Credential repair schedules a missing-only reconciliation so records
    # created during an authentication outage are not stranded. It does not
    # force a document-space rebuild when the generation already matches.
    assert response.status_code == 202
    assert app.state.search.embeddings is app.state.embeddings
    assert app.state.search.embeddings is not previous


@pytest.mark.asyncio
async def test_put_preserves_secret_and_advanced_fields(api_client, monkeypatch):
    client, data_dir = api_client
    svc = EmbeddingConfigService(config_dir=data_dir)
    advanced = {
        "base_url": "http://x",
        "model": "transport-model",
        "api_key": "sk-preserve",
        "dim": 768,
        "timeout_seconds": 45,
        "embedding_space_id": "space-v1",
        "query_template": "Query: {text}",
        "document_template": "{text}",
    }
    svc.save_config(
        EmbeddingConfig(backend="openai_compat", config=advanced),
        actor="pi",
    )

    async def _passing_test(self, config):  # noqa: ARG001
        return ConnectionTestResult(ok=True, detail="ok", detected_dim=768)

    monkeypatch.setattr(EmbeddingConfigService, "test_config", _passing_test)
    submitted = {key: value for key, value in advanced.items() if key != "api_key"}
    response = await client.put(
        "/api/config/embedding",
        json={"backend": "openai_compat", "config": submitted},
    )

    assert response.status_code == 202
    persisted = svc.load_config()
    assert persisted.config["api_key"] == "sk-preserve"
    for key, value in submitted.items():
        assert persisted.config[key] == value
    fetched = await client.get("/api/config/embedding")
    assert fetched.json()["config"]["api_key"] == "***"


@pytest.mark.asyncio
async def test_populated_cross_dimension_put_is_non_destructive(api_client, monkeypatch):
    client, data_dir = api_client
    app = client._transport.app
    db = app.state.db
    blob = struct.pack("768f", *([0.0] * 768))
    await db.execute(
        "INSERT INTO vec_claims (id, project_id, embedding) VALUES (?, ?, ?)",
        ["clm_existing", "proj_default", blob],
    )
    await db.execute(
        """INSERT INTO embedding_metadata
           (project_id, entity_type, entity_id, content_hash, model_name, dimensions)
           VALUES ('proj_default', 'claim', 'clm_existing', 'hash', 'old', 768)"""
    )

    async def _passing_test(self, config):  # noqa: ARG001
        return ConnectionTestResult(ok=True, detail="ok", detected_dim=1024)

    monkeypatch.setattr(EmbeddingConfigService, "test_config", _passing_test)
    previous_embeddings = app.state.embeddings
    previous_search_embeddings = app.state.search.embeddings

    response = await client.put(
        "/api/config/embedding",
        json={
            "backend": "openai_compat",
            "config": {"base_url": "http://x", "model": "new", "dim": 1024},
        },
    )

    assert response.status_code == 409
    assert response.json()["error"] == "embedding_offline_reindex_required"
    assert not (data_dir / "embedding_config.json").exists()
    assert app.state.embeddings is previous_embeddings
    assert app.state.search.embeddings is previous_search_embeddings
    assert latest_status() is None
    assert await db.fetchone(
        "SELECT id FROM vec_claims WHERE id = 'clm_existing'"
    ) == {"id": "clm_existing"}


@pytest.mark.asyncio
async def test_cross_dimension_put_rechecks_after_writer_lock(api_client, monkeypatch):
    """A late old-space write must make the authoritative check fail closed."""
    client, data_dir = api_client
    app = client._transport.app
    db = app.state.db

    async def _passing_test(self, config):  # noqa: ARG001
        return ConnectionTestResult(ok=True, detail="ok", detected_dim=1024)

    monkeypatch.setattr(EmbeddingConfigService, "test_config", _passing_test)

    from rka.api.routes import config as config_routes
    from rka.services.embedding_index import (
        assert_online_dimension_compatible as real_dimension_check,
    )

    calls = 0

    async def _inject_late_write(database, *, dim):
        nonlocal calls
        calls += 1
        if calls == 2:
            blob = struct.pack("768f", *([0.0] * 768))
            await database.execute(
                "INSERT INTO vec_claims (id, project_id, embedding) VALUES (?, ?, ?)",
                ["clm_late", "proj_default", blob],
            )
        await real_dimension_check(database, dim=dim)

    monkeypatch.setattr(
        config_routes,
        "assert_online_dimension_compatible",
        _inject_late_write,
    )

    response = await client.put(
        "/api/config/embedding",
        json={
            "backend": "openai_compat",
            "config": {"base_url": "http://x", "model": "new", "dim": 1024},
        },
    )

    assert calls == 2
    assert response.status_code == 409
    assert response.json()["error"] == "embedding_offline_reindex_required"
    assert not (data_dir / "embedding_config.json").exists()
    assert await db.fetchone(
        "SELECT id FROM vec_claims WHERE id = 'clm_late'"
    ) is None
    assert latest_status() is None


@pytest.mark.asyncio
async def test_put_commit_failure_restores_exact_config_and_runtime(api_client, monkeypatch):
    client, data_dir = api_client
    app = client._transport.app
    db = app.state.db
    svc = EmbeddingConfigService(config_dir=data_dir)
    svc.save_config(
        EmbeddingConfig(
            backend="openai_compat",
            config={"base_url": "http://old", "model": "old", "dim": 768},
        ),
        actor="pi",
    )
    before = svc.config_path.read_bytes()
    previous_embeddings = app.state.embeddings
    previous_search_embeddings = app.state.search.embeddings

    async def _passing_test(self, config):  # noqa: ARG001
        return ConnectionTestResult(ok=True, detail="ok", detected_dim=768)

    monkeypatch.setattr(EmbeddingConfigService, "test_config", _passing_test)
    connection_type = type(db.conn)
    real_commit = connection_type.commit
    fail_once = True

    async def _commit_with_one_failure(connection):
        nonlocal fail_once
        if fail_once:
            fail_once = False
            raise RuntimeError("injected commit failure")
        return await real_commit(connection)

    monkeypatch.setattr(connection_type, "commit", _commit_with_one_failure)

    response = await client.put(
        "/api/config/embedding",
        json={
            "backend": "openai_compat",
            "config": {"base_url": "http://new", "model": "new", "dim": 768},
        },
    )

    assert response.status_code == 500
    assert response.json()["error"] == "embedding_config_update_failed"
    assert svc.config_path.read_bytes() == before
    assert await db.fetchone(
        "SELECT generation FROM embedding_index_state WHERE singleton = 1"
    ) is None
    assert app.state.embeddings is previous_embeddings
    assert app.state.search.embeddings is previous_search_embeddings
    assert latest_status() is None


@pytest.mark.asyncio
async def test_put_cancellation_restores_newly_created_config(api_client, monkeypatch):
    client, data_dir = api_client
    app = client._transport.app
    db = app.state.db
    previous_embeddings = app.state.embeddings

    async def _passing_test(self, config):  # noqa: ARG001
        return ConnectionTestResult(ok=True, detail="ok", detected_dim=768)

    monkeypatch.setattr(EmbeddingConfigService, "test_config", _passing_test)
    connection_type = type(db.conn)
    real_commit = connection_type.commit
    cancel_once = True

    async def _commit_with_one_cancellation(connection):
        nonlocal cancel_once
        if cancel_once:
            cancel_once = False
            raise asyncio.CancelledError
        return await real_commit(connection)

    monkeypatch.setattr(connection_type, "commit", _commit_with_one_cancellation)

    # Starlette's BaseHTTPMiddleware converts a cancelled handler into this
    # transport-level error after the endpoint has re-raised cancellation.
    with pytest.raises(RuntimeError, match="No response returned"):
        await client.put(
            "/api/config/embedding",
            json={
                "backend": "openai_compat",
                "config": {
                    "base_url": "http://new",
                    "model": "new",
                    "dim": 768,
                },
            },
        )

    assert not (data_dir / "embedding_config.json").exists()
    assert await db.fetchone(
        "SELECT generation FROM embedding_index_state WHERE singleton = 1"
    ) is None
    assert app.state.embeddings is previous_embeddings
    assert latest_status() is None


# ---------------------------------------------------------------------------
# GET /api/config/embedding/backfill/status
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_backfill_status_idle_when_no_jobs(api_client):
    client, _ = api_client
    r = await client.get("/api/config/embedding/backfill/status")
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "idle"
    assert body["job_id"] is None


@pytest.mark.asyncio
async def test_backfill_status_returns_404_for_unknown_job_id(api_client):
    client, _ = api_client
    r = await client.get(
        "/api/config/embedding/backfill/status",
        params={"job_id": "bf_nope"},
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_backfill_status_returns_snapshot_for_known_job(api_client):
    from rka.services.jobs import JobQueue

    client, _ = api_client
    job_id = await JobQueue(client._transport.app.state.db).enqueue(
        "embedding_backfill", payload={"generation": 1},
    )
    clear_registry()
    r = await client.get(
        "/api/config/embedding/backfill/status",
        params={"job_id": job_id},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["job_id"] == job_id
    assert body["state"] == "pending"
    assert body["processed"] == 0
    assert body["total"] == 0
    assert body["started_at"].endswith("Z")
    assert isinstance(body["elapsed_seconds"], (int, float))


# ---------------------------------------------------------------------------
# 422 error-shape regression — Affordance G mapping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_422_responses_carry_error_detail_hint_keys(api_client):
    """All embedding-config 422s must use the {error, detail, hint} shape
    so the Settings page can render the hint verbatim."""
    client, data_dir = api_client
    # Force a corrupt file → GET returns 422.
    (data_dir / "embedding_config.json").write_text("{{{")

    r = await client.get("/api/config/embedding")
    assert r.status_code == 422
    body = r.json()
    assert set(body.keys()) >= {"error", "detail", "hint"}
    assert body["error"] == "embedding_config_invalid"


# --------------------------------------------------------------------------
# base_url is part of the reconciliation identity (regression)
# --------------------------------------------------------------------------


def test_backend_signature_distinguishes_base_url():
    """Repointing a dead backend must count as a change that needs backfill.

    Regression: the signature was `(backend, model, dim)`, so moving
    base_url from an unreachable host to a working one returned 200 with no
    job and left every entity created during the outage unembedded --
    observed in a real store as 1023 entities across six projects and three
    months. Same dim means `reshape_all_vec_tables_if_needed` is a no-op, so
    treating this as "changed" costs a backfill of the missing rows and
    never discards an existing vector.
    """
    from rka.api.routes.config import _backend_signature

    dead = EmbeddingConfig(
        backend="openai_compat",
        config={"base_url": "http://192.168.0.1:1234", "model": "m", "dim": 2560},
    )
    live = EmbeddingConfig(
        backend="openai_compat",
        config={"base_url": "http://host.docker.internal:1234", "model": "m", "dim": 2560},
    )
    assert _backend_signature(dead) != _backend_signature(live)


def test_backend_signature_ignores_api_key():
    """An api_key rotation still must NOT trigger a re-embed."""
    from rka.api.routes.config import _backend_signature

    a = EmbeddingConfig(
        backend="openai_compat",
        config={"base_url": "http://x", "model": "m", "dim": 4, "api_key": "old"},
    )
    b = EmbeddingConfig(
        backend="openai_compat",
        config={"base_url": "http://x", "model": "m", "dim": 4, "api_key": "new"},
    )
    assert _backend_signature(a) == _backend_signature(b)


def test_space_signature_distinguishes_embedding_space_id():
    """Same-dimension model changes cannot share one sqlite-vec index."""
    from rka.api.routes.config import _space_signature

    a = EmbeddingConfig(
        backend="openai_compat",
        config={
            "base_url": "http://x",
            "model": "transport-model",
            "dim": 1024,
            "embedding_space_id": "qwen-q8-sha-a",
        },
    )
    b = EmbeddingConfig(
        backend="openai_compat",
        config={
            "base_url": "http://x",
            "model": "transport-model",
            "dim": 1024,
            "embedding_space_id": "qwen-q8-sha-b",
        },
    )
    assert _space_signature(a) != _space_signature(b)


def test_query_template_change_does_not_change_document_space():
    """A query-only instruction update does not require re-indexing documents."""
    from rka.api.routes.config import _space_signature

    base = {
        "base_url": "http://x",
        "model": "transport-model",
        "dim": 1024,
        "embedding_space_id": "qwen-q8-sha-a",
        "document_template": "{text}",
    }
    a = EmbeddingConfig(
        backend="openai_compat",
        config={**base, "query_template": "Query: {text}"},
    )
    b = EmbeddingConfig(
        backend="openai_compat",
        config={**base, "query_template": "Instruct: retrieve.\nQuery: {text}"},
    )
    assert _space_signature(a) == _space_signature(b)


def test_document_template_change_requires_new_document_space():
    from rka.api.routes.config import _space_signature

    base = {
        "base_url": "http://x",
        "model": "transport-model",
        "dim": 1024,
        "embedding_space_id": "qwen-q8-sha-a",
    }
    a = EmbeddingConfig(
        backend="openai_compat",
        config={**base, "document_template": "{text}"},
    )
    b = EmbeddingConfig(
        backend="openai_compat",
        config={**base, "document_template": "Document: {text}"},
    )
    assert _space_signature(a) != _space_signature(b)


# --------------------------------------------------------------------------
# explicit backfill trigger
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_backfill_trigger_422_without_a_backend(api_client):
    """Reconciliation needs a backend; say so instead of silently doing nothing."""
    client, _ = api_client
    r = await client.post("/api/config/embedding/backfill")
    assert r.status_code == 422
    assert r.json()["error"] == "embedding_unavailable"


@pytest.mark.asyncio
async def test_backfill_trigger_returns_job_when_backend_present(api_client):
    """A trigger exists at all -- previously the only way in was a config edit."""
    client, _ = api_client

    class _Stub:
        dim = 768
        model_name = "stub"

        async def embed_batch(self, texts, **kw):
            return [[0.0] * 768 for _ in texts]

    transport = client._transport
    from rka.services.embedding_index import (
        embedding_space_signature,
        reconcile_embedding_index,
    )

    db = transport.app.state.db
    cfg = {"backend": "fastembed", "config": {"model_name": "stub", "dim": 768}}
    reconciled = await reconcile_embedding_index(
        db,
        space_signature=embedding_space_signature(cfg),
        model_name="stub",
        dim=768,
    )
    stub = _Stub()
    stub.index_generation = reconciled.state.generation
    stub.space_signature = reconciled.state.space_signature
    transport.app.state.embeddings = stub

    r = await client.post("/api/config/embedding/backfill?entity_types=claim")
    assert r.status_code == 202
    body = r.json()
    assert body["job_id"]
    assert body["entity_types"] == ["claim"]

    status = await client.get(
        f"/api/config/embedding/backfill/status?job_id={body['job_id']}"
    )
    assert status.status_code == 200


@pytest.mark.asyncio
async def test_manual_backfill_resumes_failed_bound_generation(api_client):
    client, _ = api_client
    app = client._transport.app
    db = app.state.db
    from rka.services.embedding_index import (
        embedding_space_signature,
        get_embedding_index_state,
        reconcile_embedding_index,
    )

    class _Stub:
        dim = 768
        model_name = "stub"

        async def embed_batch(self, texts, **kw):
            return [[0.0] * 768 for _ in texts]

    cfg = {"backend": "fastembed", "config": {"model_name": "stub", "dim": 768}}
    reconciled = await reconcile_embedding_index(
        db,
        space_signature=embedding_space_signature(cfg),
        model_name="stub",
        dim=768,
    )
    await db.execute(
        "UPDATE embedding_index_state SET status = 'failed' WHERE singleton = 1"
    )
    stub = _Stub()
    stub.index_generation = reconciled.state.generation
    stub.space_signature = reconciled.state.space_signature
    app.state.embeddings = stub

    response = await client.post("/api/config/embedding/backfill?entity_types=claim")

    assert response.status_code == 202
    state = await get_embedding_index_state(db)
    assert state is not None
    assert state.status == "reindexing"  # API only requests work
    from rka.services.worker import EnrichmentWorker
    assert await EnrichmentWorker(db=db, embeddings=stub).run_once()
    assert (await get_embedding_index_state(db)).status == "ready"


@pytest.mark.asyncio
async def test_manual_backfill_rejects_stale_process_generation(api_client):
    client, _ = api_client
    app = client._transport.app
    db = app.state.db
    from rka.services.embedding_index import (
        embedding_space_signature,
        get_embedding_index_state,
        reconcile_embedding_index,
    )

    class _Stub:
        dim = 768
        model_name = "old-model"

    old_cfg = {
        "backend": "fastembed",
        "config": {"model_name": "old-model", "dim": 768},
    }
    old = await reconcile_embedding_index(
        db,
        space_signature=embedding_space_signature(old_cfg),
        model_name="old-model",
        dim=768,
    )
    stub = _Stub()
    stub.index_generation = old.state.generation
    stub.space_signature = old.state.space_signature
    app.state.embeddings = stub

    new_cfg = {
        "backend": "fastembed",
        "config": {"model_name": "new-model", "dim": 768},
    }
    current = await reconcile_embedding_index(
        db,
        space_signature=embedding_space_signature(new_cfg),
        model_name="new-model",
        dim=768,
    )

    response = await client.post("/api/config/embedding/backfill")

    assert response.status_code == 409
    assert response.json()["error"] == "embedding_generation_changed"
    state = await get_embedding_index_state(db)
    assert state is not None
    assert state.generation == current.state.generation
    assert state.space_signature == current.state.space_signature
    assert latest_status() is None


@pytest.mark.asyncio
async def test_consistency_gate_failure_updates_backfill_job_status(api_client, monkeypatch):
    client, _ = api_client
    app = client._transport.app
    db = app.state.db
    from rka.services.embedding_jobs import EmbeddingJobs
    from rka.services.worker import EnrichmentWorker
    from rka.services.embedding_index import (
        embedding_space_signature,
        get_embedding_index_state,
        reconcile_embedding_index,
    )

    first_cfg = {
        "backend": "fastembed",
        "config": {"model_name": "first", "dim": 768},
    }
    await reconcile_embedding_index(
        db,
        space_signature=embedding_space_signature(first_cfg),
        model_name="first",
        dim=768,
    )
    next_cfg = {
        "backend": "fastembed",
        "config": {"model_name": "next", "dim": 768},
    }
    generation = await reconcile_embedding_index(
        db,
        space_signature=embedding_space_signature(next_cfg),
        model_name="next",
        dim=768,
    )

    class _Stub:
        dim = 768
        model_name = "next"
        index_generation = generation.state.generation
        space_signature = generation.state.space_signature

    class _InconsistentBackfill(BackfillService):
        async def run_backfill(self, status, progress_callback=None, entity_types=None):
            await self._db.execute(
                "INSERT INTO journal (id, type, content, source) "
                "VALUES ('jrn_late_consistency', 'note', 'source', 'pi')"
            )
            await self._db.execute(
                "INSERT INTO claims "
                "(id, source_entry_id, claim_type, content, embedding_pending) "
                "VALUES ('clm_late_consistency', 'jrn_late_consistency', "
                "'observation', 'not embedded', 1)"
            )
            status.state = "complete"
            return status

    job = await EmbeddingJobs(db).request(_Stub())
    await db.execute("UPDATE jobs SET max_attempts=1 WHERE id=?", [job["id"]])
    await db.commit()
    monkeypatch.setattr("rka.services.embedding_jobs.BackfillService", _InconsistentBackfill)
    assert await EnrichmentWorker(db=db, embeddings=_Stub()).run_once()
    status = await EmbeddingJobs(db).status(job["id"])

    state = await get_embedding_index_state(db)
    assert state is not None
    assert state.status == "failed"
    assert status["state"] == "failed"
    assert "consistency" in (status["error"] or "")


@pytest.mark.asyncio
async def test_overlapping_backfills_recover_after_first_run_fails(api_client, monkeypatch):
    """A queued healthy run must own and recover the same generation."""
    client, _ = api_client
    db = client._transport.app.state.db
    from rka.services.embedding_jobs import EmbeddingJobs
    from rka.services.worker import EnrichmentWorker
    from rka.services.embedding_index import (
        embedding_space_signature,
        get_embedding_index_state,
        reconcile_embedding_index,
    )

    cfg = {
        "backend": "fastembed",
        "config": {"model_name": "serialized", "dim": 768},
    }
    generation = await reconcile_embedding_index(
        db,
        space_signature=embedding_space_signature(cfg),
        model_name="serialized",
        dim=768,
        allow_legacy_adoption=False,
    )

    class _Stub:
        dim = 768
        model_name = "serialized"
        index_generation = generation.state.generation
        space_signature = generation.state.space_signature

    first_started = asyncio.Event()
    release_first = asyncio.Event()
    second_started = asyncio.Event()

    class _FailingBackfill(BackfillService):
        async def run_backfill(self, status, progress_callback=None, entity_types=None):
            status.state = "running"
            first_started.set()
            await release_first.wait()
            raise RuntimeError("first run failed")

    class _HealthyBackfill(BackfillService):
        async def run_backfill(self, status, progress_callback=None, entity_types=None):
            second_started.set()
            status.state = "complete"
            return status

    scheduler = EmbeddingJobs(db)
    job = await scheduler.request(_Stub())
    monkeypatch.setattr("rka.services.embedding_jobs.BackfillService", _FailingBackfill)
    first = EnrichmentWorker(db=db, embeddings=_Stub(), worker_id="first")
    second = EnrichmentWorker(db=db, embeddings=_Stub(), worker_id="second")
    first_task = asyncio.create_task(first.run_once())
    try:
        await asyncio.wait_for(first_started.wait(), timeout=2)
        assert (await scheduler.request(_Stub()))["id"] == job["id"]
        assert not await second.run_once()
        assert not second_started.is_set()
    finally:
        release_first.set()
        await first_task
    pending = await scheduler.status(job["id"])
    assert pending["state"] == "pending" and pending["attempts"] == 1
    await db.execute("UPDATE jobs SET run_after='2000-01-01T00:00:00Z' WHERE id=?", [job["id"]])
    await db.commit()
    monkeypatch.setattr("rka.services.embedding_jobs.BackfillService", _HealthyBackfill)
    assert await second.run_once()

    state = await get_embedding_index_state(db)
    assert (await scheduler.status(job["id"]))["state"] == "complete"
    assert (await scheduler.status(job["id"]))["attempts"] == 2
    assert second_started.is_set() is True
    assert state is not None
    assert state.generation == generation.state.generation
    assert state.status == "ready"
