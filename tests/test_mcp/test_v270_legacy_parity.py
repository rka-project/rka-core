"""v2.7.0 PR-1 — legacy ↔ verb byte-parity tests.

For a sample of legacy tools, assert that calling the legacy tool and
calling the equivalent v2.7.0 verb produces the SAME REST traffic:
same HTTP method, same URL path, same JSON body. This is the
load-bearing contract that lets PR-2 demote the legacy tools without
behavioural drift — by PR-2 day the orchestrator will have moved its
WRITE_TOOLS to the verbs, and the parity guarantee here is what makes
that move safe.

Strategy: monkeypatch `_client(...)` in `rka.mcp.server` to return an
in-process httpx.AsyncClient with a MockTransport that records every
request. After a legacy call and the equivalent verb call, compare the
captured (method, url, body) triples.

We sample one tool per dispatcher family:

  - rka_add_note          ≡ rka_record_note
  - rka_add_decision      ≡ rka_record_decision
  - rka_get_status        ≡ rka_query(scope='status')
  - rka_search            ≡ rka_query(scope='search', query=...)
  - rka_create_mission    ≡ rka_mission(action='create', ...)
  - rka_submit_checkpoint ≡ rka_checkpoint(action='submit', ...)
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from rka.mcp import server as mcp_mod


# ---------------------------------------------------------------------------
# Mock-transport fixture — captures every request
# ---------------------------------------------------------------------------


class _Capture:
    """Records every (method, url_path, body) tuple seen by the mock
    httpx transport."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []


def _make_handler(capture: _Capture, response_factory):
    """Build the MockTransport handler.

    response_factory(method, path) -> (status, json_body) lets each
    test inject the response shape the legacy tool needs to parse
    (e.g. /api/notes POST returns {"id": "jrn_x", "type": ..., ...}).
    """

    def handler(request: httpx.Request) -> httpx.Response:
        body = None
        if request.content:
            try:
                body = json.loads(request.content)
            except Exception:
                body = request.content.decode("utf-8", errors="replace")
        capture.requests.append({
            "method": request.method,
            "path": request.url.path,
            "params": dict(request.url.params),
            "body": body,
        })
        status, payload = response_factory(request.method, request.url.path)
        return httpx.Response(status, json=payload)

    return handler


@pytest.fixture
def captured_calls(monkeypatch):
    """Two captures (one per call) so we can compare side-by-side.

    Returns a tuple (legacy_capture, verb_capture, install_handler):
        install_handler(response_factory) -> activates the mock for
            the next two calls (legacy then verb).
    """
    leg = _Capture()
    ver = _Capture()

    state = {"active": leg, "factory": lambda m, p: (200, {})}

    def fake_client(project_id: str | None = None) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.MockTransport(
                _make_handler(state["active"], state["factory"])
            ),
            base_url="http://testserver",
            headers={"X-RKA-Project": project_id} if project_id else {},
        )

    monkeypatch.setattr(mcp_mod, "_client", fake_client)

    async def _noop_fire(_pid):
        pass
    monkeypatch.setattr(mcp_mod, "_maybe_fire_session_start", _noop_fire)

    def set_factory(response_factory) -> None:
        state["factory"] = response_factory

    def switch_to(c: _Capture) -> None:
        state["active"] = c

    return leg, ver, set_factory, switch_to


# ---------------------------------------------------------------------------
# rka_add_note ≡ rka_record_note
# ---------------------------------------------------------------------------


async def test_parity_add_note_record_note(captured_calls):
    """rka_add_note(content='x') and rka_record_note(content='x') hit
    the same POST /api/notes with the same body."""
    leg, ver, set_factory, switch_to = captured_calls
    set_factory(lambda m, p: (200, {
        "id": "jrn_1", "type": "note", "confidence": "hypothesis",
    }))

    switch_to(leg)
    await mcp_mod.rka_add_note(
        content="x", project_id="prj_t",
    )
    switch_to(ver)
    await mcp_mod.rka_record_note(
        content="x", project_id="prj_t",
    )

    assert len(leg.requests) == 1 and len(ver.requests) == 1
    L, V = leg.requests[0], ver.requests[0]
    assert L["method"] == V["method"] == "POST"
    assert L["path"] == V["path"] == "/api/notes"
    # Body equivalence: legacy strips None, verb also strips None,
    # required fields and provided fields must match. We compare the
    # canonical JSON form.
    assert L["body"] == V["body"], (
        f"body parity drift:\n  legacy: {L['body']!r}\n  verb:   {V['body']!r}"
    )


