"""Synthetic regressions for durable project scope and import receipts."""

import io
import json
import zipfile

import pytest

from rka.services.embedding_jobs import EmbeddingJobs, BackfillScopeBusy
from rka.services.knowledge_pack import KnowledgePackService, PACK_SCHEMA_VERSION
from rka.services.worker import EnrichmentWorker
from tests.test_services.test_durable_embedding_backfill import setup_index


def pack_bytes(project_id="prj_durable_import"):
    stream = io.BytesIO()
    manifest = {
        "pack_format_version": PACK_SCHEMA_VERSION,
        "schema_version": 21,
        "project": {"id": project_id, "name": project_id, "created_by": "system"},
        "project_state": None,
        "tables": {
            "journal": [
                {
                    "id": "jrn_import_durable",
                    "project_id": project_id,
                    "content": "synthetic imported record",
                    "type": "note",
                    "source": "executor",
                }
            ]
        },
        "table_counts": {"journal": 1},
    }
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
    stream.seek(0)
    return stream


@pytest.mark.asyncio
async def test_scoped_force_keeps_other_project_vectors(db):
    service, calls = await setup_index(db, count=1)
    await db.execute("INSERT INTO projects (id,name) VALUES ('prj_other','Other')")
    await db.execute(
        "INSERT INTO journal (id,project_id,content,type,source) VALUES ('jrn_other','prj_other','Other synthetic note','note','executor')"
    )
    await db.commit()
    scheduler = EmbeddingJobs(db)
    await scheduler.request(service)
    await EnrichmentWorker(db=db, embeddings=service).run_once()
    before = await db.fetchone("SELECT embedding FROM vec_journal WHERE id='jrn_other'")
    calls.clear()
    job = await scheduler.request(service, ["journal"], project_id="proj_default", force=True)
    assert await EnrichmentWorker(db=db, embeddings=service).run_once()
    assert (await scheduler.status(job["id"]))["state"] == "complete"
    assert calls == ["Synthetic durable note 0"]
    assert await db.fetchone("SELECT embedding FROM vec_journal WHERE id='jrn_other'") == before


@pytest.mark.asyncio
async def test_hash_check_repairs_changed_rows_without_reembedding_unchanged_rows(db):
    service, calls = await setup_index(db, count=2)
    scheduler = EmbeddingJobs(db)
    await scheduler.request(service)
    worker = EnrichmentWorker(db=db, embeddings=service)
    await worker.run_once()
    await db.execute(
        "UPDATE journal SET content='Changed synthetic source' WHERE id='jrn_durable_0'"
    )
    await db.commit()
    calls.clear()
    job = await scheduler.request(
        service, ["journal"], project_id="proj_default", check_hashes=True
    )
    await worker.run_once()
    status = await scheduler.status(job["id"])
    assert status["state"] == "complete" and status["processed"] == status["total"] == 1
    assert calls == ["Changed synthetic source"]
    assert (
        await db.fetchone(
            "SELECT content_hash FROM embedding_metadata WHERE entity_id='jrn_durable_0'"
        )
    )["content_hash"] == service.content_hash("Changed synthetic source")


@pytest.mark.asyncio
async def test_unknown_payload_version_cannot_run_inference(db):
    service, calls = await setup_index(db, count=1)
    scheduler = EmbeddingJobs(db)
    job = await scheduler.request(service, ["journal"], project_id="proj_default")
    assert job["job_type"] == "embedding_backfill_v2"
    payload = {**job["payload"], "version": 999}
    await db.execute("UPDATE jobs SET payload=? WHERE id=?", [json.dumps(payload), job["id"]])
    await db.commit()
    await EnrichmentWorker(db=db, embeddings=service).run_once()
    status = await scheduler.status(job["id"])
    assert status["state"] != "complete"
    assert status["error_code"] == "embedding_job_version_unsupported"
    assert calls == []


