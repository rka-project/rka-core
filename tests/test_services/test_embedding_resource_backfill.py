"""Model-free backfill regression for the reported long-record input scale."""

import json
from dataclasses import replace

import httpx
import pytest

from rka.infra.embedding_backends.openai_compat import OpenAICompatBackend
from rka.infra.embeddings import EmbeddingService
from rka.services.embedding_backfill import (
    BackfillService,
    register_job,
    stored_metadata_hashes_match,
)
from rka.services.embedding_reshape import reshape_all_vec_tables_if_needed
from rka.services.jobs import JobQueue
from rka.services.worker import EnrichmentWorker


async def setup_store(db, count=18):
    await reshape_all_vec_tables_if_needed(db, dim=2)
    for index in range(count):
        text = "x" * 20_500 if index < 8 else f"Synthetic short note {index}"
        await db.execute(
            "INSERT INTO journal (id, content, source, type, project_id) VALUES (?, ?, 'executor', 'note', 'proj_default')",
            [f"jrn_resource_{index:03d}", text],
        )
    await db.commit()


def embeddings_for(db, calls, resource_limits=None):
    def handler(request):
        texts = json.loads(request.content)["input"]
        calls.append(texts)
        return httpx.Response(
            200, json={"data": [{"index": i, "embedding": [0.5, 0.5]} for i in range(len(texts))]}
        )

    return EmbeddingService(
        db=db,
        backend=OpenAICompatBackend(
            base_url="http://isolated.test",
            model="synthetic",
            dim=2,
            resource_limits=resource_limits,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        ),
    )


@pytest.mark.asyncio
async def test_eight_long_records_do_not_block_later_valid_rows(db):
    await setup_store(db)
    calls = []
    service = embeddings_for(db, calls)
    status = await BackfillService(db=db, embeddings=service).run_backfill(
        register_job(),
        entity_types=["journal"],
    )
    assert status.state == "failed"
    assert "embedding_input_limit" in status.error
    assert status.total == 18 and status.processed == 10
    assert sum(len(batch) for batch in calls) == 10
    assert all(len(text.encode()) <= 8192 for batch in calls for text in batch)
    assert (await db.fetchone("SELECT count(*) AS n FROM embedding_metadata"))["n"] == 10
    assert (await db.fetchone("SELECT count(*) AS n FROM vec_journal"))["n"] == 10
    row = await db.fetchone("SELECT content FROM journal WHERE id='jrn_resource_000'")
    assert row["content"] == "x" * 20_500
    assert not await db.fetchone(
        "SELECT * FROM embedding_metadata WHERE entity_id='jrn_resource_000'"
    )
    calls.clear()
    repeat = await BackfillService(db=db, embeddings=service).run_backfill(
        register_job(),
        entity_types=["journal"],
    )
    assert repeat.state == "failed" and repeat.processed == 0 and repeat.total == 8
    assert calls == [], "existing healthy vectors must not be regenerated"


@pytest.mark.asyncio
async def test_legacy_hash_inspection_is_keyset_paged(db):
    await setup_store(db)
    for index in range(18):
        text = "x" * 20_500 if index < 8 else f"Synthetic short note {index}"
        await db.execute(
            "INSERT INTO embedding_metadata (project_id, entity_type, entity_id, content_hash, model_name, dimensions) VALUES ('proj_default', 'journal', ?, ?, 'synthetic', 2)",
            [f"jrn_resource_{index:03d}", EmbeddingService.content_hash(text)],
        )
    await db.commit()
    pages = []

    class PagedDB:
        async def fetchall(self, sql, params):
            assert "LIMIT" in sql and "ORDER BY" in sql
            rows = await db.fetchall(sql, params)
            pages.append(len(rows))
            assert len(rows) <= 8
            return rows

    assert await stored_metadata_hashes_match(PagedDB(), model_name="synthetic", dimensions=2)
    assert sum(pages) == 18 and max(pages) == 8


@pytest.mark.parametrize("batch_size", [0, -1, True, 1.5, 129])
def test_backfill_rejects_unbounded_or_invalid_fetch_size(batch_size):
    with pytest.raises(ValueError, match="batch_size"):
        BackfillService(db=None, embeddings=None, batch_size=batch_size)


