"""v2.7.0 PR-1 — verb-dispatch unit tests.

Exercises the routing layer in `rka/mcp/verb_dispatch.py` without
hitting the live REST API. Approach: monkeypatch the legacy tool
records inside `_TOOL_REGISTRY` so each dispatcher call lands on a
recording fake that captures (tool_name, kwargs). This lets us
assert:

  - Each rka_query scope routes to the right legacy read tool.
  - rka_record_note rejects source='pi' without verbatim_input
    (Phase-X²-prime polish).
  - rka_record_decision rejects empty/missing related_journal
    (provenance discipline preserved).
  - rka_record_literature rejects ambiguous multi-mode calls and
    handles the explicit `action=` discriminator correctly.
  - dispatch_mission(action='create') requires
    provenance.motivated_by_decision (Graft A surface).
  - dispatch_checkpoint(action='submit') accepts the content/
    description/message alias (Phase-X²' Layer 1).
  - Unknown discriminators surface as structured error JSON.

The fake legacy tools return marker strings/JSON so the dispatcher
output is fully observable from the test side.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from rka.mcp import verb_dispatch
import rka.mcp.server as _live_server


# ---------------------------------------------------------------------------
# Fake registry helpers
# ---------------------------------------------------------------------------


class _Recorder:
    """Holds the captured (tool_name, kwargs) tuples across a single
    test. Each fake legacy tool appends its call here."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def make_fake(self, name: str, return_value: str | None = None):
        async def fake(**kwargs: Any) -> str:
            self.calls.append((name, kwargs))
            return return_value or f"ok:{name}"
        return fake


@pytest.fixture
def recorder(monkeypatch) -> _Recorder:
    """Replace EVERY legacy tool's fn in the registry with a recording
    fake. The verb dispatchers do `_TOOL_REGISTRY[name]['fn']` lookups,
    so this intercepts all routes without touching FastMCP or httpx.
    """
    rec = _Recorder()
    # Snapshot original fns so we can restore (monkeypatch handles cleanup,
    # but we want explicit fakes for each name reached during the test).
    for name, entry in list(_live_server._TOOL_REGISTRY.items()):
        # We replace the inner 'fn' field of the registry record so the
        # late-bound _legacy(name) lookup inside verb_dispatch returns
        # our fake.
        monkeypatch.setitem(
            _live_server._TOOL_REGISTRY,
            name,
            {**entry, "fn": rec.make_fake(name)},
        )
    return rec


# ---------------------------------------------------------------------------
# rka_query scope routing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "scope,expected_tool,extra_kw",
    [
        ("status", "rka_get_status", {}),
        ("context", "rka_get_context", {}),
        ("pending_maintenance", "rka_get_pending_maintenance", {}),
        ("research_map", "rka_get_research_map", {}),
        ("review_queue", "rka_get_review_queue", {}),
        ("checkpoints", "rka_get_checkpoints", {}),
        ("calibration_metrics", "rka_get_calibration_metrics", {}),
        ("integrity", "rka_check_integrity", {}),
        ("graph_stats", "rka_graph_stats", {}),
        ("graph", "rka_get_graph", {}),
        ("graph_mermaid", "rka_export_mermaid", {}),
        ("journal", "rka_get_journal", {}),
        ("literature", "rka_get_literature", {}),
        ("clusters", "rka_list_clusters", {}),
        ("claims", "rka_get_claims", {}),
        ("interpretation_candidates", "rka_get_interpretation_candidates", {}),
        ("hooks", "rka_list_hooks", {}),
        ("hook_executions", "rka_get_hook_executions", {}),
        ("brain_notifications", "rka_get_brain_notifications", {}),
        ("freshness", "rka_check_freshness", {}),
        ("decision_tree", "rka_get_decision_tree", {}),
    ],
)
async def test_dispatch_query_routes_to_legacy_tool(
    recorder: _Recorder, scope: str, expected_tool: str, extra_kw: dict
) -> None:
    """Each rka_query scope routes through verb_dispatch.dispatch_query
    to the documented legacy tool."""
    await verb_dispatch.dispatch_query(
        scope, project_id="prj_test", **extra_kw,
    )
    tools_called = [t for t, _ in recorder.calls]
    assert expected_tool in tools_called, (
        f"scope={scope!r} expected to route to {expected_tool!r}, "
        f"got: {tools_called}"
    )


async def test_dispatch_query_search_requires_query(
    recorder: _Recorder,
) -> None:
    """scope='search' without query yields a structured error and
    does NOT call any legacy tool."""
    out = await verb_dispatch.dispatch_query(
        "search", project_id="prj_test",
    )
    parsed = json.loads(out)
    assert parsed["error"] == "missing_field"
    assert "query" in parsed["message"]
    assert recorder.calls == [], (
        f"dispatch_query(search) without query must not dispatch; "
        f"got: {recorder.calls}"
    )


