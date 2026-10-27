"""Issue #177: failures stay visible without touching any live RKA service."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from mcp import types

from rka.api.app import create_app
from rka.config import RKAConfig
from rka.models.mission import MissionCreate
from rka.services.missions import MissionService


@pytest_asyncio.fixture
async def test_app(tmp_path: Path):
    app = create_app(
        RKAConfig(
            project_dir=tmp_path,
            db_path=Path("hidden-failures.db"),
            llm_enabled=False,
            embeddings_enabled=False,
        )
    )
    async with app.router.lifespan_context(app):
        yield app


@pytest_asyncio.fixture
async def rest(test_app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=test_app, raise_app_exceptions=False),
        base_url="http://testserver",
        headers={"X-RKA-Project": "proj_default"},
    ) as client:
        yield client


async def seed(client, headers=None):
    note = await client.post("/api/notes", json={"content": "synthetic source"}, headers=headers)
    assert note.status_code == 201, note.text
    claim = await client.post(
        "/api/claims",
        json={
            "source_entry_id": note.json()["id"],
            "content": "synthetic claim",
            "claim_type": "evidence",
        },
        headers=headers,
    )
    cluster = await client.post(
        "/api/clusters", json={"label": "synthetic cluster"}, headers=headers
    )
    assert claim.status_code == cluster.status_code == 201
    return claim.json()["id"], cluster.json()["id"]


@pytest.mark.parametrize("field", ["source_claim_id", "target_claim_id", "cluster_id"])
@pytest.mark.parametrize("foreign", [False, True])
async def test_invalid_edge_scope_is_4xx(rest, field, foreign):
    claim, cluster = await seed(rest)
    bad = "clm_missing" if field != "cluster_id" else "ecl_missing"
    if foreign:
        project = await rest.post("/api/projects", json={"name": "Other"})
        assert project.status_code == 200, project.text
        other_claim, other_cluster = await seed(rest, {"X-RKA-Project": project.json()["id"]})
        bad = other_cluster if field == "cluster_id" else other_claim
    payload = {"source_claim_id": claim, "cluster_id": cluster, "relation": "member_of"}
    if field == "target_claim_id":
        payload = {"source_claim_id": claim, "target_claim_id": bad, "relation": "supports"}
    payload[field] = bad
    result = await rest.post("/api/claims/edges", json=payload)
    assert result.status_code == 422, result.text
    assert (await rest.get(f"/api/clusters/{cluster}")).json()["claim_count"] == 0


async def test_edge_success_and_retry_remain_idempotent(rest):
    claim, cluster = await seed(rest)
    payload = {"source_claim_id": claim, "cluster_id": cluster, "relation": "member_of"}
    first = await rest.post("/api/claims/edges", json=payload)
    retry = await rest.post("/api/claims/edges", json=payload)
    assert first.status_code == retry.status_code == 201
    assert first.json()["id"] == retry.json()["id"]
    assert (await rest.get(f"/api/clusters/{cluster}")).json()["claim_count"] == 1


def mock_backend(monkeypatch, *, assignment_status=500, update_status=200, timeout=False):
    import rka.mcp.server as server

    requests = []

    def handle(request):
        requests.append((request.method, request.url.path))
        if request.method == "POST" and request.url.path == "/api/clusters":
            return httpx.Response(201, json={"id": "ecl_created"})
        if request.method == "POST" and request.url.path == "/api/claims/edges":
            claim_id = json.loads(request.content)["source_claim_id"]
            if claim_id == "clm_ok":
                return httpx.Response(201, json={"id": "edge_ok"})
            if timeout:
                raise httpx.ReadTimeout("synthetic timeout", request=request)
            return httpx.Response(assignment_status, json={"detail": "synthetic failure"})
        if request.method == "PUT":
            return httpx.Response(update_status, json={"detail": "synthetic update response"})
        raise AssertionError(f"Unexpected request: {request.method} {request.url.path}")

    def client(project_id=None):
        return httpx.AsyncClient(
            transport=httpx.MockTransport(handle),
            base_url="http://synthetic",
        )

    monkeypatch.setattr(server, "_client", client)
    monkeypatch.setattr(server, "_record_entity", lambda *args, **kwargs: None)
    return server, requests


async def rpc(server, operation, *, surface="typed", **kwargs):
    tool_name = "rka_execute" if surface == "typed" else "rka_" + operation
    was_registered = server._TOOL_REGISTRY[tool_name]["registered"]
    if surface == "legacy":
        # Other MCP suites may collect in default mode. Exercise the supported
        # explicit legacy load instead of depending on collection order.
        await server.rka_load_tools(names=[tool_name], ctx=server.mcp.get_context())
    args = {"project_id": "proj_default", **kwargs}
    request = types.CallToolRequest(
        params=types.CallToolRequestParams(
            name=tool_name,
            arguments={"args": {"operation": operation, **args}} if surface == "typed" else args,
        )
    )
    try:
        response = await server.mcp._mcp_server.request_handlers[types.CallToolRequest](request)
        return response.root
    finally:
        if not was_registered:
            server.mcp._tool_manager.remove_tool(tool_name)
            server._TOOL_REGISTRY[tool_name]["registered"] = False
            getattr(server.mcp._mcp_server, "_tool_cache", {}).pop(tool_name, None)


def error_payload(result):
    assert result.isError, result
    text = "\n".join(block.text for block in result.content if isinstance(block, types.TextContent))
    # FastMCP may prefix a ToolError with the tool name.
    return json.loads(text[text.index("{") :])


@pytest.mark.parametrize("status", [422, 500, 503])
@pytest.mark.parametrize("surface", ["typed", "legacy"])
async def test_assignment_failure_is_mcp_error_with_per_id_outcomes(monkeypatch, status, surface):
    server, _ = mock_backend(monkeypatch, assignment_status=status)
    result = await rpc(
        server,
        "assign_claims_to_cluster",
        surface=surface,
        cluster_id="ecl_target",
        claim_ids=["clm_ok", "clm_bad"],
    )
    payload = error_payload(result)
    assert payload["cluster_id"] == "ecl_target"
    assert payload["assigned_claim_ids"] == ["clm_ok"]
    assert payload["errors"][0]["claim_id"] == "clm_bad"
    assert payload["errors"][0]["status_code"] == status
    assert len(payload["outcomes"]) == 2


async def test_create_cluster_update_failure_preserves_created_identity(monkeypatch):
    server, requests = mock_backend(monkeypatch, update_status=500)
    result = await rpc(server, "create_cluster", label="synthetic", claim_ids=["clm_ok"])
    payload = error_payload(result)
    assert payload["cluster_id"] == "ecl_created"
    assert payload["cluster_created"] is True
    assert payload["assigned_claim_ids"] == ["clm_ok"]
    assert payload["errors"][0]["stage"] == "cluster_update"
    assert ("PUT", "/api/clusters/ecl_created") in requests
    assert "recreate" in payload["retry_guidance"].lower()


async def test_create_cluster_membership_failure_does_not_mark_synthesized(monkeypatch):
    server, requests = mock_backend(monkeypatch)
    result = await rpc(server, "create_cluster", label="synthetic", claim_ids=["clm_ok", "clm_bad"])
    payload = error_payload(result)
    assert payload["cluster_created"] is True
    assert payload["assigned_claim_ids"] == ["clm_ok"]
    assert payload["errors"][0]["claim_id"] == "clm_bad"
    assert not any(method == "PUT" for method, _ in requests)


async def test_assignment_timeout_is_unknown_not_a_safe_recreate(monkeypatch):
    server, _ = mock_backend(monkeypatch, timeout=True)
    result = await rpc(server, "create_cluster", label="synthetic", claim_ids=["clm_ok", "clm_bad"])
    payload = error_payload(result)
    assert payload["cluster_id"] == "ecl_created"
    assert payload["assigned_claim_ids"] == ["clm_ok"]
    assert payload["errors"][0]["outcome"] == "unknown"
    assert "reconcile" in payload["retry_guidance"].lower()


async def test_successful_cluster_calls_keep_success_text(monkeypatch):
    server, _ = mock_backend(monkeypatch)
    created = await rpc(server, "create_cluster", label="synthetic", claim_ids=["clm_ok"])
    assigned = await rpc(
        server, "assign_claims_to_cluster", cluster_id="ecl_target", claim_ids=["clm_ok"]
    )
    assert not created.isError and not assigned.isError
    assert "Created cluster ecl_created" in created.content[0].text
    assert "Assigned 1/1 claims" in assigned.content[0].text


async def test_timeout_stops_unattempted_memberships(monkeypatch):
    server, requests = mock_backend(monkeypatch, timeout=True)
    result = await rpc(
        server,
        "assign_claims_to_cluster",
        cluster_id="ecl_target",
        claim_ids=["clm_ok", "clm_bad", "clm_later"],
    )
    payload = error_payload(result)
    assert [entry["outcome"] for entry in payload["outcomes"]] == [
        "assigned",
        "unknown",
        "not_attempted",
    ]
    assert len(requests) == 2


async def test_corrupt_report_warning_survives_rest_read(test_app, rest, caplog):
    mission = await rest.post("/api/missions", json={"phase": "test", "objective": "synthetic"})
    assert mission.status_code == 201, mission.text
    mission_id = mission.json()["id"]
    secret = "invalid-private-report-body"
    await test_app.state.db.execute(
        "UPDATE missions SET report = ? WHERE id = ?", [secret, mission_id]
    )
    await test_app.state.db.commit()
    with caplog.at_level(logging.WARNING, logger="rka.services.missions"):
        response = await rest.get(f"/api/missions/{mission_id}")
    assert response.status_code == 200
    assert response.json()["report"] is None
    assert any("report_parse_error" in entry for entry in response.json()["consistency_warnings"])
    assert secret not in response.text
    assert all(secret not in record.message for record in caplog.records)


@pytest.mark.parametrize("stored", ["{not valid json", "[]", "null", ""])
async def test_corrupt_report_is_visible_logged_and_not_rewritten(db, caplog, stored):
    service = MissionService(db)
    mission = await service.create(MissionCreate(phase="test", objective="synthetic"))
    await db.execute("UPDATE missions SET report = ? WHERE id = ?", [stored, mission.id])
    await db.commit()
    with caplog.at_level(logging.WARNING, logger="rka.services.missions"):
        read = await service.get(mission.id)
    assert read.report is None
    assert any("report_parse_error" in warning for warning in read.consistency_warnings)
    assert any(mission.id in record.message for record in caplog.records)
    assert not any(stored and stored in record.message for record in caplog.records)
    assert (await db.fetchone("SELECT report FROM missions WHERE id = ?", [mission.id]))[
        "report"
    ] == stored


async def test_missing_or_valid_report_has_no_parse_warning(db, caplog):
    service = MissionService(db)
    mission = await service.create(MissionCreate(phase="test", objective="synthetic"))
    assert not (await service.get(mission.id)).consistency_warnings
    await db.execute(
        "UPDATE missions SET report = ? WHERE id = ?",
        [json.dumps({"mission_id": mission.id, "summary": "valid synthetic report"}), mission.id],
    )
    await db.commit()
    read = await service.get(mission.id)
    assert read.report.summary == "valid synthetic report"
    assert not read.consistency_warnings
    assert not caplog.records
