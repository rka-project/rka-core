"""E1b model-free durable ownership, recovery and write-fencing regressions."""

import asyncio
import json

import httpx
import pytest

from rka.infra.database import Database
from rka.infra.embedding_backends.openai_compat import OpenAICompatBackend
from rka.infra.embeddings import EmbeddingService
from rka.services.embedding_index import reconcile_embedding_index
from rka.services.jobs import JobLeaseLost, JobQueue
from rka.services.worker import EnrichmentWorker


async def setup_index(db, *, count=3):
    calls = []

    def handler(request):
        texts = json.loads(request.content)["input"]
        calls.extend(texts)
        return httpx.Response(
            200, json={"data": [{"index": i, "embedding": [0.5, 0.5]} for i in range(len(texts))]}
        )

    service = EmbeddingService(
        db=db,
        backend=OpenAICompatBackend(
            base_url="http://isolated.test",
            model="synthetic",
            dim=2,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        ),
    )
    state = (
        await reconcile_embedding_index(
            db,
            space_signature=service.space_signature,
            model_name=service.model_name,
            dim=2,
        )
    ).state
    service.bind_index_generation(state.generation, space_signature=state.space_signature)
    for i in range(count):
        await db.execute(
            "INSERT INTO journal (id, project_id, content, type, source) VALUES (?, 'proj_default', ?, 'note', 'executor')",
            [f"jrn_durable_{i}", f"Synthetic durable note {i}"],
        )
    await db.commit()
    return service, calls


@pytest.mark.asyncio
async def test_schedule_is_durable_deduplicated_and_does_not_infer(db):
    from rka.services.embedding_jobs import EmbeddingJobs

    service, calls = await setup_index(db)
    scheduler = EmbeddingJobs(db)
    first = await scheduler.request(service)
    second = await scheduler.request(service)
    assert first["id"] == second["id"]
    assert calls == []
    assert first["job_type"] == "embedding_backfill"
    assert "base_url" not in first["payload"] and "api_key" not in first["payload"]
    snapshot = await EmbeddingJobs(db).status(first["id"])
    assert snapshot["state"] == "pending" and snapshot["attempts"] == 0


@pytest.mark.asyncio
async def test_worker_finishes_durable_backfill_and_status_survives_registry_loss(db):
    from rka.services.embedding_jobs import EmbeddingJobs
    from rka.services.embedding_backfill import clear_registry
    from rka.services.embedding_index import get_embedding_index_state

    service, calls = await setup_index(db)
    job = await EmbeddingJobs(db).request(service)
    worker = EnrichmentWorker(db=db, embeddings=service)
    assert await worker.run_once()
    clear_registry()
    status = await EmbeddingJobs(db).status(job["id"])
    assert status["state"] == "complete" and status["processed"] == 3
    assert status["total"] == 3 and len(calls) == 3
    assert (await get_embedding_index_state(db)).status == "ready"
    assert not await worker.run_once()


@pytest.mark.asyncio
async def test_queue_heartbeat_and_progress_require_live_lease(db):
    queue = JobQueue(db, lease_seconds=60)
    await queue.enqueue("note_embed")
    job = await queue.claim_next("old")
    await queue.renew(job)
    await queue.progress(job, {"processed": 1, "total": 2})
    await db.execute("UPDATE jobs SET lease_until='2000-01-01T00:00:00Z' WHERE id=?", [job["id"]])
    await db.commit()
    reclaimed = await queue.claim_next("new")
    assert reclaimed["lease_token"] != job["lease_token"]
    for operation in (
        queue.renew(job),
        queue.progress(job, {"processed": 2}),
        queue.assert_owned(job),
    ):
        with pytest.raises(JobLeaseLost):
            await operation