async def test_dispatch_query_entity_requires_id(
    recorder: _Recorder,
) -> None:
    """scope='entity' without id is rejected pre-flight."""
    out = await verb_dispatch.dispatch_query(
        "entity", project_id="prj_test",
    )
    parsed = json.loads(out)
    assert parsed["error"] == "missing_field"
    assert recorder.calls == []


async def test_dispatch_query_unknown_scope_returns_error(
    recorder: _Recorder,
) -> None:
    """Unknown scopes surface as invalid_scope JSON (no dispatch)."""
    out = await verb_dispatch.dispatch_query(
        "no_such_scope", project_id="prj_test",
    )
    parsed = json.loads(out)
    assert parsed["error"] == "invalid_scope"
    assert "valid_scopes" in parsed
    assert recorder.calls == []


async def test_dispatch_query_threads_project_id(
    recorder: _Recorder,
) -> None:
    """The project_id kwarg flows through to the legacy call (v2.6
    contract preserved at the verb tier)."""
    await verb_dispatch.dispatch_query(
        "status", project_id="prj_specific_test",
    )
    assert recorder.calls
    _, captured_kw = recorder.calls[-1]
    assert captured_kw.get("project_id") == "prj_specific_test"


async def test_dispatch_query_provenance_uses_endpoint_safe_depth_default(
    recorder: _Recorder,
) -> None:
    """Typed provenance dispatch must not exceed the ego endpoint's max depth."""
    await verb_dispatch.dispatch_query(
        "provenance",
        id="dec_01XYZ",
        project_id="prj_test",
    )

    assert recorder.calls[-1] == (
        "rka_trace_provenance",
        {
            "entity_id": "dec_01XYZ",
            "direction": "both",
            "max_depth": 3,
            "project_id": "prj_test",
        },
    )


async def test_dispatch_interpretation_staging_threads_typed_review_fields(
    recorder: _Recorder,
) -> None:
    await verb_dispatch.dispatch_execute(
        "create_interpretation_candidate",
        project_id="prj_stage",
        source_type="journal",
        source_id="jrn_source",
        locator_kind="record",
        locator_value="full_record",
        statement="The local run measured 42 ms.",
        epistemic_kind="observation",
        created_by="brain",
        extraction_tool="manual_review",
        proposed_claim_type="result",
    )
    name, payload = recorder.calls[-1]
    assert name == "rka_create_interpretation_candidate"
    assert payload["project_id"] == "prj_stage"
    assert payload["locator_value"] == "full_record"
    assert payload["proposed_claim_type"] == "result"

    await verb_dispatch.dispatch_execute(
        "triage_interpretation_candidate",
        project_id="prj_stage",
        id="icd_candidate",
        action="promote",
        expected_revision=2,
        actor="brain",
        reason="Checked the exact journal record.",
        grounding_verified=True,
        claim_confidence=0.83,
    )
    name, payload = recorder.calls[-1]
    assert name == "rka_triage_interpretation_candidate"
    assert payload == {
        "candidate_id": "icd_candidate",
        "action": "promote",
        "expected_revision": 2,
        "actor": "brain",
        "reason": "Checked the exact journal record.",
        "target_candidate_id": None,
        "target_entity_id": None,
        "grounding_verified": True,
        "claim_confidence": 0.83,
        "project_id": "prj_stage",
    }


# ---------------------------------------------------------------------------
# rka_record_note — source='pi' requires verbatim_input
# ---------------------------------------------------------------------------


async def test_dispatch_record_note_pi_requires_verbatim_input(
    recorder: _Recorder,
) -> None:
    """Phase-X²-prime polish: source='pi' without verbatim_input is
    rejected. The REST layer would accept the call without it, but
    the verb-tier guard preserves PI intellectual attribution."""
    out = await verb_dispatch.dispatch_record_note(
        "PI analysis goes here.",
        project_id="prj_test",
        source="pi",
        # NO verbatim_input
    )
    parsed = json.loads(out)
    assert parsed["error"] == "missing_provenance"
    assert "verbatim_input" in parsed["message"]
    assert recorder.calls == [], (
        "record_note(source='pi') without verbatim_input must not dispatch"
    )


async def test_dispatch_record_note_pi_with_verbatim_input_passes(
    recorder: _Recorder,
) -> None:
    """source='pi' with verbatim_input dispatches into rka_add_note."""
    await verb_dispatch.dispatch_record_note(
        "PI says go right.",
        project_id="prj_test",
        source="pi",
        verbatim_input="go right",
    )
    tools = [t for t, _ in recorder.calls]
    assert "rka_add_note" in tools
    last_kw = recorder.calls[-1][1]
    assert last_kw["source"] == "pi"
    assert last_kw["verbatim_input"] == "go right"


