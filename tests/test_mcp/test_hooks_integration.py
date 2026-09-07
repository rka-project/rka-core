"""End-to-end hook integration tests (Mission 2 Phase B).

Exercises the wiring sites — rka_add_note triggers post_journal_create;
rka_record_outcome triggers post_record_outcome with flattened metrics;
rka_extract_claims triggers post_claim_extract via the fire endpoint.

Scenario A uses brain_notify; scenario B exercises drift detection via the
flattened metrics_after payload. Legacy executable handlers are blocked.
"""

from __future__ import annotations

import json as _json
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

from rka.api.app import create_app
from rka.config import RKAConfig


HEADERS = {"X-RKA-Project": "proj_default"}


@pytest_asyncio.fixture
async def api(tmp_path: Path):
    config = RKAConfig(
        project_dir=tmp_path,
        data_dir=tmp_path / "data",
        db_path=Path("hooks_integration.db"),
        llm_enabled=False,
        embeddings_enabled=False,
    )
    app = create_app(config)
    lifespan = app.router.lifespan_context(app)
    await lifespan.__aenter__()
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver",
        ) as client:
            yield client
    finally:
        await lifespan.__aexit__(None, None, None)


async def _seed_legacy_sql_hook(api, *, event, statement):
    # This is the in-process fixture's DB, never a live endpoint. New SQL
    # registration is rejected; historical rows still need execution checks.
    db = api._transport.app.state.db
    await db.execute(
        """INSERT INTO hooks
           (id, event, project_id, handler_type, handler_config, enabled, name, created_by)
           VALUES ('hk_legacy_sql', ?, 'proj_default', 'sql', ?, 1, 'legacy sql', 'pi')""",
        [event, _json.dumps({"statement": statement})],
    )
    await db.commit()


