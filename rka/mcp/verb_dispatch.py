"""v2.7.0 PR-1 verb dispatcher helpers.

Thin adapters that route the 8 always-on v2.7.0 verbs to existing legacy
@tool(...) functions defined in rka/mcp/server.py. By design we never
duplicate REST-call code here — we look up the legacy function in
``_TOOL_REGISTRY`` (the registry the @tool decorator populates) and
invoke it with normalized kwargs.

This module is consumed by the verb bodies in rka/mcp/server.py and is
not itself a FastMCP-tooled module. All public helpers are sync wrappers
that call into legacy async tools — they MUST be awaited by the verb
bodies.

Design constraint: the legacy 91 tools stay UNCHANGED in PR-1. PR-2
demotes their tier and (optionally) deprecates them; until then both
surfaces coexist.

Bookkeeper invariant: this module lives under rka/, so it goes to
``main`` like the rest of rka/. Orchestrator never imports from here.

---

THE 8 VERBS:
  1. rka_query(scope, *, project_id, id?, query?, limit?, filters?, options?)
     — project-scoped read operations
  2. rka_record_note(content, *, project_id, source, type, ...)
  3. rka_record_decision(question, chosen, rationale, *, project_id, ...)
  4. rka_record_literature(*, project_id, title?|bibtex?|search_query?|doi?,
     action?, ...) — 8 modes
  5. rka_mission(action, *, project_id, ...) — 6 actions
  6. rka_checkpoint(action, *, project_id, ...) — 8 actions
  7. rka_review(target, *, project_id, payload) — ~24 targets
  8. rka_session(action, ...) — UNSCOPED, ~9 actions

HYBRID design (from workflow w2cnkgz0k synthesis):
  - Design B (7 mid-grain verbs + 1 unscoped) as spine.
  - Graft A: provenance is a first-class top-level kwarg (not nested
    inside a body dict). The dispatcher unpacks
    `provenance={related_decisions: [...], related_literature: [...],
    related_mission: ..., supersedes: ..., related_journal: [...],
    motivated_by_decision: ...}` into the REST endpoint's flat fields.
  - Graft C: every verb description leads with role tag
    [BRAIN]/[EXECUTOR]/[PI]/[ANY] (lives on the verb @tool decorator
    docstrings; this module's dispatchers are role-neutral).

PHASE-X²' provenance enforcement (carried over from the orchestrator):
  - rka_record_note(source='pi', ...) requires verbatim_input.
  - rka_record_decision(...) requires related_journal as non-empty list.
  - rka_mission(action='create', ...) requires
    provenance['motivated_by_decision'].
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)


def _registry() -> dict:
    """Late-import the registry to avoid circular import at module load.

    rka/mcp/server.py imports from this module; this module needs the
    registry that server.py populates. Late binding sidesteps the cycle.
    """
    from rka.mcp.server import _TOOL_REGISTRY

    return _TOOL_REGISTRY


def _legacy(name: str):
    """Look up a legacy tool's wrapped callable from the registry."""
    reg = _registry()
    rec = reg.get(name)
    if rec is None:
        raise ValueError(
            f"v2.7.0 verb dispatch: legacy tool {name!r} not in registry "
            f"(known: {sorted(reg)[:5]}... total={len(reg)})"
        )
    return rec["fn"]


def _err(error_code: str, message: str, **extra: Any) -> str:
    """Render a structured verb-level error as JSON for the LLM caller."""
    body: dict[str, Any] = {"error": error_code, "message": message}
    body.update(extra)
    return json.dumps(body, indent=2)


# ---------------------------------------------------------------------------
# rka_record_literature dispatch
# ---------------------------------------------------------------------------


async def dispatch_record_literature(
    *,
    project_id: str,
    title: str | None,
    bibtex: str | None,
    search_query: str | None,
    search_source: str | None,
    doi: str | None,
    authors: list[str] | None,
    year_min: int | None,
    venue: str | None,
    status: str,
    abstract: str | None,
    url: str | None,
    tags: list[str] | None,
    related_decisions: list[str] | None,
    action: str | None,
    lit_id: str | None,
    manuscript_id: str | None,
    zotero_key: str | None,
    pdf_path: str | None,
    annotations: list[dict] | None,
    summary: str | None,
    add_to_library: bool,
    limit: int,
    import_top_n: int | None = None,
    year: int | None = None,
) -> str:
    """Route to one of the literature sub-modes by kwarg presence + action.

    Modes (priority):
      1. action='link_zotero' (id required)
      2. action='import_bibtex' OR bibtex provided (no action)
      3. action='enrich_doi' OR (doi only, no title/authors)
      4. action='search_semantic_scholar' OR search_source='semantic_scholar'
      5. action='search_arxiv' OR search_source='arxiv'
      6. action='process_paper' (lit_id + annotations)
      7. Default: explicit-create via title

    Phase v2.7.0a2 enum/alias resolution:
      - `year` is the deprecated alias of `year_min` (one-release deprecation
        window per Decision 3 / Option A). Supplying both is an error.
      - `import_top_n` caps the import slice on search modes when
        add_to_library=True (None = import all returned).
    """
    # ------------------------------------------------------------------
    # v2.7.0a2 Decision 3: year → year_min deprecation alias.
    # If only legacy `year` is set, back-fill year_min. If BOTH are set,
    # reject as ambiguous so callers don't silently drop the wrong one.
    # ------------------------------------------------------------------
    if year is not None and year_min is not None:
        return _err(
            "conflicting_args",
            "rka_record_literature: pass only year_min — "
            "year is the deprecated alias (one-release deprecation; "
            "removal scheduled v2.8)",
        )
    if year is not None and year_min is None:
        year_min = year

    # Explicit action wins
    if action == "link_zotero":
        if not lit_id:
            return _err(
                "missing_field",
                "action='link_zotero' requires lit_id",
            )
        # v2.7.0.2 (Bug 3 fix): thread `zotero_key` through. Pre-fix the
        # legacy tool didn't accept it; if supplied, it was silently
        # dropped here and the linker ran fuzzy matching anyway. Now
        # supplied keys bypass matching and validate via direct GET on
        # /items/<key>.
        return await _legacy("rka_link_literature_to_zotero")(
            id=lit_id, project_id=project_id, zotero_key=zotero_key
        )

    if action == "import_bibtex" or (action is None and bibtex):
        if not bibtex:
            return _err(
                "missing_field",
                "action='import_bibtex' requires bibtex",
            )
        return await _legacy("rka_import_bibtex")(
            bibtex=bibtex,
            default_status=status,
            project_id=project_id,
        )

    if action == "enrich_doi":
        if not lit_id:
            return _err(
                "missing_field",
                "action='enrich_doi' requires lit_id",
            )
        return await _legacy("rka_enrich_doi")(lit_id=lit_id, project_id=project_id)

    if action == "search_semantic_scholar" or search_source == "semantic_scholar":
        q = search_query
        if not q:
            return _err(
                "missing_field",
                "semantic_scholar search requires search_query",
            )
        return await _legacy("rka_search_semantic_scholar")(
            query=q,
            limit=limit or 10,
            year_min=year_min,
            add_to_library=add_to_library,
            import_top_n=import_top_n,
            project_id=project_id,
        )

    if action == "search_arxiv" or search_source == "arxiv":
        q = search_query
        if not q:
            return _err(
                "missing_field",
                "arxiv search requires search_query",
            )
        return await _legacy("rka_search_arxiv")(
            query=q,
            limit=limit or 10,
            year_min=year_min,
            add_to_library=add_to_library,
            import_top_n=import_top_n,
            project_id=project_id,
        )

    if action == "process_paper":
        if not lit_id or annotations is None:
            return _err(
                "missing_field",
                "action='process_paper' requires lit_id + annotations",
            )
        return await _legacy("rka_process_paper")(
            lit_id=lit_id,
            annotations=annotations,
            summary=summary,
            project_id=project_id,
        )

    # Default: title-based add. DOI-only (no title/authors) is also OK if
    # the user is bootstrapping a row to enrich later.
    if not title and not doi:
        return _err(
            "missing_field",
            "rka_record_literature: provide one of title, bibtex, doi, or "
            "search_query+search_source (or set action=...)",
        )

    return await _legacy("rka_add_literature")(
        title=title or f"(DOI: {doi})",
        authors=authors,
        # On default-add (one-paper mode) the year-min is also the paper's
        # publication year — a single-element set's floor equals the
        # element. The rka_add_literature legacy tool's contract is
        # unchanged (still takes `year` for the paper's pub year), so the
        # bookkeeper invariant on rka/ services is preserved.
        year=year_min,
        venue=venue,
        doi=doi,
        url=url,
        abstract=abstract,
        added_by="brain",
        status=status,
        tags=tags,
        related_decisions=related_decisions,
        project_id=project_id,
    )


# ---------------------------------------------------------------------------
# rka_mission dispatch — 6 actions
# ---------------------------------------------------------------------------


async def dispatch_mission(action: str, *, project_id: str, **kw: Any) -> str:
    """Discriminated dispatcher: action ∈ {create | update | update_status |
    submit_report | get_report | advance_rq}.

    Provenance: action='create' requires
    ``provenance.motivated_by_decision`` (Graft A surfaces it via a
    top-level provenance kwarg).
    """
    if action == "create":
        provenance = kw.get("provenance") or {}
        motivated_by = kw.get("motivated_by_decision") or provenance.get("motivated_by_decision")
        if not motivated_by:
            return _err(
                "missing_provenance",
                "rka_mission(action='create') requires "
                "provenance.motivated_by_decision (or top-level "
                "motivated_by_decision kwarg) — preserves the decision -> "
                "mission causality chain",
            )
        return await _legacy("rka_create_mission")(
            phase=kw.get("phase", "execution"),
            objective=kw["objective"],
            tasks=kw.get("tasks"),
            context=kw.get("context"),
            acceptance_criteria=kw.get("acceptance_criteria"),
            scope_boundaries=kw.get("scope_boundaries"),
            checkpoint_triggers=kw.get("checkpoint_triggers"),
            depends_on=kw.get("depends_on"),
            motivated_by_decision=motivated_by,
            tags=kw.get("tags"),
            project_id=project_id,
        )

    if action == "update":
        if not kw.get("mission_id"):
            return _err("missing_field", "action='update' requires mission_id")
        return await _legacy("rka_update_mission")(
            id=kw["mission_id"],
            phase=kw.get("phase"),
            objective=kw.get("objective"),
            context=kw.get("context"),
            acceptance_criteria=kw.get("acceptance_criteria"),
            scope_boundaries=kw.get("scope_boundaries"),
            checkpoint_triggers=kw.get("checkpoint_triggers"),
            depends_on=kw.get("depends_on"),
            parent_mission_id=kw.get("parent_mission_id"),
            motivated_by_decision=kw.get("motivated_by_decision"),
            tags=kw.get("tags"),
            project_id=project_id,
        )

    if action == "update_status":
        if not kw.get("mission_id") or not kw.get("status"):
            return _err(
                "missing_field",
                "action='update_status' requires mission_id + status",
            )
        return await _legacy("rka_update_mission_status")(
            id=kw["mission_id"],
            status=kw["status"],
            tasks=kw.get("tasks"),
            project_id=project_id,
        )

    if action == "submit_report":
        if not kw.get("mission_id"):
            return _err(
                "missing_field",
                "action='submit_report' requires mission_id",
            )
        return await _legacy("rka_submit_report")(
            mission_id=kw["mission_id"],
            summary=kw.get("summary"),
            findings=kw.get("findings", ""),
            anomalies=kw.get("anomalies", ""),
            questions=kw.get("questions", ""),
            codebase_state=kw.get("codebase_state", ""),
            recommended_next=kw.get("recommended_next", ""),
            content=kw.get("content"),
            project_id=project_id,
        )

    if action == "get_report":
        return await _legacy("rka_get_report")(
            mission_id=kw.get("mission_id"),
            project_id=project_id,
        )

    if action == "advance_rq":
        if not kw.get("rq_id") or not kw.get("status"):
            return _err(
                "missing_field",
                "action='advance_rq' requires rq_id + status",
            )
        return await _legacy("rka_advance_rq")(
            rq_id=kw["rq_id"],
            status=kw["status"],
            conclusion=kw.get("conclusion"),
            evidence_cluster_ids=kw.get("evidence_cluster_ids"),
            project_id=project_id,
        )

    return _err(
        "invalid_action",
        f"rka_mission: unknown action {action!r}; expected "
        "create|update|update_status|submit_report|get_report|advance_rq",
    )


# ---------------------------------------------------------------------------
# rka_checkpoint dispatch — 8 actions
# ---------------------------------------------------------------------------