@pytest.mark.asyncio
async def test_cancel_is_persistent_and_does_not_restart_automatically(db):
    from rka.services.embedding_jobs import EmbeddingJobs

    service, calls = await setup_index(db)
    scheduler = EmbeddingJobs(db)
    job = await scheduler.request(service)
    await scheduler.cancel(job["id"])
    assert (await scheduler.status(job["id"]))["error_code"] == "embedding_backfill_cancelled"
    assert (await scheduler.request(service, automatic=True))["id"] == job["id"]
    assert not await EnrichmentWorker(db=db, embeddings=service).run_once()
    assert calls == []
    retried = await scheduler.request(service)
    assert retried["id"] != job["id"]


@pytest.mark.asyncio
async def test_cancelled_owner_cannot_write_after_inference(db):
    from rka.services.embedding_jobs import EmbeddingJobs

    service, _ = await setup_index(db)
    entered, release = asyncio.Event(), asyncio.Event()

    async def delayed(texts, *, is_query=False):
        entered.set()
        await release.wait()
        return [[0.5, 0.5] for _ in texts]

    service.embed_batch = delayed
    job = await EmbeddingJobs(db).request(service)
    worker = EnrichmentWorker(db=db, embeddings=service)
    task = asyncio.create_task(worker.run_once())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await EmbeddingJobs(db).cancel(job["id"])
    finally:
        release.set()
        await task
    assert (await db.fetchone("SELECT count(*) AS n FROM embedding_metadata"))["n"] == 0


@pytest.mark.asyncio
async def test_status_can_be_read_through_a_second_database_connection(db):
    from rka.services.embedding_jobs import EmbeddingJobs

    service, _ = await setup_index(db)
    job = await EmbeddingJobs(db).request(service)
    peer = Database(db.db_path)
    await peer.connect()
    try:
        assert (await EmbeddingJobs(peer).status(job["id"]))["state"] == "pending"
    finally:
        await peer.close()


@pytest.mark.asyncio
async def test_cross_connection_dedupe_and_scope_conflict(db):
    from rka.services.embedding_jobs import EmbeddingJobs, BackfillScopeBusy

    service, _ = await setup_index(db)
    peer = Database(db.db_path)
    await peer.connect()
    try:
        one, two = await asyncio.gather(
            EmbeddingJobs(db).request(service, ["journal"]),
            EmbeddingJobs(peer).request(service, ["journal"]),
        )
        assert one["id"] == two["id"]
        with pytest.raises(BackfillScopeBusy):
            await EmbeddingJobs(peer).request(service)
    finally:
        await peer.close()


@pytest.mark.asyncio
async def test_final_expired_attempt_is_terminal_and_startup_does_not_reset_it(db):
    from rka.services.embedding_jobs import EmbeddingJobs
    from rka.services.embedding_index import get_embedding_index_state

    service, calls = await setup_index(db)
    job = await EmbeddingJobs(db).request(service)
    await db.execute("UPDATE jobs SET max_attempts=1 WHERE id=?", [job["id"]])
    await db.commit()
    await JobQueue(db).claim_next("dead-process")
    await db.execute("UPDATE jobs SET lease_until='2000-01-01T00:00:00Z' WHERE id=?", [job["id"]])
    await db.commit()
    assert not await EnrichmentWorker(db=db, embeddings=service).run_once()
    assert (await EmbeddingJobs(db).status(job["id"]))["state"] == "failed"
    assert (await get_embedding_index_state(db)).status == "failed"
    assert (await EmbeddingJobs(db).request(service, automatic=True))["id"] == job["id"]
    assert calls == []


@pytest.mark.asyncio
async def test_old_generation_is_superseded_without_touching_new_vectors(db):
    from rka.services.embedding_jobs import EmbeddingJobs

    service, calls = await setup_index(db)
    old = await EmbeddingJobs(db).request(service)
    next_state = (
        await reconcile_embedding_index(
            db,
            space_signature="new-space",
            model_name=service.model_name,
            dim=2,
        )
    ).state
    service.bind_index_generation(next_state.generation, space_signature="new-space")
    new = await EmbeddingJobs(db).request(service)
    assert old["id"] != new["id"]
    assert (await EmbeddingJobs(db).status(old["id"]))[
        "error_code"
    ] == "embedding_generation_superseded"
    assert await EnrichmentWorker(db=db, embeddings=service).run_once()
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_healthy_startup_does_not_schedule_or_reembed(db):
    from rka.services.embedding_jobs import EmbeddingJobs

    service, calls = await setup_index(db)
    await EmbeddingJobs(db).request(service)
    await EnrichmentWorker(db=db, embeddings=service).run_once()
    assert await EmbeddingJobs(db).request(service, automatic=True) is None
    assert len(calls) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["backfill", "entity_job"])
