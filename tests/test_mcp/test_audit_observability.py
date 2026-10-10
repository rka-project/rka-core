"""Read-only adapters must not hide degraded coverage or paged findings."""

import httpx
import pytest
from pydantic import TypeAdapter, ValidationError

from rka.mcp import server
from rka.mcp.operation_args import QueryArgsUnion
from rka.mcp.operations_schema import OPERATIONS_SCHEMA


def _install(monkeypatch, embedding, *, status=200, manifest=None, issues=None):
    requests = []

    def handler(request):
        requests.append(request)
        path = request.url.path
        if path == "/api/capabilities":
            return httpx.Response(status, json={"embedding": embedding})
        data = {
            "/api/status": {"project_name": "Audit"},
            "/api/missions": [], "/api/checkpoints": [],
            "/api/context": {"topic": "audit", "entries": ["preserved context"]},
            "/api/search": [], "/api/maintenance/summary": {"total_items": 0},
            "/api/maintenance": manifest,
            "/api/integrity": {"total_issues": 35, "issues": issues or []},
            "/api/notes": [{"id": "jrn_long", "type": "directive", "confidence": "verified", "source": "pi", "content": "a" * 501 + "CURRENT RULE"}],
        }
        return httpx.Response(200, json=data[path])

    monkeypatch.setattr(server, "_client", lambda project_id=None: httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://testserver",
        headers={"X-RKA-Project": project_id} if project_id else {},
    ))
    return requests


@pytest.mark.parametrize("embedding,warning", [
    ({"available": True, "index_status": "partial", "search_mode": "hybrid"}, "Partial semantic coverage"),
    ({"available": False, "index_status": "failed", "search_mode": "lexical"}, "FTS-only"),
    ({"available": True}, "coverage is unknown"),
    ({"available": True, "index_status": "ready"}, None),
])
async def test_status_search_context_share_coverage_warning(monkeypatch, embedding, warning):
    _install(monkeypatch, embedding)
    outputs = [
        await server.rka_get_status(project_id="prj_audit"),
        await server.rka_search(query="nothing", project_id="prj_audit"),
        await server.rka_get_context(topic="audit", project_id="prj_audit"),
    ]
    for output in outputs:
        if warning:
            assert warning in output
        else:
            assert "⚠" not in output
    assert "No results" in outputs[1]  # Empty search still carries the warning.
    assert "preserved context" in outputs[2]
    assert f"index: {embedding.get('index_status', 'unknown')}" in outputs[0]


async def test_failed_discovery_is_not_claimed_as_fts_or_complete(monkeypatch):
    _install(monkeypatch, {}, status=404)
    for output in [await server.rka_get_status(project_id="prj_audit"), await server.rka_search(query="q", project_id="prj_audit"), await server.rka_get_context(project_id="prj_audit")]:
        assert "status unknown" in output
        assert "FTS-only" not in output


async def test_malformed_embedding_discovery_does_not_break_status(monkeypatch):
    requests = _install(monkeypatch, "unexpected payload")
    output = await server.rka_get_status(project_id="prj_audit")
    assert "Project: Audit" in output
    assert "Embedding status unknown" in output
    discovery = next(r for r in requests if r.url.path == "/api/capabilities")
    assert discovery.extensions["timeout"]["read"] == 2.0


async def test_typed_maintenance_forwards_page_and_returns_all_requested_ids(monkeypatch):
    ids = [f"jrn_{i}" for i in range(50, 70)]
    requests = _install(monkeypatch, {}, manifest={
        "total_items": 84, "estimated_tool_calls": 84, "returned_count": 20,
        "advisory_limits": {"missions_without_upstream_gate": 10},
        "categories": {"gap": {"count": 84, "ids": ids, "description": "Gap", "fix_action": "Review", "has_more": True, "next_offset": 70}},
    })
    args = TypeAdapter(QueryArgsUnion).validate_python({"operation": "pending_maintenance", "project_id": "prj_audit", "limit": 20, "offset": 50})
    output = await server.rka_query(args)
    assert dict(requests[0].url.params) == {"limit": "20", "offset": "50"}
    assert requests[0].headers["X-RKA-Project"] == "prj_audit"
    assert all(row_id in output for row_id in ids)
    assert "offset=70" in output
    assert "84 items" in output
    assert "not unique entities" in output
    assert "not exhaustive" in output


async def test_integrity_global_scope_is_not_silently_project_local(monkeypatch):
    _install(monkeypatch, {}, issues=[{"description": "Stranded entities", "count": 35, "ids": ["jrn_orphan"], "scope": "database", "count_is_exact": False}])
    output = await server.rka_check_integrity(project_id="prj_audit")
    assert "Scope: database" in output
    assert "not attributed to the requested project" in output
    assert "not an exhaustive database total" in output


@pytest.mark.parametrize("page", [{"limit": 20}, {"offset": 50}])
async def test_old_backend_cannot_silently_ignore_pagination(monkeypatch, page):
    _install(monkeypatch, {}, manifest={"total_items": 1, "estimated_tool_calls": 1, "categories": {}})
    output = await server.rka_get_pending_maintenance(project_id="prj_audit", **page)
    assert "unsupported" in output
    assert "was not confirmed" in output


async def test_old_backend_default_manifest_remains_readable(monkeypatch):
    _install(monkeypatch, {}, manifest={"total_items": 1, "estimated_tool_calls": 1, "categories": {}})
    output = await server.rka_get_pending_maintenance(project_id="prj_audit")
    assert "1 items" in output
    assert "counts may be list-capped" in output


async def test_journal_marks_truncation_and_provides_scoped_full_record_lookup(monkeypatch):
    _install(monkeypatch, {})
    output = await server.rka_get_journal(project_id="prj_audit")
    assert "[truncated]" in output
    assert "entity(id='jrn_long', project_id='prj_audit')" in output
    assert "CURRENT RULE" not in output


@pytest.mark.parametrize("operation", ["journal", "changelog", "pending_maintenance"])
@pytest.mark.parametrize("limit", [201])
def test_discovery_and_typed_limits_agree(operation, limit):
    data = {"operation": operation, "project_id": "prj_audit", "limit": limit}
    if operation == "changelog":
        data["filters"] = {"since": "2026-10-01"}
    with pytest.raises(ValidationError):
        TypeAdapter(QueryArgsUnion).validate_python(data)
    assert "200" in OPERATIONS_SCHEMA[operation]["notes"]


@pytest.mark.parametrize("operation", ["journal", "changelog"])
def test_existing_list_operations_do_not_gain_new_lower_bound(operation):
    data = {"operation": operation, "project_id": "prj_audit", "limit": 0}
    if operation == "changelog":
        data["filters"] = {"since": "2026-10-01"}
    assert TypeAdapter(QueryArgsUnion).validate_python(data).limit == 0


@pytest.mark.parametrize("page", [{"limit": 0}, {"offset": -1}])
def test_new_maintenance_page_bounds(page):
    with pytest.raises(ValidationError):
        TypeAdapter(QueryArgsUnion).validate_python({"operation": "pending_maintenance", "project_id": "prj_audit", **page})