async def test_dispatch_record_note_brain_default_dispatches(
    recorder: _Recorder,
) -> None:
    """source='brain' / 'executor' don't need verbatim_input — only
    'pi' does."""
    await verb_dispatch.dispatch_record_note(
        "Brain note.",
        project_id="prj_test",
        source="brain",
    )
    assert any(t == "rka_add_note" for t, _ in recorder.calls)


async def test_dispatch_record_note_unpacks_provenance(
    recorder: _Recorder,
) -> None:
    """Graft A: provenance={related_decisions:[dec_a], related_mission:mis_b}
    unpacks to top-level kwargs on rka_add_note."""
    await verb_dispatch.dispatch_record_note(
        "executor finding",
        project_id="prj_test",
        source="executor",
        provenance={
            "related_decisions": ["dec_a"],
            "related_mission": "mis_b",
            "related_literature": ["lit_x"],
        },
    )
    assert recorder.calls
    last_kw = recorder.calls[-1][1]
    assert last_kw["related_decisions"] == ["dec_a"]
    assert last_kw["related_mission"] == "mis_b"
    assert last_kw["related_literature"] == ["lit_x"]


async def test_dispatch_record_note_ingest_document_mode(
    recorder: _Recorder,
) -> None:
    """action='ingest_document' routes to rka_ingest_document, not
    rka_add_note."""
    await verb_dispatch.dispatch_record_note(
        "# Heading\nbody",
        project_id="prj_test",
        source="brain",
        action="ingest_document",
    )
    tools = [t for t, _ in recorder.calls]
    assert "rka_ingest_document" in tools
    assert "rka_add_note" not in tools


async def test_dispatch_record_note_unknown_action(
    recorder: _Recorder,
) -> None:
    """Unknown action surfaces as invalid_action."""
    out = await verb_dispatch.dispatch_record_note(
        "x", project_id="prj_test", source="brain",
        action="not_a_real_mode",
    )
    parsed = json.loads(out)
    assert parsed["error"] == "invalid_action"
    assert recorder.calls == []


# ---------------------------------------------------------------------------
# rka_record_decision — related_journal required non-empty
# ---------------------------------------------------------------------------


async def test_dispatch_record_decision_rejects_missing_related_journal(
    recorder: _Recorder,
) -> None:
    """Provenance discipline preserved from Phase-X²: a decision must
    cite at least one journal entry."""
    out = await verb_dispatch.dispatch_record_decision(
        "Q?", "A", "because",
        project_id="prj_test",
        decided_by="brain",
        kind="decision",
        # NO related_journal
    )
    parsed = json.loads(out)
    assert parsed["error"] == "missing_provenance"
    assert "related_journal" in parsed["message"]
    assert recorder.calls == []


async def test_dispatch_record_decision_rejects_empty_related_journal(
    recorder: _Recorder,
) -> None:
    """Empty list is structurally distinct from None — must also fail."""
    out = await verb_dispatch.dispatch_record_decision(
        "Q?", "A", "because",
        project_id="prj_test",
        decided_by="brain",
        kind="decision",
        related_journal=[],  # empty list
    )
    parsed = json.loads(out)
    assert parsed["error"] == "missing_provenance"
    assert recorder.calls == []


async def test_dispatch_record_decision_provenance_kw_provides_related_journal(
    recorder: _Recorder,
) -> None:
    """Graft A: related_journal can come via provenance={...}."""
    await verb_dispatch.dispatch_record_decision(
        "Q?", "A", "because",
        project_id="prj_test",
        decided_by="brain",
        kind="decision",
        provenance={"related_journal": ["jrn_1", "jrn_2"]},
    )
    assert recorder.calls
    _, kw = recorder.calls[-1]
    assert kw["related_journal"] == ["jrn_1", "jrn_2"]


async def test_dispatch_record_decision_supersede_routes_to_supersede(
    recorder: _Recorder,
) -> None:
    """supersedes_decision_id flips the route to rka_supersede_decision."""
    await verb_dispatch.dispatch_record_decision(
        "Q?", "A", "because",
        project_id="prj_test",
        decided_by="brain",
        kind="decision",
        related_journal=["jrn_1"],
        supersedes_decision_id="dec_old",
    )
    tools = [t for t, _ in recorder.calls]
    assert "rka_supersede_decision" in tools
    assert "rka_add_decision" not in tools