async def test_source_edit_during_inference_rejects_stale_vector(db, kind):
    from rka.services.embedding_jobs import EmbeddingJobs

    service, _ = await setup_index(db, count=1)
    entered, release = asyncio.Event(), asyncio.Event()

    async def delayed(texts, **kw):
        entered.set()
        await release.wait()
        return [[0.5, 0.5] for _ in texts]

    if kind == "backfill":
        service.embed_batch = delayed
        job = await EmbeddingJobs(db).request(service)
        job_id = job["id"]
    else:

        async def document(text):
            return (await delayed([text]))[0]

        service.embed_document = document
        job_id = await JobQueue(db).enqueue(
            "note_embed", entity_type="journal", entity_id="jrn_durable_0"
        )
    task = asyncio.create_task(EnrichmentWorker(db=db, embeddings=service).run_once())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await db.execute(
            "UPDATE journal SET content='Changed during inference' WHERE id='jrn_durable_0'"
        )
        await db.commit()
    finally:
        release.set()
        await task
    assert (await db.fetchone("SELECT count(*) AS n FROM embedding_metadata"))["n"] == 0
    assert (await JobQueue(db).get(job_id))["status"] == "pending"


@pytest.mark.asyncio
async def test_worker_renews_lease_while_inference_is_waiting(db, monkeypatch):
    from rka.services.embedding_jobs import EmbeddingJobs

    service, _ = await setup_index(db, count=1)
    renewed, release = asyncio.Event(), asyncio.Event()
    original = JobQueue.renew

    async def renew(queue, job):
        await original(queue, job)
        renewed.set()

    async def delayed(texts, **kw):
        await release.wait()
        return [[0.5, 0.5] for _ in texts]

    service.embed_batch = delayed
    monkeypatch.setattr(JobQueue, "renew", renew)
    job = await EmbeddingJobs(db).request(service)
    task = asyncio.create_task(EnrichmentWorker(db=db, embeddings=service).run_once())
    try:
        await asyncio.wait_for(renewed.wait(), 2)
        assert (await JobQueue(db).get(job["id"]))["lease_until"] is not None
        assert await JobQueue(db).claim_next("competing-worker") is None
    finally:
        release.set()
        await task


@pytest.mark.asyncio
async def test_heartbeat_failure_aborts_attempt_without_writing(db, monkeypatch):
    from rka.services.embedding_jobs import EmbeddingJobs

    service, _ = await setup_index(db)
    entered = asyncio.Event()

    async def delayed(texts, **kw):
        entered.set()
        await asyncio.Event().wait()

    async def broken_heartbeat(queue, job):
        await entered.wait()
        raise RuntimeError("synthetic heartbeat storage failure")

    service.embed_batch = delayed
    monkeypatch.setattr(JobQueue, "renew", broken_heartbeat)
    job = await EmbeddingJobs(db).request(service)
    await asyncio.wait_for(EnrichmentWorker(db=db, embeddings=service).run_once(), 3)
    assert (await JobQueue(db).get(job["id"]))["status"] == "pending"
    assert (await db.fetchone("SELECT count(*) AS n FROM embedding_metadata"))["n"] == 0


