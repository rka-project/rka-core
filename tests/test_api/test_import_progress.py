"""Importing a pack returns as soon as the rows are durable, not when indexed.

The two phases of an import differ by orders of magnitude. Rows insert in
seconds; indexing embeds every entity one at a time and ran for over thirty
minutes on a 9,365-row pack. Done inline, the HTTP request times out long
before the work finishes — the caller sees a failure for an import that
succeeded and is still going, and the project is meanwhile partly searchable
with nothing to say more is coming.

That gap is not hypothetical: measuring the half-built index during one of
those windows produced a confident and wrong conclusion that import does not
build indexes at all.

So: 202 once the rows land, plus a job to poll.
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

from rka.api.app import create_app
from rka.config import RKAConfig
from rka.services.embedding_backfill import clear_registry
from rka.services.knowledge_pack import PACK_SCHEMA_VERSION

HEADERS = {"X-RKA-Project": "proj_default"}


def _pack(project_id: str = "prj_01IMPORTPROGRESS0000000000") -> bytes:
    buffer = io.BytesIO()
    manifest = {
        "pack_format_version": PACK_SCHEMA_VERSION,
        "schema_version": 21,
        "project": {"id": project_id, "name": "import progress", "created_by": "system"},
        "project_state": None,
        "tables": {
            "journal": [
                {
                    "id": f"jrn_01IMPORTPROGRESS{index:09d}",
                    "project_id": project_id,
                    "type": "note",
                    "content": f"imported entry {index}",
                    "source": "executor",
                }
                for index in range(3)
            ]
        },
        "table_counts": {"journal": 3},
    }
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
    return buffer.getvalue()


@pytest_asyncio.fixture
async def client(tmp_path: Path):
    clear_registry()
    config = RKAConfig(
        project_dir=tmp_path,
        db_path=Path("import_progress.db"),
        llm_enabled=False,
        embeddings_enabled=False,
    )
    app = create_app(config)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
            yield c


async def _import(client: httpx.AsyncClient) -> httpx.Response:
    return await client.post(
        "/api/projects/import",
        files={"file": ("p.rka-pack.zip", _pack(), "application/zip")},
        data={"project_name": "import progress"},
        headers=HEADERS,
    )


class TestTheImportReturnsEarly:
    @pytest.mark.asyncio
    async def test_it_answers_202_not_200(self, client):
        response = await _import(client)
        assert response.status_code == 202, response.text

    @pytest.mark.asyncio
    async def test_it_names_a_job_to_poll(self, client):
        body = (await _import(client)).json()
        assert body["indexing"]["job_id"].startswith("job_")
        assert body["indexing"]["job_id"] in body["indexing"]["status_url"]

    @pytest.mark.asyncio
    async def test_it_says_search_is_not_ready_yet(self, client):
        """Silence here is what made a half-built index look like a missing one."""
        note = (await _import(client)).json()["indexing"]["note"].lower()
        assert "search" in note

    @pytest.mark.asyncio
    async def test_the_rows_are_already_durable(self, client):
        """202 must not mean "queued" — the import itself is done."""
        body = (await _import(client)).json()
        listed = await client.get(
            "/api/notes", headers={"X-RKA-Project": body["project_id"]}
        )
        assert listed.status_code == 200
        assert len(listed.json()) == 3

    @pytest.mark.asyncio
    async def test_the_result_still_carries_the_import_summary(self, client):
        body = (await _import(client)).json()
        assert body["project_name"] == "import progress"
        assert body["imported_counts"]["journal"] == 3


class TestTheStatusEndpoint:
    @pytest.mark.asyncio
    async def test_the_job_is_reportable(self, client):
        job_id = (await _import(client)).json()["indexing"]["job_id"]
        status = await client.get("/api/projects/import/status", params={"job_id": job_id})
        assert status.status_code == 200
        assert status.json()["job_id"] == job_id

    @pytest.mark.asyncio
    async def test_it_reports_a_total_to_measure_progress_against(self, client):
        """`processed` alone cannot distinguish slow from stuck."""
        job_id = (await _import(client)).json()["indexing"]["job_id"]
        body = (await client.get("/api/projects/import/status", params={"job_id": job_id})).json()
        assert "total" in body and "processed" in body
        assert body["state"] in {"pending", "running", "complete"}

    @pytest.mark.asyncio
    async def test_an_unknown_job_is_404_not_an_empty_success(self, client):
        response = await client.get(
            "/api/projects/import/status", params={"job_id": "imp_nosuchjob"}
        )
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_no_job_id_returns_the_latest(self, client):
        job_id = (await _import(client)).json()["indexing"]["job_id"]
        body = (await client.get("/api/projects/import/status")).json()
        assert body["job_id"] == job_id


class TestIndexingIsRerunnable:
    @pytest.mark.asyncio
    async def test_index_project_reads_rows_back_from_the_database(self, client, tmp_path):
        """It must not need the manifest, which is gone once import returns."""
        from rka.services.knowledge_pack import KnowledgePackService

        body = (await _import(client)).json()
        project_id = body["project_id"]

        app = client._transport.app  # type: ignore[attr-defined]
        svc = KnowledgePackService(app.state.db)
        assert await svc.count_indexable(project_id) == 3
        assert await svc.index_project(project_id) == 3
