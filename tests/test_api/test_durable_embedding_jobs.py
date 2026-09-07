"""REST/startup lifecycle uses durable intents, not API corpus inference."""

import httpx
import pytest
import pytest_asyncio

from rka.api.app import create_app
from rka.config import RKAConfig
from rka.infra.database import Database
from rka.infra.embeddings import EmbeddingService
from rka.services.embedding_config import EmbeddingConfig, EmbeddingConfigService
from rka.services.embedding_index import reconcile_embedding_index
from rka.services.jobs import JobQueue
from rka.services.worker import EnrichmentWorker


@pytest_asyncio.fixture
async def client_store(tmp_path):
    config = RKAConfig(
        project_dir=tmp_path,
        db_path=tmp_path / "api.db",
        data_dir=tmp_path,
        llm_enabled=False,
        embeddings_enabled=False,
    )
    app = create_app(config)
    async with app.router.lifespan_context(app):
        cfg = {
            "backend": "openai_compat",
            "config": {
                "base_url": "http://isolated.test",
                "model": "synthetic",
                "dim": 2,
            },
        }
        service = EmbeddingService.from_config(cfg, db=app.state.db)
        state = (
            await reconcile_embedding_index(
                app.state.db,
                space_signature=service.space_signature,
                model_name=service.model_name,
                dim=2,
            )
        ).state
        service.bind_index_generation(state.generation, space_signature=state.space_signature)
        app.state.embeddings = service
        app.state.search.embeddings = service
        await app.state.db.execute(
            "INSERT INTO journal (id, project_id, content, type, source) VALUES ('jrn_api_durable', 'proj_default', 'Synthetic note', 'note', 'executor')",
        )
        await app.state.db.commit()
        calls = []

        async def embed(texts, **kw):
            calls.extend(texts)
            return [[0.5, 0.5] for _ in texts]

        service.embed_batch = embed
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://isolated.test"
        ) as client:
            yield client, app.state.db, service, calls


@pytest.mark.asyncio
async def test_post_returns_pending_without_inference_then_worker_finishes(client_store):
    client, db, service, calls = client_store
    response = await client.post("/api/config/embedding/backfill")
    assert response.status_code == 202 and calls == []
    url = response.json()["status_url"]
    assert (await client.get(url)).json()["state"] == "pending"
    duplicate = await client.post("/api/config/embedding/backfill")
    assert duplicate.json()["job_id"] == response.json()["job_id"]
    assert await EnrichmentWorker(db=db, embeddings=service).run_once()
    assert (await client.get(url)).json()["state"] == "complete"
    assert calls == ["Synthetic note"]


@pytest.mark.asyncio
async def test_cancel_endpoint_is_idempotent_and_keeps_status(client_store):
    client, db, service, calls = client_store
    response = await client.post("/api/config/embedding/backfill")
    job_id = response.json()["job_id"]
    for _ in range(2):
        cancelled = await client.post(f"/api/config/embedding/backfill/{job_id}/cancel")
        assert cancelled.status_code == 200
        assert cancelled.json()["state"] == "failed"
        assert cancelled.json()["error_code"] == "embedding_backfill_cancelled"
    assert not await EnrichmentWorker(db=db, embeddings=service).run_once()
    assert calls == []
    assert (await client.post("/api/config/embedding/backfill/unknown/cancel")).status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("types", ["invalid", "claim,invalid", "", "claim," * 100])
async def test_invalid_scope_is_rejected_before_enqueue(client_store, types):
    client, db, _, calls = client_store
    response = await client.post("/api/config/embedding/backfill", params={"entity_types": types})
    assert response.status_code == 422
    assert (await db.fetchone("SELECT count(*) AS n FROM jobs"))["n"] == 0
    assert calls == []


@pytest.mark.asyncio
async def test_wider_scope_conflict_is_explicit(client_store):
    client, _, _, calls = client_store
    assert (
        await client.post("/api/config/embedding/backfill?entity_types=journal")
    ).status_code == 202
    response = await client.post("/api/config/embedding/backfill")
    assert response.status_code == 409 and response.json()["error"] == "embedding_backfill_busy"
    assert calls == []


@pytest.mark.asyncio
async def test_pack_registry_is_not_embedding_job_authority(client_store):
    from rka.services.embedding_backfill import register_job

    client, _, _, _ = client_store
    pack = register_job("imp")
    assert (
        await client.get("/api/config/embedding/backfill/status", params={"job_id": pack.job_id})
    ).status_code == 404
    assert (await client.get("/api/config/embedding/backfill/status")).json()["state"] == "idle"


@pytest.mark.asyncio
@pytest.mark.parametrize("dimension", [0, 2])
async def test_startup_never_probes_or_runs_corpus_inference(tmp_path, monkeypatch, dimension):
    async def forbidden(*args, **kw):
        raise AssertionError("startup must not call a provider or backfill loop")

    monkeypatch.setattr(EmbeddingConfigService, "test_config", forbidden)
    monkeypatch.setattr(EmbeddingService, "embed_batch", forbidden)
    config = RKAConfig(
        project_dir=tmp_path,
        db_path=tmp_path / "startup.db",
        data_dir=tmp_path,
        llm_enabled=False,
        embeddings_enabled=True,
    )
    EmbeddingConfigService(config_dir=tmp_path).save_config(
        EmbeddingConfig(
            backend="openai_compat",
            config={"base_url": "http://isolated.test", "model": "synthetic", "dim": dimension},
        ),
        actor="system",
    )
    seed = Database(config.database_url)
    await seed.connect()
    await seed.initialize_schema()
    await seed.initialize_phase2_schema()
    await seed.execute(
        "INSERT INTO journal (id, project_id, content, type, source) VALUES ('jrn_startup_durable', 'proj_default', 'Synthetic note', 'note', 'executor')",
    )
    await seed.commit()
    await seed.close()
    app = create_app(config)
    async with app.router.lifespan_context(app):
        jobs = await app.state.db.fetchall("SELECT * FROM jobs WHERE job_type='embedding_backfill'")
        if dimension:
            assert len(jobs) == 1 and jobs[0]["status"] == "pending"
        else:
            assert jobs == [] and app.state.embeddings is None
            assert "dimension is missing" in app.state.embedding_unavailable_reason


@pytest.mark.asyncio
async def test_put_config_and_enqueue_roll_back_together(client_store, monkeypatch):
    from rka.infra.embedding_backends import ConnectionTestResult

    client, db, service, calls = client_store

    async def probe(*args):
        return ConnectionTestResult(ok=True, detail="synthetic", detected_dim=2)

    async def enqueue_failure(*args, **kw):
        raise RuntimeError("synthetic queue write failure")

    monkeypatch.setattr(EmbeddingConfigService, "test_config", probe)
    monkeypatch.setattr(JobQueue, "enqueue", enqueue_failure)
    before = await db.fetchone("SELECT * FROM embedding_index_state")
    response = await client.put(
        "/api/config/embedding",
        json={
            "backend": "openai_compat",
            "config": {"base_url": "http://isolated.test", "model": "new-model", "dim": 2},
        },
    )
    assert response.status_code == 500
    assert (await db.fetchone("SELECT * FROM embedding_index_state")) == before
    assert client._transport.app.state.embeddings is service
    assert calls == []