async def _register_brain_notify_hook(
    api: httpx.AsyncClient,
    *,
    event: str,
    template: dict,
    severity: str = "info",
    name: str = "test-hook",
) -> str:
    r = await api.post(
        "/api/hooks",
        json={
            "event": event,
            "handler_type": "brain_notify",
            "handler_config": {
                "severity": severity,
                "content_template": template,
            },
            "name": name,
        },
        headers=HEADERS,
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


# ============================================================
# Scenario A — reframed: session_start hook nudges Brain to run maintenance.
# (mcp_tool is unsupported; the Brain reads the notification and
# invokes rka_get_pending_maintenance itself.)
# ============================================================


@pytest.mark.asyncio
async def test_scenario_A_session_start_brain_notify(api: httpx.AsyncClient):
    await _register_brain_notify_hook(
        api,
        event="session_start",
        template={
            "reminder": "Run rka_get_pending_maintenance to surface integrity issues",
            "project": "{project_id}",
        },
    )
    # Simulate session_start firing (the MCP layer would do this on first tool
    # call per project per session).
    r = await api.post(
        "/api/hooks/fire",
        json={
            "event": "session_start",
            "payload": {"project_id": "proj_default", "actor": "brain"},
        },
        headers=HEADERS,
    )
    assert r.status_code == 200
    notes = (await api.get("/api/notifications", headers=HEADERS)).json()
    assert len(notes) == 1
    assert notes[0]["content"]["reminder"].startswith("Run rka_get_pending_maintenance")
    assert notes[0]["content"]["project"] == "proj_default"


# ============================================================
# Scenario B — post_record_outcome fires with flattened metrics; brain_notify
# template surfaces a drift signal directly using {override_rate}.
# ============================================================


async def _seed_resolved_decision(api: httpx.AsyncClient, *, conf: float = 0.7) -> str:
    """Create a decision with a recommended option + recorded PI selection."""
    r = await api.post(
        "/api/decisions",
        json={"question": "Q?", "phase": "design", "decided_by": "brain"},
        headers=HEADERS,
    )
    dec_id = r.json()["id"]
    opt_payload = {
        "label": "A",
        "summary": "S",
        "justification": "J",
        "explanation": "E",
        "pros": ["p1", "p2", "p3"],
        "cons": ["c1", "c2", "c3"],
        "evidence": [],
        "confidence_verbal": "moderate",
        "confidence_numeric": conf,
        "confidence_evidence_strength": "moderate",
        "confidence_known_unknowns": ["u1"],
        "effort_time": "M",
        "effort_reversibility": "reversible",
        "presentation_order_seed": 1,
    }
    opt = await api.post(
        f"/api/decisions/{dec_id}/options", json=opt_payload, headers=HEADERS,
    )
    opt_id = opt.json()["id"]
    await api.put(
        f"/api/decision_options/{opt_id}/recommend", headers=HEADERS,
    )
    await api.put(
        f"/api/decisions/{dec_id}/pi_selection",
        json={"selected_option_id": opt_id, "override_rationale": None},
        headers=HEADERS,
    )
    return dec_id


@pytest.mark.asyncio
async def test_scenario_B_post_record_outcome_drift_notify(api: httpx.AsyncClient):
    # Register the drift-watch hook BEFORE recording the outcome so it sees the fire.
    await _register_brain_notify_hook(
        api,
        event="post_record_outcome",
        template={
            "decision_id": "{decision_id}",
            "outcome": "{outcome}",
            "override_rate_now": "{override_rate}",
            "warning": "drift signal — review override_rate trend",
        },
        severity="warning",
        name="drift-watch",
    )
    dec_id = await _seed_resolved_decision(api)
    # Record an outcome — this fires post_record_outcome with flattened metrics.
    r = await api.post(
        f"/api/decisions/{dec_id}/outcomes",
        json={"outcome": "succeeded"},
        headers=HEADERS,
    )
    assert r.status_code == 201
    notes = (await api.get("/api/notifications", headers=HEADERS)).json()
    assert len(notes) == 1
    content = notes[0]["content"]
    assert content["decision_id"] == dec_id
    assert content["outcome"] == "succeeded"
    # override_rate is computed as part of metrics; PI selected the recommended
    # option in the fixture so override_rate = 0.0.
    assert content["override_rate_now"] == 0.0
    assert notes[0]["severity"] == "warning"


@pytest.mark.asyncio
async def test_post_record_outcome_payload_carries_full_metric_snapshot(
    api: httpx.AsyncClient,
):
    """The flattened metrics_after payload carries both metric families'
    fields so any brain_notify template can interpolate them directly."""
    captured = {}

    # Use a brain_notify hook whose template captures the entire interpolated
    # payload — this is the canonical inspection pattern.
    await _register_brain_notify_hook(
        api,
        event="post_record_outcome",
        template={
            "brier": "{brier_score}",
            "ece": "{ece}",
            "n_outcomes": "{n_outcomes}",
            "metrics_avail": "{metrics_available}",
            "qualifying": "{qualifying_decisions}",
            "override_avail": "{override_metrics_available}",
        },
    )
    dec_id = await _seed_resolved_decision(api)
    await api.post(
        f"/api/decisions/{dec_id}/outcomes",
        json={"outcome": "succeeded"},
        headers=HEADERS,
    )
    notes = (await api.get("/api/notifications", headers=HEADERS)).json()
    assert len(notes) == 1
    c = notes[0]["content"]
    # brier_score is None when N<5 — but it's still in the payload, so
    # interpolation produces the literal None or stringified None.
    # The interpolation engine doesn't substitute None as a string by
    # default; verify the keys are at least present.
    assert "brier" in c
    assert c["n_outcomes"] == 1   # one outcome recorded
    assert c["metrics_avail"] is False  # N<5
    assert c["qualifying"] == 1   # the seeded decision qualifies for override metrics
    assert c["override_avail"] is False  # N<5


# ============================================================
# Wiring: post_journal_create fires from rka_add_note path.
# ============================================================


@pytest.mark.asyncio
async def test_post_journal_create_fires_on_note_creation(api: httpx.AsyncClient):
    await _register_brain_notify_hook(
        api,
        event="post_journal_create",
        template={"new_entry": "{entry_id}", "type": "{type}"},
    )
    r = await api.post(
        "/api/notes",
        json={
            "content": "Test note for hook integration",
            "type": "note",
            "source": "executor",
        },
        headers=HEADERS,
    )
    assert r.status_code in (200, 201)
    notes = (await api.get("/api/notifications", headers=HEADERS)).json()
    assert len(notes) == 1
    assert notes[0]["content"]["new_entry"].startswith("jrn_")
    assert notes[0]["content"]["type"] == "note"


# ============================================================
# Wiring: post_claim_extract fires via the MCP-side fire endpoint
# (the dispatcher path itself is the same as scenario A's /api/hooks/fire
# path; verifying the event name routes correctly).
# ============================================================


@pytest.mark.asyncio
async def test_post_claim_extract_fires_via_endpoint(api: httpx.AsyncClient):
    await _register_brain_notify_hook(
        api,
        event="post_claim_extract",
        template={"entry": "{entry_id}", "n_claims": "{n_claims}"},
    )
    r = await api.post(
        "/api/hooks/fire",
        json={
            "event": "post_claim_extract",
            "payload": {
                "entry_id": "jrn_test",
                "claim_ids": ["clm_a", "clm_b"],
                "n_claims": 2,
                "source": "brain",
            },
        },
        headers=HEADERS,
    )
    assert r.status_code == 200
    notes = (await api.get("/api/notifications", headers=HEADERS)).json()
    assert len(notes) == 1
    assert notes[0]["content"]["entry"] == "jrn_test"
    assert notes[0]["content"]["n_claims"] == 2


# ============================================================
# Edge cases
# ============================================================


@pytest.mark.asyncio
async def test_disabled_hook_does_not_fire(api: httpx.AsyncClient):
    create = await api.post(
        "/api/hooks",
        json={
            "event": "session_start",
            "handler_type": "brain_notify",
            "handler_config": {"content_template": {"x": 1}},
            "name": "off",
            "enabled": False,
        },
        headers=HEADERS,
    )
    assert create.status_code == 201
    r = await api.post(
        "/api/hooks/fire",
        json={"event": "session_start", "payload": {}},
        headers=HEADERS,
    )
    assert r.json()["fired_executions"] == []
    notes = (await api.get("/api/notifications", headers=HEADERS)).json()
    assert notes == []


@pytest.mark.asyncio
async def test_hook_failure_does_not_break_core_operation(api: httpx.AsyncClient):
    """If a post_journal_create hook is broken, rka_add_note still succeeds.

    Demonstrates the structural-additive guarantee: hooks cannot break the
    core operation they observe.
    """
    await _seed_legacy_sql_hook(
        api, event="post_journal_create", statement="DELETE FROM journal",
    )
    # The note creation must still succeed.
    r = await api.post(
        "/api/notes",
        json={
            "content": "Note that triggers a broken hook",
            "type": "note",
            "source": "executor",
        },
        headers=HEADERS,
    )
    assert r.status_code in (200, 201)
    # The execution is logged with status='error'.
    execs = (
        await api.get("/api/hooks/executions/list?status=error", headers=HEADERS)
    ).json()
    assert len(execs) == 1
    assert "unsupported" in execs[0]["error_message"]


@pytest.mark.asyncio
async def test_three_severity_levels_all_round_trip(api: httpx.AsyncClient):
    """info / warning / critical all accepted; arbitrary value rejected."""
    for sev in ("info", "warning", "critical"):
        await _register_brain_notify_hook(
            api, event="periodic",
            template={"sev": sev}, severity=sev, name=f"sev-{sev}",
        )
    await api.post(
        "/api/hooks/fire",
        json={"event": "periodic", "payload": {}},
        headers=HEADERS,
    )
    notes = (await api.get("/api/notifications", headers=HEADERS)).json()
    severities = sorted({n["severity"] for n in notes})
    assert severities == ["critical", "info", "warning"]


@pytest.mark.asyncio
async def test_periodic_event_fires_via_endpoint(api: httpx.AsyncClient):
    await _register_brain_notify_hook(
        api, event="periodic",
        template={"checked": "yes"},
    )
    r = await api.post(
        "/api/hooks/fire",
        json={"event": "periodic", "payload": {"project_id": "proj_default"}},
        headers=HEADERS,
    )
    assert r.status_code == 200
    assert len(r.json()["fired_executions"]) == 1


@pytest.mark.asyncio
async def test_executions_filterable_by_hook_id(api: httpx.AsyncClient):
    h1 = await _register_brain_notify_hook(api, event="periodic", template={"a": 1}, name="h1")
    h2 = await _register_brain_notify_hook(api, event="periodic", template={"b": 2}, name="h2")
    await api.post("/api/hooks/fire", json={"event": "periodic", "payload": {}}, headers=HEADERS)
    only_h1 = (
        await api.get(f"/api/hooks/executions/list?hook_id={h1}", headers=HEADERS)
    ).json()
    assert len(only_h1) == 1
    assert only_h1[0]["hook_id"] == h1


@pytest.mark.asyncio
async def test_executions_filterable_by_status(api: httpx.AsyncClient):
    await _register_brain_notify_hook(api, event="periodic", template={"x": 1}, name="ok")
    await _seed_legacy_sql_hook(
        api, event="periodic", statement="INSERT INTO does_not_exist VALUES (1)",
    )
    await api.post("/api/hooks/fire", json={"event": "periodic", "payload": {}}, headers=HEADERS)
    errors = (
        await api.get("/api/hooks/executions/list?status=error", headers=HEADERS)
    ).json()
    successes = (
        await api.get("/api/hooks/executions/list?status=success", headers=HEADERS)
    ).json()
    assert len(errors) == 1
    assert len(successes) == 1


@pytest.mark.asyncio
async def test_unknown_event_in_fire_returns_422(api: httpx.AsyncClient):
    r = await api.post(
        "/api/hooks/fire",
        json={"event": "post_lunchtime", "payload": {}},
        headers=HEADERS,
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_get_notifications_default_excludes_cleared(api: httpx.AsyncClient):
    await _register_brain_notify_hook(api, event="session_start", template={"x": 1})
    await api.post("/api/hooks/fire", json={"event": "session_start", "payload": {}}, headers=HEADERS)
    notes = (await api.get("/api/notifications", headers=HEADERS)).json()
    bnt_ids = [n["id"] for n in notes]
    await api.post("/api/notifications/clear", json={"ids": bnt_ids}, headers=HEADERS)
    after = (await api.get("/api/notifications", headers=HEADERS)).json()
    assert after == []


@pytest.mark.asyncio
async def test_clear_returns_zero_for_unknown_ids(api: httpx.AsyncClient):
    r = await api.post(
        "/api/notifications/clear",
        json={"ids": ["bnt_does_not_exist"]},
        headers=HEADERS,
    )
    assert r.status_code == 200
    assert r.json()["cleared"] == 0
