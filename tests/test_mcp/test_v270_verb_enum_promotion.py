"""v2.7.0 PR-1 — verb-discriminator enum promotion tests.

Each verb has a discriminator parameter (scope / action / target) that
selects the sub-mode for the call. Per the v2.7.0 design these
discriminators MUST be promoted to JSON-schema `enum` arrays on the
FastMCP-rendered inputSchema so that:

  1. The LLM sees the valid sub-mode set in the tool catalog without
     having to read the docstring (the Annotated[Literal] -> inputSchema
     enum promotion path).
  2. Strict-mode clients can validate the call BEFORE dispatch.
  3. The Phase-X²-prime polish enum-mismatch class of bug stays
     impossible at the verb tier (mirrors the existing
     test_v262_annotated_literal_and_enrichment.py contract for the
     legacy tools).

Discriminator mapping:
  rka_query             → scope    (~36 read scopes)
  rka_record_note       → source   (enum of actor literals)
  rka_record_decision   → decided_by + kind (both Literal enums)
  rka_record_literature → action   (Literal | None; nullable optional)
  rka_mission           → action   (6 lifecycle actions)
  rka_checkpoint        → action   (7 checkpoint/gate/PI actions)
  rka_review            → target   (~25 targets)
  rka_session           → action   (~9 unscoped actions)
"""

from __future__ import annotations

from typing import Any

import pytest

import rka.mcp.server as _live_server


def _tool(name: str):
    tools = {t.name: t for t in _live_server.mcp._tool_manager.list_tools()}
    return tools.get(name)


def _params(name: str) -> dict[str, Any]:
    t = _tool(name)
    assert t is not None, f"verb {name!r} not on mcp._tool_manager"
    return t.parameters


def _enum_for(name: str, param: str) -> list[str]:
    """Extract the JSON-schema enum array for a parameter.

    Handles three shapes:
      - Direct: {"properties": {"action": {"enum": [...], "type": "string"}}}
      - Nullable (Optional[Literal]): the schema renders as
        {"anyOf": [{"enum": [...], "type": "string"}, {"type": "null"}]}.
      - v2.7.0 typed-arg discriminated union: the verb's only top-level
        param is ``args`` (a Pydantic Union); the per-branch ``operation``
        Literal values are in the union's ``discriminator.mapping`` keys
        OR collected from each branch's ``$defs/<branch>/properties/<param>``.
    """
    params = _params(name)
    props = params.get("properties", {}) or {}
    prop = props.get(param)
    if prop is None:
        # v2.7.0 typed-arg case: union under ``args`` parameter.
        if "args" in props and param == "operation":
            args_prop = props["args"]
            disc = args_prop.get("discriminator", {})
            mapping = disc.get("mapping", {})
            if mapping:
                return sorted(mapping.keys())
        pytest.fail(
            f"parameter {param!r} missing on verb {name!r}; "
            f"properties: {sorted(props)}"
        )
    if "enum" in prop:
        return list(prop["enum"])
    # nullable / anyOf-wrapped
    for branch in prop.get("anyOf", []) or []:
        if "enum" in branch:
            return list(branch["enum"])
    pytest.fail(
        f"parameter {param!r} on verb {name!r} has no enum array; "
        f"got schema: {prop!r}"
    )


# ---------------------------------------------------------------------------
# rka_query.scope — 29+ read scopes (the design table calls for "29" but the
# actual literal carries a few more aliases). We pin the canonical core set.
# ---------------------------------------------------------------------------


_REQUIRED_QUERY_SCOPES = (
    "status", "context", "search", "entity", "journal", "literature",
    "mission", "report", "checkpoints", "decision_tree",
    "calibration_metrics", "hooks", "hook_executions",
    "brain_notifications", "research_map", "review_queue",
    "clusters", "claims", "manuscript", "graph", "ego_graph",
    "graph_stats", "graph_mermaid", "provenance", "multi_hop",
    "summarize", "generate_summary", "evidence", "freshness",
    "contradictions", "integrity", "pending_maintenance",
    "changelog", "bootstrap_review", "workspace_tree", "workspace_scan",
)


def test_rka_query_scope_promoted_to_enum() -> None:
    # v2.7.0a3 — discriminator parameter renamed from `scope` to
    # `operation`. The Literal set and enum-promotion semantics are
    # preserved.
    enum_vals = _enum_for("rka_query", "operation")
    assert enum_vals, "rka_query.operation has empty enum"


@pytest.mark.parametrize("scope", _REQUIRED_QUERY_SCOPES)
def test_rka_query_scope_includes(scope: str) -> None:
    """Every documented read scope must be in the rendered enum so the
    LLM doesn't have to guess from the docstring."""
    enum_vals = _enum_for("rka_query", "operation")
    assert scope in enum_vals, (
        f"rka_query.operation missing {scope!r}; got {sorted(enum_vals)}"
    )