async def dispatch_checkpoint(action: str, *, project_id: str, **kw: Any) -> str:
    """Discriminated dispatcher: action ∈ {submit | resolve | create_gate |
    evaluate_gate | present_decision | pi_select | record_outcome}.
    """
    if action == "submit":
        if not kw.get("mission_id") or not kw.get("type"):
            return _err(
                "missing_field",
                "action='submit' requires mission_id + type",
            )
        return await _legacy("rka_submit_checkpoint")(
            mission_id=kw["mission_id"],
            type=kw["type"],
            description=kw.get("description"),
            task_reference=kw.get("task_reference"),
            context=kw.get("context"),
            options=kw.get("options"),
            recommendation=kw.get("recommendation"),
            blocking=kw.get("blocking", True),
            content=kw.get("content"),
            project_id=project_id,
        )

    if action == "resolve":
        cid = kw.get("id") or kw.get("checkpoint_id")
        if not cid or not kw.get("resolution") or not kw.get("resolved_by"):
            return _err(
                "missing_field",
                "action='resolve' requires id + resolution + resolved_by",
            )
        return await _legacy("rka_resolve_checkpoint")(
            id=cid,
            resolution=kw["resolution"],
            resolved_by=kw["resolved_by"],
            rationale=kw.get("rationale"),
            create_decision=kw.get("create_decision", False),
            project_id=project_id,
        )

    if action == "create_gate":
        for f in ("mission_id", "gate_type", "deliverables", "pass_criteria"):
            if kw.get(f) in (None, ""):
                return _err(
                    "missing_field",
                    f"action='create_gate' requires {f}",
                )
        return await _legacy("rka_create_gate")(
            mission_id=kw["mission_id"],
            gate_type=kw["gate_type"],
            deliverables=kw["deliverables"],
            pass_criteria=kw["pass_criteria"],
            assumptions_to_verify=kw.get("assumptions_to_verify"),
            project_id=project_id,
        )

    if action == "evaluate_gate":
        if not kw.get("gate_id") or not kw.get("verdict") or not kw.get("notes"):
            return _err(
                "missing_field",
                "action='evaluate_gate' requires gate_id + verdict + notes",
            )
        return await _legacy("rka_evaluate_gate")(
            gate_id=kw["gate_id"],
            verdict=kw["verdict"],
            notes=kw["notes"],
            assumption_status=kw.get("assumption_status"),
            project_id=project_id,
        )

    if action == "present_decision":
        dec_id = kw.get("decision_id") or kw.get("id")
        if not dec_id:
            return _err(
                "missing_field",
                "action='present_decision' requires decision_id",
            )
        if not kw.get("confirmation_brief") or kw.get("options") is None:
            return _err(
                "missing_field",
                "action='present_decision' requires confirmation_brief + options",
            )
        return await _legacy("rka_present_decision")(
            decision_id=dec_id,
            confirmation_brief=kw["confirmation_brief"],
            options=kw["options"],
            pi_preference=kw.get("pi_preference"),
            project_id=project_id,
        )

    if action == "pi_select":
        dec_id = kw.get("decision_id") or kw.get("id")
        if not dec_id:
            return _err(
                "missing_field",
                "action='pi_select' requires decision_id",
            )
        return await _legacy("rka_record_pi_selection")(
            decision_id=dec_id,
            selected_option_id=kw.get("selected_option_id"),
            override_rationale=kw.get("override_rationale"),
            project_id=project_id,
        )

    if action == "record_outcome":
        dec_id = kw.get("decision_id") or kw.get("id")
        if not dec_id or not kw.get("outcome"):
            return _err(
                "missing_field",
                "action='record_outcome' requires decision_id + outcome",
            )
        return await _legacy("rka_record_outcome")(
            decision_id=dec_id,
            outcome=kw["outcome"],
            outcome_details=kw.get("outcome_details") or kw.get("lessons"),
            recorded_by=kw.get("recorded_by", "pi"),
            project_id=project_id,
        )

    return _err(
        "invalid_action",
        f"rka_checkpoint: unknown action {action!r}; expected submit|resolve|"
        "create_gate|evaluate_gate|present_decision|pi_select|record_outcome",
    )


# ---------------------------------------------------------------------------
# rka_review dispatch — ~25 targets
# ---------------------------------------------------------------------------


# Map target names to legacy tool name + payload key transform.
# Each entry: (legacy_tool_name, payload_unpacker)
# The unpacker takes (project_id, payload) and returns kwargs for the legacy.


async def dispatch_review(target: str, *, project_id: str, payload: dict[str, Any] | None) -> str:
    """Dispatcher for the rka_review verb. Routes a target string to the
    correct legacy tool, unpacking `payload` into legacy kwargs.

    Targets supported (see SEMANTIC MAPPING TABLE in spec):
      Note / decision / literature updates:
        note_update, decision_update, literature_update
      Bulk:
        bulk_update, batch_import
      Hooks (config):
        hook_add, hook_enable, hook_disable, hook_delete
      Notifications:
        brain_notifications_clear
      Claims / clusters:
        extract_claims, cluster, claims, cluster_create, cluster_assign,
        cluster_split, cluster_merge
      Contradictions / freshness / maintenance:
        contradiction, flag_stale, eviction_sweep
      Workspace:
        bootstrap_workspace
      Status / manuscript:
        status_update, manuscript_register
    """
    p = payload or {}

    if target == "note_update":
        if "capture_mode" in p:
            return _err("invalid_attribution", "capture_mode requires correct_note_attribution")
        if not p.get("id"):
            return _err("missing_field", "review note_update requires payload.id")
        return await _legacy("rka_update_note")(
            id=p["id"],
            content=p.get("content"),
            summary=p.get("summary"),
            type=p.get("type"),
            confidence=p.get("confidence"),
            importance=p.get("importance"),
            status=p.get("status"),
            pinned=p.get("pinned"),
            verbatim_input=p.get("verbatim_input"),
            related_decisions=p.get("related_decisions"),
            related_literature=p.get("related_literature"),
            related_mission=p.get("related_mission"),
            tags=p.get("tags"),
            phase=p.get("phase"),
            source=p.get("source"),
            project_id=project_id,
        )

    if target == "decision_update":
        if not p.get("id"):
            return _err("missing_field", "review decision_update requires payload.id")
        return await _legacy("rka_update_decision")(
            id=p["id"],
            status=p.get("status"),
            chosen=p.get("chosen"),
            rationale=p.get("rationale"),
            abandonment_reason=p.get("abandonment_reason"),
            kind=p.get("kind"),
            related_journal=p.get("related_journal"),
            parent_id=p.get("parent_id"),
            related_literature=p.get("related_literature"),
            related_missions=p.get("related_missions"),
            phase=p.get("phase"),
            tags=p.get("tags"),
            assumptions=p.get("assumptions"),
            project_id=project_id,
        )

    if target == "literature_update":
        if not p.get("id"):
            return _err("missing_field", "review literature_update requires payload.id")
        return await _legacy("rka_update_literature")(
            id=p["id"],
            title=p.get("title"),
            authors=p.get("authors"),
            year=p.get("year"),
            venue=p.get("venue"),
            doi=p.get("doi"),
            url=p.get("url"),
            bibtex=p.get("bibtex"),
            pdf_path=p.get("pdf_path"),
            abstract=p.get("abstract"),
            status=p.get("status"),
            key_findings=p.get("key_findings"),
            methodology_notes=p.get("methodology_notes"),
            relevance=p.get("relevance"),
            relevance_score=p.get("relevance_score"),
            related_decisions=p.get("related_decisions"),
            notes=p.get("notes"),
            tags=p.get("tags"),
            project_id=project_id,
        )

    if target == "bulk_update":
        if not p.get("updates"):
            return _err("missing_field", "review bulk_update requires payload.updates")
        return await _legacy("rka_bulk_update")(updates=p["updates"], project_id=project_id)

    if target == "batch_import":
        if not p.get("entries"):
            return _err("missing_field", "review batch_import requires payload.entries")
        actor = p.get("actor", "system")
        if actor == "import":
            actor = "system"
        return await _legacy("rka_batch_import")(
            entries=p["entries"], actor=actor, project_id=project_id
        )

    if target == "hook_add":
        for f in ("event", "handler_type", "handler_config", "name"):
            if p.get(f) is None:
                return _err("missing_field", f"hook_add payload requires {f}")
        return await _legacy("rka_add_hook")(
            event=p["event"],
            handler_type=p["handler_type"],
            handler_config=p["handler_config"],
            name=p["name"],
            enabled=p.get("enabled", True),
            created_by=p.get("created_by", "pi"),
            project_id=project_id,
        )

    if target == "hook_enable":
        hid = p.get("hook_id") or p.get("id")
        if not hid:
            return _err("missing_field", "hook_enable requires payload.hook_id")
        return await _legacy("rka_enable_hook")(hook_id=hid, project_id=project_id)

    if target == "hook_disable":
        hid = p.get("hook_id") or p.get("id")
        if not hid:
            return _err("missing_field", "hook_disable requires payload.hook_id")
        return await _legacy("rka_disable_hook")(hook_id=hid, project_id=project_id)

    if target == "hook_delete":
        hid = p.get("hook_id") or p.get("id")
        if not hid:
            return _err("missing_field", "hook_delete requires payload.hook_id")
        return await _legacy("rka_delete_hook")(hook_id=hid, project_id=project_id)

    if target == "brain_notifications_clear":
        if not p.get("ids"):
            return _err(
                "missing_field",
                "brain_notifications_clear requires payload.ids",
            )
        return await _legacy("rka_clear_brain_notifications")(ids=p["ids"], project_id=project_id)

    if target == "extract_claims":
        if not p.get("entry_id") or not p.get("claims"):
            return _err(
                "missing_field",
                "extract_claims requires payload.entry_id + payload.claims",
            )
        return await _legacy("rka_extract_claims")(
            entry_id=p["entry_id"],
            claims=p["claims"],
            project_id=project_id,
        )

    if target == "cluster":
        if not p.get("cluster_id") or not p.get("confidence") or not p.get("synthesis"):
            return _err(
                "missing_field",
                "review target='cluster' requires cluster_id + confidence + synthesis",
            )
        return await _legacy("rka_review_cluster")(
            cluster_id=p["cluster_id"],
            confidence=p["confidence"],
            synthesis=p["synthesis"],
            gaps=p.get("gaps"),
            contradictions=p.get("contradictions"),
            resolve_queue_items=p.get("resolve_queue_items"),
            research_question_id=p.get("research_question_id"),
            project_id=project_id,
        )

    if target == "claims":
        if not p.get("claim_ids"):
            return _err(
                "missing_field",
                "review target='claims' requires payload.claim_ids",
            )
        return await _legacy("rka_review_claims")(
            claim_ids=p["claim_ids"],
            action=p.get("action", "approve"),
            confidence_override=p.get("confidence_override"),
            evidence_status=p.get("evidence_status"),
            project_id=project_id,
        )

    if target == "cluster_create":
        if not p.get("label"):
            return _err(
                "missing_field",
                "cluster_create requires payload.label",
            )
        return await _legacy("rka_create_cluster")(
            label=p["label"],
            research_question_id=p.get("research_question_id"),
            synthesis=p.get("synthesis"),
            confidence=p.get("confidence", "emerging"),
            claim_ids=p.get("claim_ids"),
            project_id=project_id,
        )

    if target == "cluster_assign":
        if not p.get("cluster_id") or not p.get("claim_ids"):
            return _err(
                "missing_field",
                "cluster_assign requires cluster_id + claim_ids",
            )
        return await _legacy("rka_assign_claims_to_cluster")(
            cluster_id=p["cluster_id"],
            claim_ids=p["claim_ids"],
            project_id=project_id,
        )

    if target == "cluster_split":
        if not p.get("source_id") or not p.get("new_clusters"):
            return _err(
                "missing_field",
                "cluster_split requires source_id + new_clusters",
            )
        return await _legacy("rka_split_cluster")(
            source_id=p["source_id"],
            new_clusters=p["new_clusters"],
            project_id=project_id,
        )

    if target == "cluster_merge":
        if not p.get("source_ids") or not p.get("target_label"):
            return _err(
                "missing_field",
                "cluster_merge requires source_ids + target_label",
            )
        return await _legacy("rka_merge_clusters")(
            source_ids=p["source_ids"],
            target_label=p["target_label"],
            target_synthesis=p.get("target_synthesis"),
            research_question_id=p.get("research_question_id"),
            project_id=project_id,
        )

    if target == "contradiction":
        if not p.get("cluster_id") or not p.get("resolution"):
            return _err(
                "missing_field",
                "review target='contradiction' requires cluster_id + resolution",
            )
        return await _legacy("rka_resolve_contradiction")(
            cluster_id=p["cluster_id"],
            resolution=p["resolution"],
            claim_actions=p.get("claim_actions"),
            project_id=project_id,
        )

    if target == "flag_stale":
        if not p.get("entity_id") or not p.get("reason"):
            return _err(
                "missing_field",
                "flag_stale requires payload.entity_id + payload.reason",
            )
        return await _legacy("rka_flag_stale")(
            entity_id=p["entity_id"],
            reason=p["reason"],
            staleness=p.get("staleness", "yellow"),
            propagate=p.get("propagate", True),
            project_id=project_id,
        )

    if target == "eviction_sweep":
        return await _legacy("rka_eviction_sweep")(
            dry_run=p.get("dry_run", True), project_id=project_id
        )

    if target == "bootstrap_workspace":
        if not p.get("folder_path"):
            return _err(
                "missing_field",
                "bootstrap_workspace requires payload.folder_path",
            )
        return await _legacy("rka_bootstrap_workspace")(
            folder_path=p["folder_path"],
            phase=p.get("phase"),
            override_tags=p.get("override_tags"),
            skip_files=p.get("skip_files"),
            use_llm=p.get("use_llm", True),
            dry_run=p.get("dry_run", False),
            project_id=project_id,
        )

    if target == "status_update":
        return await _legacy("rka_update_status")(
            current_phase=p.get("current_phase") or p.get("phase"),
            summary=p.get("summary") or p.get("focus"),
            blockers=p.get("blockers"),
            metrics=p.get("metrics"),
            content=p.get("content"),
            project_id=project_id,
        )

    if target == "manuscript_register":
        if not p.get("venue") or not p.get("title"):
            return _err(
                "missing_field",
                "manuscript_register requires payload.venue + payload.title",
            )
        return await _legacy("rka_register_manuscript")(
            venue=p["venue"],
            title=p["title"],
            abstract=p.get("abstract"),
            sections=p.get("sections"),
            project_id=project_id,
        )

    if target == "supersede_decision":
        for f in ("old_decision_id", "question", "chosen", "rationale"):
            if not p.get(f):
                return _err(
                    "missing_field",
                    f"supersede_decision payload requires {f}",
                )
        # v2.7.0.5 — forward related_journal (and any other provenance fields
        # the caller may have supplied) to the adapter so the supersede path
        # has the same provenance discipline as plain rka_record_decision.
        return await _legacy("rka_supersede_decision")(
            old_decision_id=p["old_decision_id"],
            question=p["question"],
            chosen=p["chosen"],
            rationale=p["rationale"],
            decided_by=p.get("decided_by", "brain"),
            phase=p.get("phase", ""),
            kind=p.get("kind", "decision"),
            related_journal=p.get("related_journal"),
            related_literature=p.get("related_literature"),
            related_missions=p.get("related_missions"),
            parent_id=p.get("parent_id"),
            options=p.get("options"),
            assumptions=p.get("assumptions"),
            tags=p.get("tags"),
            status=p.get("status"),
            project_id=project_id,
        )

    return _err(
        "invalid_target",
        f"rka_review: unknown target {target!r}",
    )