async def test_parity_add_note_with_provenance(captured_calls):
    """Legacy related_decisions/related_mission as kwargs ≡ verb
    provenance={...} unpack."""
    leg, ver, set_factory, switch_to = captured_calls
    set_factory(lambda m, p: (200, {
        "id": "jrn_1", "type": "note", "confidence": "hypothesis",
    }))

    switch_to(leg)
    await mcp_mod.rka_add_note(
        content="x", project_id="prj_t",
        related_decisions=["dec_a"], related_mission="mis_b",
    )
    switch_to(ver)
    await mcp_mod.rka_record_note(
        content="x", project_id="prj_t",
        provenance={
            "related_decisions": ["dec_a"],
            "related_mission": "mis_b",
        },
    )

    L, V = leg.requests[0], ver.requests[0]
    assert L["body"] == V["body"], (
        f"provenance unpack drift:\n  legacy: {L['body']!r}\n  verb:   {V['body']!r}"
    )


# ---------------------------------------------------------------------------
# rka_add_decision ≡ rka_record_decision
# ---------------------------------------------------------------------------


async def test_parity_add_decision_record_decision(captured_calls):
    """Legacy rka_add_decision requires `phase` (positional, no default);
    verb dispatch_record_decision threads phase="" by default. We pass
    phase="design" on both sides for an apples-to-apples comparison."""
    leg, ver, set_factory, switch_to = captured_calls
    set_factory(lambda m, p: (200, {
        "id": "dec_1", "question": "Q?",
    }))

    switch_to(leg)
    await mcp_mod.rka_add_decision(
        question="Q?",
        phase="design",
        decided_by="pi",
        chosen="A", rationale="because",
        related_journal=["jrn_1"],
        kind="decision",
        project_id="prj_t",
    )
    switch_to(ver)
    await mcp_mod.rka_record_decision(
        question="Q?", chosen="A", rationale="because",
        phase="design",
        related_journal=["jrn_1"],
        decided_by="pi", kind="decision",
        project_id="prj_t",
    )

    L, V = leg.requests[0], ver.requests[0]
    assert L["method"] == V["method"] == "POST"
    assert L["path"] == V["path"] == "/api/decisions"
    # v2.7.0.6 — `confidence` is no longer forwarded by either side
    # (DecisionCreate(extra='forbid') has no confidence field; it lives
    # on journal entries). Both bodies must omit it; the parity test now
    # asserts this explicitly.
    for k in ("question", "chosen", "rationale", "related_journal",
              "decided_by", "kind"):
        assert L["body"].get(k) == V["body"].get(k), (
            f"decision field {k!r} drift: "
            f"legacy={L['body'].get(k)!r} verb={V['body'].get(k)!r}"
        )
    assert "confidence" not in L["body"], (
        f"v2.7.0.6 — legacy rka_record_decision must not forward confidence; "
        f"got body={L['body']}"
    )
    assert "confidence" not in V["body"], (
        f"v2.7.0.6 — verb rka_record_decision (via dispatch) must not forward "
        f"confidence; got body={V['body']}"
    )


# ---------------------------------------------------------------------------
# rka_get_status ≡ rka_query(scope='status')
# ---------------------------------------------------------------------------


async def test_parity_get_status_query_status(captured_calls):
    """Legacy rka_get_status calls many endpoints (/api/status,
    /api/missions, /api/checkpoints, /api/maintenance/summary,
    /api/capabilities). The v2.7.0 verb scope='status' calls only
    /api/status. They are NOT a byte-parity match — the verb is a
    thinner read.

    This test pins that the verb hits exactly the right primary
    endpoint and the legacy at least covers it (so existing legacy
    callers don't regress)."""
    leg, ver, set_factory, switch_to = captured_calls

    def factory(method, path):
        # Return shape that rka_get_status's body can parse without
        # crashing during the multiple-GET aggregation.
        if path == "/api/status":
            return (200, {
                "project_name": "Test", "current_phase": "design",
                "summary": "", "blockers": "",
            })
        if path == "/api/missions":
            return (200, [])
        if path == "/api/checkpoints":
            return (200, [])
        if path == "/api/maintenance/summary":
            return (200, {"total_items": 0})
        if path == "/api/capabilities":
            return (200, {"embedding": {"available": True}})
        return (200, {})

    set_factory(factory)

    switch_to(leg)
    await mcp_mod.rka_get_status(project_id="prj_t")
    switch_to(ver)
    # v2.7.0 Phase 3: typed-args surface.
    from pydantic import TypeAdapter
    from rka.mcp.operation_args import QueryArgsUnion
    adapter = TypeAdapter(QueryArgsUnion)
    typed_args = adapter.validate_python(
        {"operation": "status", "project_id": "prj_t"}
    )
    await mcp_mod.rka_query(typed_args)

    # v2.7.0 Phase 3: rka_query(operation='status') now routes through
    # the typed-args dispatcher into the legacy rka_get_status tool
    # (single source of truth for the status aggregator). Both must
    # call /api/status; the aggregator's other GETs are tolerated.
    leg_paths = [r["path"] for r in leg.requests]
    ver_paths = [r["path"] for r in ver.requests]
    assert "/api/status" in leg_paths, (
        f"legacy rka_get_status didn't hit /api/status: {leg_paths}"
    )
    assert "/api/status" in ver_paths, (
        f"rka_query(operation='status') expected to hit /api/status; "
        f"got: {ver_paths}"
    )