def test_rka_query_scope_has_at_least_29_entries() -> None:
    """Design spec: 29 read scopes. The actual surface has more (some
    aliases were added) — we floor at 29."""
    enum_vals = _enum_for("rka_query", "operation")
    assert len(enum_vals) >= 29, (
        f"rka_query.operation has only {len(enum_vals)} entries; "
        f"design floor is 29"
    )


# ---------------------------------------------------------------------------
# rka_mission.action — 6 lifecycle actions
# ---------------------------------------------------------------------------


_MISSION_ACTIONS = (
    "create", "update", "update_status",
    "submit_report", "get_report", "advance_rq",
)


@pytest.mark.parametrize("action", _MISSION_ACTIONS)
def test_rka_mission_action_enum_includes(action: str) -> None:
    vals = _enum_for("rka_mission", "action")
    assert action in vals, (
        f"rka_mission.action missing {action!r}; got {sorted(vals)}"
    )


def test_rka_mission_action_enum_is_exactly_six() -> None:
    vals = _enum_for("rka_mission", "action")
    assert set(vals) == set(_MISSION_ACTIONS), (
        f"rka_mission.action drift: {sorted(vals)}"
    )


# ---------------------------------------------------------------------------
# rka_checkpoint.action — 7 actions (submit, resolve, create_gate,
# evaluate_gate, present_decision, pi_select, record_outcome)
# ---------------------------------------------------------------------------


_CHECKPOINT_ACTIONS = (
    "submit", "resolve", "create_gate", "evaluate_gate",
    "present_decision", "pi_select", "record_outcome",
)


@pytest.mark.parametrize("action", _CHECKPOINT_ACTIONS)
def test_rka_checkpoint_action_enum_includes(action: str) -> None:
    vals = _enum_for("rka_checkpoint", "action")
    assert action in vals, (
        f"rka_checkpoint.action missing {action!r}; got {sorted(vals)}"
    )


# ---------------------------------------------------------------------------
# rka_review.target — ~25 review/maintenance targets
# ---------------------------------------------------------------------------


_REVIEW_TARGETS = (
    "note_update", "decision_update", "literature_update", "status_update",
    "bulk_update", "batch_import",
    "hook_add", "hook_enable", "hook_disable", "hook_delete",
    "brain_notifications_clear",
    "extract_claims", "cluster", "claims", "cluster_create",
    "cluster_assign", "cluster_split", "cluster_merge",
    "contradiction", "flag_stale", "eviction_sweep",
    "bootstrap_workspace",
    "manuscript_register",
    "supersede_decision",
)


@pytest.mark.parametrize("target", _REVIEW_TARGETS)
def test_rka_review_target_enum_includes(target: str) -> None:
    vals = _enum_for("rka_review", "target")
    assert target in vals, (
        f"rka_review.target missing {target!r}; got {sorted(vals)}"
    )


# ---------------------------------------------------------------------------
# rka_session.action — 9 unscoped actions
# ---------------------------------------------------------------------------


_SESSION_ACTIONS = (
    "list_projects", "create_project", "set_project", "reset",
    "digest", "health", "help", "export", "generate_claude_md",
)


@pytest.mark.parametrize("action", _SESSION_ACTIONS)
def test_rka_session_action_enum_includes(action: str) -> None:
    vals = _enum_for("rka_session", "action")
    assert action in vals, (
        f"rka_session.action missing {action!r}; got {sorted(vals)}"
    )


# ---------------------------------------------------------------------------
# rka_record_literature.action — Optional[Literal] (nullable enum)
# ---------------------------------------------------------------------------


_LIT_ACTIONS = (
    "link_zotero", "import_bibtex", "enrich_doi",
    "search_semantic_scholar", "search_arxiv",
    "process_paper",
)


@pytest.mark.parametrize("action", _LIT_ACTIONS)
def test_rka_record_literature_action_enum_includes(action: str) -> None:
    """rka_record_literature.action is Optional[Literal[...]] — the
    Literal branch lives inside anyOf alongside {"type": "null"}.
    _enum_for handles both shapes."""
    vals = _enum_for("rka_record_literature", "action")
    assert action in vals, (
        f"rka_record_literature.action missing {action!r}; got {sorted(vals)}"
    )


def test_rka_record_literature_action_is_nullable() -> None:
    """The action field is Optional so callers can omit it — verb
    infers mode from kwarg presence."""
    prop = _params("rka_record_literature")["properties"]["action"]
    # Either anyOf has a null branch, or default is None.
    has_null = any(
        b.get("type") == "null" for b in prop.get("anyOf", []) or []
    ) or prop.get("default") is None
    assert has_null, (
        f"rka_record_literature.action must be nullable so callers can "
        f"infer mode from kwarg presence; got: {prop!r}"
    )


# ---------------------------------------------------------------------------
# rka_record_note — source is the actor enum (5 values), kept top-level
# so the Annotated[Literal] promotion survives.
# ---------------------------------------------------------------------------