# ---------------------------------------------------------------------------
# rka_query dispatch — project-scoped read operations
# ---------------------------------------------------------------------------
#
# Each scope maps to a legacy @tool function. Kwargs flow through normalized
# parameter names (id / query / limit / filters / options) into the
# legacy-tool signature. The scope keys are the SINGLE source of truth for
# what's queryable; mismatches surface as `invalid_scope` errors.


_QUERY_DISPATCH: dict[str, str] = {
    # always-on minimal-session-start
    "status": "rka_get_status",
    "context": "rka_get_context",
    "pending_maintenance": "rka_get_pending_maintenance",
    "checkpoints": "rka_get_checkpoints",
    "research_map": "rka_get_research_map",
    "review_queue": "rka_get_review_queue",
    # search / get-by-id
    "search": "rka_search",
    "entity": "rka_get",
    # journal / decisions / literature / missions / reports lists
    "journal": "rka_get_journal",
    "literature": "rka_get_literature",
    "report": "rka_get_report",
    "mission": "rka_get_mission",
    # decisions / graph
    "decision_tree": "rka_get_decision_tree",
    "orphan_supersedes": "rka_orphan_supersedes",
    "graph": "rka_get_graph",
    "ego_graph": "rka_get_ego_graph",
    "graph_stats": "rka_graph_stats",
    "graph_mermaid": "rka_export_mermaid",
    # claims / clusters
    "clusters": "rka_list_clusters",
    "claims": "rka_get_claims",
    "claim_scope": "rka_get_claim_scope",
    "interpretation_candidates": "rka_get_interpretation_candidates",
    "sources": "rka_get_sources",
    "note_attribution_history": "rka_get_note_attribution_history",
    "experiments": "rka_get_experiments",
    "experiment_runs": "rka_get_experiment_runs",
    "experiment_observations": "rka_get_experiment_observations",
    "planning_branches": "rka_get_planning_branches",
    "planning_resume": "rka_resume_planning",
    "planning_compare": "rka_compare_planning_branches",
    "planning_artifact_versions": "rka_get_planning_artifact_versions",
    "planning_argument_workflow": "rka_get_planning_argument_workflow",
    "planning_promotions": "rka_get_planning_promotions",
    "planning_evaluation_workflow": "rka_get_planning_evaluation_workflow",
    "planning_evaluation_events": "rka_get_planning_evaluation_events",
    "semantic_patch_proposals": "rka_get_semantic_patch_proposals",
    "semantic_patch_schema": "rka_get_semantic_patch_schema",
    # provenance / multi-hop
    "provenance": "rka_trace_provenance",
    "multi_hop": "rka_multi_hop_retrieval",
    "collect_report_context": "rka_collect_report_context",
    "staleness_impact": "rka_staleness_impact",
    "mission_guard": "rka_mission_guard",
    "belief_as_of": "rka_belief_as_of",
    "evidence": "rka_assemble_evidence",
    # session-flavored project-scoped reads
    "summarize": "rka_summarize",
    "generate_summary": "rka_generate_summary",
    "calibration_metrics": "rka_get_calibration_metrics",
    "changelog": "rka_get_changelog",
    # hooks reads
    "hooks": "rka_list_hooks",
    "hook_executions": "rka_get_hook_executions",
    "brain_notifications": "rka_get_brain_notifications",
    # maintenance / freshness reads
    "integrity": "rka_check_integrity",
    "freshness": "rka_check_freshness",
    "contradictions": "rka_detect_contradictions",
    # workspace
    "workspace_tree": "rka_scan_workspace_tree",
    "workspace_scan": "rka_scan_workspace",
    "bootstrap_review": "rka_review_bootstrap",
    # manuscript
    "manuscript": "rka_get_manuscript",
    "manuscript_context": "rka_get_manuscript_context",
    "manuscript_reference_manifest": "rka_get_manuscript_reference_manifest",
    "manuscript_readiness": "rka_get_manuscript_readiness",
    "manuscript_spine": "rka_get_manuscript_spine",
    "manuscript_outline": "rka_get_manuscript_outline",
    "manuscript_writing_candidates": "rka_get_manuscript_writing_candidates",
    "manuscript_impact": "rka_get_manuscript_impact",
    "reference_validation_status": "rka_get_reference_validation_status",
    # bounded heterogeneous resolver
    "resolve_entities": "rka_resolve_entities",
    # durable semantic change cursor
    "changes_since": "rka_changes_since",
}