async def test_dispatch_record_decision_supersede_forwards_all_provenance(
    recorder: _Recorder,
) -> None:
    """v2.7.0.5 — when record_decision is dispatched with supersedes_decision_id
    set, ALL provenance + multi-choice fields the typed-args layer exposed
    must reach the rka_supersede_decision adapter. Prior versions silently
    dropped related_literature / related_missions / parent_id / options /
    assumptions / tags / status, leaving the replacement decision
    provenance-orphaned even though the typed-args layer enforced
    related_journal non-empty.
    """
    await verb_dispatch.dispatch_record_decision(
        "Reframed Q?", "Option B", "switching",
        project_id="prj_test",
        decided_by="pi",
        kind="design_choice",
        phase="design",
        related_journal=["jrn_evidence"],
        supersedes_decision_id="dec_old",
        options=[{"id": "o1", "label": "A"}, {"id": "o2", "label": "B"}],
        related_literature=["lit_p1"],
        related_missions=["mis_active"],
        parent_id="dec_parent",
        assumptions=["A1", "A2"],
        tags=["t1"],
        status="active",
    )
    # Find the captured supersede call.
    supersede_calls = [
        kw for tool, kw in recorder.calls if tool == "rka_supersede_decision"
    ]
    assert len(supersede_calls) == 1
    kw = supersede_calls[0]
    assert kw["old_decision_id"] == "dec_old"
    assert kw["related_journal"] == ["jrn_evidence"]
    assert kw["related_literature"] == ["lit_p1"]
    assert kw["related_missions"] == ["mis_active"]
    assert kw["parent_id"] == "dec_parent"
    assert kw["options"] == [
        {"id": "o1", "label": "A"},
        {"id": "o2", "label": "B"},
    ]
    assert kw["assumptions"] == ["A1", "A2"]
    assert kw["tags"] == ["t1"]
    assert kw["status"] == "active"
    assert kw["kind"] == "design_choice"
    assert kw["phase"] == "design"
    assert kw["decided_by"] == "pi"


async def test_dispatch_review_supersede_decision_forwards_related_journal(
    recorder: _Recorder,
) -> None:
    """v2.7.0.5 — the dispatch_review('supersede_decision', ...) entry point
    must also forward related_journal + any other provenance fields the
    caller supplied in the payload. SupersedeDecisionArgs enforces
    related_journal non-empty at the typed-args layer, but prior versions
    dropped it before reaching the adapter."""
    payload = {
        "old_decision_id": "dec_old",
        "question": "Q",
        "chosen": "C",
        "rationale": "R",
        "decided_by": "brain",
        "phase": "design",
        "kind": "decision",
        "related_journal": ["jrn_required"],
        "related_literature": ["lit_a"],
        "related_missions": ["mis_x"],
        "parent_id": "dec_parent",
        "options": [{"id": "o1", "label": "A"}],
        "assumptions": ["A1"],
        "tags": ["tag1"],
        "status": "active",
    }
    await verb_dispatch.dispatch_review(
        "supersede_decision", project_id="prj_test", payload=payload
    )
    supersede_calls = [
        kw for tool, kw in recorder.calls if tool == "rka_supersede_decision"
    ]
    assert len(supersede_calls) == 1
    kw = supersede_calls[0]
    assert kw["related_journal"] == ["jrn_required"]
    assert kw["related_literature"] == ["lit_a"]
    assert kw["related_missions"] == ["mis_x"]
    assert kw["parent_id"] == "dec_parent"
    assert kw["options"] == [{"id": "o1", "label": "A"}]
    assert kw["assumptions"] == ["A1"]
    assert kw["tags"] == ["tag1"]
    assert kw["status"] == "active"


async def test_dispatch_record_decision_create_routes_to_add(
    recorder: _Recorder,
) -> None:
    await verb_dispatch.dispatch_record_decision(
        "Q?", "A", "because",
        project_id="prj_test",
        decided_by="brain",
        kind="decision",
        related_journal=["jrn_1"],
    )
    tools = [t for t, _ in recorder.calls]
    assert "rka_add_decision" in tools


# ---------------------------------------------------------------------------
# rka_record_literature — multi-mode dispatch
# ---------------------------------------------------------------------------


async def test_dispatch_record_literature_default_title_routes_to_add(
    recorder: _Recorder,
) -> None:
    await verb_dispatch.dispatch_record_literature(
        project_id="prj_test",
        title="A Paper",
        bibtex=None, search_query=None, search_source=None,
        doi=None, authors=None, year_min=None, venue=None,
        status="to_read", abstract=None, url=None, tags=None,
        related_decisions=None, action=None,
        lit_id=None, manuscript_id=None, zotero_key=None,
        pdf_path=None, annotations=None, summary=None,
        add_to_library=False, limit=10,
    )
    tools = [t for t, _ in recorder.calls]
    assert "rka_add_literature" in tools