@pytest.mark.asyncio
async def test_worker_records_oversize_failure_and_processes_next_job(db):
    await setup_store(db, count=9)
    calls = []
    queue = JobQueue(db)
    for priority, index in enumerate([0, 8]):
        await queue.enqueue(
            "note_embed",
            entity_type="journal",
            entity_id=f"jrn_resource_{index:03d}",
            priority=priority,
            max_attempts=1,
        )
    worker = EnrichmentWorker(db=db, embeddings=embeddings_for(db, calls))
    assert await worker.run_once()
    assert await worker.run_once()
    assert not await worker.run_once()
    jobs = await db.fetchall("SELECT * FROM jobs ORDER BY priority")
    assert jobs[0]["status"] == "failed" and jobs[0]["attempts"] == 1
    assert "embedding_input_limit" in jobs[0]["last_error"]
    assert jobs[1]["status"] == "completed"
    assert calls == [["Synthetic short note 8"]]
    assert (await db.fetchone("SELECT count(*) AS n FROM embedding_metadata"))["n"] == 1
    assert (await db.fetchone("SELECT count(*) AS n FROM vec_journal"))["n"] == 1
    assert (await db.fetchone("SELECT content FROM journal WHERE id='jrn_resource_000'"))[
        "content"
    ] == "x" * 20_500


@pytest.mark.asyncio
async def test_tightened_limits_still_allow_backfill_to_progress(db):
    await setup_store(db, count=10)
    calls = []
    service = embeddings_for(db, calls, {"max_call_inputs": 1, "max_batch_inputs": 1})
    status = await BackfillService(db=db, embeddings=service).run_backfill(
        register_job(),
        entity_types=["journal"],
    )
    assert status.state == "failed" and status.processed == 2 and status.total == 10
    assert calls == [["Synthetic short note 8"], ["Synthetic short note 9"]]


@pytest.mark.asyncio
async def test_compose_failure_is_reported_but_does_not_block_later_rows(db, monkeypatch):
    from rka.services.embedding_backfill import _ENTITY_BACKFILL_CONFIGS

    await setup_store(db, count=10)
    cfg = _ENTITY_BACKFILL_CONFIGS["journal"]

    def compose(row):
        if row["id"] == "jrn_resource_008":
            raise ValueError("sensitive source text should not enter the error summary")
        return cfg.compose_text(row)

    monkeypatch.setitem(_ENTITY_BACKFILL_CONFIGS, "journal", replace(cfg, compose_text=compose))
    status = await BackfillService(db=db, embeddings=embeddings_for(db, [])).run_backfill(
        register_job(),
        entity_types=["journal"],
    )
    assert status.state == "failed" and status.processed == 1
    assert "9 row(s) failed" in status.error
    assert "sensitive source" not in status.error
    assert not await db.fetchone(
        "SELECT * FROM embedding_metadata WHERE entity_id='jrn_resource_008'"
    )


def test_error_sampling_is_bounded_and_does_not_include_arbitrary_exception_text():
    from rka.services.embedding_backfill import _RowErrors

    errors = _RowErrors()
    for index in range(1000):
        errors.record(f"jrn_{index}", ValueError("private source"))
    assert errors.count == 1000 and len(errors.samples) == 3
    assert "private source" not in errors.summary()


@pytest.mark.asyncio
async def test_http_16k_backfill_preserves_sources_and_existing_vectors(db):
    await reshape_all_vec_tables_if_needed(db, dim=2)
    texts = ["existing short note", "x" * 10527, "界" * 5461 + "x", "x" * 16385]
    for index, text in enumerate(texts):
        await db.execute(
            "INSERT INTO journal (id, content, source, type, project_id) "
            "VALUES (?, ?, 'executor', 'note', 'proj_default')",
            [f"jrn_16k_{index}", text],
        )
    await db.commit()
    calls = []
    await BackfillService(db=db, embeddings=embeddings_for(
        db, calls, {"max_input_bytes": 8192},
    )).run_backfill(
        register_job(), entity_types=["journal"],
    )
    assert calls == [[texts[0]]]
    existing = dict(await db.fetchone(
        "SELECT * FROM embedding_metadata WHERE entity_id='jrn_16k_0'"
    ))
    calls.clear()
    service = embeddings_for(db, calls)
    backfill = BackfillService(db=db, embeddings=service)
    assert backfill._batch_size == 1
    result = await backfill.run_backfill(register_job(), entity_types=["journal"])
    assert result.state == "failed" and result.processed == 2
    assert "max_input_bytes=16384" in result.error
    assert calls == [[texts[1]], [texts[2]]]
    assert dict(await db.fetchone(
        "SELECT * FROM embedding_metadata WHERE entity_id='jrn_16k_0'"
    )) == existing
    for index, text in enumerate(texts):
        row = await db.fetchone("SELECT content FROM journal WHERE id=?", [f"jrn_16k_{index}"])
        assert row["content"] == text
        metadata = await db.fetchone(
            "SELECT content_hash FROM embedding_metadata WHERE entity_id=?", [f"jrn_16k_{index}"]
        )
        if index == 3:
            assert metadata is None
        else:
            assert metadata["content_hash"] == EmbeddingService.content_hash(text)
    assert (await db.fetchone("SELECT count(*) AS n FROM vec_journal"))["n"] == 3
    calls.clear()
    repeat = await backfill.run_backfill(register_job(), entity_types=["journal"])
    assert repeat.processed == 0 and calls == []
