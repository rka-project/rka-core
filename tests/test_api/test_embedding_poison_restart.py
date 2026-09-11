"""#158: real input policy, synthetic native inference, durable API restarts."""

import httpx
import pytest

from rka.api.app import create_app
from rka.config import RKAConfig
from rka.infra.database import Database
from rka.infra.embedding_backends.fastembed import FastEmbedBackend
from rka.infra.native_embedding_process import NATIVE_PROCESS
from rka.services.embedding_config import EmbeddingConfig, EmbeddingConfigService
from rka.services.jobs import JobQueue
from rka.services.worker import EnrichmentWorker


@pytest.mark.asyncio
async def test_native_poison_backfill_stays_failed_across_api_restarts(tmp_path, monkeypatch):
    # Full-model RSS and historical-schema acceptance remain separate opt-in
    # container gates. This regression must never load/download a real model.
    calls = []

    def infer(options, batches, **kwargs):
        texts = [text for batch in batches for text in batch]
        assert all(len(text.encode()) <= 2048 for text in texts)
        calls.extend(texts)
        return [[1.0] + [0.0] * 767 for _ in texts]

    def forbidden(*args, **kwargs):
        raise AssertionError("API startup must not load a model or test a provider")

    monkeypatch.setattr(NATIVE_PROCESS, "call", infer)
    monkeypatch.setattr(FastEmbedBackend, "_get_model", forbidden)
    monkeypatch.setattr(EmbeddingConfigService, "test_config", forbidden)
    EmbeddingConfigService(tmp_path).save_config(
        EmbeddingConfig(
            backend="fastembed",
            config={"model_name": "nomic-ai/nomic-embed-text-v1.5", "dim": 768},
        ),
        "system",
    )
    path = tmp_path / "startup-poison.db"
    db = Database(str(path))
    await db.connect()
    try:
        await db.initialize_schema()
        await db.initialize_phase2_schema()
        for entity_id, text in (("jrn_poison", "x" * 20535), ("jrn_valid", "Short synthetic note")):
            await db.execute(
                "INSERT INTO journal(id,content,type,source,project_id) VALUES (?,?,'note','pi','proj_default')",
                [entity_id, text],
            )
        await db.commit()
    finally:
        await db.close()
    config = RKAConfig(
        project_dir=tmp_path,
        db_path=path,
        data_dir=tmp_path,
        llm_enabled=False,
        embeddings_enabled=True,
    )
    app = create_app(config)
    async with app.router.lifespan_context(app):
        db = app.state.db
        assert calls == []
        job = await db.fetchone(
            "SELECT id,status,attempts FROM jobs WHERE job_type='embedding_backfill'"
        )
        assert job["status"] == "pending" and job["attempts"] == 0
        # Change only the synthetic queue's attempt cap/backoff, not production
        # policy; this unit regression must not sleep for retry scheduling.
        await db.execute("UPDATE jobs SET max_attempts=2 WHERE id=?", [job["id"]])
        await db.commit()
        worker = EnrichmentWorker(db=db, embeddings=app.state.embeddings)
        assert await worker.run_once()
        assert (await JobQueue(db).get(job["id"]))["status"] == "pending"
        await db.execute("UPDATE jobs SET run_after='2000-01-01T00:00:00Z' WHERE id=?", [job["id"]])
        await db.commit()
        assert await worker.run_once()
        terminal = await JobQueue(db).get(job["id"])
        assert terminal["status"] == "failed" and terminal["attempts"] == 2
        assert "embedding_input_limit" in terminal["last_error"]
        assert len(calls) == 1
        metadata = await db.fetchall("SELECT * FROM embedding_metadata ORDER BY entity_id")
        assert len(metadata) == 1 and metadata[0]["entity_id"] == "jrn_valid"
        vector = await db.fetchone("SELECT embedding FROM vec_journal WHERE id='jrn_valid'")

    for _ in range(2):
        restarted = create_app(config)
        async with restarted.router.lifespan_context(restarted):
            db = restarted.state.db
            assert await db.fetchall(
                "SELECT id,status,attempts FROM jobs WHERE job_type='embedding_backfill'"
            ) == [
                {"id": job["id"], "status": "failed", "attempts": 2},
            ]
            assert (await db.fetchone("SELECT status FROM embedding_index_state"))[
                "status"
            ] == "failed"
            assert (
                await db.fetchall("SELECT * FROM embedding_metadata ORDER BY entity_id") == metadata
            )
            assert (
                await db.fetchone("SELECT embedding FROM vec_journal WHERE id='jrn_valid'")
                == vector
            )
            assert (await db.fetchone("SELECT content FROM journal WHERE id='jrn_poison'"))[
                "content"
            ] == "x" * 20535
            assert not await EnrichmentWorker(
                db=db, embeddings=restarted.state.embeddings
            ).run_once()
            assert len(calls) == 1
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=restarted), base_url="http://test"
            ) as client:
                assert (await client.get("/api/health")).status_code == 200
