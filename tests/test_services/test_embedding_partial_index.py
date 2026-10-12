"""A resource-rejected row must not masquerade as complete semantic coverage."""

import json
from types import SimpleNamespace

import httpx
import pytest

from rka.infra.embedding_backends.openai_compat import OpenAICompatBackend
from rka.infra.embeddings import EmbeddingService
from rka.services.embedding_index import get_embedding_index_state, reconcile_embedding_index
from rka.services.embedding_jobs import EmbeddingJobs
from rka.services.worker import EnrichmentWorker

LONG_TEXT = "Longonlyneedle " + "界" * 7000


async def _partial_fixture(db):
    calls = []

    def handler(request):
        texts = json.loads(request.content)["input"]
        calls.extend(texts)
        return httpx.Response(200, json={"data": [
            {"index": i, "embedding": [0.6, 0.8]} for i in range(len(texts))
        ]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    service = EmbeddingService(db=db, backend=OpenAICompatBackend(
        base_url="http://isolated.test", model="synthetic", dim=2, http_client=client,
    ))
    state = (await reconcile_embedding_index(
        db, space_signature=service.space_signature, model_name=service.model_name, dim=2,
    )).state
    service.bind_index_generation(state.generation, space_signature=state.space_signature)
    for entity_id, text in (("jrn_short", "Synthetic searchable research record"),
                            ("jrn_long", LONG_TEXT)):
        await db.execute(
            "INSERT INTO journal (id, project_id, type, source, content) "
            "VALUES (?, 'proj_default', 'note', 'executor', ?)", [entity_id, text],
        )
        await db.execute(
            "INSERT INTO fts_journal (id, content, summary) VALUES (?, ?, '')", [entity_id, text],
        )
    await db.commit()
    return service, calls, client


@pytest.mark.asyncio
async def test_terminal_oversize_failure_keeps_verified_vectors_searchable(db):
    service, calls, client = await _partial_fixture(db)
    try:
        jobs = EmbeddingJobs(db)
        job = await jobs.request(service)
        await db.execute("UPDATE jobs SET max_attempts=1 WHERE id=?", [job["id"]])
        await db.commit()
        assert await EnrichmentWorker(db=db, embeddings=service).run_once()
        status = await jobs.status(job["id"])
        assert status["state"] == "failed"  # never certify missing long records
        assert status["processed"] == 1 and status["total"] == 2
        assert "embedding_input_limit" in status["error"]
        assert calls == ["Synthetic searchable research record"]
        assert (await db.fetchone("SELECT content FROM journal WHERE id='jrn_long'"))[
            "content"
        ] == LONG_TEXT
        assert (await db.fetchone("SELECT count(*) AS n FROM embedding_metadata"))["n"] == 1
        assert (await get_embedding_index_state(db)).status == "partial"
        assert await service.index_search_ready()
        # A process restart must neither reset the failed job nor start inference.
        before = list(calls)
        assert (await jobs.request(service, automatic=True))["id"] == job["id"]
        assert calls == before
        from rka.services.search import SearchService
        search = SearchService(db, service)
        # No lexical match: this hit proves actual vector retrieval, not a label.
        hits = await search.search("unrelatedqueryneedle", entity_types=["journal"])
        assert [hit.entity_id for hit in hits] == ["jrn_short"]
        assert hits[0].vec_rank is not None and hits[0].fts_rank is None
        long_hits = await search.search("Longonlyneedle", entity_types=["journal"])
        long_hit = next(hit for hit in long_hits if hit.entity_id == "jrn_long")
        assert long_hit.fts_rank is not None and long_hit.vec_rank is None
    finally:
        await client.aclose()


async def _seed_terminal_candidate(db):
    service, calls, client = await _partial_fixture(db)
    await EmbeddingJobs(db).request(service)
    await service.embed_and_store("journal", "jrn_short", "Synthetic searchable research record")
    return service, calls, client


@pytest.mark.parametrize("corruption", [
    "stale_hash", "wrong_model", "wrong_dimension", "missing_vector", "orphan_vector",
    "orphan_metadata", "unknown_type", "wrong_project", "wrong_schema", "no_vectors",
])
async def test_partial_gate_rejects_unproven_pairs(db, corruption):
    import struct

    from rka.services.embedding_index import finish_embedding_transition

    service, _, client = await _seed_terminal_candidate(db)
    try:
        statements = {
            "stale_hash": "UPDATE embedding_metadata SET content_hash='stale'",
            "wrong_model": "UPDATE embedding_metadata SET model_name='other-model'",
            "wrong_dimension": "UPDATE embedding_metadata SET dimensions=3",
            "missing_vector": "DELETE FROM vec_journal",
            "orphan_metadata": "UPDATE embedding_metadata SET entity_id='jrn_missing'",
            "unknown_type": "UPDATE embedding_metadata SET entity_type='unknown'",
            "wrong_project": "UPDATE embedding_metadata SET project_id='another-project'",
            "no_vectors": "DELETE FROM embedding_metadata",
        }
        if corruption == "orphan_vector":
            await db.execute(
                "INSERT INTO vec_journal (id, project_id, embedding) VALUES ('jrn_orphan', 'proj_default', ?)",
                [struct.pack("2f", 0.6, 0.8)],
            )
        elif corruption == "wrong_schema":
            await db.execute("DROP TABLE vec_decisions")
            await db.execute("CREATE VIRTUAL TABLE vec_decisions USING vec0(id TEXT PRIMARY KEY, project_id TEXT, embedding float[3])")
        else:
            await db.execute(statements[corruption])
            if corruption == "no_vectors":
                await db.execute("DELETE FROM vec_journal")
        await db.commit()
        assert await finish_embedding_transition(
            db, generation=service.index_generation, success=False, error="synthetic_failure",
        )
        assert (await get_embedding_index_state(db)).status == "failed"
        assert not await service.index_search_ready()
    finally:
        await client.aclose()


async def test_partial_verification_budget_or_storage_failure_stays_closed(db, monkeypatch):
    from rka.services import embedding_inspection
    from rka.services.embedding_index import finish_embedding_transition

    service, _, client = await _seed_terminal_candidate(db)

    async def unavailable(*args, **kwargs):
        assert kwargs["max_rows"] == 50000
        raise embedding_inspection.EmbeddingInspectionError("inspection_budget_exceeded")

    monkeypatch.setattr(embedding_inspection, "_inspect_snapshot", unavailable)
    try:
        await finish_embedding_transition(db, generation=service.index_generation, success=False)
        assert (await get_embedding_index_state(db)).status == "failed"
        assert not await service.index_search_ready()
    finally:
        await client.aclose()


async def test_upgrade_rechecks_failed_generation_without_retry_or_vector_rewrite(db):
    service, calls, client = await _seed_terminal_candidate(db)
    try:
        # Reproduce a pre-upgrade exhausted job and failed generation.
        await db.execute("UPDATE jobs SET status='failed', attempts=max_attempts, last_error='embedding_input_limit'")
        await db.execute("UPDATE embedding_index_state SET status='failed', last_error='embedding_input_limit'")
        await db.commit()
        records = await db.fetchall("SELECT * FROM journal ORDER BY id")
        metadata = await db.fetchall("SELECT * FROM embedding_metadata")
        vectors = await db.fetchall("SELECT id, project_id, embedding FROM vec_journal")
        jobs = await db.fetchall("SELECT * FROM jobs")
        calls.clear()
        restarted = (await reconcile_embedding_index(
            db, space_signature=service.space_signature, model_name=service.model_name, dim=2,
        )).state
        assert restarted.status == "reindexing"
        await EmbeddingJobs(db).request(service, automatic=True)
        assert await service.index_search_status() == "partial"
        assert (await get_embedding_index_state(db)).last_error == "embedding_input_limit"
        assert not await EnrichmentWorker(db=db, embeddings=service).run_once()
        assert calls == []
        assert await db.fetchall("SELECT * FROM journal ORDER BY id") == records
        assert await db.fetchall("SELECT * FROM embedding_metadata") == metadata
        assert await db.fetchall("SELECT id, project_id, embedding FROM vec_journal") == vectors
        assert await db.fetchall("SELECT * FROM jobs") == jobs
        # A new restart keeps partial coverage and never relaunches the failed job.
        again = (await reconcile_embedding_index(
            db, space_signature=service.space_signature, model_name=service.model_name, dim=2,
        )).state
        assert again.status == "partial" and restarted.generation == again.generation
        await EmbeddingJobs(db).request(service, automatic=True)
        assert await db.fetchall("SELECT * FROM jobs") == jobs
    finally:
        await client.aclose()


async def test_partial_generation_fences_stale_readers_and_writers(db):
    from rka.services.embedding_index import (
        EmbeddingGenerationMismatch,
        embedding_index_search_ready,
        finish_embedding_transition,
    )

    service, _, client = await _seed_terminal_candidate(db)
    try:
        await finish_embedding_transition(db, generation=service.index_generation, success=False)
        assert await service.index_search_ready()
        for args in (
            {"generation": service.index_generation + 1, "space_signature": service.space_signature, "dim": 2},
            {"generation": service.index_generation, "space_signature": "wrong-space", "dim": 2},
            {"generation": service.index_generation, "space_signature": service.space_signature, "dim": 3},
        ):
            assert not await embedding_index_search_ready(db, **args)
        state = await reconcile_embedding_index(
            db, space_signature="new-space", model_name="new-model", dim=2,
        )
        assert state.state.generation == service.index_generation + 1
        assert not await service.index_search_ready()
        with pytest.raises(EmbeddingGenerationMismatch):
            await service.assert_current_generation()
        assert not await finish_embedding_transition(
            db, generation=service.index_generation, success=False,
        )
    finally:
        await client.aclose()


async def test_oversized_inspection_source_stays_closed_without_fetching_text(db, monkeypatch):
    from rka.services import embedding_inspection
    from rka.services.embedding_index import finish_embedding_transition

    service, _, client = await _seed_terminal_candidate(db)
    try:
        await db.execute("UPDATE journal SET content=? WHERE id='jrn_long'", ["x" * (2 * 1024 * 1024 + 1)])
        await db.commit()

        async def must_not_fetch(*args, **kwargs):
            pytest.fail("oversized source must be rejected before fetching document pages")

        monkeypatch.setattr(embedding_inspection, "_inspect_snapshot", must_not_fetch)
        await finish_embedding_transition(db, generation=service.index_generation, success=False)
        assert await service.index_search_status() == "failed"
    finally:
        await client.aclose()


async def test_partial_generation_accepts_new_records_and_explicit_repair_reaches_ready(db):
    from rka.services.embedding_index import finish_embedding_transition

    service, _, client = await _seed_terminal_candidate(db)
    try:
        await finish_embedding_transition(db, generation=service.index_generation, success=False)
        # Correct the synthetic fixture, not a production source: now all text fits.
        await db.execute("UPDATE journal SET content=? WHERE id='jrn_long'", [LONG_TEXT[:10]])
        # Normal writes still use the same generation/lease gates in partial mode.
        await service.embed_and_store("journal", "jrn_long", LONG_TEXT[:10])
        await db.execute("UPDATE jobs SET status='failed', last_error='synthetic_failure'")
        await db.commit()
        job = await EmbeddingJobs(db).request(service)
        assert await service.index_search_status() == "reindexing"
        assert await EnrichmentWorker(db=db, embeddings=service).run_once()
        assert (await EmbeddingJobs(db).status(job["id"]))["state"] == "complete"
        assert await service.index_search_status() == "ready"
        assert (await get_embedding_index_state(db)).last_error is None
    finally:
        await client.aclose()


@pytest.mark.parametrize("oversized_counts", [
    {"journal": 50001},
    {"journal": 25000, "decisions": 25001},
    {"embedding_metadata": 50001},
    {"vec_artifacts": 50001},
])
async def test_partial_row_budgets_reject_before_fetching_documents(db, monkeypatch, oversized_counts):
    from rka.services import embedding_inspection
    from rka.services.embedding_index import finish_embedding_transition

    service, _, client = await _seed_terminal_candidate(db)
    original_fetchone = db.fetchone
    counts = {f"SELECT count(*) AS n FROM {table}": n for table, n in oversized_counts.items()}

    async def count_boundary(sql, params=None):
        if sql in counts:
            return {"n": counts[sql]}
        return await original_fetchone(sql, params)

    async def must_not_inspect(*args, **kwargs):
        pytest.fail("row budget must be checked before source pages are fetched")

    monkeypatch.setattr(db, "fetchone", count_boundary)
    monkeypatch.setattr(embedding_inspection, "_inspect_snapshot", must_not_inspect)
    try:
        await finish_embedding_transition(db, generation=service.index_generation, success=False)
        assert await service.index_search_status() == "failed"
        assert not await service.index_search_ready()
    finally:
        await client.aclose()


@pytest.mark.parametrize("expiry", ["preflight", "after_inspection"])
async def test_partial_deadline_cannot_admit_an_expired_proof(db, monkeypatch, expiry):
    from rka.services import embedding_index, embedding_inspection

    service, _, client = await _seed_terminal_candidate(db)
    original_inspect = embedding_inspection._inspect_snapshot
    ticks = iter([0, 31]) if expiry == "preflight" else None
    clock = SimpleNamespace(now=0)
    monkeypatch.setattr(embedding_index, "time", SimpleNamespace(
        monotonic=lambda: next(ticks) if ticks is not None else clock.now,
    ))

    async def inspect(*args, **kwargs):
        if expiry == "preflight":
            pytest.fail("expired preflight must not fetch source pages")
        # This is a valid real proof, but it arrives after the caller's deadline.
        report = await original_inspect(*args, **{**kwargs, "deadline": float("inf")})
        assert report["totals"]["reusable"] == 1
        clock.now = 31
        return report

    monkeypatch.setattr(embedding_inspection, "_inspect_snapshot", inspect)
    try:
        await embedding_index.finish_embedding_transition(
            db, generation=service.index_generation, success=False,
        )
        assert await service.index_search_status() == "failed"
        assert not await service.index_search_ready()
    finally:
        await client.aclose()


async def test_partial_writes_preserve_receipts_rwf_notes_and_and_filters(db):
    from rka.models.journal import JournalEntryCreate
    from rka.services.embedding_index import finish_embedding_transition
    from rka.services.notes import NoteService

    service, calls, client = await _seed_terminal_candidate(db)
    try:
        await finish_embedding_transition(db, generation=service.index_generation, success=False)
        assert await service.index_search_status() == "partial"
        notes = NoteService(db, embeddings=service)
        intent = JournalEntryCreate(
            request_id="partial-rwf-receipt", source="pi", type="directive",
            content="Synthetic RWF stop decision\r\n  Evidence still incomplete.\n",
            capture_mode="raw_capture", tags=["rwf-stop-pivot", "rwf-v0"],
        )
        note = await notes.create(intent)
        receipt = await notes.write_receipt(intent.request_id)
        jobs = await db.fetchall("SELECT * FROM jobs ORDER BY id")
        calls_before_retry = list(calls)
        assert await notes.create(intent) == note
        assert await notes.write_receipt(intent.request_id) == receipt
        assert await db.fetchall("SELECT * FROM jobs ORDER BY id") == jobs
        assert calls == calls_before_retry  # receipt recovery never calls inference
        await notes.create(JournalEntryCreate(content="Only one tag", tags=["rwf-v0"], source="pi"))
        await notes.create(JournalEntryCreate(
            content="Different source", tags=["rwf-stop-pivot", "rwf-v0"], source="executor",
        ))
        assert await notes.list(source="pi", tags=["rwf-stop-pivot", "rwf-v0"]) == [note]
        assert await notes.list(source="pi", tags=["rwf-stop-pivot", "missing"]) == []
        assert note.content == note.verbatim_input == intent.content
        assert receipt.entry == note
        assert await service.index_search_status() == "partial"
    finally:
        await client.aclose()


async def test_partial_index_still_falls_back_when_query_provider_is_unavailable(db, monkeypatch):
    from rka.services.embedding_index import finish_embedding_transition
    from rka.services.search import SearchService

    service, _, client = await _seed_terminal_candidate(db)

    async def unavailable(*args, **kwargs):
        raise httpx.ConnectError("Synthetic provider is offline")

    try:
        await finish_embedding_transition(db, generation=service.index_generation, success=False)
        monkeypatch.setattr(service.backend, "embed", unavailable)
        search = SearchService(db, service)
        assert await search.search("unrelatedqueryneedle", entity_types=["journal"]) == []
        hits = await search.search("Longonlyneedle", entity_types=["journal"])
        assert [hit.entity_id for hit in hits] == ["jrn_long"]
        assert hits[0].fts_rank is not None and hits[0].vec_rank is None
        assert service.runtime_available is False
        assert await service.index_search_status() == "partial"
    finally:
        await client.aclose()