@pytest.mark.asyncio
async def test_import_receipt_and_vectors_are_distinct_and_durable(db):
    service, calls = await setup_index(db, count=0)
    result = await KnowledgePackService(db, embeddings=service).import_pack(pack_bytes())
    assert calls == []
    assert result.indexing["job_id"].startswith("job_")
    status = await KnowledgePackService(db).import_status(result.indexing["job_id"])
    assert status["lexical_state"] == "complete" and status["semantic_state"] == "pending"
    assert await db.fetchone("SELECT id FROM fts_journal WHERE fts_journal MATCH 'synthetic'")
    assert await EnrichmentWorker(db=db, embeddings=service).run_once()
    status = await KnowledgePackService(db).import_status(result.indexing["job_id"])
    assert status["state"] == "complete" and status["semantic_state"] == "complete"
    assert calls == ["synthetic imported record"]


@pytest.mark.asyncio
async def test_import_busy_rolls_back_rows_and_receipt(db):
    service, calls = await setup_index(db, count=0)
    await EmbeddingJobs(db).request(service)
    with pytest.raises(BackfillScopeBusy):
        await KnowledgePackService(db, embeddings=service).import_pack(pack_bytes())
    assert not await db.fetchone("SELECT id FROM projects WHERE id='prj_durable_import'")
    assert not await db.fetchone("SELECT id FROM jobs WHERE job_type='pack_import'")
    assert calls == []


@pytest.mark.asyncio
async def test_disabled_import_is_lexical_complete_not_semantic_ready(db):
    result = await KnowledgePackService(db).import_pack(pack_bytes(), defer_indexing=True)
    status = await KnowledgePackService(db).import_status(result.indexing["job_id"])
    assert status["state"] == "complete"
    assert status["semantic_state"] == "disabled" and status["semantic_ready"] is False
    assert not await EnrichmentWorker(db=db, embeddings_enabled=False).run_once()


@pytest.mark.asyncio
async def test_receipt_failure_rolls_back_lexical_and_rows(db, monkeypatch):
    from rka.services.jobs import JobQueue

    async def fail(*args, **kw):
        raise RuntimeError("synthetic receipt failure")

    monkeypatch.setattr(JobQueue, "enqueue", fail)
    with pytest.raises(RuntimeError, match="receipt failure"):
        await KnowledgePackService(db).import_pack(pack_bytes())
    assert not await db.fetchone("SELECT id FROM projects WHERE id='prj_durable_import'")
    assert not await db.fetchone("SELECT id FROM fts_journal WHERE fts_journal MATCH 'synthetic'")


@pytest.mark.asyncio
async def test_scoped_completion_does_not_certify_unrelated_missing_rows(db):
    from rka.services.embedding_index import get_embedding_index_state

    service, calls = await setup_index(db, count=1)
    await db.execute("INSERT INTO projects (id,name) VALUES ('prj_other','Other')")
    await db.execute(
        "INSERT INTO journal (id,project_id,content,type,source) VALUES ('jrn_other','prj_other','Missing elsewhere','note','executor')"
    )
    await db.commit()
    job = await EmbeddingJobs(db).request(service, ["journal"], project_id="proj_default")
    await EnrichmentWorker(db=db, embeddings=service).run_once()
    assert (await EmbeddingJobs(db).status(job["id"]))["state"] == "complete"
    assert (await get_embedding_index_state(db)).status == "reindexing"
    assert calls == ["Synthetic durable note 0"]
    assert not await db.fetchone("SELECT id FROM vec_journal WHERE project_id='prj_other'")


@pytest.mark.asyncio
async def test_import_receipt_survives_reopen_and_does_not_report_other_job_types(db):
    from rka.infra.database import Database
    from rka.services.jobs import JobQueue
    from rka.services.embedding_backfill import clear_registry

    result = await KnowledgePackService(db).import_pack(pack_bytes())
    unrelated = await JobQueue(db).enqueue("note_embed")
    clear_registry()
    reader = Database(db.db_path)
    await reader.connect()
    try:
        status = await KnowledgePackService(reader).import_status()
        assert status["job_id"] == result.indexing["job_id"]
        assert await KnowledgePackService(reader).import_status(unrelated) is None
    finally:
        await reader.close()