async def dispatch_query(
    scope: str,
    *,
    project_id: str,
    id: str | None = None,
    query: str | None = None,
    limit: int | None = None,
    filters: dict[str, Any] | None = None,
    ids: list[str] | None = None,
    include_sources: bool = False,
    include_edges: bool = False,
    target_phase: str | None = None,
    cursor: int | None = None,
    since_cursor: int | None = None,
    manuscript_id: str | None = None,
    job_id: str | None = None,
    include_archived: bool = True,
    base_branch_id: str | None = None,
    other_branch_id: str | None = None,
) -> str:
    """[ANY] Universal read dispatcher.

    Routes a `scope` discriminator to the matching legacy @tool function,
    normalizing kwargs onto the legacy parameter names.

    Args:
        scope: One of `_QUERY_DISPATCH` keys.
        project_id: RKA project ID (prj_...).
        id: Entity ID when the scope is id-addressed (entity, mission,
            manuscript, ego_graph, provenance, bootstrap_review,
            contradictions).
        query: Query string for search-shaped scopes (search, multi_hop).
        limit: Result limit (folded into filters when applicable).
        filters: Per-scope filter dict.
        ids: Entity IDs for the bounded ``resolve_entities`` operation.
        include_sources: Include terminal claim-source closure when resolving.
        include_edges: Include same-project typed edges when resolving.
        target_phase: Lifecycle phase for ``manuscript_readiness``.
        cursor: Opaque monotonic cursor for ``changes_since``.
        since_cursor: Last synchronized cursor for ``manuscript_impact``.
        manuscript_id: Manuscript scope for ``reference_validation_status``.
        job_id: Validation-job identifier for ``reference_validation_status``.
    """
    if scope not in _QUERY_DISPATCH:
        valid = ", ".join(sorted(_QUERY_DISPATCH))
        return _err(
            "invalid_scope",
            f"rka_query: unknown scope {scope!r}",
            valid_scopes=valid,
        )
    tool_name = _QUERY_DISPATCH[scope]
    legacy = _legacy(tool_name)
    f = filters or {}

    # --- Scopes with NO kwargs other than project_id ---
    if scope in (
        "status",
        "pending_maintenance",
        "research_map",
        "graph_stats",
        "calibration_metrics",
        "integrity",
    ):
        return await legacy(project_id=project_id)

    # --- search / multi_hop ---
    if scope == "search":
        if not query:
            return _err("missing_field", "rka_query(scope='search'): query is required")
        return await legacy(
            query=query,
            entity_types=f.get("entity_types"),
            limit=limit or f.get("limit", 20),
            project_id=project_id,
        )

    if scope == "multi_hop":
        if not query and not f.get("seeds"):
            return _err(
                "missing_field",
                "rka_query(scope='multi_hop'): query or filters.seeds required",
            )
        return await legacy(
            query=query or "",
            seeds=f.get("seeds"),
            max_depth=f.get("max_depth", 3),
            max_nodes=f.get("max_nodes", 50),
            edge_weights=f.get("edge_weights"),
            project_id=project_id,
        )

    if scope == "staleness_impact":
        if not id:
            return _err(
                "missing_field", "rka_query(scope='staleness_impact'): id (entity) is required"
            )
        return await legacy(entity_id=id, max_depth=f.get("max_depth", 3), project_id=project_id)

    if scope == "mission_guard":
        if not id:
            return _err(
                "missing_field", "rka_query(scope='mission_guard'): id (mission) is required"
            )
        return await legacy(mission_id=id, project_id=project_id)

    if scope == "belief_as_of":
        if not query:
            return _err(
                "missing_field", "rka_query(scope='belief_as_of'): query (the ISO date) is required"
            )
        return await legacy(date=query, project_id=project_id)

    if scope == "collect_report_context":
        if not query:
            return _err(
                "missing_field",
                "rka_query(scope='collect_report_context'): query (the report "
                "description) is required",
            )
        return await legacy(
            description=query,
            angle_queries=f.get("angle_queries"),
            max_depth=f.get("max_depth", 2),
            max_nodes=f.get("max_nodes", 60),
            seed_limit=f.get("seed_limit", 8),
            project_id=project_id,
        )

    # --- entity (id-prefix dispatcher) ---
    if scope == "entity":
        if not id:
            return _err("missing_field", "rka_query(scope='entity'): id is required")
        return await legacy(id=id, project_id=project_id)

    # --- mission (id optional) ---
    if scope == "mission":
        return await legacy(id=id, project_id=project_id)

    # --- report (mission_id optional) ---
    if scope == "report":
        return await legacy(mission_id=id, project_id=project_id)

    # --- list-style reads with named filters ---
    if scope == "journal":
        return await legacy(
            type=f.get("type"),
            phase=f.get("phase"),
            confidence=f.get("confidence"),
            status=f.get("status"),
            since=f.get("since"),
            limit=limit or f.get("limit", 20),
            project_id=project_id,
        )

    if scope == "literature":
        return await legacy(
            status=f.get("status"),
            query=query or f.get("query"),
            limit=limit or f.get("limit", 20),
            project_id=project_id,
        )

    if scope == "checkpoints":
        return await legacy(
            status=f.get("status", "open"),
            project_id=project_id,
        )

    if scope == "review_queue":
        return await legacy(
            status=f.get("status", "pending"),
            limit=limit or f.get("limit", 20),
            project_id=project_id,
        )

    if scope == "decision_tree":
        return await legacy(
            root_id=id or f.get("root_id"),
            phase=f.get("phase"),
            active_only=f.get("active_only", False),
            project_id=project_id,
        )

    # Takes nothing but the project. It was registered in the scope->tool map
    # and listed in the operations index without a branch here, so it resolved
    # to a tool name and then fell through to `invalid_scope` — advertised and
    # uncallable. That left an asymmetry worth naming: link_supersession could
    # repair an orphaned supersede chain, but nothing could enumerate which
    # chains needed repairing.
    if scope == "orphan_supersedes":
        return await legacy(project_id=project_id)

    if scope == "graph":
        return await legacy(
            include_types=f.get("include_types"),
            phase=f.get("phase"),
            limit=limit or f.get("limit", 500),
            project_id=project_id,
        )

    if scope == "ego_graph":
        if not id:
            return _err("missing_field", "rka_query(scope='ego_graph'): id is required")
        return await legacy(
            entity_id=id,
            depth=f.get("depth", 1),
            project_id=project_id,
        )

    if scope == "graph_mermaid":
        return await legacy(
            phase=f.get("phase"),
            active_only=f.get("active_only", False),
            project_id=project_id,
        )

    if scope == "clusters":
        return await legacy(
            research_question_id=f.get("research_question_id"),
            confidence=f.get("confidence"),
            limit=limit or f.get("limit", 50),
            project_id=project_id,
        )

    if scope == "claims":
        return await legacy(
            source_entry_id=f.get("source_entry_id"),
            cluster_id=f.get("cluster_id"),
            claim_type=f.get("claim_type") or f.get("type"),
            verified=f.get("verified"),
            evidence_status=f.get("evidence_status"),
            stale=f.get("stale"),
            limit=limit or f.get("limit", 20),
            project_id=project_id,
        )

    if scope == "claim_scope":
        if not id:
            return _err(
                "missing_field",
                "rka_query(operation='claim_scope'): id is required",
            )
        return await legacy(claim_id=id, project_id=project_id)

    if scope == "interpretation_candidates":
        return await legacy(
            candidate_id=id,
            review_status=f.get("review_status"),
            disposition=f.get("disposition"),
            epistemic_kind=f.get("epistemic_kind"),
            source_type=f.get("source_type"),
            source_id=f.get("source_id"),
            limit=limit or f.get("limit", 50),
            project_id=project_id,
        )

    if scope == "sources":
        return await legacy(
            source_id=id,
            source_kind=f.get("source_kind"),
            ownership_kind=f.get("ownership_kind"),
            limit=limit or f.get("limit", 50),
            project_id=project_id,
        )

    if scope == "note_attribution_history":
        return await legacy(
            id=id, after_revision=f.get("after_revision", 0), limit=limit or 50,
            project_id=project_id,
        )

    if scope == "experiments":
        return await legacy(
            experiment_id=id,
            status=f.get("status"),
            limit=limit or f.get("limit", 50),
            project_id=project_id,
        )

    if scope == "experiment_runs":
        return await legacy(
            run_id=id,
            experiment_id=f.get("experiment_id"),
            status=f.get("status"),
            limit=limit or f.get("limit", 50),
            project_id=project_id,
        )

    if scope == "experiment_observations":
        return await legacy(
            observation_id=id,
            run_id=f.get("run_id"),
            direction=f.get("direction"),
            kind=f.get("kind"),
            claim_id=f.get("claim_id"),
            limit=limit or f.get("limit", 50),
            project_id=project_id,
        )

    if scope == "planning_branches":
        return await legacy(
            branch_id=id,
            manuscript_id=manuscript_id,
            include_archived=include_archived,
            project_id=project_id,
        )

    if scope == "planning_resume":
        return await legacy(manuscript_id=manuscript_id, project_id=project_id)

    if scope == "planning_compare":
        if not base_branch_id or not other_branch_id:
            return _err(
                "missing_field",
                "planning_compare requires base_branch_id and other_branch_id",
            )
        return await legacy(
            base_branch_id=base_branch_id,
            other_branch_id=other_branch_id,
            project_id=project_id,
        )

    if scope == "planning_artifact_versions":
        if not id:
            return _err("missing_field", "planning_artifact_versions requires id")
        return await legacy(artifact_id=id, project_id=project_id)

    if scope == "planning_argument_workflow":
        if not id:
            return _err("missing_field", "planning_argument_workflow requires id")
        return await legacy(branch_id=id, project_id=project_id)

    if scope == "planning_promotions":
        if not id:
            return _err("missing_field", "planning_promotions requires id")
        return await legacy(branch_id=id, project_id=project_id)

    if scope == "planning_evaluation_workflow":
        if not id:
            return _err("missing_field", "planning_evaluation_workflow requires id")
        return await legacy(branch_id=id, project_id=project_id)

    if scope == "planning_evaluation_events":
        if not id:
            return _err("missing_field", "planning_evaluation_events requires id")
        return await legacy(branch_id=id, project_id=project_id)

    if scope == "semantic_patch_proposals":
        return await legacy(
            proposal_id=id,
            status=f.get("status"),
            limit=limit or f.get("limit", 100),
            project_id=project_id,
        )

    if scope == "semantic_patch_schema":
        return await legacy(project_id=project_id)

    if scope == "provenance":
        if not id:
            return _err("missing_field", "rka_query(scope='provenance'): id required")
        return await legacy(
            entity_id=id,
            direction=f.get("direction", "both"),
            max_depth=f.get("max_depth", f.get("depth", 3)),
            project_id=project_id,
        )

    if scope == "evidence":
        rq_id = id or f.get("research_question_id")
        if not rq_id:
            return _err(
                "missing_field",
                "rka_query(scope='evidence'): id (research_question_id) required",
            )
        return await legacy(
            research_question_id=rq_id,
            format=f.get("format", "progress_report"),
            project_id=project_id,
        )

    if scope == "context":
        return await legacy(
            topic=query or f.get("topic"),
            phase=f.get("phase"),
            project_id=project_id,
        )

    if scope == "summarize":
        return await legacy(
            topic=query or f.get("topic"),
            phase=f.get("phase"),
            entity_ids=f.get("entity_ids"),
            project_id=project_id,
        )

    if scope == "generate_summary":
        return await legacy(
            scope_type=f.get("scope_type", "project"),
            scope_id=id or f.get("scope_id"),
            granularity=f.get("granularity", "paragraph"),
            project_id=project_id,
        )

    if scope == "changelog":
        since = f.get("since")
        if not since:
            return _err(
                "missing_field",
                "rka_query(scope='changelog'): filters.since required",
            )
        return await legacy(
            since=since,
            limit=limit or f.get("limit", 50),
            project_id=project_id,
        )

    if scope == "hooks":
        return await legacy(
            event=f.get("event"),
            enabled_only=f.get("enabled_only", False),
            project_id=project_id,
        )

    if scope == "hook_executions":
        return await legacy(
            hook_id=f.get("hook_id"),
            since=f.get("since"),
            status=f.get("status"),
            limit=limit or f.get("limit", 100),
            project_id=project_id,
        )

    if scope == "brain_notifications":
        return await legacy(
            since=f.get("since"),
            include_cleared=f.get("include_cleared", False),
            limit=limit or f.get("limit", 100),
            project_id=project_id,
        )

    if scope == "freshness":
        return await legacy(
            days_threshold=f.get("days_threshold", 30),
            project_id=project_id,
        )

    if scope == "contradictions":
        if not id:
            return _err(
                "missing_field",
                "rka_query(scope='contradictions'): id (entity_id) required",
            )
        return await legacy(
            entity_id=id,
            similarity_threshold=f.get("similarity_threshold", 0.7),
            max_results=f.get("max_results", 5),
            project_id=project_id,
        )

    if scope == "workspace_tree":
        fp = f.get("folder_path")
        if not fp:
            return _err(
                "missing_field",
                "rka_query(scope='workspace_tree'): filters.folder_path required",
            )
        return await legacy(
            folder_path=fp,
            max_depth=f.get("max_depth", 2),
            project_id=project_id,
        )

    if scope == "workspace_scan":
        fp = f.get("folder_path")
        if not fp:
            return _err(
                "missing_field",
                "rka_query(scope='workspace_scan'): filters.folder_path required",
            )
        return await legacy(
            folder_path=fp,
            ignore_patterns=f.get("ignore_patterns"),
            max_file_size_mb=f.get("max_file_size_mb", 50.0),
            use_llm=f.get("use_llm", True),
            project_id=project_id,
        )

    if scope == "bootstrap_review":
        if not id:
            return _err(
                "missing_field",
                "rka_query(scope='bootstrap_review'): id (scan_id) required",
            )
        return await legacy(scan_id=id, project_id=project_id)

    if scope == "manuscript":
        if not id:
            return _err(
                "missing_field",
                "rka_query(scope='manuscript'): id required",
            )
        return await legacy(manuscript_id=id, project_id=project_id)

    if scope in (
        "manuscript_context",
        "manuscript_spine",
        "manuscript_outline",
        "manuscript_writing_candidates",
    ):
        if not id:
            return _err(
                "missing_field",
                f"rka_query(scope={scope!r}): id required",
            )
        return await legacy(manuscript_id=id, project_id=project_id)

    if scope == "manuscript_readiness":
        if not id:
            return _err(
                "missing_field",
                "rka_query(scope='manuscript_readiness'): id required",
            )
        return await legacy(
            manuscript_id=id,
            target_phase=target_phase or "drafting",
            project_id=project_id,
        )

    if scope == "resolve_entities":
        if not ids:
            return _err(
                "missing_field",
                "rka_query(scope='resolve_entities'): ids required",
            )
        return await legacy(
            ids=ids,
            include_sources=include_sources,
            include_edges=include_edges,
            project_id=project_id,
        )

    if scope == "changes_since":
        return await legacy(
            cursor=0 if cursor is None else cursor,
            limit=100 if limit is None else limit,
            project_id=project_id,
        )

    if scope == "manuscript_impact":
        if not id:
            return _err(
                "missing_field",
                "rka_query(scope='manuscript_impact'): id required",
            )
        return await legacy(
            manuscript_id=id,
            since_cursor=0 if since_cursor is None else since_cursor,
            limit=100 if limit is None else limit,
            project_id=project_id,
        )

    if scope == "manuscript_reference_manifest":
        if not id:
            return _err(
                "missing_field",
                "rka_query(scope='manuscript_reference_manifest'): id required",
            )
        return await legacy(
            manuscript_id=id,
            project_id=project_id,
        )

    if scope == "reference_validation_status":
        if not manuscript_id or not job_id:
            return _err(
                "missing_field",
                "rka_query(scope='reference_validation_status') requires manuscript_id and job_id",
            )
        return await legacy(
            manuscript_id=manuscript_id,
            job_id=job_id,
            project_id=project_id,
        )

    return _err(
        "invalid_scope",
        f"rka_query: scope {scope!r} not yet wired",
    )


# ---------------------------------------------------------------------------
# rka_record_note dispatch — 2 modes (create + ingest_document)
# ---------------------------------------------------------------------------

# Provenance keys consumed by the create / ingest_document modes.
_NOTE_PROVENANCE_KEYS = (
    "related_decisions",
    "related_literature",
    "related_mission",
    "supersedes",
)


def _unpack_provenance(
    provenance: dict[str, Any] | None,
    explicit: dict[str, Any],
    allowed: tuple[str, ...],
) -> dict[str, Any]:
    """Graft A — promote provenance dict entries to top-level kwargs.

    Explicit kwargs always win (caller-supplied wins over provenance).
    """
    if not provenance:
        return explicit
    out = dict(explicit)
    for key in allowed:
        if out.get(key) is None and key in provenance:
            out[key] = provenance[key]
    return out


async def dispatch_record_note(
    content: str,
    *,
    project_id: str,
    source: str = "executor",
    type: str = "note",
    confidence: str = "hypothesis",
    importance: str = "normal",
    verbatim_input: str | None = None,
    phase: str | None = None,
    tags: list[str] | None = None,
    provenance: dict[str, Any] | None = None,
    action: str = "create",
    # ingest_document mode kwargs:
    default_type: str | None = None,
    split_by_headings: bool | None = None,
    # v2.7.0.7 — NoteCreate fields previously accepted by RecordNoteArgs but
    # dropped before the POST.
    summary: str | None = None,
    status: str | None = None,
    pinned: bool | None = None,
    capture_mode: str = "unknown",
) -> str:
    """[ANY] Record a journal entry (create or ingest_document).

    Modes (selected by `action`):
      - 'create' (default): POST /api/notes — single note.
      - 'ingest_document': POST /api/ingest/document — markdown
        splitter; many entries from one call.

    PI note creation requires an original unless explicit raw_capture snapshots
    supplied content. Document ingestion already carries the source text and
    preserves exact slices before formatting; no duplicate quote is required.

    Provenance: Graft A — pass `provenance={related_decisions:[...],
    related_literature:[...], related_mission:..., supersedes:...}`
    OR the same fields as explicit kwargs.
    """
    # Phase-X²' validation: source='pi' must carry verbatim_input.
    if (action == "create" and capture_mode != "raw_capture"
            and source == "pi" and not (verbatim_input or "").strip()):
        return _err(
            "missing_provenance",
            "rka_record_note(source='pi'): verbatim_input is required "
            "(preserves PI's exact wording for intellectual attribution)",
        )

    explicit = {
        "related_decisions": None,
        "related_literature": None,
        "related_mission": None,
        "supersedes": None,
    }
    merged = _unpack_provenance(provenance, explicit, _NOTE_PROVENANCE_KEYS)

    if action == "ingest_document":
        return await _legacy("rka_ingest_document")(
            content=content,
            source=source
            if source
            in (
                "brain",
                "executor",
                "pi",
                "llm",
                "web_ui",
                "system",
            )
            else "brain",
            default_type=default_type or "finding",
            phase=phase,
            tags=tags,
            related_literature=merged["related_literature"],
            related_decisions=merged["related_decisions"],
            related_mission=merged["related_mission"],
            split_by_headings=(True if split_by_headings is None else split_by_headings),
            project_id=project_id,
        )

    if action != "create":
        return _err(
            "invalid_action",
            f"rka_record_note: unknown action {action!r}; valid: create, ingest_document",
        )

    return await _legacy("rka_add_note")(
        content=content,
        type=type,
        source=source,
        phase=phase,
        verbatim_input=verbatim_input,
        related_decisions=merged["related_decisions"],
        related_literature=merged["related_literature"],
        related_mission=merged["related_mission"],
        supersedes=merged["supersedes"],
        confidence=confidence,
        importance=importance,
        tags=tags,
        summary=summary,
        status=status,
        pinned=pinned,
        capture_mode=capture_mode,
        project_id=project_id,
    )


# ---------------------------------------------------------------------------
# rka_record_decision dispatch — 2 modes (create + supersede)
# ---------------------------------------------------------------------------

_DECISION_PROVENANCE_KEYS = (
    "related_journal",
    "related_literature",
    "parent_id",
)