async def test_dispatch_record_literature_bibtex_routes_to_import(
    recorder: _Recorder,
) -> None:
    await verb_dispatch.dispatch_record_literature(
        project_id="prj_test",
        title=None,
        bibtex="@article{foo,title={Bar}}",
        search_query=None, search_source=None,
        doi=None, authors=None, year_min=None, venue=None,
        status="to_read", abstract=None, url=None, tags=None,
        related_decisions=None, action=None,
        lit_id=None, manuscript_id=None, zotero_key=None,
        pdf_path=None, annotations=None, summary=None,
        add_to_library=False, limit=10,
    )
    tools = [t for t, _ in recorder.calls]
    assert "rka_import_bibtex" in tools


async def test_dispatch_record_literature_search_s2(
    recorder: _Recorder,
) -> None:
    await verb_dispatch.dispatch_record_literature(
        project_id="prj_test",
        title=None, bibtex=None,
        search_query="transformer paper",
        search_source="semantic_scholar",
        doi=None, authors=None, year_min=None, venue=None,
        status="to_read", abstract=None, url=None, tags=None,
        related_decisions=None, action=None,
        lit_id=None, manuscript_id=None, zotero_key=None,
        pdf_path=None, annotations=None, summary=None,
        add_to_library=False, limit=10,
    )
    tools = [t for t, _ in recorder.calls]
    assert "rka_search_semantic_scholar" in tools


async def test_dispatch_record_literature_action_explicit_wins(
    recorder: _Recorder,
) -> None:
    """Explicit action=link_zotero takes precedence over title presence."""
    await verb_dispatch.dispatch_record_literature(
        project_id="prj_test",
        title="ignored title",
        bibtex=None, search_query=None, search_source=None,
        doi=None, authors=None, year_min=None, venue=None,
        status="to_read", abstract=None, url=None, tags=None,
        related_decisions=None,
        action="link_zotero",
        lit_id="lit_abc",
        manuscript_id=None, zotero_key=None, pdf_path=None,
        annotations=None, summary=None,
        add_to_library=False, limit=10,
    )
    tools = [t for t, _ in recorder.calls]
    assert "rka_link_literature_to_zotero" in tools
    assert "rka_add_literature" not in tools


async def test_dispatch_record_literature_missing_required_field(
    recorder: _Recorder,
) -> None:
    """No title, no bibtex, no doi, no search_query → missing_field."""
    out = await verb_dispatch.dispatch_record_literature(
        project_id="prj_test",
        title=None, bibtex=None, search_query=None, search_source=None,
        doi=None, authors=None, year_min=None, venue=None,
        status="to_read", abstract=None, url=None, tags=None,
        related_decisions=None, action=None,
        lit_id=None, manuscript_id=None, zotero_key=None,
        pdf_path=None, annotations=None, summary=None,
        add_to_library=False, limit=10,
    )
    parsed = json.loads(out)
    assert parsed["error"] == "missing_field"
    assert recorder.calls == []


# ---------------------------------------------------------------------------
# v2.7.0a2 — Decision 2: import_top_n threads to search legacy tools.
# ---------------------------------------------------------------------------


async def test_dispatch_record_literature_import_top_n_threaded_to_s2(
    recorder: _Recorder,
) -> None:
    """import_top_n flows through to rka_search_semantic_scholar so the
    legacy tool can cap the import slice (Decision 2 / Option C)."""
    await verb_dispatch.dispatch_record_literature(
        project_id="prj_test",
        title=None, bibtex=None,
        search_query="transformer",
        search_source="semantic_scholar",
        doi=None, authors=None, year_min=None, venue=None,
        status="to_read", abstract=None, url=None, tags=None,
        related_decisions=None, action=None,
        lit_id=None, manuscript_id=None, zotero_key=None,
        pdf_path=None, annotations=None, summary=None,
        add_to_library=True,
        import_top_n=3,
        limit=10,
    )
    calls = {t: kw for t, kw in recorder.calls}
    assert "rka_search_semantic_scholar" in calls
    kw = calls["rka_search_semantic_scholar"]
    assert kw["import_top_n"] == 3
    assert kw["add_to_library"] is True


async def test_dispatch_record_literature_import_top_n_threaded_to_arxiv(
    recorder: _Recorder,
) -> None:
    """import_top_n flows through to rka_search_arxiv on arxiv mode."""
    await verb_dispatch.dispatch_record_literature(
        project_id="prj_test",
        title=None, bibtex=None,
        search_query="transformer",
        search_source="arxiv",
        doi=None, authors=None, year_min=None, venue=None,
        status="to_read", abstract=None, url=None, tags=None,
        related_decisions=None, action=None,
        lit_id=None, manuscript_id=None, zotero_key=None,
        pdf_path=None, annotations=None, summary=None,
        add_to_library=True,
        import_top_n=5,
        limit=10,
    )
    calls = {t: kw for t, kw in recorder.calls}
    assert "rka_search_arxiv" in calls
    assert calls["rka_search_arxiv"]["import_top_n"] == 5