@pytest.mark.asyncio
async def test_import_semantic_failure_is_not_silently_complete(db):
    service, calls = await setup_index(db, count=0)
    result = await KnowledgePackService(db, embeddings=service).import_pack(pack_bytes())
    status = await KnowledgePackService(db).import_status(result.indexing["job_id"])
    await EmbeddingJobs(db).cancel(status["embedding_job_id"])
    status = await KnowledgePackService(db).import_status(result.indexing["job_id"])
    assert status["state"] == "failed" and not status["semantic_ready"]
    assert status["lexical_state"] == "complete"
    assert await db.fetchone("SELECT id FROM journal WHERE project_id='prj_durable_import'")
    assert calls == []


@pytest.mark.asyncio
async def test_strict_fts_failure_is_atomic(db, monkeypatch):
    execute = db.execute

    async def fail(sql, params=None):
        if "INSERT INTO fts_journal" in sql:
            raise RuntimeError("synthetic FTS failure")
        return await execute(sql, params)

    monkeypatch.setattr(db, "execute", fail)
    with pytest.raises(RuntimeError, match="FTS failure"):
        await KnowledgePackService(db).import_pack(pack_bytes())
    assert not await db.fetchone("SELECT id FROM projects WHERE id='prj_durable_import'")
    assert not await db.fetchone("SELECT id FROM jobs WHERE job_type='pack_import'")


@pytest.mark.asyncio
async def test_force_failure_preserves_previous_vectors(db):
    service, _ = await setup_index(db, count=1)
    scheduler = EmbeddingJobs(db)
    await scheduler.request(service)
    worker = EnrichmentWorker(db=db, embeddings=service, max_attempts=1)
    await worker.run_once()
    before = await db.fetchone("SELECT * FROM vec_journal WHERE id='jrn_durable_0'")

    async def failed(*args, **kw):
        raise RuntimeError("synthetic provider failure")

    service.embed_batch = failed
    job = await scheduler.request(service, ["journal"], project_id="proj_default", force=True)
    await worker.run_once()
    assert (await scheduler.status(job["id"]))["state"] != "complete"
    assert await db.fetchone("SELECT * FROM vec_journal WHERE id='jrn_durable_0'") == before


@pytest.mark.asyncio
async def test_late_import_failure_removes_only_its_published_files(db, tmp_path, monkeypatch):
    from pathlib import Path
    from rka.infra.file_access import FileAccessPolicy
    from rka.services.artifacts import ArtifactService
    from rka.services.import_indexing import ImportIndexing

    service, calls = await setup_index(db, count=0)
    source_file = tmp_path / "source.txt"
    source_file.write_bytes(b"synthetic source survives a failed import")
    await ArtifactService(db, file_policy=FileAccessPolicy([tmp_path])).register(
        filepath=str(source_file),
        created_by="system",
    )
    pack_path, _ = await KnowledgePackService(db).export_pack("proj_default")
    importer = KnowledgePackService(db, embeddings=service)
    before = await db.fetchone("SELECT * FROM embedding_index_state")
    record = ImportIndexing.record

    async def late_failure(*args, **kw):
        await record(*args, **kw)
        raise RuntimeError("synthetic failure after receipt")

    monkeypatch.setattr(ImportIndexing, "record", late_failure)
    with Path(pack_path).open("rb") as stream:
        with pytest.raises(RuntimeError, match="after receipt"):
            await importer.import_pack(
                stream, project_id="prj_failed_copy", project_name="Failed Copy"
            )
    assert source_file.read_bytes() == b"synthetic source survives a failed import"
    assert not importer._artifact_import_root("prj_failed_copy").parent.exists()
    assert not await db.fetchone("SELECT id FROM projects WHERE id='prj_failed_copy'")
    assert not await db.fetchone(
        "SELECT id FROM jobs WHERE job_type IN ('pack_import','embedding_backfill','embedding_backfill_v2')"
    )
    assert await db.fetchone("SELECT * FROM embedding_index_state") == before
    assert calls == []
