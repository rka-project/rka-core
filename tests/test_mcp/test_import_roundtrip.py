"""Import adapters exercised through ASGI and real SQLite, never live services."""

import pytest
import pytest_asyncio
import httpx

from pydantic import TypeAdapter

from rka.api.app import create_app
from rka.config import RKAConfig
from rka.mcp.operation_args import ExecuteArgsUnion


@pytest_asyncio.fixture
async def import_api(db, tmp_path, monkeypatch):
    from rka.mcp import server

    config = RKAConfig(data_dir=tmp_path / "data", embeddings_enabled=False, llm_enabled=False)
    app = create_app(config)
    app.state.db = db
    app.state.config = config
    app.state.llm = None
    app.state.embeddings = None

    def client(project_id=None):
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://isolated.test",
            headers={"X-RKA-Project": project_id or "proj_default"},
        )

    monkeypatch.setattr(server, "_client", client)
    return client


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["rest", "upload", "typed", "legacy"])
async def test_bibtex_adapters_preserve_status_and_origin(import_api, db, surface):
    from rka.mcp import server, verb_dispatch

    bibtex = "@article{test, title={A {Nested} Import}, doi={10.0000/adapter}, year=2026}"
    if surface in {"rest", "upload"}:
        async with import_api() as api:
            if surface == "rest":
                response = await api.post(
                    "/api/import/bibtex", json={"bibtex": bibtex, "default_status": "cited"}
                )
            else:
                response = await api.post(
                    "/api/import/bibtex-file",
                    params={"default_status": "cited"},
                    files={"file": ("synthetic.bib", bibtex.encode(), "text/plain")},
                )
        assert response.status_code == 200, response.text
        assert not response.json()["errors"], response.text
    elif surface == "typed":
        args = TypeAdapter(ExecuteArgsUnion).validate_python(
            {
                "operation": "import_bibtex",
                "project_id": "proj_default",
                "bibtex": bibtex,
                "default_status": "cited",
            }
        )
        assert "Imported: 1" in await verb_dispatch.dispatch_execute_typed(args)
    else:
        assert "Imported: 1" in await server.rka_import_bibtex(
            bibtex, default_status="cited", project_id="proj_default"
        )
    row = await db.fetchone("SELECT * FROM literature")
    assert row["title"] == "A Nested Import"
    assert row["status"] == "cited"
    assert row["added_by"] == "import"
    assert (await db.fetchone("SELECT actor FROM events WHERE entity_id = ?", [row["id"]]))[
        "actor"
    ] == "system"


@pytest.mark.asyncio
async def test_mixed_batch_default_actor_and_per_entry_errors(import_api, db):
    async with import_api() as api:
        response = await api.post(
            "/api/import/batch",
            json={
                "entries": [
                    {
                        "entity_type": "literature",
                        "data": {"title": "Imported reference", "added_by": "import"},
                    },
                    {
                        "entity_type": "note",
                        "data": {"content": "Imported original note", "source": "executor"},
                    },
                    {
                        "entity_type": "decision",
                        "data": {
                            "question": "Synthetic choice?",
                            "chosen": "A",
                            "rationale": "Test",
                            "decided_by": "executor",
                            "kind": "decision",
                            "phase": "test",
                        },
                    },
                    {
                        "entity_type": "literature",
                        "data": {"title": "Rejected", "status": "not-a-status"},
                    },
                ]
            },
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["imported"]) == 3, body
    assert [item["index"] for item in body["errors"]] == [3]
    assert (await db.fetchone("SELECT count(*) AS n FROM literature"))["n"] == 1
    assert (await db.fetchone("SELECT added_by FROM literature"))["added_by"] == "import"
    assert (await db.fetchone("SELECT source FROM journal"))["source"] == "executor"
    assert {row["actor"] for row in await db.fetchall("SELECT actor FROM events")} == {"system"}