async def dispatch_record_decision(
    question: str,
    chosen: str,
    rationale: str,
    *,
    project_id: str,
    decided_by: str,
    kind: str = "decision",
    phase: str = "",
    related_journal: list[str] | None = None,
    supersedes_decision_id: str | None = None,
    options: list[dict] | None = None,
    related_literature: list[str] | None = None,
    related_missions: list[str] | None = None,
    parent_id: str | None = None,
    assumptions: list[str] | None = None,
    importance: str | None = None,
    justified_by: list[str] | None = None,
    provenance: dict[str, Any] | None = None,
    tags: list[str] | None = None,
    status: str | None = None,
) -> str:
    """[BRAIN/PI] Record a decision (create or supersede).

    Modes:
      - create (default): POST /api/decisions.
      - supersede: POST /api/decisions/{old}/supersede (set by
        `supersedes_decision_id`); atomically marks the old decision
        superseded, creates the replacement, re-distills affected
        knowledge.

    Provenance discipline: related_journal must be a NON-EMPTY list.
    Pass it directly OR through provenance={'related_journal': [...]}.
    """
    rj = related_journal
    if (rj is None or len(rj) == 0) and provenance:
        rj = provenance.get("related_journal")
    if not rj or len(rj) == 0:
        return _err(
            "missing_provenance",
            "rka_record_decision: related_journal must be a non-empty "
            "list — decisions need justifying journal entries (provenance "
            "discipline preserved from Phase-X²)",
        )

    explicit = {
        "related_journal": rj,
        "related_literature": related_literature,
        "parent_id": parent_id,
    }
    merged = _unpack_provenance(
        provenance,
        explicit,
        _DECISION_PROVENANCE_KEYS,
    )

    if supersedes_decision_id:
        # v2.7.0.5 — forward provenance + multi-choice metadata to the
        # supersede adapter so the supersede path has parity with the plain
        # rka_add_decision path below. Prior versions silently dropped
        # related_journal / related_literature / related_missions / parent_id
        # / options / assumptions / tags / status from the new decision's
        # record, which broke provenance discipline for any decision that
        # arrived through the typed-args layer.
        return await _legacy("rka_supersede_decision")(
            old_decision_id=supersedes_decision_id,
            question=question,
            chosen=chosen,
            rationale=rationale,
            decided_by=decided_by,
            phase=phase,
            kind=kind,
            related_journal=merged["related_journal"],
            related_literature=merged["related_literature"],
            related_missions=related_missions,
            parent_id=merged["parent_id"],
            options=options,
            assumptions=assumptions,
            tags=tags,
            status=status,
            project_id=project_id,
        )

    return await _legacy("rka_add_decision")(
        question=question,
        phase=phase,
        decided_by=decided_by,
        options=options,
        chosen=chosen,
        rationale=rationale,
        parent_id=merged["parent_id"],
        related_literature=merged["related_literature"],
        related_journal=merged["related_journal"],
        kind=kind,
        assumptions=assumptions,
        # v2.7.0.7 — forward related_missions/tags/status on the create path
        # for parity with the supersede path (which already forwards them).
        related_missions=related_missions,
        tags=tags,
        status=status,
        project_id=project_id,
    )


# ---------------------------------------------------------------------------
# rka_session dispatch — UNSCOPED (no project_id required)
# ---------------------------------------------------------------------------

_SESSION_ACTIONS = (
    "list_projects",
    "create_project",
    "set_project",
    "reset",
    "digest",
    "health",
    "help",
    "export",
    "generate_claude_md",
)


async def dispatch_session(
    action: str,
    *,
    # project mgmt
    project_id: str | None = None,
    name: str | None = None,
    description: str | None = None,
    # export / generate_claude_md
    format: str = "markdown",
    scope: str = "state",
    role: str = "executor",
    # help
    name_lookup: str | None = None,
) -> str:
    """[ANY] Session-level dispatcher — UNSCOPED.

    Most actions take no project_id. The exceptions are `export`,
    `generate_claude_md`, `digest`, and `set_project`, all of which
    need project_id (session-flavored but project-scoped at the REST
    layer).

    Actions:
      - list_projects: discover available projects.
      - create_project: bootstrap a new project (returns project_id).
      - set_project: DEPRECATED no-op (kept for clear warning).
      - reset: clear in-process session tracker.
      - digest: compact session summary.
      - health: API health probe.
      - help: per-verb / per-legacy-tool help.
      - export: data dump (project-scoped).
      - generate_claude_md: render project-specific CLAUDE.md.
    """
    if action not in _SESSION_ACTIONS:
        return _err(
            "invalid_action",
            f"rka_session: unknown action {action!r}",
            valid=list(_SESSION_ACTIONS),
        )

    if action == "list_projects":
        return await _legacy("rka_list_projects")()

    if action == "create_project":
        if not name:
            return _err(
                "missing_field",
                "rka_session(action='create_project'): name is required",
            )
        return await _legacy("rka_create_project")(
            name=name,
            description=description,
        )

    if action == "set_project":
        if not project_id:
            return _err(
                "missing_field",
                "rka_session(action='set_project'): project_id is required "
                "(though this action is a deprecated no-op since v2.6)",
            )
        return await _legacy("rka_set_project")(project_id=project_id)

    if action == "reset":
        return await _legacy("rka_reset_session")()

    if action == "digest":
        if not project_id:
            return _err(
                "missing_field",
                "rka_session(action='digest'): project_id is required",
            )
        return await _legacy("rka_session_digest")(project_id=project_id)

    if action == "health":
        # Best-effort REST probe — uses the same _client() shape the
        # other dispatchers use.
        from rka.mcp.server import _client

        try:
            async with _client() as c:
                r = await c.get("/api/projects")
                ok = r.is_success
                code = r.status_code
        except Exception as exc:
            # `str(httpx.ReadTimeout())` is the empty string, so the operation
            # whose entire job is reporting reachability answered
            # {"status": "unhealthy", "error": ""} — unhealthy for no stated
            # reason. This catch is inside the verb, so the timeout guard on
            # rka_query never sees it.
            detail = str(exc)[:200] or f"{type(exc).__name__} (no message)"
            return json.dumps(
                {
                    "status": "unhealthy",
                    "error": detail,
                    "kind": type(exc).__name__,
                },
                indent=2,
            )
        return json.dumps(
            {
                "status": "healthy" if ok else "degraded",
                "rest_status_code": code,
            },
            indent=2,
        )

    if action == "help":
        if not name_lookup:
            return json.dumps(
                {
                    "verbs": list(VERBS),
                    "session_actions": list(_SESSION_ACTIONS),
                    "hint": (
                        "Pass name_lookup=<tool_name> for per-legacy-tool "
                        "help (e.g. name_lookup='rka_add_note')."
                    ),
                },
                indent=2,
            )
        return await _legacy("rka_help")(name=name_lookup)

    if action == "export":
        if not project_id:
            return _err(
                "missing_field",
                "rka_session(action='export'): project_id is required",
            )
        return await _legacy("rka_export")(
            format=format,
            scope=scope,
            project_id=project_id,
        )

    if action == "generate_claude_md":
        if not project_id:
            return _err(
                "missing_field",
                "rka_session(action='generate_claude_md'): project_id is required",
            )
        return await _legacy("rka_generate_claude_md")(
            role=role,
            project_id=project_id,
        )

    return _err(
        "invalid_action",
        f"rka_session: unhandled action {action!r}",
    )


# ---------------------------------------------------------------------------
# rka_record_decision Phase-X²' helpers — legacy decision tools don't
# expose `importance` / `justified_by` directly, but the verb signature
# accepts them for forward-compat (PR-2 will widen the REST layer to
# accept them; in PR-1 we silently drop). The dispatcher above already
# routes through `rka_add_decision` / `rka_supersede_decision` which
# don't accept those kwargs.
# ---------------------------------------------------------------------------

# (No-op marker comment so future readers know why those kwargs are
# accepted but not forwarded.)


# ---------------------------------------------------------------------------
# v2.7.0a3 — rka_execute dispatch (the unified write/lifecycle surface)
# ---------------------------------------------------------------------------
#
# Routes all 61 write/lifecycle operations to the appropriate existing
# dispatcher above (or to a legacy @tool function for ops that aren't
# already covered by one of the 8 v2.7.0a2 verbs). The goal: one
# always-on tool for ALL writes, with an Annotated[Literal] operation
# discriminator that puts the full enum in the LLM's inputSchema.
#
# Categorization (decision 1 taxonomy):
#
#   record_*      — notes / decisions / literature / ingest / import
#   update_*      — entity updates (note / decision / literature / status)
#                   + bulk_update + supersede_decision
#   create_*      — missions / projects / clusters / gates
#   submit_*      — checkpoint submission, report submission
#   resolve_*     — checkpoint resolution, contradiction resolution
#   present_/     — present_decision (TWO-TAP), record_pi_selection,
#   record_*        record_outcome
#   review_*      — review_claims, review_cluster
#   extract_*     — extract_claims
#   assign_*/     — claim cluster operations
#   split_*/
#   merge_*
#   hook_*        — hook add/enable/disable/delete + notifications_clear
#   workspace_*   — bootstrap_workspace + scan_workspace
#   flag_*/       — staleness flagging + eviction sweep
#   eviction_*
#   session       — reset_session (UNSCOPED)
#
# Per Decision 4: project_id is REQUIRED on every project-scoped op
# (top-level kwarg-only). The 3 unscoped operations (create_project,
# reset_session, and the diagnostic health/list_projects which live on
# rka_query) do not require project_id. The validator emits a uniform
# `missing_field` error matching the v2.6 contract.

# Operations that are unscoped (no project_id required).
_EXECUTE_UNSCOPED_OPS = frozenset(
    {
        "create_project",
        "reset_session",
    }
)

# Canonical set of execute operations — single source of truth, mirrors
# the ExecuteOpLit Literal in server.py. Drift between this set and the
# Literal is checked by tests in tests/test_mcp.
EXECUTE_OPERATIONS = (
    # record / ingest / import
    "record_note",
    "record_decision",
    "record_literature",
    "ingest_document",
    "import_bibtex",
    "batch_import",
    "register_manuscript",
    # canonical native manuscript aggregate
    "create_manuscript",
    "update_manuscript",
    "upsert_argument_spine",
    "replace_manuscript_reference_manifest",
    "ratify_manuscript_claim",
    "transition_manuscript_phase",
    "create_manuscript_checkpoint",
    "resolve_manuscript_checkpoint",
    "record_verification_attestation",
    # interpretation staging
    "create_interpretation_candidate",
    "register_source",
    "admit_source_interpretation",
    "add_interpretation_hint",
    "triage_interpretation_candidate",
    "set_claim_scope",
    # experiment evidence substrate
    "create_experiment",
    "append_experiment_plan",
    "transition_experiment",
    "create_experiment_run",
    "transition_experiment_run",
    "record_experiment_observation",
    "add_evidence_locator",
    "create_planning_branch",
    "transition_planning_branch",
    "append_planning_artifact_version",
    "promote_planning_rq",
    "prepare_planning_contribution",
    "ratify_planning_contribution",
    "create_planning_evaluation_mission",
    "prepare_planning_evaluation_result",
    "prepare_manuscript_outline_proposal",
    "prepare_semantic_patch_context",
    "create_semantic_patch_proposal",
    "apply_semantic_patch_proposal",
    "reject_semantic_patch_proposal",
    "generate_lm_studio_semantic_patch",
    # update
    "update_note",
    "correct_note_attribution",
    "update_decision",
    "update_literature",
    "update_status",
    "bulk_update",
    "supersede_decision",
    "link_supersession",
    # decision lifecycle (PI ratification + calibration)
    "record_pi_selection",
    "record_outcome",
    # literature lifecycle
    "enrich_doi",
    "link_literature_to_zotero",
    "process_paper",
    # mission lifecycle
    "create_mission",
    "update_mission",
    "update_mission_status",
    "submit_report",
    "advance_rq",
    # checkpoint / gate lifecycle
    "submit_checkpoint",
    "resolve_checkpoint",
    "create_gate",
    "evaluate_gate",
    "present_decision",
    # claims / clusters
    "extract_claims",
    "create_cluster",
    "assign_claims_to_cluster",
    "split_cluster",
    "merge_clusters",
    "review_claims",
    "review_cluster",
    "resolve_contradiction",
    # hooks / notifications
    "hook_add",
    "hook_enable",
    "hook_disable",
    "hook_delete",
    "brain_notifications_clear",
    # workspace
    "bootstrap_workspace",
    "scan_workspace",
    # maintenance
    "flag_stale",
    "eviction_sweep",
    # session / project
    "create_project",
    "reset_session",
    "session_digest",
)


# Operations that MODIFY an existing row, where `source`, `confidence` and
# `importance` must never be supplied by dispatch_execute's own signature
# defaults. Threading them in rewrote fields the caller never mentioned: a PI
# directive became an executor note and a `verified` finding reverted to
# `hypothesis`, while `verbatim_input` survived the demotion so the row went
# on to assert the Executor had written the PI's exact words.
# Creation defaults are applied only after recording which fields were supplied.
# Comparing values to these defaults cannot distinguish an omitted field from
# an explicit request to reset a stored value to its default.
_IDENTITY_FIELD_DEFAULTS = {
    "source": "executor",
    "confidence": "hypothesis",
    "importance": "normal",
}

_IDENTITY_SENSITIVE_UPDATES = frozenset({
    "update_note",
    "update_decision",
    "update_literature",
    "update_status",
    "bulk_update",
})

_OMITTED_VERBATIM = object()