# ---------------------------------------------------------------------------
# rka_search ≡ rka_query(scope='search', query=...)
# ---------------------------------------------------------------------------


async def test_parity_search_query_search(captured_calls):
    """Legacy rka_search returns a list-of-result-dicts. We return an
    empty list so the legacy's `if not results` short-circuit fires
    cleanly. The verb's /api/search call gets the same wire body."""
    leg, ver, set_factory, switch_to = captured_calls

    def factory(method, path):
        if path == "/api/search":
            return (200, [])
        if path == "/api/maintenance/summary":
            return (200, {"total_items": 0})
        if path == "/api/capabilities":
            return (200, {"embedding": {"available": True}})
        return (200, {})

    set_factory(factory)

    switch_to(leg)
    await mcp_mod.rka_search(
        query="needle", limit=5, project_id="prj_t",
    )
    switch_to(ver)
    # v2.7.0 Phase 3: typed-args surface.
    from pydantic import TypeAdapter
    from rka.mcp.operation_args import QueryArgsUnion
    adapter = TypeAdapter(QueryArgsUnion)
    typed_args = adapter.validate_python({
        "operation": "search", "query": "needle", "limit": 5,
        "project_id": "prj_t",
    })
    await mcp_mod.rka_query(typed_args)

    # Both legacy and verb hit /api/search at least once.
    leg_search = [r for r in leg.requests if r["path"] == "/api/search"]
    ver_search = [r for r in ver.requests if r["path"] == "/api/search"]
    assert len(leg_search) == 1 and len(ver_search) == 1
    L, V = leg_search[0], ver_search[0]
    assert L["method"] == V["method"] == "POST"
    # query and limit must match in the body.
    assert L["body"].get("query") == V["body"].get("query") == "needle"
    assert L["body"].get("limit") == V["body"].get("limit") == 5


# ---------------------------------------------------------------------------
# rka_create_mission ≡ rka_mission(action='create', ...)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("has_results", [False, True])
async def test_search_surfaces_partial_semantic_warning(captured_calls, has_results):
    leg, _, set_factory, switch_to = captured_calls

    def factory(method, path):
        if path == "/api/search":
            return (200, [{"entity_type": "journal", "entity_id": "jrn_example",
                           "title": "Synthetic example"}] if has_results else [])
        if path == "/api/maintenance/summary":
            return (200, {"total_items": 0})
        if path == "/api/capabilities":
            return (200, {"embedding": {"available": True, "index_status": "partial"}})
        return (200, {})

    set_factory(factory)
    switch_to(leg)
    result = await mcp_mod.rka_search(query="needle", project_id="prj_t")
    assert "Partial semantic coverage" in result
    assert "keyword-searchable" in result
    assert "FTS-only" not in result


async def test_parity_create_mission_verb(captured_calls):
    leg, ver, set_factory, switch_to = captured_calls
    set_factory(lambda m, p: (200, {
        "id": "mis_1", "phase": "execution", "objective": "Build a thing",
        "status": "pending",
    }))

    switch_to(leg)
    await mcp_mod.rka_create_mission(
        phase="execution", objective="Build a thing",
        motivated_by_decision="dec_root",
        project_id="prj_t",
    )
    switch_to(ver)
    await mcp_mod.rka_mission(
        action="create",
        project_id="prj_t",
        phase="execution",
        objective="Build a thing",
        motivated_by_decision="dec_root",
    )

    L, V = leg.requests[0], ver.requests[0]
    assert L["method"] == V["method"] == "POST"
    assert L["path"] == V["path"] == "/api/missions"
    # Core provenance + body fields must match.
    for k in ("objective", "motivated_by_decision", "phase"):
        assert L["body"].get(k) == V["body"].get(k), (
            f"mission field {k!r} drift: "
            f"legacy={L['body'].get(k)!r} verb={V['body'].get(k)!r}"
        )


# ---------------------------------------------------------------------------
# rka_submit_checkpoint ≡ rka_checkpoint(action='submit', ...)
# ---------------------------------------------------------------------------