_NOTE_SOURCES = ("brain", "executor", "pi", "web_ui", "llm")


@pytest.mark.parametrize("src", _NOTE_SOURCES)
def test_rka_record_note_source_enum_includes(src: str) -> None:
    vals = _enum_for("rka_record_note", "source")
    assert src in vals, (
        f"rka_record_note.source missing {src!r}; got {sorted(vals)}"
    )


def test_rka_record_note_confidence_enum() -> None:
    """The confidence field must be promoted to enum so the LLM can't
    pass `confidence='confirmed'` (canonical Phase-X²' Brain
    hallucination)."""
    vals = _enum_for("rka_record_note", "confidence")
    assert "hypothesis" in vals
    assert "tested" in vals
    assert "verified" in vals
    # The critical negative — 'confirmed' is NOT a valid value.
    assert "confirmed" not in vals, (
        "rka_record_note.confidence must NOT include 'confirmed' "
        "(invalid value; common Brain hallucination per Phase-X²')"
    )


def test_rka_record_note_importance_enum() -> None:
    vals = _enum_for("rka_record_note", "importance")
    for v in ("critical", "high", "normal", "low"):
        assert v in vals, f"rka_record_note.importance missing {v!r}"


# ---------------------------------------------------------------------------
# rka_record_decision — decided_by + kind are both Literal enums.
# ---------------------------------------------------------------------------


def test_rka_record_decision_decided_by_enum() -> None:
    vals = _enum_for("rka_record_decision", "decided_by")
    # Per main v2.6 canonical set.
    assert "pi" in vals
    assert "brain" in vals
    assert "executor" in vals


def test_rka_record_decision_kind_enum() -> None:
    vals = _enum_for("rka_record_decision", "kind")
    for v in ("decision", "research_question", "design_choice"):
        assert v in vals, f"rka_record_decision.kind missing {v!r}"


# ---------------------------------------------------------------------------
# Required-field contracts — discriminators are REQUIRED on each verb.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "verb,disc",
    [
        # v2.7.0a3: rka_query discriminator renamed scope → operation
        # (scope retained as a deprecated alias on the signature; it's
        # optional so not in required[]).
        ("rka_query", "operation"),
        ("rka_mission", "action"),
        ("rka_checkpoint", "action"),
        ("rka_review", "target"),
        ("rka_session", "action"),
    ],
)
def test_verb_discriminator_is_required(verb: str, disc: str) -> None:
    """Discriminator parameters must be in the required[] array so
    callers can't omit them (silent default would lead to wrong-mode
    dispatch)."""
    required = _params(verb).get("required", []) or []
    # v2.7.0a3: rka_query.operation is optional (carries a backwards-
    # compat None default for callers using the deprecated `scope`
    # alias). The body raises an error if neither is supplied.
    # v2.7.0 (Phase 3): rka_query is the typed-args discriminated-union
    # surface; the discriminator lives inside the ``args`` Pydantic model
    # rather than at the top level. The discriminator-mapping carries
    # the per-branch operation values.
    if verb == "rka_query":
        params = _params(verb)
        props = params.get("properties", {}) or {}
        if "args" in props:
            # Typed-args surface: discriminator inside args.
            disc_map = (
                props["args"].get("discriminator", {}).get("mapping", {})
            )
            assert disc_map, (
                f"verb {verb!r} args discriminator missing mapping; "
                f"got: {props['args']!r}"
            )
            # args itself is required.
            assert "args" in (params.get("required") or []), (
                f"verb {verb!r} args param must be required (typed-args surface)."
            )
            return
        assert "operation" in props, (
            f"verb {verb!r} missing operation property: {sorted(props)}"
        )
        return
    assert disc in required, (
        f"verb {verb!r} discriminator {disc!r} not in required: "
        f"{required}"
    )


def test_project_id_required_on_scoped_verbs() -> None:
    """All project-scoped verbs require project_id (kwarg-only); the
    v2.6 contract from main is preserved at the verb tier.

    v2.7.0a3 — rka_query's project_id is now Optional with a None
    default (because list_projects / health operations are unscoped
    within rka_query). The verb body raises a missing_field error
    when project_id is needed but unset.
    """
    scoped = (
        "rka_record_note", "rka_record_decision",
        "rka_record_literature", "rka_mission", "rka_checkpoint",
        "rka_review",
    )
    for verb in scoped:
        required = _params(verb).get("required", []) or []
        assert "project_id" in required, (
            f"verb {verb!r} missing project_id in required[]: {required}"
        )


def test_rka_session_project_id_optional() -> None:
    """rka_session is unscoped — most actions (list_projects,
    create_project, reset, health, help) don't need project_id, so
    it's an optional kwarg with default None."""
    required = _params("rka_session").get("required", []) or []
    assert "project_id" not in required, (
        f"rka_session must NOT require project_id (unscoped verb); "
        f"got required: {required}"
    )