@pytest.mark.asyncio
async def test_edit_after_vector_write_cannot_lose_the_coalesced_retry(db):
    service, _ = await setup_index(db, count=1)
    job_id = await JobQueue(db).enqueue(
        "note_embed", entity_type="journal", entity_id="jrn_durable_0"
    )
    worker = EnrichmentWorker(db=db, embeddings=service)
    original = worker._with_heartbeat

    async def edit_after_work(job):
        result = await original(job)
        assert (await db.fetchone("SELECT count(*) AS n FROM embedding_metadata"))["n"] == 1
        await db.execute(
            "UPDATE journal SET content='Edited before job completion' WHERE id='jrn_durable_0'"
        )
        await db.commit()
        return result

    worker._with_heartbeat = edit_after_work
    assert await worker.run_once()
    assert (await JobQueue(db).get(job_id))["status"] == "pending"
    worker._with_heartbeat = original
    await db.execute("UPDATE jobs SET run_after='2000-01-01T00:00:00Z' WHERE id=?", [job_id])
    await db.commit()
    assert await worker.run_once()
    assert (await JobQueue(db).get(job_id))["status"] == "completed"
    metadata = await db.fetchone(
        "SELECT content_hash FROM embedding_metadata WHERE entity_id='jrn_durable_0'"
    )
    assert metadata["content_hash"] == EmbeddingService.content_hash("Edited before job completion")


@pytest.mark.asyncio
async def test_disabled_worker_is_not_reported_as_a_superseded_generation(db):
    from rka.services.embedding_jobs import EmbeddingJobs

    service, calls = await setup_index(db)
    job = await EmbeddingJobs(db).request(service)
    assert await EnrichmentWorker(db=db, embeddings=None, embeddings_enabled=False).run_once()
    status = await EmbeddingJobs(db).status(job["id"])
    assert status["state"] == "pending" and status["error_code"] == "embedding_unavailable"
    assert calls == []


@pytest.mark.asyncio
async def test_dead_worker_process_resumes_without_reembedding_committed_rows(db):
    import subprocess
    import sys
    from rka.services.embedding_jobs import EmbeddingJobs

    service, calls = await setup_index(db)
    job = await EmbeddingJobs(db).request(service)
    # A disposable child uses a fake model, commits two rows, then stalls on
    # the third. Only this known test child is terminated, never a live worker.
    script = """
import asyncio, sys
from rka.infra.database import Database
from rka.infra.embeddings import EmbeddingService
from rka.infra.embedding_backends.openai_compat import OpenAICompatBackend
from rka.services.embedding_index import get_embedding_index_state
from rka.services.worker import EnrichmentWorker
async def main():
    db = Database(sys.argv[1])
    await db.connect()
    await db.initialize_phase2_schema()
    state = await get_embedding_index_state(db)
    service = EmbeddingService(db=db, backend=OpenAICompatBackend(base_url="http://isolated.test", model="synthetic", dim=2))
    service.bind_index_generation(state.generation, space_signature=state.space_signature)
    count = 0
    async def embed(texts, **kw):
        nonlocal count
        count += 1
        if count == 2:
            print("PARTIAL_COMMIT_READY", flush=True)
            await asyncio.Event().wait()
        return [[0.5, 0.5] for _ in texts]
    service.embed_batch = embed
    await EnrichmentWorker(db=db, embeddings=service, worker_id="disposable-child").run_once()
asyncio.run(main())
"""
    child = subprocess.Popen(
        [sys.executable, "-c", script, db.db_path],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        line = await asyncio.wait_for(asyncio.to_thread(child.stdout.readline), 15)
        assert line.strip() == "PARTIAL_COMMIT_READY"
    finally:
        child.terminate()
        try:
            await asyncio.wait_for(asyncio.to_thread(child.wait), 5)
        except TimeoutError:
            child.kill()
            await asyncio.to_thread(child.wait)
        child.stdout.close()
        child.stderr.close()
    assert (await db.fetchone("SELECT count(*) AS n FROM embedding_metadata"))["n"] == 2
    assert (await EmbeddingJobs(db).status(job["id"]))["processed"] == 2
    await db.execute("UPDATE jobs SET lease_until='2000-01-01T00:00:00Z' WHERE id=?", [job["id"]])
    await db.commit()
    assert await EnrichmentWorker(db=db, embeddings=service, worker_id="replacement").run_once()
    status = await EmbeddingJobs(db).status(job["id"])
    assert status["state"] == "complete" and status["attempts"] == 2
    assert calls == ["Synthetic durable note 2"]