async def dispatch_execute(
    operation: str,
    *,
    project_id: str | None,
    source: str | None = None,
    confidence: str | float | None = None,
    importance: str | None = None,
    verbatim_input: str | None = _OMITTED_VERBATIM,  # type: ignore[assignment]
    provenance: dict[str, Any] | None = None,
    tags: list[str] | None = None,
    phase: str | None = None,
    **kw: Any,
) -> str:
    """v2.7.0a3 unified write/lifecycle dispatcher.

    Routes `operation` to the appropriate sub-dispatcher / legacy tool
    while preserving Phase-X²' provenance discipline (related_journal,
    motivated_by_decision, verbatim_input when source='pi').

    Sub-dispatcher routing:
      - record_note, ingest_document  → dispatch_record_note
      - record_decision, supersede_decision → dispatch_record_decision
        (supersede_decision also reachable via dispatch_review for
        back-compat with the v2.7.0a2 surface).
      - record_literature, ingest_document, import_bibtex, enrich_doi,
        link_literature_to_zotero, process_paper
            → dispatch_record_literature
      - create_mission, update_mission, update_mission_status,
        submit_report, advance_rq → dispatch_mission
      - submit_checkpoint, resolve_checkpoint, create_gate,
        evaluate_gate, present_decision, record_pi_selection,
        record_outcome → dispatch_checkpoint
      - extract_claims, create_cluster, assign_claims_to_cluster,
        split_cluster, merge_clusters, review_claims, review_cluster,
        resolve_contradiction, hook_*, brain_notifications_clear,
        bootstrap_workspace, scan_workspace (via legacy direct call),
        flag_stale, eviction_sweep, batch_import, register_manuscript,
        update_note, update_decision, update_literature, update_status,
        bulk_update → dispatch_review
      - create_project, reset_session → dispatch_session
    """
    op = operation
    supplied_verbatim = verbatim_input is not _OMITTED_VERBATIM
    if not supplied_verbatim:
        verbatim_input = None
    supplied_identity_fields = {
        key for key, value in (
            ("source", source), ("confidence", confidence), ("importance", importance)
        ) if value is not None
    }
    # None means omission on these update fields, just as on the typed surface.
    # Hint confidence is numeric and has a different operation-specific default.
    source = source if source is not None else _IDENTITY_FIELD_DEFAULTS["source"]
    confidence = confidence if confidence is not None else (
        0.5 if op == "add_interpretation_hint" else _IDENTITY_FIELD_DEFAULTS["confidence"]
    )
    importance = importance if importance is not None else _IDENTITY_FIELD_DEFAULTS["importance"]
    if op not in EXECUTE_OPERATIONS:
        valid = sorted(EXECUTE_OPERATIONS)
        return _err(
            "invalid_operation",
            f"rka_execute: unknown operation {op!r}",
            valid_operations=valid,
        )

    # project_id enforcement on all but the unscoped operations.
    if op not in _EXECUTE_UNSCOPED_OPS and not project_id:
        return _err(
            "missing_field",
            f"rka_execute(operation={op!r}) requires project_id "
            "(every project-scoped write needs explicit project pinning "
            "in v2.6+).",
        )

    if op == "correct_note_attribution":
        if "source" not in supplied_identity_fields:
            return _err("missing_field", "correct_note_attribution requires explicit source")
        if not supplied_verbatim:
            return _err("missing_field", "correct_note_attribution requires explicit verbatim_input (nullable)")
        return await _legacy("rka_correct_note_attribution")(
            id=kw.get("id"), expected_revision=kw.get("expected_revision"),
            request_id=kw.get("request_id"), actor=kw.get("actor"),
            reason=kw.get("reason"), source=source, verbatim_input=verbatim_input,
            capture_mode=kw.get("capture_mode"),
            project_id=project_id,
        )

    # --- canonical native manuscript aggregate ---
    if op == "create_interpretation_candidate":
        return await _legacy("rka_create_interpretation_candidate")(
            source_type=kw.get("source_type"),
            source_id=kw.get("source_id"),
            locator_kind=kw.get("locator_kind"),
            locator_start=kw.get("locator_start"),
            locator_end=kw.get("locator_end"),
            locator_value=kw.get("locator_value"),
            statement=kw.get("statement"),
            epistemic_kind=kw.get("epistemic_kind"),
            scope_conditions=kw.get("scope_conditions"),
            uncertainty=kw.get("uncertainty", "unknown"),
            uncertainty_note=kw.get("uncertainty_note"),
            falsifier=kw.get("falsifier"),
            proposed_claim_type=kw.get("proposed_claim_type"),
            created_by=kw.get("created_by"),
            extraction_tool=kw.get("extraction_tool"),
            extraction_model=kw.get("extraction_model"),
            project_id=project_id,
        )

    if op == "register_source":
        return await _legacy("rka_register_source")(
            source_kind=kw.get("source_kind"),
            registered_by=kw.get("registered_by"),
            title=kw.get("title"),
            filepath=kw.get("filepath"),
            pasted_text=kw.get("pasted_text"),
            stable_locator=kw.get("stable_locator"),
            mime=kw.get("mime"),
            expected_content_hash=kw.get("expected_content_hash"),
            ownership_kind=kw.get("ownership_kind", "unknown"),
            ownership_note=kw.get("ownership_note"),
            provenance=provenance,
            project_id=project_id,
        )

    if op == "admit_source_interpretation":
        return await _legacy("rka_admit_source_interpretation")(
            source_id=kw.get("source_id"),
            candidate_id=kw.get("candidate_id"),
            expected_revision=kw.get("expected_revision"),
            target_type=kw.get("target_type"),
            target_id=kw.get("target_id"),
            actor=kw.get("actor"),
            reason=kw.get("reason"),
            grounding_verified=kw.get("grounding_verified", False),
            project_id=project_id,
        )

    if op == "add_interpretation_hint":
        return await _legacy("rka_add_interpretation_hint")(
            candidate_id=kw.get("id"),
            related_candidate_id=kw.get("related_candidate_id"),
            kind=kw.get("kind"),
            rationale=kw.get("rationale"),
            created_by=kw.get("created_by"),
            expected_revision=kw.get("expected_revision"),
            confidence=confidence,
            project_id=project_id,
        )

    if op == "triage_interpretation_candidate":
        triage_kwargs = {
            "candidate_id": kw.get("id"),
            "action": kw.get("action"),
            "expected_revision": kw.get("expected_revision"),
            "actor": kw.get("actor"),
            "reason": kw.get("reason"),
            "target_candidate_id": kw.get("target_candidate_id"),
            "target_entity_id": kw.get("target_entity_id"),
            "grounding_verified": kw.get("grounding_verified", False),
            "claim_confidence": kw.get("claim_confidence", 0.5),
            "project_id": project_id,
        }
        if kw.get("evidence_role") is not None:
            triage_kwargs["evidence_role"] = kw["evidence_role"]
        return await _legacy("rka_triage_interpretation_candidate")(**triage_kwargs)

    if op == "create_experiment":
        return await _legacy("rka_create_experiment")(
            payload=kw,
            project_id=project_id,
        )

    if op == "append_experiment_plan":
        return await _legacy("rka_append_experiment_plan")(
            experiment_id=kw.pop("id", None),
            payload=kw,
            project_id=project_id,
        )

    if op == "transition_experiment":
        return await _legacy("rka_transition_experiment")(
            experiment_id=kw.pop("id", None),
            payload=kw,
            project_id=project_id,
        )

    if op == "create_experiment_run":
        return await _legacy("rka_create_experiment_run")(
            payload=kw,
            project_id=project_id,
        )

    if op == "transition_experiment_run":
        return await _legacy("rka_transition_experiment_run")(
            run_id=kw.pop("id", None),
            payload=kw,
            project_id=project_id,
        )

    if op == "record_experiment_observation":
        return await _legacy("rka_record_experiment_observation")(
            payload=kw,
            project_id=project_id,
        )

    if op == "add_evidence_locator":
        return await _legacy("rka_add_evidence_locator")(
            payload=kw,
            project_id=project_id,
        )

    if op == "set_claim_scope":
        return await _legacy("rka_set_claim_scope")(
            claim_id=kw.get("claim_id"),
            expected_revision=kw.get("expected_revision"),
            actor=kw.get("actor"),
            reason=kw.get("reason"),
            conditions=kw.get("conditions") or [],
            uncertainty=kw.get("uncertainty", "unknown"),
            uncertainty_note=kw.get("uncertainty_note"),
            extension_policy=kw.get("extension_policy"),
            allowed_extensions=kw.get("allowed_extensions") or [],
            prohibited_extensions=kw.get("prohibited_extensions") or [],
            falsifier_status=kw.get("falsifier_status", "unknown"),
            falsifier=kw.get("falsifier"),
            falsifier_rationale=kw.get("falsifier_rationale"),
            disconfirming_claim_ids=kw.get("disconfirming_claim_ids") or [],
            review_status=kw.get("review_status", "draft"),
            project_id=project_id,
        )

    if op == "create_planning_branch":
        return await _legacy("rka_create_planning_branch")(
            payload=kw,
            project_id=project_id,
        )

    if op == "transition_planning_branch":
        return await _legacy("rka_transition_planning_branch")(
            branch_id=kw.pop("id", None),
            payload=kw,
            project_id=project_id,
        )

    if op == "append_planning_artifact_version":
        return await _legacy("rka_append_planning_artifact_version")(
            branch_id=kw.pop("id", None),
            payload=kw,
            project_id=project_id,
        )

    if op == "promote_planning_rq":
        return await _legacy("rka_promote_planning_research_question")(
            branch_id=kw.pop("id", None),
            payload=kw,
            project_id=project_id,
        )

    if op == "prepare_planning_contribution":
        return await _legacy("rka_prepare_planning_contribution")(
            branch_id=kw.pop("id", None),
            payload=kw,
            project_id=project_id,
        )

    if op == "ratify_planning_contribution":
        return await _legacy("rka_ratify_planning_contribution")(
            branch_id=kw.pop("id", None),
            payload=kw,
            project_id=project_id,
        )

    if op == "create_planning_evaluation_mission":
        return await _legacy("rka_create_planning_evaluation_mission")(
            branch_id=kw.pop("id", None),
            payload=kw,
            project_id=project_id,
        )

    if op == "prepare_planning_evaluation_result":
        return await _legacy("rka_prepare_planning_evaluation_result")(
            branch_id=kw.pop("id", None),
            payload=kw,
            project_id=project_id,
        )

    if op == "prepare_manuscript_outline_proposal":
        return await _legacy("rka_prepare_manuscript_outline_proposal")(
            manuscript_id=kw.pop("id", None),
            payload=kw,
            project_id=project_id,
        )

    if op == "prepare_semantic_patch_context":
        return await _legacy("rka_prepare_semantic_patch_context")(
            payload=kw,
            project_id=project_id,
        )

    if op == "create_semantic_patch_proposal":
        return await _legacy("rka_create_semantic_patch_proposal")(
            payload=kw,
            project_id=project_id,
        )

    if op in {"apply_semantic_patch_proposal", "reject_semantic_patch_proposal"}:
        tool_name = (
            "rka_apply_semantic_patch_proposal"
            if op == "apply_semantic_patch_proposal"
            else "rka_reject_semantic_patch_proposal"
        )
        return await _legacy(tool_name)(
            proposal_id=kw.pop("id", None),
            payload=kw,
            project_id=project_id,
        )

    if op == "generate_lm_studio_semantic_patch":
        return await _legacy("rka_generate_lm_studio_semantic_patch")(
            payload=kw,
            project_id=project_id,
        )

    # --- canonical native manuscript aggregate ---
    if op == "create_manuscript":
        return await _legacy("rka_create_native_manuscript")(
            title=kw.get("title"),
            abstract=kw.get("abstract"),
            venue=kw.get("venue"),
            phase=phase or "planning",
            state=kw.get("state", "active"),
            workspace_ref=kw.get("workspace_ref"),
            legacy_journal_id=kw.get("legacy_journal_id"),
            project_id=project_id,
        )

    if op == "update_manuscript":
        provided_fields = set(kw.pop("_provided_fields", ()))
        if not provided_fields:
            # Defense-in-depth for direct legacy dispatcher callers that do
            # not pass the typed model's fields-set metadata.
            provided_fields = {
                field_name
                for field_name in (
                    "title",
                    "abstract",
                    "venue",
                    "state",
                    "workspace_ref",
                )
                if field_name in kw
            }
            if phase is not None:
                provided_fields.add("phase")
        updates: dict[str, Any] = {}
        for field_name in (
            "title",
            "abstract",
            "venue",
            "phase",
            "state",
            "workspace_ref",
        ):
            if field_name not in provided_fields:
                continue
            updates[field_name] = phase if field_name == "phase" else kw.get(field_name)
        return await _legacy("rka_update_native_manuscript")(
            manuscript_id=kw.get("id"),
            expected_revision=kw.get("expected_revision"),
            updates=updates,
            project_id=project_id,
        )

    if op == "upsert_argument_spine":
        return await _legacy("rka_upsert_argument_spine")(
            manuscript_id=kw.get("id"),
            expected_revision=kw.get("expected_revision"),
            spine=kw.get("spine"),
            project_id=project_id,
        )

    if op == "replace_manuscript_reference_manifest":
        return await _legacy("rka_replace_manuscript_reference_manifest")(
            manuscript_id=kw.get("id"),
            expected_revision=kw.get("expected_revision"),
            members=kw.get("members", []),
            project_id=project_id,
        )

    if op == "ratify_manuscript_claim":
        return await _legacy("rka_ratify_manuscript_claim")(
            manuscript_id=kw.get("id"),
            claim_ref=kw.get("claim_ref"),
            expected_revision=kw.get("expected_revision"),
            decision_id=kw.get("decision_id"),
            claim_version=kw.get("claim_version"),
            ratified_at=kw.get("ratified_at"),
            project_id=project_id,
        )

    if op == "transition_manuscript_phase":
        return await _legacy("rka_transition_manuscript_phase")(
            manuscript_id=kw.get("id"),
            expected_revision=kw.get("expected_revision"),
            target_phase=kw.get("target_phase"),
            target_state=kw.get("target_state"),
            project_id=project_id,
        )

    if op == "create_manuscript_checkpoint":
        return await _legacy("rka_create_native_manuscript_checkpoint")(
            manuscript_id=kw.get("id"),
            expected_revision=kw.get("expected_revision"),
            kind=kw.get("kind"),
            unit_id=kw.get("unit_id"),
            supersedes_id=kw.get("supersedes_id"),
            project_id=project_id,
        )

    if op == "resolve_manuscript_checkpoint":
        return await _legacy("rka_resolve_native_manuscript_checkpoint")(
            checkpoint_id=kw.get("checkpoint_id"),
            expected_revision=kw.get("expected_revision"),
            decision_id=kw.get("decision_id"),
            status=kw.get("status"),
            resolved_at=kw.get("resolved_at"),
            project_id=project_id,
        )

    if op == "record_verification_attestation":
        attestation_fields = (
            "claim_id",
            "claim_version",
            "overall_verdict",
            "grounding_verdict",
            "evidence_verdict",
            "contradiction_verdict",
            "currency_verdict",
            "ratification_verdict",
            "unit_coverage_verdict",
            "changelog_cursor",
            "dependency_snapshot",
            "full_json_payload",
            "validator_version",
            "started_at",
            "completed_at",
        )
        attestation = {
            field_name: kw[field_name] for field_name in attestation_fields if field_name in kw
        }
        return await _legacy("rka_record_manuscript_verification_attestation")(
            manuscript_id=kw.get("id"),
            expected_revision=kw.get("expected_revision"),
            attestation=attestation,
            project_id=project_id,
        )

    # --- record_note / ingest_document ---
    if op in ("record_note", "ingest_document"):
        content = kw.get("content")
        if not content:
            return _err(
                "missing_field",
                f"rka_execute(operation={op!r}) requires `content`",
            )
        sub_action = "ingest_document" if op == "ingest_document" else "create"
        return await dispatch_record_note(
            content=content,
            project_id=project_id,  # type: ignore[arg-type]
            source=source,
            type=kw.get("type", "note"),
            confidence=confidence,
            importance=importance,
            verbatim_input=verbatim_input,
            phase=phase,
            tags=tags,
            provenance=provenance,
            action=sub_action,
            default_type=kw.get("default_type"),
            split_by_headings=kw.get("split_by_headings"),
            summary=kw.get("summary"),
            status=kw.get("status"),
            pinned=kw.get("pinned"),
            capture_mode=kw.get("capture_mode", "unknown"),
        )

    # --- record_decision (also handles supersede_decision in record form) ---
    if op == "record_decision":
        question = kw.get("question")
        chosen = kw.get("chosen")
        rationale = kw.get("rationale")
        if not question or not chosen or not rationale:
            return _err(
                "missing_field",
                "rka_execute(operation='record_decision') requires question + chosen + rationale",
            )
        decided_by = kw.get("decided_by") or "brain"
        return await dispatch_record_decision(
            question=question,
            chosen=chosen,
            rationale=rationale,
            project_id=project_id,  # type: ignore[arg-type]
            decided_by=decided_by,
            kind=kw.get("kind", "decision"),
            # v2.7.0.6 — preserve None for the service-layer phase-inheritance
            # path. Coercing to "" here hid the "Brain omitted phase" signal
            # and made tree-by-phase queries skip the superseded row.
            phase=phase,
            related_journal=kw.get("related_journal"),
            supersedes_decision_id=kw.get("supersedes_decision_id"),
            options=kw.get("options"),
            related_literature=kw.get("related_literature"),
            related_missions=kw.get("related_missions"),
            parent_id=kw.get("parent_id"),
            assumptions=kw.get("assumptions"),
            importance=importance,
            justified_by=kw.get("justified_by"),
            provenance=provenance,
            tags=tags,
            status=kw.get("status"),
        )

    # --- supersede_decision (via review dispatcher) ---
    if op == "supersede_decision":
        payload = {
            "old_decision_id": kw.get("old_decision_id"),
            "question": kw.get("question"),
            "chosen": kw.get("chosen"),
            "rationale": kw.get("rationale"),
            "decided_by": kw.get("decided_by", "brain"),
            # v2.7.0.6 — None preserved so the service-layer inheritance fires;
            # `phase or kw.get('phase', '')` used to coerce missing phase to "".
            "phase": phase,
            "kind": kw.get("kind", "decision"),
            # v2.7.0.5 — SupersedeDecisionArgs enforces related_journal
            # non-empty at the typed-args layer, but prior versions dropped
            # it from the payload, so the supersede adapter never received
            # it and the resulting decision was provenance-orphaned.
            "related_journal": kw.get("related_journal"),
        }
        return await dispatch_review(
            "supersede_decision",
            project_id=project_id,  # type: ignore[arg-type]
            payload=payload,
        )

    # --- record_literature + literature-action ops ---
    if op in (
        "record_literature",
        "import_bibtex",
        "enrich_doi",
        "link_literature_to_zotero",
        "process_paper",
    ):
        # Map our v2.7.0a3 op name to the action= sub-mode that
        # dispatch_record_literature understands.
        action_map = {
            "record_literature": None,  # explicit-create / search / default
            "import_bibtex": "import_bibtex",
            "enrich_doi": "enrich_doi",
            "link_literature_to_zotero": "link_zotero",
            "process_paper": "process_paper",
        }
        return await dispatch_record_literature(
            project_id=project_id,  # type: ignore[arg-type]
            title=kw.get("title"),
            bibtex=kw.get("bibtex"),
            search_query=kw.get("search_query"),
            search_source=kw.get("search_source"),
            doi=kw.get("doi"),
            authors=kw.get("authors"),
            year_min=kw.get("year_min"),
            venue=kw.get("venue"),
            status=kw.get("default_status", "to_read") if op == "import_bibtex" else kw.get("status", "to_read"),
            abstract=kw.get("abstract"),
            url=kw.get("url"),
            tags=tags,
            related_decisions=kw.get("related_decisions"),
            action=action_map[op] or kw.get("action"),
            lit_id=kw.get("lit_id") or kw.get("id"),
            manuscript_id=None,
            zotero_key=kw.get("zotero_key"),
            pdf_path=kw.get("pdf_path"),
            annotations=kw.get("annotations"),
            summary=kw.get("summary"),
            add_to_library=kw.get("add_to_library", False),
            import_top_n=kw.get("import_top_n"),
            limit=kw.get("limit", 10),
            year=kw.get("year"),
        )

    # --- mission ops ---
    if op in (
        "create_mission",
        "update_mission",
        "update_mission_status",
        "submit_report",
        "advance_rq",
    ):
        action_map = {
            "create_mission": "create",
            "update_mission": "update",
            "update_mission_status": "update_status",
            "submit_report": "submit_report",
            "advance_rq": "advance_rq",
        }
        # Compose kwargs that dispatch_mission expects via **kw.
        mission_kw = {
            "mission_id": kw.get("mission_id") or kw.get("id"),
            "rq_id": kw.get("rq_id"),
            "objective": kw.get("objective"),
            "phase": phase,
            "tasks": kw.get("tasks"),
            "context": kw.get("context"),
            "acceptance_criteria": kw.get("acceptance_criteria"),
            "scope_boundaries": kw.get("scope_boundaries"),
            "checkpoint_triggers": kw.get("checkpoint_triggers"),
            "depends_on": kw.get("depends_on"),
            "parent_mission_id": kw.get("parent_mission_id"),
            "motivated_by_decision": kw.get("motivated_by_decision"),
            "provenance": provenance,
            "tags": tags,
            "status": kw.get("status"),
            "summary": kw.get("summary"),
            "content": kw.get("content"),
            "findings": kw.get("findings", ""),
            "anomalies": kw.get("anomalies", ""),
            "questions": kw.get("questions", ""),
            "codebase_state": kw.get("codebase_state", ""),
            "recommended_next": kw.get("recommended_next", ""),
            "conclusion": kw.get("conclusion"),
            "evidence_cluster_ids": kw.get("evidence_cluster_ids"),
        }
        return await dispatch_mission(
            action_map[op],
            project_id=project_id,  # type: ignore[arg-type]
            **mission_kw,
        )

    # --- checkpoint / gate / decision-ratification ops ---
    if op in (
        "submit_checkpoint",
        "resolve_checkpoint",
        "create_gate",
        "evaluate_gate",
        "present_decision",
        "record_pi_selection",
        "record_outcome",
    ):
        action_map = {
            "submit_checkpoint": "submit",
            "resolve_checkpoint": "resolve",
            "create_gate": "create_gate",
            "evaluate_gate": "evaluate_gate",
            "present_decision": "present_decision",
            "record_pi_selection": "pi_select",
            "record_outcome": "record_outcome",
        }
        chk_kw = {
            "id": kw.get("id") or kw.get("checkpoint_id"),
            "mission_id": kw.get("mission_id"),
            "decision_id": kw.get("decision_id"),
            "gate_id": kw.get("gate_id"),
            "type": kw.get("type"),
            "description": kw.get("description"),
            "content": kw.get("content"),
            "task_reference": kw.get("task_reference"),
            "context": kw.get("context"),
            "options": kw.get("options"),
            "recommendation": kw.get("recommendation"),
            "blocking": kw.get("blocking", True),
            "resolution": kw.get("resolution"),
            "resolved_by": kw.get("resolved_by"),
            "rationale": kw.get("rationale"),
            "create_decision": kw.get("create_decision", False),
            "gate_type": kw.get("gate_type"),
            "deliverables": kw.get("deliverables"),
            "pass_criteria": kw.get("pass_criteria"),
            "assumptions_to_verify": kw.get("assumptions_to_verify"),
            "verdict": kw.get("verdict"),
            "notes": kw.get("notes"),
            "assumption_status": kw.get("assumption_status"),
            "confirmation_brief": kw.get("confirmation_brief"),
            "pi_preference": kw.get("pi_preference"),
            "selected_option_id": kw.get("selected_option_id"),
            "override_rationale": kw.get("override_rationale"),
            "outcome": kw.get("outcome"),
            "outcome_details": kw.get("outcome_details"),
            "lessons": kw.get("lessons"),
            "recorded_by": kw.get("recorded_by", "pi"),
        }
        return await dispatch_checkpoint(
            action_map[op],
            project_id=project_id,  # type: ignore[arg-type]
            **chk_kw,
        )

    # --- review-style ops (updates / hooks / claims / clusters /
    # workspace / maintenance / manuscript) ---
    _REVIEW_OP_MAP = {
        "update_note": "note_update",
        "update_decision": "decision_update",
        "update_literature": "literature_update",
        "update_status": "status_update",
        "bulk_update": "bulk_update",
        "batch_import": "batch_import",
        "hook_add": "hook_add",
        "hook_enable": "hook_enable",
        "hook_disable": "hook_disable",
        "hook_delete": "hook_delete",
        "brain_notifications_clear": "brain_notifications_clear",
        "extract_claims": "extract_claims",
        "review_claims": "claims",
        "review_cluster": "cluster",
        "create_cluster": "cluster_create",
        "assign_claims_to_cluster": "cluster_assign",
        "split_cluster": "cluster_split",
        "merge_clusters": "cluster_merge",
        "resolve_contradiction": "contradiction",
        "flag_stale": "flag_stale",
        "eviction_sweep": "eviction_sweep",
        "bootstrap_workspace": "bootstrap_workspace",
        "register_manuscript": "manuscript_register",
    }
    if op in _REVIEW_OP_MAP:
        payload = dict(kw)
        # Thread the operation-common kwargs into the payload when they are
        # not already present.
        #
        # On updates, omit synthetic creation defaults but retain explicit
        # values, including a deliberate reset to hypothesis/normal/executor.
        for k, v in (
            ("source", source),
            ("confidence", confidence),
            ("importance", importance),
            ("verbatim_input", verbatim_input),
            ("phase", phase),
            ("tags", tags),
        ):
            if (
                op in _IDENTITY_SENSITIVE_UPDATES
                and k in _IDENTITY_FIELD_DEFAULTS
                and k not in supplied_identity_fields
            ):
                continue
            payload.setdefault(k, v)
        return await dispatch_review(
            _REVIEW_OP_MAP[op],
            project_id=project_id,  # type: ignore[arg-type]
            payload=payload,
        )

    # --- link_supersession (direct legacy call; repairs an existing pair
    # rather than creating a replacement, so it is not a dispatch_review
    # target) ---
    if op == "link_supersession":
        old_id = kw.get("old_decision_id")
        new_id = kw.get("new_decision_id")
        if not old_id or not new_id:
            return _err(
                "missing_field",
                "rka_execute(operation='link_supersession') requires "
                "old_decision_id + new_decision_id",
            )
        return await _legacy("rka_link_supersession")(
            old_decision_id=old_id,
            new_decision_id=new_id,
            apply=bool(kw.get("apply", False)),
            actor=kw.get("actor", "pi"),
            project_id=project_id,  # type: ignore[arg-type]
        )

    # --- scan_workspace (direct legacy call; not part of dispatch_review's
    # surface because the legacy tool is `rka_scan_workspace` not
    # `rka_review_*`) ---
    if op == "scan_workspace":
        folder_path = kw.get("folder_path")
        if not folder_path:
            return _err(
                "missing_field",
                "rka_execute(operation='scan_workspace') requires folder_path",
            )
        return await _legacy("rka_scan_workspace")(
            folder_path=folder_path,
            ignore_patterns=kw.get("ignore_patterns"),
            max_file_size_mb=kw.get("max_file_size_mb", 50.0),
            use_llm=kw.get("use_llm", True),
            project_id=project_id,  # type: ignore[arg-type]
        )

    # --- session ops (UNSCOPED) ---
    if op == "create_project":
        name = kw.get("name")
        if not name:
            return _err(
                "missing_field",
                "rka_execute(operation='create_project') requires `name`",
            )
        return await dispatch_session(
            "create_project",
            name=name,
            description=kw.get("description"),
        )

    if op == "reset_session":
        return await dispatch_session("reset")

    if op == "session_digest":
        return await dispatch_session("digest", project_id=project_id)

    # If we reached here a route is missing — should never happen if
    # EXECUTE_OPERATIONS and the routing branches stay in sync.
    return _err(
        "unhandled_operation",
        f"rka_execute: operation {op!r} listed but not wired",
    )