async def test_dispatch_record_literature_import_top_n_none_default(
    recorder: _Recorder,
) -> None:
    """Without import_top_n, the legacy tool receives None — preserving
    the pre-v2.7.0a2 'import all returned' default."""
    await verb_dispatch.dispatch_record_literature(
        project_id="prj_test",
        title=None, bibtex=None,
        search_query="t",
        search_source="semantic_scholar",
        doi=None, authors=None, year_min=None, venue=None,
        status="to_read", abstract=None, url=None, tags=None,
        related_decisions=None, action=None,
        lit_id=None, manuscript_id=None, zotero_key=None,
        pdf_path=None, annotations=None, summary=None,
        add_to_library=True, limit=10,
    )
    calls = {t: kw for t, kw in recorder.calls}
    assert calls["rka_search_semantic_scholar"]["import_top_n"] is None


# ---------------------------------------------------------------------------
# v2.7.0a2 — Decision 3: year → year_min deprecation alias.
# ---------------------------------------------------------------------------


async def test_dispatch_record_literature_year_min_threaded_to_s2(
    recorder: _Recorder,
) -> None:
    """year_min flows through to rka_search_semantic_scholar — fixing
    the T4-surfaced silent-drop bug (Decision 3 / Option A)."""
    await verb_dispatch.dispatch_record_literature(
        project_id="prj_test",
        title=None, bibtex=None,
        search_query="t",
        search_source="semantic_scholar",
        doi=None, authors=None, year_min=2023, venue=None,
        status="to_read", abstract=None, url=None, tags=None,
        related_decisions=None, action=None,
        lit_id=None, manuscript_id=None, zotero_key=None,
        pdf_path=None, annotations=None, summary=None,
        add_to_library=False, limit=10,
    )
    calls = {t: kw for t, kw in recorder.calls}
    assert calls["rka_search_semantic_scholar"]["year_min"] == 2023


async def test_dispatch_record_literature_year_min_threaded_to_arxiv(
    recorder: _Recorder,
) -> None:
    """year_min flows through to rka_search_arxiv."""
    await verb_dispatch.dispatch_record_literature(
        project_id="prj_test",
        title=None, bibtex=None,
        search_query="t",
        search_source="arxiv",
        doi=None, authors=None, year_min=2024, venue=None,
        status="to_read", abstract=None, url=None, tags=None,
        related_decisions=None, action=None,
        lit_id=None, manuscript_id=None, zotero_key=None,
        pdf_path=None, annotations=None, summary=None,
        add_to_library=False, limit=10,
    )
    calls = {t: kw for t, kw in recorder.calls}
    assert calls["rka_search_arxiv"]["year_min"] == 2024


async def test_dispatch_record_literature_legacy_year_alias_backfills_year_min(
    recorder: _Recorder,
) -> None:
    """Decision 3 backwards-compat: callers passing the deprecated
    `year=` still get year_min populated for one release."""
    await verb_dispatch.dispatch_record_literature(
        project_id="prj_test",
        title=None, bibtex=None,
        search_query="t",
        search_source="semantic_scholar",
        doi=None, authors=None, year_min=None, venue=None,
        status="to_read", abstract=None, url=None, tags=None,
        related_decisions=None, action=None,
        lit_id=None, manuscript_id=None, zotero_key=None,
        pdf_path=None, annotations=None, summary=None,
        add_to_library=False, limit=10,
        year=2022,
    )
    calls = {t: kw for t, kw in recorder.calls}
    assert calls["rka_search_semantic_scholar"]["year_min"] == 2022


async def test_dispatch_record_literature_year_and_year_min_conflict_rejected(
    recorder: _Recorder,
) -> None:
    """Decision 3: passing BOTH year (deprecated) AND year_min raises
    conflicting_args so callers don't silently drop the wrong one."""
    out = await verb_dispatch.dispatch_record_literature(
        project_id="prj_test",
        title=None, bibtex=None,
        search_query="t",
        search_source="semantic_scholar",
        doi=None, authors=None, year_min=2023, venue=None,
        status="to_read", abstract=None, url=None, tags=None,
        related_decisions=None, action=None,
        lit_id=None, manuscript_id=None, zotero_key=None,
        pdf_path=None, annotations=None, summary=None,
        add_to_library=False, limit=10,
        year=2022,
    )
    parsed = json.loads(out)
    assert parsed["error"] == "conflicting_args"
    assert recorder.calls == []