async def test_parity_submit_checkpoint_verb(captured_calls):
    leg, ver, set_factory, switch_to = captured_calls
    set_factory(lambda m, p: (200, {
        "id": "chk_1", "type": "design", "blocking": True,
        "description": "x",
    }))

    switch_to(leg)
    await mcp_mod.rka_submit_checkpoint(
        mission_id="mis_x", type="design",
        description="why blocked",
        blocking=True,
        project_id="prj_t",
    )
    switch_to(ver)
    await mcp_mod.rka_checkpoint(
        action="submit",
        project_id="prj_t",
        mission_id="mis_x", type="design",
        description="why blocked",
        blocking=True,
    )

    L, V = leg.requests[0], ver.requests[0]
    assert L["method"] == V["method"] == "POST"
    assert L["path"] == V["path"] == "/api/checkpoints"
    # description is the canonical body field; both calls must
    # produce it identically.
    for k in ("mission_id", "type", "description", "blocking"):
        assert L["body"].get(k) == V["body"].get(k), (
            f"checkpoint field {k!r} drift: "
            f"legacy={L['body'].get(k)!r} verb={V['body'].get(k)!r}"
        )


# ---------------------------------------------------------------------------
# Verb-side content alias preserved at the wire
# ---------------------------------------------------------------------------


async def test_parity_submit_checkpoint_content_alias(captured_calls):
    """The verb accepts content= and the legacy tool accepts content=
    as an alias for description (Phase-X²' Layer 1). Both paths must
    yield the same wire body."""
    leg, ver, set_factory, switch_to = captured_calls
    set_factory(lambda m, p: (200, {
        "id": "chk_1", "type": "design",
        "description": "y", "blocking": True,
    }))

    switch_to(leg)
    await mcp_mod.rka_submit_checkpoint(
        mission_id="mis_x", type="design",
        content="alias body",
        blocking=True,
        project_id="prj_t",
    )
    switch_to(ver)
    await mcp_mod.rka_checkpoint(
        action="submit",
        project_id="prj_t",
        mission_id="mis_x", type="design",
        content="alias body",
        blocking=True,
    )

    L, V = leg.requests[0], ver.requests[0]
    assert L["path"] == V["path"] == "/api/checkpoints"
    # The alias resolution either lifts to description or preserves
    # the content key — but BOTH paths must agree at the wire.
    assert L["body"] == V["body"], (
        f"content-alias drift: legacy={L['body']!r} verb={V['body']!r}"
    )


# ---------------------------------------------------------------------------
# Regression: create_mission with tasks as plain DICTS (v2.7.0 rka_execute path)
# ---------------------------------------------------------------------------


async def test_create_mission_tasks_as_dicts_does_not_crash(captured_calls):
    """Regression (2026-06-15): rka_execute(create_mission) routes through
    verb_dispatch -> _legacy('rka_create_mission') passing tasks as PLAIN DICTS
    (CreateMissionArgs.tasks: list[dict]). The legacy tool built the body with
    `[task.model_dump() for task in tasks]`, raising
    `'dict' object has no attribute 'model_dump'`. Both the verb path and the
    legacy tool must accept dict tasks (passed through) AND MissionTask models
    (model_dump'd)."""
    leg, ver, set_factory, switch_to = captured_calls
    set_factory(lambda m, p: (200, {"id": "mis_1", "phase": "execution",
                                     "objective": "x", "status": "pending"}))
    dict_tasks = [{"id": "T1", "description": "do the thing", "status": "pending"}]

    # v2.7.0 verb path (rka_mission action=create) — delivers dicts to the tool.
    switch_to(ver)
    await mcp_mod.rka_mission(
        action="create", project_id="prj_t", phase="execution",
        objective="x", motivated_by_decision="dec_root", tasks=dict_tasks,
    )
    assert ver.requests[0]["body"]["tasks"] == dict_tasks  # dicts passed through

    # legacy tool path directly with dict tasks (same coercion site).
    switch_to(leg)
    await mcp_mod.rka_create_mission(
        phase="execution", objective="x", motivated_by_decision="dec_root",
        project_id="prj_t", tasks=dict_tasks,
    )
    assert leg.requests[0]["body"]["tasks"] == dict_tasks


async def test_create_mission_tasks_as_models_still_model_dumped(captured_calls):
    """The pre-existing MissionTask-model path must still work (model_dump)."""
    from rka.models.mission import MissionTask
    leg, ver, set_factory, switch_to = captured_calls
    set_factory(lambda m, p: (200, {"id": "mis_1", "phase": "execution",
                                     "objective": "x", "status": "pending"}))
    mt = MissionTask(description="modelled task")
    switch_to(leg)
    await mcp_mod.rka_create_mission(
        phase="execution", objective="x", motivated_by_decision="dec_root",
        project_id="prj_t", tasks=[mt],
    )
    sent = leg.requests[0]["body"]["tasks"]
    assert isinstance(sent[0], dict) and sent[0].get("description") == "modelled task"
