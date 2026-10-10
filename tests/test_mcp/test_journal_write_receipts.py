"""Public REST/MCP creation and recovery are the same project-scoped contract."""

import json
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from pydantic import ValidationError

from rka.api.app import create_app
from rka.config import RKAConfig
from rka.mcp.operation_args import QueryNoteWriteReceiptArgs, RecordNoteArgs
from rka.mcp.verb_dispatch import dispatch_execute, dispatch_execute_typed, dispatch_query_typed


@pytest_asyncio.fixture
async def receipt_api(tmp_path, monkeypatch):
    from rka.mcp import server

    app = create_app(RKAConfig(project_dir=tmp_path, db_path=Path("receipts.db"),
                               data_dir=tmp_path / "data", llm_enabled=False, embeddings_enabled=False))
    async with app.router.lifespan_context(app):
        def client(project_id=None):
            return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver",
                                     headers={"X-RKA-Project": project_id} if project_id else {})

        monkeypatch.setattr(server, "_client", client)
        async with client("proj_default") as rest:
            yield rest


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["rest", "typed", "raw", "legacy", "record"])
async def test_all_single_note_surfaces_preserve_key_and_expose_receipt(receipt_api, surface):
    from rka.mcp import server

    data = {"content": "Exact 原文\r\n", "type": "directive", "source": "pi",
            "capture_mode": "raw_capture", "request_id": "lost-response:1"}

    async def create():
        if surface == "rest":
            response = await receipt_api.post("/api/notes", json=data)
            assert response.status_code == 201, response.text
            return response.json()
        if surface == "typed":
            return await dispatch_execute_typed(RecordNoteArgs(project_id="proj_default", **data))
        if surface == "raw":
            return await dispatch_execute("record_note", project_id="proj_default", **data)
        adapter = server.rka_add_note if surface == "legacy" else server.rka_record_note
        return await adapter(project_id="proj_default", **data)

    first = await create()
    assert await create() == first
    receipt_response = await receipt_api.get("/api/notes/write-receipts/lost-response:1")
    assert receipt_response.status_code == 200, receipt_response.text
    receipt = receipt_response.json()
    assert receipt["entry"]["verbatim_input"] == data["content"]
    assert receipt["journal_id"] == receipt["entry"]["id"]
    assert receipt["project_id"] == "proj_default"
    assert json.loads(await dispatch_query_typed(QueryNoteWriteReceiptArgs(
        project_id="proj_default", request_id=data["request_id"],
    ))) == receipt
    assert json.loads(await server.rka_get_note_write_receipt(
        data["request_id"], project_id="proj_default",
    )) == receipt
    assert len((await receipt_api.get("/api/notes")).json()) == 1
    if surface != "rest":
        assert "Acknowledged" in first and "not current note state" in first


@pytest.mark.asyncio
async def test_rest_conflict_validation_scope_and_legacy_behavior(receipt_api):
    client = receipt_api
    assert (await client.get("/api/notes/write-receipts/missing")).status_code == 404
    data = {"content": "Exact text", "request_id": "one"}
    first = await client.post("/api/notes", json=data)
    assert first.status_code == 201
    conflict = await client.post("/api/notes", json={**data, "content": "different"})
    assert conflict.status_code == 409
    invalid = await client.post("/api/notes", json={**data, "request_id": "bad\n"})
    assert invalid.status_code == 422
    project = await client.post("/api/projects", json={"id": "prj_other", "name": "Other"})
    assert project.is_success, project.text
    wrong = await client.get("/api/notes/write-receipts/one", headers={"X-RKA-Project": "prj_other"})
    assert wrong.status_code == 404 and "Exact text" not in wrong.text
    assert (await client.get("/api/notes/write-receipts/one", headers={"X-RKA-Project": "prj_missing"})).status_code == 404
    a = await client.post("/api/notes", json={"content": "Unkeyed"})
    b = await client.post("/api/notes", json={"content": "Unkeyed"})
    assert a.status_code == b.status_code == 201 and a.json()["id"] != b.json()["id"]


@pytest.mark.asyncio
async def test_transport_timeout_after_commit_recovers_without_a_second_note(receipt_api, monkeypatch):
    from rka.mcp import server

    original_client = server._client

    @asynccontextmanager
    async def lossy_client(project_id=None):
        async with original_client(project_id) as client:
            original_post = client.post

            async def post(*args, **kwargs):
                response = await original_post(*args, **kwargs)
                assert response.status_code == 201
                raise httpx.ReadTimeout("Response lost after server commit")

            client.post = post
            yield client

    args = RecordNoteArgs(project_id="proj_default", content="Network recovery", request_id="network-1")
    monkeypatch.setattr(server, "_client", lossy_client)
    with pytest.raises(httpx.ReadTimeout):
        await dispatch_execute_typed(args)
    monkeypatch.setattr(server, "_client", original_client)
    receipt = json.loads(await dispatch_query_typed(QueryNoteWriteReceiptArgs(
        project_id="proj_default", request_id="network-1",
    )))
    assert receipt["journal_id"] in await dispatch_execute_typed(args)
    assert len((await receipt_api.get("/api/notes")).json()) == 1


@pytest.mark.asyncio
async def test_old_backend_key_rejection_never_retries_without_key(monkeypatch):
    from rka.mcp import server

    sent = []

    async def old_backend(request):
        sent.append(json.loads(request.content))
        return httpx.Response(422, json={"detail": "request_id: extra field not permitted"})

    monkeypatch.setattr(server, "_client", lambda project_id: httpx.AsyncClient(
        transport=httpx.MockTransport(old_backend), base_url="http://oldserver",
    ))
    with pytest.raises(Exception, match="API error 422"):
        await dispatch_execute_typed(RecordNoteArgs(
            project_id="proj_default", content="Do not duplicate", request_id="safe-key",
        ))
    assert len(sent) == 1 and sent[0]["request_id"] == "safe-key"


@pytest.mark.asyncio
async def test_discovery_constraints_and_multi_note_rejection():
    from rka.mcp.operations_schema import OPERATIONS_SCHEMA

    assert "request_id" in OPERATIONS_SCHEMA["record_note"]["optional_fields"]
    assert "note_write_receipt" in OPERATIONS_SCHEMA
    for model, kwargs in ((RecordNoteArgs, {"content": "body"}), (QueryNoteWriteReceiptArgs, {})):
        with pytest.raises(ValidationError):
            model(project_id="proj_default", request_id="../bad", **kwargs)
    result = await dispatch_execute("ingest_document", project_id="proj_default",
                                    content="Many notes", request_id="not-supported")
    assert json.loads(result)["error"] == "invalid_field"