async def test_dispatch_record_literature_default_add_uses_year_min_as_pub_year(
    recorder: _Recorder,
) -> None:
    """On default-add mode (title-based), year_min populates the
    paper's publication year (single-paper year is the floor of its
    own year set)."""
    await verb_dispatch.dispatch_record_literature(
        project_id="prj_test",
        title="A Paper",
        bibtex=None, search_query=None, search_source=None,
        doi=None, authors=None, year_min=2023, venue=None,
        status="to_read", abstract=None, url=None, tags=None,
        related_decisions=None, action=None,
        lit_id=None, manuscript_id=None, zotero_key=None,
        pdf_path=None, annotations=None, summary=None,
        add_to_library=False, limit=10,
    )
    calls = {t: kw for t, kw in recorder.calls}
    assert "rka_add_literature" in calls
    assert calls["rka_add_literature"]["year"] == 2023


# ---------------------------------------------------------------------------
# rka_mission — create requires provenance.motivated_by_decision
# ---------------------------------------------------------------------------


async def test_dispatch_mission_create_requires_motivated_by_decision(
    recorder: _Recorder,
) -> None:
    out = await verb_dispatch.dispatch_mission(
        "create",
        project_id="prj_test",
        objective="Build a thing",
        # NO motivated_by_decision, NO provenance
    )
    parsed = json.loads(out)
    assert parsed["error"] == "missing_provenance"
    assert "motivated_by_decision" in parsed["message"]
    assert recorder.calls == []


async def test_dispatch_mission_create_motivated_by_top_level(
    recorder: _Recorder,
) -> None:
    """Top-level motivated_by_decision kwarg is honored."""
    await verb_dispatch.dispatch_mission(
        "create",
        project_id="prj_test",
        objective="Build a thing",
        motivated_by_decision="dec_root",
    )
    tools = [t for t, _ in recorder.calls]
    assert "rka_create_mission" in tools
    _, kw = recorder.calls[-1]
    assert kw["motivated_by_decision"] == "dec_root"


async def test_dispatch_mission_create_motivated_by_provenance(
    recorder: _Recorder,
) -> None:
    """Graft A: provenance={motivated_by_decision:...} also works."""
    await verb_dispatch.dispatch_mission(
        "create",
        project_id="prj_test",
        objective="Build a thing",
        provenance={"motivated_by_decision": "dec_root"},
    )
    tools = [t for t, _ in recorder.calls]
    assert "rka_create_mission" in tools


async def test_dispatch_mission_update_status_routes(
    recorder: _Recorder,
) -> None:
    await verb_dispatch.dispatch_mission(
        "update_status",
        project_id="prj_test",
        mission_id="mis_x",
        status="active",
    )
    tools = [t for t, _ in recorder.calls]
    assert "rka_update_mission_status" in tools


async def test_dispatch_mission_submit_report_routes(
    recorder: _Recorder,
) -> None:
    await verb_dispatch.dispatch_mission(
        "submit_report",
        project_id="prj_test",
        mission_id="mis_x",
        summary="all done",
    )
    tools = [t for t, _ in recorder.calls]
    assert "rka_submit_report" in tools


async def test_dispatch_mission_unknown_action(
    recorder: _Recorder,
) -> None:
    out = await verb_dispatch.dispatch_mission(
        "no_such_action", project_id="prj_test",
    )
    parsed = json.loads(out)
    assert parsed["error"] == "invalid_action"
    assert recorder.calls == []


# ---------------------------------------------------------------------------
# rka_checkpoint — content/description/message alias on submit
# ---------------------------------------------------------------------------


async def test_dispatch_checkpoint_submit_description_alias(
    recorder: _Recorder,
) -> None:
    """The canonical submit body field is description; both content
    and description flow through into rka_submit_checkpoint (the
    Phase-X²' Layer 1 alias)."""
    await verb_dispatch.dispatch_checkpoint(
        "submit",
        project_id="prj_test",
        mission_id="mis_x",
        type="design",
        description="canonical body",
    )
    assert recorder.calls
    _, kw = recorder.calls[-1]
    assert kw["description"] == "canonical body"


async def test_dispatch_checkpoint_submit_content_alias_passes_through(
    recorder: _Recorder,
) -> None:
    """content= is accepted and passed through to rka_submit_checkpoint
    where the underlying tool's alias logic accepts it."""
    await verb_dispatch.dispatch_checkpoint(
        "submit",
        project_id="prj_test",
        mission_id="mis_x",
        type="design",
        content="alias body",
    )
    assert recorder.calls
    _, kw = recorder.calls[-1]
    # Either content or description ended up populated — content
    # alias is preserved up the chain.
    assert kw.get("content") == "alias body" or kw.get("description") == "alias body"