# ---------------------------------------------------------------------------
# v2.7.0 typed-arg dispatchers (discriminated union)
# ---------------------------------------------------------------------------
#
# The typed dispatchers receive a Pydantic model instance whose union
# membership has already been validated by FastMCP. Routing inspects
# ``args.operation`` and delegates to the existing untyped sub-dispatchers,
# extracting per-operation kwargs from the typed instance via
# ``args.model_dump(exclude={'operation', 'project_id'}, exclude_none=False)``.
#
# Design:
#   - The typed-arg layer is the PRIMARY enum/required-field catch (FastMCP
#     surfaces the JSON Schema ``oneOf`` to the LLM, so a bad enum is
#     rejected pre-dispatch).
#   - The legacy untyped sub-dispatchers are kept as defense-in-depth and
#     as the legacy entry points reachable via ``RKA_LEGACY_TOOLS=1``.
#   - Phase-X²' alias resolution (description/content, summary/content)
#     is handled at the sub-dispatcher layer where it already lived.


def _coerce_result_to_str(result: Any) -> str:
    """v2.7.0.2 (Bug 2 fix): JSON-stringify dict/list dispatch results.

    The ``rka_execute`` and ``rka_query`` MCP wrappers declare ``result:
    str`` in their outputSchema. Pre-v2.7.0.2, operations whose legacy
    tool returned a dict (``link_literature_to_zotero``, ``enrich_doi``,
    ``process_paper``) tripped FastMCP's output
    validator and surfaced as client-side errors — even though the
    underlying DB writes had already landed. Programmatic callers saw
    every dict-returning success as a failure and had to re-query the
    entity to confirm.

    This helper keeps the typed-arg contract (``result: str``) while
    accommodating structured returns: anything that isn't already a
    string gets ``json.dumps(...)``'d. Callers can ``json.loads(result)``
    when the operation is known to return structured data; otherwise
    they get a clean string suitable for direct display.

    ``default=str`` handles datetime / Path / Decimal / UUID without
    raising on otherwise-fine payloads (rare, but the underlying REST
    layer can leak them).
    """
    if isinstance(result, str):
        return result
    if result is None:
        return ""
    try:
        return json.dumps(result, indent=None, default=str, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        # Fall back to repr so we never raise out of the dispatch layer.
        # An unrenderable payload is a bug worth surfacing as text, not
        # as a Pydantic output-validation error after the write landed.
        logger.warning(
            "dispatch result coerce-to-str failed (%s); falling back to repr",
            exc,
        )
        return repr(result)


async def _dispatch_capabilities_typed(args: "BaseModel") -> str:  # type: ignore[name-defined]  # noqa: F821
    """Proxy versioned discovery while keeping connector/backend drift visible."""

    from rka import __version__
    from rka.mcp.server import _client
    from rka.services.capabilities import CORE_CONTRACT

    required_contract = getattr(args, "required_contract", None)
    required_capabilities = getattr(args, "required_capabilities", None) or []
    params: list[tuple[str, str]] = []
    if required_contract:
        params.append(("required_contract", required_contract))
    params.extend(("required_capability", capability) for capability in required_capabilities)

    try:
        async with _client() as client:
            response = await client.get(
                "/api/capabilities",
                params=params or None,
            )
    except httpx.RequestError as exc:
        detail = str(exc) or f"{type(exc).__name__} (no message)"
        return _err(
            "backend_unavailable",
            "The MCP connector could not reach the RKA Core capability endpoint.",
            connector_version=__version__,
            required_contract=required_contract or CORE_CONTRACT,
            detail=detail,
            hint=(
                "Start or rebuild the Core backend, verify RKA_API_URL, and "
                "keep the local connector and backend installations synchronized."
            ),
        )

    try:
        body = response.json()
    except Exception:  # noqa: BLE001 - compatibility path may return HTML/text
        body = None

    if response.status_code in {404, 405}:
        return _err(
            "incompatible_backend",
            "The Core backend does not expose the E2 capability contract.",
            connector_version=__version__,
            rest_status_code=response.status_code,
            required_contract=required_contract or CORE_CONTRACT,
            hint=(
                "Upgrade/rebuild the Core backend or reinstall the connector "
                "so both provide rka-core/v1 discovery."
            ),
        )

    backend_version = None
    supported_contracts: list[str] = []
    if isinstance(body, dict):
        backend_version = body.get("core", {}).get("version") or body.get("core_version")
        supported_contracts = body.get("core", {}).get("supported_contracts") or body.get(
            "supported_contracts", []
        )
    connector = {
        "name": "rka-mcp",
        "version": __version__,
        "backend_version": backend_version,
        "version_match": backend_version == __version__,
        "compatible": CORE_CONTRACT in supported_contracts,
    }

    if not response.is_success:
        if isinstance(body, dict):
            body["connector"] = connector
            return json.dumps(body, indent=2)
        return _err(
            "backend_error",
            "The Core backend rejected capability discovery.",
            connector=connector,
            rest_status_code=response.status_code,
            detail=response.text[:500],
            hint="Inspect the backend response and synchronize connector/backend versions.",
        )

    if not isinstance(body, dict) or body.get("schema_version") != "rka.core-capabilities/v1":
        return _err(
            "incompatible_backend",
            "The Core backend returned a pre-E2 or malformed capability document.",
            connector=connector,
            required_contract=required_contract or CORE_CONTRACT,
            observed_payload=body,
            hint=(
                "Upgrade/rebuild the Core backend or reinstall the connector "
                "so both provide rka.core-capabilities/v1."
            ),
        )

    compatible = CORE_CONTRACT in supported_contracts
    connector["compatible"] = compatible
    body["connector"] = connector
    if not compatible:
        return _err(
            "incompatible_backend",
            f"The connector requires {CORE_CONTRACT}.",
            connector=connector,
            backend_capabilities=body,
            required_contract=CORE_CONTRACT,
            hint="Synchronize the connector and Core backend before writes.",
        )
    return json.dumps(body, indent=2)


async def dispatch_query_typed(args: "BaseModel") -> str:  # type: ignore[name-defined]  # noqa: F821
    """v2.7.0 typed-query dispatch.

    Args:
        args: A Pydantic model instance from ``QueryArgsUnion``. The
              caller (``rka_query`` in server.py) takes the model from
              FastMCP's parsed inputSchema; the model's ``operation``
              field is the discriminator.

    Returns: JSON string from the underlying REST adapter (v2.7.0.2:
    dict/list returns are JSON-stringified via ``_coerce_result_to_str``
    so they don't trip the ``rka_query`` outputSchema's ``str`` contract).
    """
    # Lazy-import operation_args to avoid module-cycle at startup. The
    # underlying classes are only used for ``isinstance`` narrowing
    # signals in tests — runtime code reads ``args.operation`` only.
    op = args.operation  # type: ignore[attr-defined]
    pid = getattr(args, "project_id", None)

    # Unscoped reads — list_projects + capabilities + health.
    if op == "list_projects":
        return _coerce_result_to_str(await dispatch_session("list_projects"))
    if op == "capabilities":
        return await _dispatch_capabilities_typed(args)
    if op == "health":
        return _coerce_result_to_str(await dispatch_session("health"))

    if not pid:
        return _err(
            "missing_field",
            f"rka_query(operation={op!r}) requires project_id",
        )

    # Most reads route through dispatch_query(scope=...) which uses
    # the same set of identifiers as the typed operation field. We
    # extract the per-op kwargs from the model dump.
    kw_all = args.model_dump(exclude_none=True)
    kw_all.pop("operation", None)
    kw_all.pop("project_id", None)

    typed_filters = dict(kw_all.get("filters") or {})
    if op == "note_attribution_history":
        typed_filters["after_revision"] = kw_all["after_revision"]
    if op == "semantic_patch_proposals" and "status" in kw_all:
        typed_filters["status"] = kw_all["status"]

    return _coerce_result_to_str(
        await dispatch_query(
            op,
            project_id=pid,
            id=kw_all.get("id"),
            query=kw_all.get("query"),
            limit=kw_all.get("limit"),
            filters=typed_filters or None,
            ids=kw_all.get("ids"),
            include_sources=kw_all.get("include_sources", False),
            include_edges=kw_all.get("include_edges", False),
            target_phase=kw_all.get("target_phase"),
            cursor=kw_all.get("cursor"),
            since_cursor=kw_all.get("since_cursor"),
            manuscript_id=kw_all.get("manuscript_id"),
            job_id=kw_all.get("job_id"),
            include_archived=kw_all.get("include_archived", True),
            base_branch_id=kw_all.get("base_branch_id"),
            other_branch_id=kw_all.get("other_branch_id"),
        )
    )


async def dispatch_execute_typed(args: "BaseModel") -> str:  # type: ignore[name-defined]  # noqa: F821
    """v2.7.0 typed-execute dispatch.

    Args:
        args: A Pydantic model instance from ``ExecuteArgsUnion``.

    Routing:
        Reads ``args.operation`` as the discriminator and dumps the model
        body via ``model_dump(exclude_none=True)``. The Phase-X²' alias
        rules (description/content, summary/content) are then resolved
        and the legacy untyped ``dispatch_execute`` is invoked with the
        flattened kwargs. This keeps the sub-dispatcher topology
        unchanged; only the contract surface (FastMCP signature) is new.
    """
    op = args.operation  # type: ignore[attr-defined]
    pid = getattr(args, "project_id", None)

    # ``model_dump`` returns a plain dict; ``exclude_none=True`` strips
    # None defaults so downstream ``kw.get(...)`` calls behave the same
    # as the legacy raw-kwarg surface.
    if op in {"update_manuscript", "prepare_manuscript_outline_proposal", "correct_note_attribution"}:
        # Preserve explicit nulls for nullable metadata fields (abstract,
        # venue, workspace_ref) and outline-patch fields (parent, transition,
        # quick-reader role, blocker) while retaining omission semantics.
        kw_all = args.model_dump(exclude_unset=True)
        if op == "update_manuscript":
            # The metadata service also needs the top-level provided-field set.
            kw_all["_provided_fields"] = sorted(args.model_fields_set)
    else:
        kw_all = args.model_dump(exclude_none=True)
    kw_all.pop("operation", None)
    kw_all.pop("project_id", None)

    # Lift the v2.7.0a3 ``operation-common`` kwargs out of the dump and
    # pass them as explicit named arguments. dispatch_execute signs them
    # at the top of its signature.
    common_keys = (
        "source",
        "confidence",
        "importance",
        "verbatim_input",
        "provenance",
        "tags",
        "phase",
    )
    common_kw = {k: kw_all.pop(k) for k in common_keys if k in kw_all}

    return _coerce_result_to_str(
        await dispatch_execute(
            op,
            project_id=pid,
            **common_kw,
            **kw_all,
        )
    )


# ---------------------------------------------------------------------------
# Module-level exports
# ---------------------------------------------------------------------------

VERBS = (
    "rka_query",
    "rka_record_note",
    "rka_record_decision",
    "rka_record_literature",
    "rka_mission",
    "rka_checkpoint",
    "rka_review",
    "rka_session",
)


__all__ = [
    "VERBS",
    "_QUERY_DISPATCH",
    "_SESSION_ACTIONS",
    "EXECUTE_OPERATIONS",
    "dispatch_query",
    "dispatch_record_note",
    "dispatch_record_decision",
    "dispatch_record_literature",
    "dispatch_mission",
    "dispatch_checkpoint",
    "dispatch_review",
    "dispatch_session",
    "dispatch_execute",
    # v2.7.0 typed dispatchers
    "dispatch_query_typed",
    "dispatch_execute_typed",
]
