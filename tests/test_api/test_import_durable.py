"""Pack HTTP returns a durable receipt without running model inference."""

import pytest

from tests.test_api.test_durable_embedding_jobs import client_store as client_store
from tests.test_services.test_backfill_entrypoints import pack_bytes
from rka.services.embedding_jobs import EmbeddingJobs
from rka.services.worker import EnrichmentWorker


async def import_request(client):
    return await client.post(
        "/api/projects/import",
        files={"file": ("synthetic.rka-pack.zip", pack_bytes().getvalue(), "application/zip")},
    )


@pytest.mark.asyncio
async def test_receipt_separates_lexical_and_semantic_completion(client_store):
    client, db, service, calls = client_store
    response = await import_request(client)
    assert response.status_code == 202, response.text
    assert calls == []
    url = response.json()["indexing"]["status_url"]
    status = (await client.get(url)).json()
    assert status["state"] == "pending" and status["lexical_state"] == "complete"
    assert not status["semantic_ready"]
    assert (
        await client.get(
            "/api/projects/import/status", params={"job_id": status["embedding_job_id"]}
        )
    ).status_code == 404
    await EnrichmentWorker(db=db, embeddings=service).run_once()
    status = (await client.get(url)).json()
    assert status["state"] == "complete" and status["semantic_state"] == "complete"
    # The fixture has another project with an unembedded row. A scoped import
    # is complete, but cannot certify that whole-generation search is ready.
    assert not status["semantic_ready"] and status["index_state"] == "reindexing"
    assert calls == ["synthetic imported record"]


@pytest.mark.asyncio
async def test_busy_import_answers_409_and_leaves_no_partial_project(client_store):
    client, db, service, calls = client_store
    await EmbeddingJobs(db).request(service)
    response = await import_request(client)
    assert response.status_code == 409
    assert not await db.fetchone("SELECT id FROM projects WHERE id='prj_durable_import'")
    assert not await db.fetchone("SELECT id FROM jobs WHERE job_type='pack_import'")
    assert calls == []


@pytest.mark.asyncio
async def test_cancelled_import_retains_lexical_records_and_reports_failure(client_store):
    client, db, _, calls = client_store
    response = await import_request(client)
    url = response.json()["indexing"]["status_url"]
    embedding_id = (await client.get(url)).json()["embedding_job_id"]
    cancelled = await client.post(f"/api/config/embedding/backfill/{embedding_id}/cancel")
    assert cancelled.status_code == 200
    status = (await client.get(url)).json()
    assert status["state"] == "failed" and status["lexical_state"] == "complete"
    assert status["error"] == "embedding_backfill_cancelled"
    assert not status["semantic_ready"] and calls == []
    assert await db.fetchone("SELECT id FROM journal WHERE project_id='prj_durable_import'")