async def test_dispatch_checkpoint_submit_requires_mission_id(
    recorder: _Recorder,
) -> None:
    out = await verb_dispatch.dispatch_checkpoint(
        "submit",
        project_id="prj_test",
        type="design",
        description="x",
        # no mission_id
    )
    parsed = json.loads(out)
    assert parsed["error"] == "missing_field"
    assert recorder.calls == []


async def test_dispatch_checkpoint_resolve_routes(
    recorder: _Recorder,
) -> None:
    await verb_dispatch.dispatch_checkpoint(
        "resolve",
        project_id="prj_test",
        id="chk_1",
        resolution="approve",
        resolved_by="pi",
    )
    tools = [t for t, _ in recorder.calls]
    assert "rka_resolve_checkpoint" in tools


async def test_dispatch_checkpoint_present_decision_routes(
    recorder: _Recorder,
) -> None:
    await verb_dispatch.dispatch_checkpoint(
        "present_decision",
        project_id="prj_test",
        decision_id="dec_x",
        confirmation_brief="brief",
        options=[{"label": "A"}],
    )
    tools = [t for t, _ in recorder.calls]
    assert "rka_present_decision" in tools


async def test_dispatch_checkpoint_unknown_action(
    recorder: _Recorder,
) -> None:
    out = await verb_dispatch.dispatch_checkpoint(
        "no_such_action", project_id="prj_test",
    )
    parsed = json.loads(out)
    assert parsed["error"] == "invalid_action"
    assert recorder.calls == []


# ---------------------------------------------------------------------------
# rka_review — target dispatcher
# ---------------------------------------------------------------------------


async def test_dispatch_review_note_update_routes(
    recorder: _Recorder,
) -> None:
    await verb_dispatch.dispatch_review(
        "note_update",
        project_id="prj_test",
        payload={"id": "jrn_1", "content": "new"},
    )
    tools = [t for t, _ in recorder.calls]
    assert "rka_update_note" in tools


async def test_dispatch_review_eviction_sweep_routes(
    recorder: _Recorder,
) -> None:
    await verb_dispatch.dispatch_review(
        "eviction_sweep",
        project_id="prj_test",
        payload={"dry_run": True},
    )
    tools = [t for t, _ in recorder.calls]
    assert "rka_eviction_sweep" in tools


async def test_dispatch_review_unknown_target(
    recorder: _Recorder,
) -> None:
    out = await verb_dispatch.dispatch_review(
        "not_a_target",
        project_id="prj_test",
        payload={},
    )
    parsed = json.loads(out)
    assert parsed["error"] == "invalid_target"
    assert recorder.calls == []


async def test_dispatch_review_note_update_requires_id(
    recorder: _Recorder,
) -> None:
    out = await verb_dispatch.dispatch_review(
        "note_update",
        project_id="prj_test",
        payload={"content": "x"},  # no id
    )
    parsed = json.loads(out)
    assert parsed["error"] == "missing_field"
    assert recorder.calls == []


# ---------------------------------------------------------------------------
# rka_session — unscoped actions
# ---------------------------------------------------------------------------


async def test_dispatch_session_list_projects_routes(
    recorder: _Recorder,
) -> None:
    await verb_dispatch.dispatch_session("list_projects")
    tools = [t for t, _ in recorder.calls]
    assert "rka_list_projects" in tools


async def test_dispatch_session_reset_routes(
    recorder: _Recorder,
) -> None:
    await verb_dispatch.dispatch_session("reset")
    tools = [t for t, _ in recorder.calls]
    assert "rka_reset_session" in tools


async def test_dispatch_session_create_project_requires_name(
    recorder: _Recorder,
) -> None:
    out = await verb_dispatch.dispatch_session("create_project")
    parsed = json.loads(out)
    assert parsed["error"] == "missing_field"
    assert recorder.calls == []


async def test_dispatch_session_unknown_action(
    recorder: _Recorder,
) -> None:
    out = await verb_dispatch.dispatch_session("no_such_action")
    parsed = json.loads(out)
    assert parsed["error"] == "invalid_action"
    assert recorder.calls == []


async def test_dispatch_session_digest_requires_project_id(
    recorder: _Recorder,
) -> None:
    """digest is project-scoped despite being session-flavored."""
    out = await verb_dispatch.dispatch_session("digest")
    parsed = json.loads(out)
    assert parsed["error"] == "missing_field"
    assert "project_id" in parsed["message"]


async def test_dispatch_session_generate_claude_md_requires_project_id(
    recorder: _Recorder,
) -> None:
    out = await verb_dispatch.dispatch_session("generate_claude_md")
    parsed = json.loads(out)
    assert parsed["error"] == "missing_field"
    assert "project_id" in parsed["message"]
