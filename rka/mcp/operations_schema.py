"""v2.7.0a3 — OPERATIONS_SCHEMA for rka_describe.

The single source of truth for the schema-lookup surface that pairs with
the rka_query / rka_execute always-on verbs. Each entry documents one
operation that the LLM can pass to either rka_query(operation=...) or
rka_execute(operation=...) (or, in this PR, one of the legacy v2.7.0a2
verbs while they remain callable).

Schema for one entry::

    {
        "operation":            str   # canonical operation name
        "tool":                 str   # 'rka_query' | 'rka_execute'
        "category":             str   # 'journal' | 'decision' | ...
        "summary":              str   # one-line "what this does"
        "signature":            str   # human-readable call shape
        "required_fields":      list[str]
        "optional_fields":      list[str]
        "enums":                dict[str, list[str]]
        "examples":             list[{"description": str, "call": dict}]
        "related_operations":   list[str]  # cross-references
        "role_tag":             str   # 'BRAIN' | 'EXECUTOR' | 'PI' | 'ANY'
        "notes":                str | None  # optional pitfall/guidance
    }

Design choices (decision #3 in the project locked-decisions list):

- Hand-curated, NOT reflection-derived. Reflection from Pydantic models +
  function signatures loses the role-tag guidance, the cross-operation
  hints, and the Phase-X²' canonical-field-name lessons (e.g. the
  `description=` vs `content=` checkpoint pitfall from the 2026-06-01
  hyperscaler-auditing PA-2 bug).
- Single dict, one entry per operation.
- Enum value-sets reference rka.mcp._enums for drift-detection — the
  enums dict here cites the values directly (mirror of _enums.py) so
  consumers don't need to import _enums. The lock-test in
  tests/test_mcp/ pins them.
- Examples demonstrate canonical field names (the names operators
  empirically get wrong) and provenance shapes (related_journal,
  motivated_by_decision, verbatim_input).

This module is consumed by ``rka.mcp.server.rka_describe`` and by the
lock-tests in tests/test_mcp/test_v270a3_describe.py.
"""

from __future__ import annotations

import difflib
import json
from typing import Any

from rka.contracts import (
    AGENTIC_UNSUPPORTED,
    CORE,
    CORE_LEGACY,
    mcp_operation_disposition,
)


# ---------------------------------------------------------------------------
# Enum value sets — kept in sync with rka/mcp/_enums.py
# ---------------------------------------------------------------------------

_ENUMS = {
    "confidence": [
        "hypothesis",
        "tested",
        "verified",
        "superseded",
        "retracted",
    ],
    "importance": [
        "critical",
        "high",
        "normal",
        "low",
        "archived",
    ],
    "source": ["brain", "executor", "pi", "web_ui", "llm"],
    "note_type": [
        "note",
        "log",
        "directive",
        "finding",
        "insight",
        "pi_instruction",
        "exploration",
        "idea",
        "observation",
        "hypothesis",
        "methodology",
        "summary",
    ],
    "journal_status": ["draft", "active", "superseded", "retracted"],
    "bulk_entity_type": ["note", "journal", "decision", "literature"],
    "decided_by": ["pi", "brain", "executor"],
    "decision_kind": [
        "research_question",
        "design_choice",
        "decision",
        "operational",
    ],
    "lit_status": ["to_read", "reading", "read", "cited", "excluded"],
    "lit_added_by": ["brain", "executor", "pi", "import", "web_ui"],
    "mission_status": [
        "pending",
        "active",
        "complete",
        "partial",
        "blocked",
        "cancelled",
    ],
    "checkpoint_type": [
        "decision",
        "clarification",
        "inspection",
        "gate",
    ],
    "gate_type": [
        "problem_framing",
        "plan_validation",
        "evidence_review",
        "synthesis_validation",
    ],
    "verdict": ["go", "kill", "hold", "recycle"],
    "claim_type": [
        "hypothesis",
        "evidence",
        "method",
        "result",
        "observation",
        "assumption",
    ],
    "claim_scope_uncertainty": ["none", "low", "medium", "high", "unknown"],
    "claim_scope_extension_policy": ["exact_only", "bounded"],
    "claim_falsifier_status": ["unknown", "applicable", "not_applicable"],
    "claim_scope_review_status": ["draft", "reviewed"],
    "claim_scope_readiness": [
        "missing",
        "stale",
        "incomplete",
        "needs_review",
        "ready",
    ],
    "claim_scope_actor": ["pi", "brain", "executor", "web_ui", "llm"],
    "claim_condition_kind": [
        "dataset",
        "population",
        "platform",
        "environment",
        "threat_model",
        "baseline",
        "workload",
        "metric",
        "parameter",
        "assumption",
        "time_window",
        "other",
    ],
    "claim_condition_operator": [
        "equals",
        "one_of",
        "range",
        "at_least",
        "at_most",
        "present",
        "absent",
        "described_by",
    ],
    "interpretation_source": [
        "journal", "literature", "artifact", "experiment_observation"
    ],
    "interpretation_locator": [
        "text_offset",
        "page",
        "line_range",
        "section",
        "url_fragment",
        "record",
    ],
    "epistemic_kind": [
        "observation",
        "reported_fact",
        "inference",
        "hypothesis",
        "plan",
        "author_intent",
    ],
    "interpretation_uncertainty": ["none", "low", "medium", "high", "unknown"],
    "interpretation_actor": ["pi", "brain", "executor", "web_ui", "llm", "import"],
    "interpretation_review_actor": ["pi", "brain", "executor", "web_ui"],
    "interpretation_review_status": ["pending", "in_review", "resolved"],
    "interpretation_disposition": [
        "promoted",
        "merged",
        "deferred",
        "rejected",
        "classified_decision",
        "classified_plan",
        "classified_author_intent",
        "evidence_mission_requested",
        "classified_evidence",
    ],
    "interpretation_hint_kind": ["duplicate", "conflict"],
    "interpretation_triage_action": [
        "start_review",
        "promote",
        "merge",
        "defer",
        "reject",
        "classify_decision",
        "classify_plan",
        "classify_author_intent",
        "request_evidence_mission",
        "reopen",
        "revoke_promotion",
        "classify_evidence",
        "revoke_evidence",
    ],
    "registered_source_kind": ["file", "pasted_text", "url", "repository", "zotero"],
    "registered_source_ownership": [
        "researcher", "institution", "third_party", "public_domain", "unknown"
    ],
    "registered_source_actor": [
        "pi", "brain", "executor", "web_ui", "llm", "import", "system"
    ],
    "source_admission_target": ["journal", "claim", "decision"],
    "source_admission_actor": ["pi", "brain", "executor", "web_ui"],
    "experiment_actor": ["pi", "brain", "executor", "web_ui", "llm", "import"],
    "experiment_status": ["planned", "active", "completed", "abandoned"],
    "working_tree_state": ["clean", "dirty", "unknown"],
    "experiment_run_status": [
        "queued", "running", "succeeded", "failed", "cancelled"
    ],
    "experiment_run_action": ["start", "succeed", "fail", "cancel"],
    "experiment_run_kind": ["local", "docker", "cluster", "manual", "import"],
    "experiment_observation_kind": [
        "metric", "comparison", "test", "qualitative", "failure", "artifact"
    ],
    "experiment_observation_direction": [
        "positive", "negative", "inconclusive", "neutral", "error"
    ],
    "evidence_source_kind": ["artifact", "repository"],
    "evidence_locator_kind": [
        "whole_artifact",
        "page",
        "line_range",
        "table",
        "table_cell",
        "json_pointer",
        "notebook_cell",
        "record",
    ],
    "claim_evidence_role": ["support", "qualifier", "counterevidence", "context"],
    "planning_actor": ["pi", "brain", "executor", "web_ui", "llm", "import"],
    "planning_branch_state": ["active", "selected", "archived", "superseded"],
    "planning_stage": [
        "seed", "paragraph_spine", "problem_scope", "landscape_gap",
        "response_mechanism", "challenge_innovation", "rq_contribution",
        "evaluation", "outline", "review",
    ],
    "planning_lifecycle": [
        "candidate", "reviewed", "selected", "parked", "superseded", "archived"
    ],
    "planning_origin": ["user", "ai_suggested", "imported", "user_revised"],
    "planning_readiness": ["blocked", "in_progress", "ready"],
    "semantic_patch_origin": ["human", "host_agent", "lm_studio"],
    "semantic_patch_ai_origin": ["host_agent", "lm_studio"],
    "semantic_patch_boundary": ["none", "host_conversation", "local_loopback"],
    "semantic_patch_ai_boundary": ["host_conversation", "local_loopback"],
    "semantic_patch_actor": ["pi", "brain", "executor", "web_ui"],
    "semantic_patch_status": [
        "proposed", "applied", "rejected", "conflicted", "superseded", "expired"
    ],
    "outline_action": ["edit", "expand", "condense", "reorder"],
    "evidence_status": [
        "unassessed",
        "supported",
        "partially_supported",
        "inconclusive",
        "contradicted",
    ],
    "cluster_confidence": [
        "strong",
        "moderate",
        "emerging",
        "contested",
        "refuted",
    ],
    "rq_status": [
        "open",
        "partially_answered",
        "answered",
        "reframed",
        "closed",
    ],
    "outcome": ["succeeded", "failed", "mixed", "unresolved"],
    "staleness": ["yellow", "red"],
    "ingest_source": ["brain", "executor", "pi", "import", "web_ui"],
    "resolved_by": ["pi", "brain", "executor"],
    "review_action": ["approve", "reject", "adjust"],
    "manuscript_phase": [
        "planning",
        "drafting",
        "review",
        "final",
        "submitted",
    ],
    "manuscript_state": [
        "active",
        "on_hold",
        "submitted",
        "accepted",
        "rejected",
        "withdrawn",
        "archived",
    ],
    "manuscript_claim_kind": [
        "empirical",
        "methodological",
        "theoretical",
        "survey",
        "position",
    ],
    "manuscript_claim_state": ["candidate", "active", "retired"],
    "manuscript_unit_kind": [
        "abstract",
        "introduction",
        "related_work",
        "background",
        "method",
        "result",
        "discussion",
        "limitation",
        "conclusion",
        "caption",
        "appendix",
        "other",
    ],
    "manuscript_unit_status": [
        "planned",
        "drafted",
        "reviewed",
        "final",
        "removed",
    ],
    "manuscript_unit_role": [
        "unspecified",
        "section",
        "argument_block",
        "paragraph_plan",
        "result",
        "caption",
        "appendix",
        "other",
    ],
    "manuscript_rhetorical_move": [
        "unspecified",
        "frame_problem",
        "establish_gap",
        "state_insight",
        "explain_mechanism",
        "address_challenge",
        "present_innovation",
        "pose_research_question",
        "state_contribution",
        "describe_method",
        "present_result",
        "interpret_result",
        "compare_prior_work",
        "state_limitation",
        "transition",
        "summarize",
        "other",
    ],
    "manuscript_evidence_role": [
        "support",
        "qualifier",
        "counterevidence",
    ],
    "manuscript_claim_unit_relationship": [
        "advances",
        "tests",
        "bounds",
        "mentions",
    ],
    "manuscript_citation_role": [
        "imports",
        "bounds",
        "baseline",
        "extends",
        "refutes",
    ],
    "manuscript_citation_verification_state": [
        "unverified",
        "self_attested",
        "verified",
        "rejected",
    ],
    "manuscript_checkpoint_kind": [
        "venue",
        "outline",
        "table_figure_plan",
        "reference_set",
        "draft_section",
        "final_layout",
    ],
    "manuscript_checkpoint_resolution_status": [
        "resolved",
        "rejected",
    ],
    "manuscript_verification_verdict": ["pass", "warn", "block", "error"],
    "manuscript_verification_dimension_verdict": [
        "pass",
        "warn",
        "block",
        "error",
        "not_checked",
    ],
}


def _e(*names: str) -> dict[str, list[str]]:
    """Build a sub-enum dict from named entries in ``_ENUMS``."""
    return {n: list(_ENUMS[n]) for n in names}


# ---------------------------------------------------------------------------
# OPERATIONS_SCHEMA — the curated table.
# ---------------------------------------------------------------------------
#
# Operation name keys MUST match the canonical sets in the project
# locked-decisions taxonomy. Drift between this table and the v2.7.0a3
# rka_query/rka_execute Literal enums is detected by the lock-tests.

OPERATIONS_SCHEMA: dict[str, dict[str, Any]] = {
    # =====================================================================
    # rka_query — read operations
    # =====================================================================
    "status": {
        "operation": "status",
        "tool": "rka_query",
        "category": "core",
        "role_tag": "ANY",
        "summary": "Current phase, focus, blockers — the minimal session-start probe.",
        "signature": "rka_query(operation='status', *, project_id)",
        "required_fields": ["project_id"],
        "optional_fields": [],
        "enums": {},
        "examples": [
            {
                "description": "Standard session-start status probe.",
                "call": {"operation": "status", "project_id": "prj_01ABC..."},
            },
        ],
        "related_operations": ["context", "pending_maintenance", "checkpoints"],
        "notes": None,
    },
    "context": {
        "operation": "context",
        "tool": "rka_query",
        "category": "core",
        "role_tag": "ANY",
        "summary": "Load current project state + recent knowledge for a topic.",
        "signature": (
            "rka_query(operation='context', *, project_id, query=None, "
            "filters={'phase': str})"
        ),
        "required_fields": ["project_id"],
        "optional_fields": ["query", "filters"],
        "enums": {},
        "examples": [
            {
                "description": "Pull current project context.",
                "call": {"operation": "context", "project_id": "prj_01ABC..."},
            },
            {
                "description": "Topic-scoped context.",
                "call": {
                    "operation": "context",
                    "project_id": "prj_01ABC...",
                    "query": "RAG benchmark methodology",
                },
            },
        ],
        "related_operations": ["status", "summarize", "search"],
        "notes": None,
    },
    "search": {
        "operation": "search",
        "tool": "rka_query",
        "category": "core",
        "role_tag": "ANY",
        "summary": "Full-text + semantic search across journal/decision/literature/clusters.",
        "signature": (
            "rka_query(operation='search', *, project_id, query, "
            "limit=20, filters={'entity_types': [str]})"
        ),
        "required_fields": ["project_id", "query"],
        "optional_fields": ["limit", "filters"],
        "enums": {
            "entity_types": [
                "journal",
                "decision",
                "literature",
                "mission",
                "claim",
                "cluster",
                "checkpoint",
            ],
        },
        "examples": [
            {
                "description": "Search 2-4 keyword terms.",
                "call": {
                    "operation": "search",
                    "project_id": "prj_01ABC...",
                    "query": "RAG retrieval latency",
                    "limit": 20,
                },
            },
        ],
        "related_operations": ["multi_hop", "context", "entity"],
        "notes": "Pass 2-4 keyword terms; longer queries reduce recall.",
    },
    "entity": {
        "operation": "entity",
        "tool": "rka_query",
        "category": "core",
        "role_tag": "ANY",
        "summary": "Fetch a single entity by ID (jrn_/dec_/lit_/mis_/clm_/ecl_/chk_).",
        "signature": "rka_query(operation='entity', *, project_id, id)",
        "required_fields": ["project_id", "id"],
        "optional_fields": [],
        "enums": {},
        "examples": [
            {
                "description": "Fetch a decision by ID.",
                "call": {
                    "operation": "entity",
                    "project_id": "prj_01ABC...",
                    "id": "dec_01XYZ...",
                },
            },
        ],
        "related_operations": ["provenance", "search"],
        "notes": (
            "Entity prefix is auto-routed (jrn_, dec_, lit_, mis_, clm_, ecl_, "
            "chk_). For claims, stale is hard structural invalidation while "
            "staleness is the freshness-review state; green does not override "
            "stale=true. Treat currentness as the canonical currency result."
        ),
    },
    "journal": {
        "operation": "journal",
        "tool": "rka_query",
        "category": "journal",
        "role_tag": "ANY",
        "summary": "List journal entries (notes, logs, directives).",
        "signature": (
            "rka_query(operation='journal', *, project_id, limit=20, "
            "filters={'type', 'phase', 'confidence', 'status', 'since', 'source', 'tags'})"
        ),
        "required_fields": ["project_id"],
        "optional_fields": ["limit", "filters"],
        "enums": _e("note_type", "confidence", "source"),
        "examples": [
            {
                "description": "Recent journal entries.",
                "call": {
                    "operation": "journal",
                    "project_id": "prj_01ABC...",
                    "limit": 20,
                },
            },
            {
                "description": "Only verified findings.",
                "call": {
                    "operation": "journal",
                    "project_id": "prj_01ABC...",
                    "filters": {"confidence": "verified"},
                },
            },
        ],
        "related_operations": ["entity", "search", "record_note"],
        "notes": None,
    },
    "literature": {
        "operation": "literature",
        "tool": "rka_query",
        "category": "literature",
        "role_tag": "ANY",
        "summary": "List literature entries.",
        "signature": (
            "rka_query(operation='literature', *, project_id, query=None, "
            "limit=20, filters={'status', 'tag'})"
        ),
        "required_fields": ["project_id"],
        "optional_fields": ["query", "limit", "filters"],
        "enums": _e("lit_status"),
        "examples": [
            {
                "description": "List to-read papers.",
                "call": {
                    "operation": "literature",
                    "project_id": "prj_01ABC...",
                    "filters": {"status": "to_read"},
                },
            },
        ],
        "related_operations": ["entity", "record_literature"],
        "notes": None,
    },
    "mission": {
        "operation": "mission",
        "tool": "rka_query",
        "category": "mission",
        "role_tag": "ANY",
        "summary": "Fetch a specific mission (by ID) or the current active/pending mission.",
        "signature": "rka_query(operation='mission', *, project_id, id=None)",
        "required_fields": ["project_id"],
        "optional_fields": ["id"],
        "enums": {},
        "examples": [
            {
                "description": "Get current active/pending mission.",
                "call": {"operation": "mission", "project_id": "prj_01ABC..."},
            },
            {
                "description": "Get a specific mission by ID.",
                "call": {
                    "operation": "mission",
                    "project_id": "prj_01ABC...",
                    "id": "mis_01XYZ...",
                },
            },
        ],
        "related_operations": ["report", "create_mission", "update_mission"],
        "notes": "Omit `id` to auto-locate the most recent active or pending mission.",
    },
    "report": {
        "operation": "report",
        "tool": "rka_query",
        "category": "mission",
        "role_tag": "ANY",
        "summary": "Fetch a mission's submitted report.",
        "signature": "rka_query(operation='report', *, project_id, id)",
        "required_fields": ["project_id", "id"],
        "optional_fields": [],
        "enums": {},
        "examples": [
            {
                "description": "Get a mission's report.",
                "call": {
                    "operation": "report",
                    "project_id": "prj_01ABC...",
                    "id": "mis_01XYZ...",
                },
            },
        ],
        "related_operations": ["mission", "submit_report"],
        "notes": "`id` is the mission_id, not a report_id.",
    },
    "checkpoints": {
        "operation": "checkpoints",
        "tool": "rka_query",
        "category": "checkpoint",
        "role_tag": "ANY",
        "summary": "List checkpoints (default: open).",
        "signature": (
            "rka_query(operation='checkpoints', *, project_id, "
            "filters={'status': 'open'|'resolved'|'all'})"
        ),
        "required_fields": ["project_id"],
        "optional_fields": ["filters"],
        "enums": {"status": ["open", "resolved", "all"]},
        "examples": [
            {
                "description": "List open checkpoints.",
                "call": {
                    "operation": "checkpoints",
                    "project_id": "prj_01ABC...",
                    "filters": {"status": "open"},
                },
            },
        ],
        "related_operations": [
            "submit_checkpoint",
            "resolve_checkpoint",
            "entity",
        ],
        "notes": None,
    },
    "decision_tree": {
        "operation": "decision_tree",
        "tool": "rka_query",
        "category": "decision",
        "role_tag": "ANY",
        "summary": "Render the decision tree, optionally rooted at a decision.",
        "signature": (
            "rka_query(operation='decision_tree', *, project_id, id=None, "
            "filters={'phase', 'active_only'})"
        ),
        "required_fields": ["project_id"],
        "optional_fields": ["id", "filters"],
        "enums": {},
        "examples": [
            {
                "description": "Full active decision tree.",
                "call": {
                    "operation": "decision_tree",
                    "project_id": "prj_01ABC...",
                    "filters": {"active_only": True},
                },
            },
        ],
        "related_operations": ["entity", "graph", "research_map"],
        "notes": None,
    },
    "calibration_metrics": {
        "operation": "calibration_metrics",
        "tool": "rka_query",
        "category": "calibration",
        "role_tag": "BRAIN",
        "summary": "Aggregate calibration outcomes for decisions you've made.",
        "signature": "rka_query(operation='calibration_metrics', *, project_id)",
        "required_fields": ["project_id"],
        "optional_fields": [],
        "enums": {},
        "examples": [
            {
                "description": "Fetch project-wide calibration stats.",
                "call": {
                    "operation": "calibration_metrics",
                    "project_id": "prj_01ABC...",
                },
            },
        ],
        "related_operations": ["record_outcome"],
        "notes": None,
    },
    "hooks": {
        "operation": "hooks",
        "tool": "rka_query",
        "category": "hooks",
        "role_tag": "ANY",
        "summary": "List configured hooks.",
        "signature": (
            "rka_query(operation='hooks', *, project_id, filters={'event', 'enabled_only'})"
        ),
        "required_fields": ["project_id"],
        "optional_fields": ["filters"],
        "enums": {},
        "examples": [
            {
                "description": "List all hooks.",
                "call": {"operation": "hooks", "project_id": "prj_01ABC..."},
            },
        ],
        "related_operations": [
            "hook_executions",
            "hook_add",
            "hook_enable",
            "hook_disable",
        ],
        "notes": None,
    },
    "hook_executions": {
        "operation": "hook_executions",
        "tool": "rka_query",
        "category": "hooks",
        "role_tag": "ANY",
        "summary": "Recent hook execution history.",
        "signature": (
            "rka_query(operation='hook_executions', *, project_id, limit=100, "
            "filters={'hook_id', 'since', 'status'})"
        ),
        "required_fields": ["project_id"],
        "optional_fields": ["limit", "filters"],
        "enums": {},
        "examples": [
            {
                "description": "Last 100 executions.",
                "call": {
                    "operation": "hook_executions",
                    "project_id": "prj_01ABC...",
                },
            },
        ],
        "related_operations": ["hooks"],
        "notes": None,
    },
    "brain_notifications": {
        "operation": "brain_notifications",
        "tool": "rka_query",
        "category": "notifications",
        "role_tag": "BRAIN",
        "summary": "Notifications queued for the Brain.",
        "signature": (
            "rka_query(operation='brain_notifications', *, project_id, "
            "limit=100, filters={'since', 'include_cleared'})"
        ),
        "required_fields": ["project_id"],
        "optional_fields": ["limit", "filters"],
        "enums": {},
        "examples": [
            {
                "description": "Outstanding notifications.",
                "call": {
                    "operation": "brain_notifications",
                    "project_id": "prj_01ABC...",
                },
            },
        ],
        "related_operations": ["brain_notifications_clear"],
        "notes": None,
    },
    "research_map": {
        "operation": "research_map",
        "tool": "rka_query",
        "category": "research_map",
        "role_tag": "ANY",
        "summary": "Top-level research map: RQs -> clusters -> claims with synthesis.",
        "signature": "rka_query(operation='research_map', *, project_id)",
        "required_fields": ["project_id"],
        "optional_fields": [],
        "enums": {},
        "examples": [
            {
                "description": "Render the research map.",
                "call": {
                    "operation": "research_map",
                    "project_id": "prj_01ABC...",
                },
            },
        ],
        "related_operations": ["clusters", "claims", "decision_tree"],
        "notes": None,
    },
    "review_queue": {
        "operation": "review_queue",
        "tool": "rka_query",
        "category": "review",
        "role_tag": "BRAIN",
        "summary": "Pending review items (claims, clusters needing synthesis, etc.).",
        "signature": (
            "rka_query(operation='review_queue', *, project_id, limit=20, "
            "filters={'status': 'pending'|'reviewed'|'all', 'target_type'})"
        ),
        "required_fields": ["project_id"],
        "optional_fields": ["limit", "filters"],
        "enums": {"status": ["pending", "reviewed", "all"]},
        "examples": [
            {
                "description": "Pending review items.",
                "call": {
                    "operation": "review_queue",
                    "project_id": "prj_01ABC...",
                    "filters": {"status": "pending"},
                },
            },
        ],
        "related_operations": ["review_claims", "review_cluster"],
        "notes": None,
    },
    "clusters": {
        "operation": "clusters",
        "tool": "rka_query",
        "category": "claims",
        "role_tag": "ANY",
        "summary": "List evidence clusters.",
        "signature": (
            "rka_query(operation='clusters', *, project_id, limit=50, "
            "filters={'research_question_id', 'confidence'})"
        ),
        "required_fields": ["project_id"],
        "optional_fields": ["limit", "filters"],
        "enums": _e("cluster_confidence"),
        "examples": [
            {
                "description": "Strong-confidence clusters.",
                "call": {
                    "operation": "clusters",
                    "project_id": "prj_01ABC...",
                    "filters": {"confidence": "strong"},
                },
            },
        ],
        "related_operations": [
            "claims",
            "create_cluster",
            "review_cluster",
            "assign_claims_to_cluster",
        ],
        "notes": None,
    },
    "claims": {
        "operation": "claims",
        "tool": "rka_query",
        "category": "claims",
        "role_tag": "ANY",
        "summary": "List claims.",
        "signature": (
            "rka_query(operation='claims', *, project_id, limit=20, "
            "filters={'source_entry_id', 'cluster_id', 'claim_type', "
            "'verified', 'evidence_status', 'stale'})"
        ),
        "required_fields": ["project_id"],
        "optional_fields": ["limit", "filters"],
        "enums": _e("claim_type", "evidence_status"),
        "examples": [
            {
                "description": "Claims from a journal entry.",
                "call": {
                    "operation": "claims",
                    "project_id": "prj_01ABC...",
                    "filters": {"source_entry_id": "jrn_01XYZ..."},
                },
            },
        ],
        "related_operations": [
            "clusters",
            "extract_claims",
            "review_claims",
        ],
        "notes": (
            "verified filters source-grounding fidelity. evidence_status "
            "filters the independent scientific evidence assessment."
        ),
    },
    "claim_scope": {
        "operation": "claim_scope",
        "tool": "rka_query",
        "category": "claims",
        "role_tag": "ANY",
        "summary": "Fetch immutable scope history and readiness for one claim.",
        "signature": "rka_query(operation='claim_scope', *, project_id, id)",
        "required_fields": ["project_id", "id"],
        "optional_fields": [],
        "enums": _e("claim_scope_readiness"),
        "examples": [
            {
                "description": "Inspect the current scope and prior revisions.",
                "call": {
                    "operation": "claim_scope",
                    "project_id": "prj_01ABC...",
                    "id": "clm_01XYZ...",
                },
            },
        ],
        "related_operations": ["claims", "set_claim_scope"],
        "notes": "Legacy claims return readiness=missing; no scope is invented.",
    },
    "interpretation_candidates": {
        "operation": "interpretation_candidates",
        "tool": "rka_query",
        "category": "claims",
        "role_tag": "ANY",
        "summary": "List reviewable interpretations or fetch one detailed candidate.",
        "signature": (
            "rka_query(operation='interpretation_candidates', *, project_id, "
            "id=None, limit=50, filters={'review_status', 'disposition', "
            "'epistemic_kind', 'source_type', 'source_id'})"
        ),
        "required_fields": ["project_id"],
        "optional_fields": ["limit", "filters", "id"],
        "enums": _e(
            "interpretation_review_status",
            "interpretation_disposition",
            "epistemic_kind",
            "interpretation_source",
        ),
        "examples": [
            {
                "description": "Load pending candidate review work.",
                "call": {
                    "operation": "interpretation_candidates",
                    "project_id": "prj_01ABC...",
                    "filters": {"review_status": "pending"},
                },
            },
            {
                "description": "Inspect one candidate and its immutable history.",
                "call": {
                    "operation": "interpretation_candidates",
                    "project_id": "prj_01ABC...",
                    "id": "icd_01XYZ...",
                },
            },
        ],
        "related_operations": [
            "create_interpretation_candidate",
            "add_interpretation_hint",
            "triage_interpretation_candidate",
            "claims",
        ],
        "notes": "Candidates are not canonical claims or scientific support.",
    },
    "sources": {
        "operation": "sources",
        "tool": "rka_query",
        "category": "sources",
        "role_tag": "ANY",
        "summary": "List registered sources or inspect one provenance envelope.",
        "signature": (
            "rka_query(operation='sources', *, project_id, id=None, limit=50, "
            "filters={'source_kind', 'ownership_kind'})"
        ),
        "required_fields": ["project_id"],
        "optional_fields": ["limit", "filters", "id"],
        "enums": _e("registered_source_kind", "registered_source_ownership"),
        "examples": [
            {
                "description": "Inspect one source, its artifact, and explicit admissions.",
                "call": {
                    "operation": "sources",
                    "project_id": "prj_01ABC...",
                    "id": "src_01XYZ...",
                },
            }
        ],
        "related_operations": [
            "register_source",
            "create_interpretation_candidate",
            "admit_source_interpretation",
        ],
        "notes": "A registered source is non-canonical until explicit admission.",
    },
    "experiments": {
        "operation": "experiments",
        "tool": "rka_query",
        "category": "experiments",
        "role_tag": "ANY",
        "summary": "List experiments or fetch one with immutable plan and run history.",
        "signature": (
            "rka_query(operation='experiments', *, project_id, id=None, "
            "limit=50, filters={'status'})"
        ),
        "required_fields": ["project_id"],
        "optional_fields": ["limit", "filters", "id"],
        "enums": _e("experiment_status"),
        "examples": [{
            "description": "Inspect an experiment and its exact plan history.",
            "call": {
                "operation": "experiments",
                "project_id": "prj_01ABC...",
                "id": "exp_01XYZ...",
            },
        }],
        "related_operations": [
            "create_experiment", "append_experiment_plan", "experiment_runs"
        ],
        "notes": "A plan version is immutable; lifecycle status is revision guarded.",
    },
    "experiment_runs": {
        "operation": "experiment_runs",
        "tool": "rka_query",
        "category": "experiments",
        "role_tag": "ANY",
        "summary": "List plan-bound runs or fetch one with events and observations.",
        "signature": (
            "rka_query(operation='experiment_runs', *, project_id, id=None, "
            "limit=50, filters={'experiment_id', 'status'})"
        ),
        "required_fields": ["project_id"],
        "optional_fields": ["limit", "filters", "id"],
        "enums": _e("experiment_run_status"),
        "examples": [{
            "description": "Inspect one run and its append-only lifecycle events.",
            "call": {
                "operation": "experiment_runs",
                "project_id": "prj_01ABC...",
                "id": "run_01XYZ...",
            },
        }],
        "related_operations": [
            "experiments", "create_experiment_run", "experiment_observations"
        ],
        "notes": "A succeeded run establishes execution status, not scientific support.",
    },
    "experiment_observations": {
        "operation": "experiment_observations",
        "tool": "rka_query",
        "category": "experiments",
        "role_tag": "ANY",
        "summary": "List immutable observations or fetch exact locator and review lineage.",
        "signature": (
            "rka_query(operation='experiment_observations', *, project_id, "
            "id=None, limit=50, filters={'run_id', 'direction', 'kind', 'claim_id'})"
        ),
        "required_fields": ["project_id"],
        "optional_fields": ["limit", "filters", "id"],
        "enums": _e("experiment_observation_direction", "experiment_observation_kind"),
        "examples": [{
            "description": "Inspect an observation and its auditable evidence locators.",
            "call": {
                "operation": "experiment_observations",
                "project_id": "prj_01ABC...",
                "id": "obs_01XYZ...",
            },
        }],
        "related_operations": [
            "record_experiment_observation",
            "add_evidence_locator",
            "create_interpretation_candidate",
            "triage_interpretation_candidate",
        ],
        "notes": "Observation direction is preserved even when negative or inconclusive.",
    },
    "manuscript": {
        "operation": "manuscript",
        "tool": "rka_query",
        "category": "manuscript",
        "role_tag": "ANY",
        "summary": "Fetch a manuscript by canonical or compatibility ID.",
        "signature": "rka_query(operation='manuscript', *, project_id, id)",
        "required_fields": ["project_id", "id"],
        "optional_fields": [],
        "enums": {},
        "examples": [
            {
                "description": "Fetch by manuscript id.",
                "call": {
                    "operation": "manuscript",
                    "project_id": "prj_01ABC...",
                    "id": "man_01XYZ...",
                },
            },
        ],
        "related_operations": [
            "register_manuscript",
            "create_manuscript",
            "manuscript_context",
        ],
        "notes": (
            "Accepts canonical man_ IDs and compatibility jrn_ aliases. "
            "Legacy-ID responses include canonical_id and deprecation metadata."
        ),
    },
    "manuscript_reference_manifest": {
        "operation": "manuscript_reference_manifest",
        "tool": "rka_query",
        "category": "manuscript",
        "role_tag": "ANY",
        "summary": ("Read the authoritative citation-key membership and validation state."),
        "signature": ("rka_query(operation='manuscript_reference_manifest', *, project_id, id)"),
        "required_fields": ["project_id", "id"],
        "optional_fields": [],
        "enums": {},
        "examples": [
            {
                "description": "Read active references before drafting.",
                "call": {
                    "operation": "manuscript_reference_manifest",
                    "project_id": "prj_01ABC...",
                    "id": "man_01XYZ...",
                },
            },
        ],
        "related_operations": [
            "replace_manuscript_reference_manifest",
            "manuscript_readiness",
        ],
        "notes": (
            "Membership is project-scoped and authoritative. Each active "
            "citation key is bound to one literature record and reports the "
            "latest exact validation attempt; historical unbound validations "
            "cannot authorize a citation."
        ),
    },
    "reference_validation_status": {
        "operation": "reference_validation_status",
        "tool": "rka_query",
        "category": "literature",
        "role_tag": "ANY",
        "summary": "Read one historical manuscript-reference validation job.",
        "signature": (
            "rka_query(operation='reference_validation_status', *, project_id, "
            "manuscript_id, job_id)"
        ),
        "required_fields": ["project_id", "manuscript_id", "job_id"],
        "optional_fields": [],
        "enums": {},
        "examples": [
            {
                "description": "Read a validation job recorded before the Writer split.",
                "call": {
                    "operation": "reference_validation_status",
                    "project_id": "prj_01ABC...",
                    "manuscript_id": "man_01XYZ...",
                    "job_id": "job_01VALIDATION...",
                },
            },
        ],
        "related_operations": ["manuscript", "manuscript_reference_manifest"],
        "notes": (
            "The job is project- and manuscript-scoped. Completed responses "
            "carry `result`; failed responses carry `error`. Unknown or "
            "cross-scope jobs return 404. Core no longer initiates or executes "
            "reference validation; external Writer or client workflows may "
            "perform verification separately."
        ),
    },
    "resolve_entities": {
        "operation": "resolve_entities",
        "tool": "rka_query",
        "category": "core",
        "role_tag": "ANY",
        "summary": "Bulk-resolve heterogeneous IDs with project attestation.",
        "signature": (
            "rka_query(operation='resolve_entities', *, project_id, ids, "
            "include_sources=False, include_edges=False)"
        ),
        "required_fields": ["project_id", "ids"],
        "optional_fields": ["include_sources", "include_edges"],
        "enums": {},
        "examples": [
            {
                "description": "Resolve evidence IDs with terminal sources.",
                "call": {
                    "operation": "resolve_entities",
                    "project_id": "prj_01ABC...",
                    "ids": ["clm_01CLAIM...", "dec_01DECISION..."],
                    "include_sources": True,
                },
            },
        ],
        "related_operations": ["entity", "manuscript_context"],
        "notes": (
            "Returns one explicit outcome per requested ID. Foreign-project "
            "records are reported opaquely and never returned as resolved."
        ),
    },
    "changes_since": {
        "operation": "changes_since",
        "tool": "rka_query",
        "category": "maintenance",
        "role_tag": "ANY",
        "summary": "Page the durable project-scoped semantic change ledger.",
        "signature": ("rka_query(operation='changes_since', *, project_id, cursor=0, limit=100)"),
        "required_fields": ["project_id"],
        "optional_fields": ["cursor", "limit"],
        "enums": {},
        "examples": [
            {
                "description": "Continue after the last delivered cursor.",
                "call": {
                    "operation": "changes_since",
                    "project_id": "prj_01ABC...",
                    "cursor": 120,
                    "limit": 100,
                },
            },
        ],
        "related_operations": ["manuscript_impact", "manuscript_spine"],
        "notes": (
            "Returns next_cursor, latest_cursor, and has_more. Continue from "
            "next_cursor while has_more is true; cursors are project scoped."
        ),
    },
    "manuscript_context": {
        "operation": "manuscript_context",
        "tool": "rka_query",
        "category": "manuscript",
        "role_tag": "ANY",
        "summary": "Read the authoritative native manuscript aggregate.",
        "signature": ("rka_query(operation='manuscript_context', *, project_id, id)"),
        "required_fields": ["project_id", "id"],
        "optional_fields": [],
        "enums": {},
        "examples": [
            {
                "description": "Load claims, units, checkpoints, and attestations.",
                "call": {
                    "operation": "manuscript_context",
                    "project_id": "prj_01ABC...",
                    "id": "man_01XYZ...",
                },
            },
        ],
        "related_operations": [
            "manuscript",
            "manuscript_readiness",
            "manuscript_spine",
            "manuscript_writing_candidates",
        ],
        "notes": "RKA is authoritative; Writer files are projections.",
    },
    "manuscript_readiness": {
        "operation": "manuscript_readiness",
        "tool": "rka_query",
        "category": "manuscript",
        "role_tag": "ANY",
        "summary": "Evaluate evidence-linked readiness for a lifecycle phase.",
        "signature": (
            "rka_query(operation='manuscript_readiness', *, project_id, id, "
            "target_phase='drafting')"
        ),
        "required_fields": ["project_id", "id"],
        "optional_fields": ["target_phase"],
        "enums": _e("manuscript_phase"),
        "examples": [
            {
                "description": "Check whether the manuscript can enter review.",
                "call": {
                    "operation": "manuscript_readiness",
                    "project_id": "prj_01ABC...",
                    "id": "man_01XYZ...",
                    "target_phase": "review",
                },
            },
        ],
        "related_operations": [
            "transition_manuscript_phase",
            "manuscript_context",
        ],
        "notes": (
            "Readiness is mechanical and evidence-linked; it does not infer "
            "scientific validity or PI ratification."
        ),
    },
    "manuscript_spine": {
        "operation": "manuscript_spine",
        "tool": "rka_query",
        "category": "manuscript",
        "role_tag": "ANY",
        "summary": "Export the deterministic Writer projection of the RKA spine.",
        "signature": ("rka_query(operation='manuscript_spine', *, project_id, id)"),
        "required_fields": ["project_id", "id"],
        "optional_fields": [],
        "enums": {},
        "examples": [
            {
                "description": "Export a Writer-cache projection.",
                "call": {
                    "operation": "manuscript_spine",
                    "project_id": "prj_01ABC...",
                    "id": "man_01XYZ...",
                },
            },
        ],
        "related_operations": [
            "upsert_argument_spine",
            "manuscript_context",
        ],
        "notes": "The export is a cache projection, not an independent store.",
    },
    "manuscript_outline": {
        "operation": "manuscript_outline",
        "tool": "rka_query",
        "category": "manuscript",
        "role_tag": "ANY",
        "summary": "Read L2-L5 outline rationale, bindings, blockers, and checkpoint state.",
        "signature": "rka_query(operation='manuscript_outline', *, project_id, id)",
        "required_fields": ["project_id", "id"],
        "optional_fields": [],
        "enums": {},
        "examples": [{
            "description": "Resume the current progressive outline.",
            "call": {
                "operation": "manuscript_outline",
                "project_id": "prj_01ABC...",
                "id": "man_01XYZ...",
            },
        }],
        "related_operations": [
            "prepare_manuscript_outline_proposal",
            "create_manuscript_checkpoint",
            "manuscript_spine",
        ],
        "notes": "This projection is read-only; structural edits are proposals.",
    },
    "manuscript_writing_candidates": {
        "operation": "manuscript_writing_candidates",
        "tool": "rka_query",
        "category": "manuscript",
        "role_tag": "ANY",
        "summary": (
            "Discover writing candidates through reviewed clusters and research questions."
        ),
        "signature": ("rka_query(operation='manuscript_writing_candidates', *, project_id, id)"),
        "required_fields": ["project_id", "id"],
        "optional_fields": [],
        "enums": {},
        "examples": [
            {
                "description": (
                    "Inspect smoothed, unratified candidates before building the manuscript spine."
                ),
                "call": {
                    "operation": "manuscript_writing_candidates",
                    "project_id": "prj_01ABC...",
                    "id": "man_01XYZ...",
                },
            },
        ],
        "related_operations": [
            "research_map",
            "review_cluster",
            "manuscript_context",
            "upsert_argument_spine",
        ],
        "notes": (
            "This is a read-only project-research-map discovery view. It "
            "never promotes journals or claims, and every returned candidate "
            "still requires PI selection, exact wording, and ratification."
        ),
    },
    "manuscript_impact": {
        "operation": "manuscript_impact",
        "tool": "rka_query",
        "category": "manuscript",
        "role_tag": "ANY",
        "summary": "Map changed dependencies to manuscript claims and units.",
        "signature": (
            "rka_query(operation='manuscript_impact', *, project_id, id, since_cursor=0, limit=100)"
        ),
        "required_fields": ["project_id", "id"],
        "optional_fields": ["since_cursor", "limit"],
        "enums": {},
        "examples": [
            {
                "description": "Find Writer units affected since synchronization.",
                "call": {
                    "operation": "manuscript_impact",
                    "project_id": "prj_01ABC...",
                    "id": "man_01XYZ...",
                    "since_cursor": 120,
                    "limit": 100,
                },
            },
        ],
        "related_operations": [
            "changes_since",
            "manuscript_context",
            "manuscript_spine",
        ],
        "notes": (
            "Returns next_cursor/latest_cursor/has_more even when one page has "
            "no relevant changes. Continue until has_more is false."
        ),
    },
    "graph": {
        "operation": "graph",
        "tool": "rka_query",
        "category": "graph",
        "role_tag": "ANY",
        "summary": "Subset of the research-knowledge graph (nodes + edges).",
        "signature": (
            "rka_query(operation='graph', *, project_id, limit=500, "
            "filters={'include_types', 'phase'})"
        ),
        "required_fields": ["project_id"],
        "optional_fields": ["limit", "filters"],
        "enums": {},
        "examples": [
            {
                "description": "Decision/journal subgraph.",
                "call": {
                    "operation": "graph",
                    "project_id": "prj_01ABC...",
                    "filters": {"include_types": ["decision", "journal"]},
                },
            },
        ],
        "related_operations": [
            "ego_graph",
            "graph_stats",
            "graph_mermaid",
        ],
        "notes": None,
    },
    "ego_graph": {
        "operation": "ego_graph",
        "tool": "rka_query",
        "category": "graph",
        "role_tag": "ANY",
        "summary": "Local neighborhood graph centered on one entity.",
        "signature": (
            "rka_query(operation='ego_graph', *, project_id, id, filters={'depth': int})"
        ),
        "required_fields": ["project_id", "id"],
        "optional_fields": ["filters"],
        "enums": {},
        "examples": [
            {
                "description": "1-hop neighborhood of a decision.",
                "call": {
                    "operation": "ego_graph",
                    "project_id": "prj_01ABC...",
                    "id": "dec_01XYZ...",
                    "filters": {"depth": 1},
                },
            },
        ],
        "related_operations": ["graph", "provenance"],
        "notes": None,
    },
    "graph_stats": {
        "operation": "graph_stats",
        "tool": "rka_query",
        "category": "graph",
        "role_tag": "ANY",
        "summary": "Top-level node/edge counts.",
        "signature": "rka_query(operation='graph_stats', *, project_id)",
        "required_fields": ["project_id"],
        "optional_fields": [],
        "enums": {},
        "examples": [
            {
                "description": "Project graph cardinality.",
                "call": {
                    "operation": "graph_stats",
                    "project_id": "prj_01ABC...",
                },
            },
        ],
        "related_operations": ["graph"],
        "notes": None,
    },
    "graph_mermaid": {
        "operation": "graph_mermaid",
        "tool": "rka_query",
        "category": "graph",
        "role_tag": "ANY",
        "summary": "Render the decision tree as Mermaid graph syntax.",
        "signature": (
            "rka_query(operation='graph_mermaid', *, project_id, filters={'phase', 'active_only'})"
        ),
        "required_fields": ["project_id"],
        "optional_fields": ["filters"],
        "enums": {},
        "examples": [
            {
                "description": "Active-only decision tree as Mermaid.",
                "call": {
                    "operation": "graph_mermaid",
                    "project_id": "prj_01ABC...",
                    "filters": {"active_only": True},
                },
            },
        ],
        "related_operations": ["graph", "decision_tree"],
        "notes": None,
    },
    "provenance": {
        "operation": "provenance",
        "tool": "rka_query",
        "category": "graph",
        "role_tag": "ANY",
        "summary": "Trace the reasoning chain that produced an entity.",
        "signature": (
            "rka_query(operation='provenance', *, project_id, id, "
            "filters={'direction': 'forward'|'backward'|'both', "
            "'max_depth': 1..3})"
        ),
        "required_fields": ["project_id", "id"],
        "optional_fields": ["filters"],
        "enums": {
            "direction": [
                "forward", "backward", "both", "downstream", "upstream"
            ]
        },
        "examples": [
            {
                "description": "Trace what justifies a decision.",
                "call": {
                    "operation": "provenance",
                    "project_id": "prj_01ABC...",
                    "id": "dec_01XYZ...",
                    "filters": {"direction": "backward"},
                },
            },
        ],
        "related_operations": ["ego_graph", "entity"],
        "notes": (
            "max_depth defaults to 3 and must be between 1 and 3. forward "
            "means what this entity led to; backward means what led to it. "
            "downstream and upstream are accepted as legacy aliases. Unknown "
            "directions are rejected. Contradictions are shown separately as "
            "non-causal context, never as before/after links."
        ),
    },
    "multi_hop": {
        "operation": "multi_hop",
        "tool": "rka_query",
        "category": "graph",
        "role_tag": "BRAIN",
        "summary": "Multi-hop graph retrieval seeded by query or explicit seed IDs.",
        "signature": (
            "rka_query(operation='multi_hop', *, project_id, query, "
            "filters={'seeds', 'max_depth', 'max_nodes', 'edge_weights'})"
        ),
        "required_fields": ["project_id", "query"],
        "optional_fields": ["filters"],
        "enums": {},
        "examples": [
            {
                "description": "Retrieve a topic-relevant subgraph.",
                "call": {
                    "operation": "multi_hop",
                    "project_id": "prj_01ABC...",
                    "query": "embedding-model selection rationale",
                    "filters": {"max_depth": 3, "max_nodes": 50},
                },
            },
        ],
        "related_operations": ["search", "provenance"],
        "notes": "Pass `filters.seeds=[...]` to override the query-based seeding.",
    },
    "collect_report_context": {
        "operation": "collect_report_context",
        "tool": "rka_query",
        "category": "graph",
        "role_tag": "ANY",
        "summary": (
            "Collect the node set relevant to a report described in prose — "
            "multi-angle search seeding + link-graph expansion with seed "
            "protection and per-node inclusion provenance."
        ),
        "signature": (
            "rka_query(operation='collect_report_context', *, project_id, "
            "query, filters={'angle_queries', 'max_depth', 'max_nodes', "
            "'seed_limit'})"
        ),
        "required_fields": ["project_id", "query"],
        "optional_fields": ["filters"],
        "enums": {},
        "examples": [
            {
                "description": (
                    "Assemble report context with angle decomposition "
                    "(ALWAYS provide angle_queries — short 1-4 word queries "
                    "from different angles of the description)."
                ),
                "call": {
                    "operation": "collect_report_context",
                    "project_id": "prj_01ABC...",
                    "query": (
                        "Report on how the embedding stack became pluggable: "
                        "motivation, backends, config persistence, dimension "
                        "fix, bugs found"
                    ),
                    "filters": {
                        "angle_queries": [
                            "pluggable embeddings",
                            "fastembed",
                            "embedding config",
                            "dimension mismatch",
                        ],
                        "max_depth": 2,
                        "max_nodes": 60,
                    },
                },
            },
        ],
        "related_operations": ["multi_hop", "search", "ego_graph"],
        "notes": (
            "Each returned node carries `included_via` (angle query + rank, "
            "or parent + link_type) so the bundle is auditable. Follow up: "
            "verify borderline nodes by content, re-search thin dimensions."
        ),
    },
    "staleness_impact": {
        "operation": "staleness_impact",
        "tool": "rka_query",
        "category": "graph",
        "role_tag": "ANY",
        "summary": (
            "Downstream blast-radius of a stale entity: everything whose "
            "reasoning rests on it, via dependent-direction links."
        ),
        "signature": (
            "rka_query(operation='staleness_impact', *, project_id, id, filters={'max_depth': 3})"
        ),
        "required_fields": ["project_id", "id"],
        "optional_fields": ["filters"],
        "enums": {},
        "examples": [
            {
                "description": "What rests on a decision about to be superseded?",
                "call": {
                    "operation": "staleness_impact",
                    "project_id": "prj_01ABC...",
                    "id": "dec_01ABC...",
                },
            },
        ],
        "related_operations": ["ego_graph", "multi_hop", "freshness"],
        "notes": "Raw observations (produced links) are immutable and excluded.",
    },
    "mission_guard": {
        "operation": "mission_guard",
        "tool": "rka_query",
        "category": "mission",
        "role_tag": "EXECUTOR",
        "summary": (
            "Negative knowledge for mission pickup: retracted/superseded "
            "findings and unresolved contradictions relevant to the objective."
        ),
        "signature": "rka_query(operation='mission_guard', *, project_id, id)",
        "required_fields": ["project_id", "id"],
        "optional_fields": [],
        "enums": {},
        "examples": [
            {
                "description": "Guard check at mission pickup.",
                "call": {
                    "operation": "mission_guard",
                    "project_id": "prj_01ABC...",
                    "id": "mis_01ABC...",
                },
            },
        ],
        "related_operations": ["mission", "context", "contradictions"],
        "notes": "Call alongside mission context; warnings list approaches already falsified.",
    },
    "belief_as_of": {
        "operation": "belief_as_of",
        "tool": "rka_query",
        "category": "graph",
        "role_tag": "ANY",
        "summary": (
            "Reconstruct the believed-current decisions and journal at a past "
            "date, plus what changed since."
        ),
        "signature": "rka_query(operation='belief_as_of', *, project_id, query='<ISO date>')",
        "required_fields": ["project_id", "query"],
        "optional_fields": [],
        "enums": {},
        "examples": [
            {
                "description": "What did we believe in mid-March?",
                "call": {
                    "operation": "belief_as_of",
                    "project_id": "prj_01ABC...",
                    "query": "2026-03-15",
                },
            },
        ],
        "related_operations": ["changelog", "staleness_impact"],
        "notes": (
            "Supersession transitions are exact (successor created_at); "
            "retraction transitions are approximated by updated_at."
        ),
    },
    "summarize": {
        "operation": "summarize",
        "tool": "rka_query",
        "category": "summary",
        "role_tag": "ANY",
        "summary": "Topic-scoped summarization across the knowledge graph.",
        "signature": (
            "rka_query(operation='summarize', *, project_id, query=None, "
            "filters={'topic', 'phase', 'entity_ids'})"
        ),
        "required_fields": ["project_id"],
        "optional_fields": ["query", "filters"],
        "enums": {},
        "examples": [
            {
                "description": "Summarize a topic.",
                "call": {
                    "operation": "summarize",
                    "project_id": "prj_01ABC...",
                    "query": "RAG latency findings",
                },
            },
        ],
        "related_operations": ["generate_summary", "context"],
        "notes": None,
    },
    "generate_summary": {
        "operation": "generate_summary",
        "tool": "rka_query",
        "category": "summary",
        "role_tag": "ANY",
        "summary": "Render a structured summary (project / mission / decision scope).",
        "signature": (
            "rka_query(operation='generate_summary', *, project_id, id=None, "
            "filters={'scope_type': 'project'|'mission'|'decision', "
            "'scope_id', 'granularity'})"
        ),
        "required_fields": ["project_id"],
        "optional_fields": ["id", "filters"],
        "enums": {
            "scope_type": ["project", "mission", "decision"],
            "granularity": ["paragraph", "outline", "bullet_list"],
        },
        "examples": [
            {
                "description": "Project-scoped paragraph summary.",
                "call": {
                    "operation": "generate_summary",
                    "project_id": "prj_01ABC...",
                    "filters": {"scope_type": "project"},
                },
            },
        ],
        "related_operations": ["summarize"],
        "notes": (
            "v2.4.0 removed the LLM-driven path; this scope is a stub "
            "until it is rewired through the orchestrator."
        ),
    },
    "evidence": {
        "operation": "evidence",
        "tool": "rka_query",
        "category": "claims",
        "role_tag": "BRAIN",
        "summary": "Assemble structured evidence supporting a research question.",
        "signature": (
            "rka_query(operation='evidence', *, project_id, id, "
            "filters={'format': 'progress_report'|'briefing'|'audit'})"
        ),
        "required_fields": ["project_id", "id"],
        "optional_fields": ["filters"],
        "enums": {
            "format": ["progress_report", "briefing", "audit"],
        },
        "examples": [
            {
                "description": "Progress report for a RQ.",
                "call": {
                    "operation": "evidence",
                    "project_id": "prj_01ABC...",
                    "id": "dec_01XYZ...",
                    "filters": {"format": "progress_report"},
                },
            },
        ],
        "related_operations": ["clusters", "claims", "advance_rq"],
        "notes": "`id` is the research_question_id (stored as a decision id with kind='research_question').",
    },
    "freshness": {
        "operation": "freshness",
        "tool": "rka_query",
        "category": "maintenance",
        "role_tag": "BRAIN",
        "summary": "Check freshness/staleness of claims and clusters.",
        "signature": (
            "rka_query(operation='freshness', *, project_id, filters={'days_threshold': int})"
        ),
        "required_fields": ["project_id"],
        "optional_fields": ["filters"],
        "enums": {},
        "examples": [
            {
                "description": "Check 30-day freshness.",
                "call": {
                    "operation": "freshness",
                    "project_id": "prj_01ABC...",
                    "filters": {"days_threshold": 30},
                },
            },
        ],
        "related_operations": ["flag_stale", "pending_maintenance"],
        "notes": None,
    },
    "contradictions": {
        "operation": "contradictions",
        "tool": "rka_query",
        "category": "maintenance",
        "role_tag": "BRAIN",
        "summary": "Detect contradicting evidence near a given entity.",
        "signature": (
            "rka_query(operation='contradictions', *, project_id, id, "
            "filters={'similarity_threshold': float, 'max_results': int})"
        ),
        "required_fields": ["project_id", "id"],
        "optional_fields": ["filters"],
        "enums": {},
        "examples": [
            {
                "description": "Find contradictions near a claim.",
                "call": {
                    "operation": "contradictions",
                    "project_id": "prj_01ABC...",
                    "id": "clm_01XYZ...",
                },
            },
        ],
        "related_operations": ["resolve_contradiction"],
        "notes": None,
    },
    "integrity": {
        "operation": "integrity",
        "tool": "rka_query",
        "category": "maintenance",
        "role_tag": "BRAIN",
        "summary": "Database integrity probe (orphans, broken links).",
        "signature": "rka_query(operation='integrity', *, project_id)",
        "required_fields": ["project_id"],
        "optional_fields": [],
        "enums": {},
        "examples": [
            {
                "description": "Quick integrity scan.",
                "call": {
                    "operation": "integrity",
                    "project_id": "prj_01ABC...",
                },
            },
        ],
        "related_operations": ["pending_maintenance"],
        "notes": None,
    },
    "pending_maintenance": {
        "operation": "pending_maintenance",
        "tool": "rka_query",
        "category": "maintenance",
        "role_tag": "BRAIN",
        "summary": "Provenance gaps, untagged entries, orphans, etc.",
        "signature": "rka_query(operation='pending_maintenance', *, project_id)",
        "required_fields": ["project_id"],
        "optional_fields": [],
        "enums": {},
        "examples": [
            {
                "description": "List maintenance work.",
                "call": {
                    "operation": "pending_maintenance",
                    "project_id": "prj_01ABC...",
                },
            },
        ],
        "related_operations": ["integrity", "freshness", "contradictions"],
        "notes": None,
    },
    "changelog": {
        "operation": "changelog",
        "tool": "rka_query",
        "category": "core",
        "role_tag": "ANY",
        "summary": "Project changelog since a given date.",
        "signature": (
            "rka_query(operation='changelog', *, project_id, limit=50, filters={'since': ISO8601})"
        ),
        "required_fields": ["project_id", "filters"],
        "optional_fields": ["limit"],
        "enums": {},
        "examples": [
            {
                "description": "Recent changes.",
                "call": {
                    "operation": "changelog",
                    "project_id": "prj_01ABC...",
                    "filters": {"since": "2026-05-01"},
                },
            },
        ],
        "related_operations": ["status"],
        "notes": "`filters.since` is REQUIRED.",
    },
    "bootstrap_review": {
        "operation": "bootstrap_review",
        "tool": "rka_query",
        "category": "workspace",
        "role_tag": "ANY",
        "summary": "Review the proposed bootstrap result for a workspace scan.",
        "signature": "rka_query(operation='bootstrap_review', *, project_id, id)",
        "required_fields": ["project_id", "id"],
        "optional_fields": [],
        "enums": {},
        "examples": [
            {
                "description": "Review a scan's bootstrap proposal.",
                "call": {
                    "operation": "bootstrap_review",
                    "project_id": "prj_01ABC...",
                    "id": "scn_01XYZ...",
                },
            },
        ],
        "related_operations": ["bootstrap_workspace", "workspace_scan"],
        "notes": "`id` is the scan_id (scn_...).",
    },
    "workspace_tree": {
        "operation": "workspace_tree",
        "tool": "rka_query",
        "category": "workspace",
        "role_tag": "ANY",
        "summary": "Read-only file-tree probe of a workspace folder.",
        "signature": (
            "rka_query(operation='workspace_tree', *, project_id, "
            "filters={'folder_path': str, 'max_depth': int})"
        ),
        "required_fields": ["project_id", "filters"],
        "optional_fields": ["max_depth"],
        "enums": {},
        "examples": [
            {
                "description": "Shallow tree probe.",
                "call": {
                    "operation": "workspace_tree",
                    "project_id": "prj_01ABC...",
                    "filters": {
                        "folder_path": "/Users/me/Research/proj",
                        "max_depth": 2,
                    },
                },
            },
        ],
        "related_operations": ["workspace_scan"],
        "notes": "Requires operator-configured RKA_HOST_FILE_ROOTS in the MCP process. Paths are denied by default; tree counts are bounded and may be partial.",
    },
    "workspace_scan": {
        "operation": "workspace_scan",
        "tool": "rka_query",
        "category": "workspace",
        "role_tag": "ANY",
        "summary": "Deep workspace scan (full file contents for ingestion).",
        "signature": (
            "rka_query(operation='workspace_scan', *, project_id, "
            "filters={'folder_path', 'ignore_patterns', "
            "'max_file_size_mb', 'use_llm'})"
        ),
        "required_fields": ["project_id", "filters"],
        "optional_fields": [
            "ignore_patterns",
            "max_file_size_mb",
            "use_llm",
        ],
        "enums": {},
        "examples": [
            {
                "description": "Scan a workspace.",
                "call": {
                    "operation": "workspace_scan",
                    "project_id": "prj_01ABC...",
                    "filters": {
                        "folder_path": "/Users/me/Research/proj",
                        "max_file_size_mb": 50,
                    },
                },
            },
        ],
        "related_operations": ["workspace_tree", "bootstrap_workspace"],
        "notes": "Requires operator-configured RKA_HOST_FILE_ROOTS. Reads bounded host files and sends previews/metadata to the configured Core server. Request paths never grant authority.",
    },
    "list_projects": {
        "operation": "list_projects",
        "tool": "rka_query",
        "category": "session",
        "role_tag": "ANY",
        "summary": "List all available projects (UNSCOPED).",
        "signature": "rka_query(operation='list_projects')",
        "required_fields": [],
        "optional_fields": [],
        "enums": {},
        "examples": [
            {
                "description": "Discover available projects.",
                "call": {"operation": "list_projects"},
            },
        ],
        "related_operations": ["status", "create_project"],
        "notes": "UNSCOPED — does not require project_id.",
    },
    "capabilities": {
        "operation": "capabilities",
        "tool": "rka_query",
        "category": "session",
        "role_tag": "ANY",
        "summary": "Discover Core, REST, MCP, and runtime capability contracts (UNSCOPED).",
        "signature": (
            "rka_query(operation='capabilities', required_contract=None, "
            "required_capabilities=None)"
        ),
        "required_fields": [],
        "optional_fields": ["required_contract", "required_capabilities"],
        "enums": {},
        "examples": [
            {
                "description": "Discover versions and stable interface contracts.",
                "call": {"operation": "capabilities"},
            },
            {
                "description": "Require a compatible Core contract and embedding runtime.",
                "call": {
                    "operation": "capabilities",
                    "required_contract": "rka-core/v1",
                    "required_capabilities": ["embedding"],
                },
            },
        ],
        "related_operations": ["health", "list_projects"],
        "notes": (
            "UNSCOPED. Unsupported contract/capability requirements return "
            "a structured actionable error without mutating Core state."
        ),
    },
    "health": {
        "operation": "health",
        "tool": "rka_query",
        "category": "session",
        "role_tag": "ANY",
        "summary": "API health probe (UNSCOPED).",
        "signature": "rka_query(operation='health')",
        "required_fields": [],
        "optional_fields": [],
        "enums": {},
        "examples": [
            {
                "description": "Probe API health.",
                "call": {"operation": "health"},
            },
        ],
        "related_operations": ["status"],
        "notes": "UNSCOPED.",
    },
    # =====================================================================
    # rka_execute — write/lifecycle operations
    # =====================================================================
    # --- journal / notes ------------------------------------------------
    "record_note": {
        "operation": "record_note",
        "tool": "rka_execute",
        "category": "journal",
        "role_tag": "ANY",
        "summary": "RECORD a journal entry (note, log, directive).",
        "signature": (
            "rka_execute(operation='record_note', *, project_id, content, "
            "source='executor', type='note', confidence='hypothesis', "
            "importance='normal', verbatim_input=None, capture_mode='unknown', phase=None, "
            "tags=None, provenance={'related_decisions':[...], "
            "'related_literature':[...], 'related_mission':..., "
            "'supersedes':...})"
        ),
        "required_fields": ["project_id", "content"],
        "optional_fields": [
            "source",
            "capture_mode",
            "type",
            "confidence",
            "importance",
            "verbatim_input",
            "phase",
            "tags",
            "summary",
            "status",
            "pinned",
            "provenance",
        ],
        "enums": {**_e("source", "note_type", "confidence", "importance"),
                  "capture_mode": ["unknown", "raw_capture", "agent_restatement"]},
        "examples": [
            {
                "description": "Executor records a finding linked to a mission.",
                "call": {
                    "operation": "record_note",
                    "project_id": "prj_01ABC...",
                    "content": "RAG p99 latency 2.3s on benchmark X.",
                    "type": "note",
                    "source": "executor",
                    "confidence": "tested",
                    "provenance": {"related_mission": "mis_01XYZ..."},
                },
            },
            {
                "description": "PI directive with verbatim input (REQUIRED when source='pi').",
                "call": {
                    "operation": "record_note",
                    "project_id": "prj_01ABC...",
                    "content": "PI: prioritise latency over recall.",
                    "type": "directive",
                    "source": "pi",
                    "capture_mode": "agent_restatement",
                    "verbatim_input": "Prioritise latency over recall.",
                },
            },
        ],
        "related_operations": [
            "update_note",
            "ingest_document",
            "record_decision",
        ],
        "notes": (
            "Use explicit raw_capture for supplied original text or agent_restatement for an agent body. "
            "Omitted mode is unknown, not inferred. PI source requires verbatim_input unless raw_capture "
            "explicitly snapshots supplied content on creation. Content edits never overwrite originals."
        ),
    },
    "ingest_document": {
        "operation": "ingest_document",
        "tool": "rka_execute",
        "category": "journal",
        "role_tag": "ANY",
        "summary": "Ingest a markdown document into many journal entries.",
        "signature": (
            "rka_execute(operation='ingest_document', *, project_id, content, "
            "source='executor', default_type='finding', split_by_headings=True, "
            "phase=None, tags=None, provenance={...})"
        ),
        "required_fields": ["project_id", "content"],
        "optional_fields": [
            "source",
            "default_type",
            "split_by_headings",
            "phase",
            "tags",
            "provenance",
        ],
        "enums": _e("ingest_source", "note_type"),
        "examples": [
            {
                "description": "Ingest a markdown doc split by H1/H2.",
                "call": {
                    "operation": "ingest_document",
                    "project_id": "prj_01ABC...",
                    "content": "# Findings\n...\n## Anomalies\n...",
                    "split_by_headings": True,
                    "default_type": "finding",
                },
            },
        ],
        "related_operations": ["record_note", "batch_import"],
        "notes": None,
    },
    "update_note": {
        "operation": "update_note",
        "tool": "rka_execute",
        "category": "journal",
        "role_tag": "BRAIN",
        "summary": "Update journal content, metadata, lifecycle, pinning, or links.",
        "signature": (
            "rka_execute(operation='update_note', *, project_id, id, "
            "content=None, summary=None, type=None, confidence=None, "
            "importance=None, status=None, pinned=None, tags=None, phase=None, "
            "related_decisions=None, "
            "related_literature=None, related_mission=None)"
        ),
        "required_fields": ["project_id", "id"],
        "optional_fields": [
            "content",
            "summary",
            "type",
            "confidence",
            "importance",
            "status",
            "pinned",
            "tags",
            "phase",
            "verbatim_input",
            "source",
            "related_decisions",
            "related_literature",
            "related_mission",
        ],
        "enums": _e(
            "confidence", "importance", "source", "note_type", "journal_status"
        ),
        "examples": [
            {
                "description": "Promote a finding to verified.",
                "call": {
                    "operation": "update_note",
                    "project_id": "prj_01ABC...",
                    "id": "jrn_01XYZ...",
                    "confidence": "verified",
                },
            },
            {
                "description": "Supersede and unpin a journal entry.",
                "call": {
                    "operation": "update_note",
                    "project_id": "prj_01ABC...",
                    "id": "jrn_01XYZ...",
                    "status": "superseded",
                    "pinned": False,
                },
            },
        ],
        "related_operations": ["record_note", "bulk_update", "correct_note_attribution"],
        "notes": "source/verbatim_input are deprecated on updates and rejected when non-null; use correct_note_attribution.",
    },
    "correct_note_attribution": {
        "operation": "correct_note_attribution", "tool": "rka_execute",
        "category": "journal", "role_tag": "ANY",
        "summary": "Correct source/original with reason, revision guard and exact-retry request ID.",
        "signature": "rka_execute(operation='correct_note_attribution', *, project_id, id, expected_revision, request_id, actor, reason, source, verbatim_input, capture_mode=None)",
        "required_fields": ["project_id", "id", "expected_revision", "request_id", "actor", "reason", "source", "verbatim_input"],
        "optional_fields": ["capture_mode"],
        "enums": {"source": list(_ENUMS["source"]), "actor": ["brain", "executor", "pi", "llm", "web_ui", "system"],
                  "capture_mode": ["unknown", "raw_capture", "agent_restatement"]},
        "examples": [{
            "description": "Correct a misattributed original after re-reading the note.",
            "call": {"operation": "correct_note_attribution", "project_id": "prj_01ABC...",
                     "id": "jrn_01XYZ...", "expected_revision": 0, "request_id": "correction-1",
                     "actor": "executor", "reason": "Original supplied by PI",
                     "source": "pi", "verbatim_input": "Preserve my exact wording."},
        }],
        "related_operations": ["entity", "note_attribution_history", "update_note"],
        "notes": "Read attribution_revision with rka_query(operation='entity', id=...) first; journal listings are truncated. Actor is caller-asserted, not authenticated. Identical retries return the same immutable event, not the current note. Omitted/null capture_mode preserves the prior mode, including on retries after later changes. An explicit mode change is part of correction intent. raw_capture requires a supplied original even for non-PI; corrections never copy the edited body. Unknown originals must not be invented.",
    },
    "note_attribution_history": {
        "operation": "note_attribution_history", "tool": "rka_query",
        "category": "journal", "role_tag": "ANY",
        "summary": "Read immutable attribution corrections in revision order.",
        "signature": "rka_query(operation='note_attribution_history', *, project_id, id, after_revision=0, limit=50)",
        "required_fields": ["project_id", "id"],
        "optional_fields": ["after_revision", "limit"], "enums": {},
        "examples": [{"description": "Read the first page of corrections.", "call": {
            "operation": "note_attribution_history", "project_id": "prj_01ABC...", "id": "jrn_01XYZ...",
        }}],
        "related_operations": ["journal", "correct_note_attribution"],
        "notes": "Not full note edit history. Revision zero has no correction events; this does not prove the original author. Paginate using the last returned revision as after_revision.",
    },
    # --- decisions ------------------------------------------------------
    "record_decision": {
        "operation": "record_decision",
        "tool": "rka_execute",
        "category": "decision",
        "role_tag": "BRAIN",
        "summary": "RECORD a decision node. Provenance-required.",
        "signature": (
            "rka_execute(operation='record_decision', *, project_id, "
            "question, chosen, rationale, decided_by, kind, "
            "related_journal=[...], options=None, supersedes_decision_id=None, "
            "confidence='tested', tags=None, phase=None, parent_id=None, "
            "related_literature=None, assumptions=None)"
        ),
        "required_fields": [
            "project_id",
            "question",
            "chosen",
            "rationale",
            "decided_by",
            "kind",
            "related_journal",
            "phase",
        ],
        "optional_fields": [
            "options",
            "supersedes_decision_id",
            "confidence",
            "tags",
            "parent_id",
            "related_literature",
            "related_missions",
            "status",
            "assumptions",
        ],
        "enums": _e("decided_by", "decision_kind", "confidence"),
        "examples": [
            {
                "description": "PI records a design choice with provenance.",
                "call": {
                    "operation": "record_decision",
                    "project_id": "prj_01ABC...",
                    "question": "Which embedding model for v2?",
                    "chosen": "nomic-embed-text-v1.5",
                    "rationale": "Best recall/latency tradeoff in benchmarks A, B.",
                    "decided_by": "pi",
                    "kind": "design_choice",
                    "related_journal": ["jrn_01XYZ...", "jrn_01ABC..."],
                    "phase": "analysis",
                },
            },
        ],
        "related_operations": [
            "update_decision",
            "supersede_decision",
            "present_decision",
            "record_pi_selection",
        ],
        "notes": (
            "Provenance discipline: related_journal MUST be non-empty. "
            "Common Brain hallucination: confidence='confirmed' is NOT "
            "valid; use 'verified' or 'tested'."
        ),
    },
    "update_decision": {
        "operation": "update_decision",
        "tool": "rka_execute",
        "category": "decision",
        "role_tag": "BRAIN",
        "summary": "Update fields on an existing decision.",
        "signature": (
            "rka_execute(operation='update_decision', *, project_id, id, "
            "status=None, chosen=None, rationale=None, kind=None, "
            "related_journal=None, parent_id=None, related_literature=None, "
            "related_missions=None, phase=None, tags=None, assumptions=None, "
            "abandonment_reason=None)"
        ),
        "required_fields": ["project_id", "id"],
        "optional_fields": [
            "status",
            "chosen",
            "rationale",
            "kind",
            "related_journal",
            "parent_id",
            "related_literature",
            "related_missions",
            "phase",
            "tags",
            "assumptions",
            "abandonment_reason",
        ],
        "enums": _e("decision_kind"),
        "examples": [
            {
                "description": "Update a decision's rationale.",
                "call": {
                    "operation": "update_decision",
                    "project_id": "prj_01ABC...",
                    "id": "dec_01XYZ...",
                    "rationale": "Refined after benchmark Y.",
                },
            },
        ],
        "related_operations": [
            "record_decision",
            "supersede_decision",
        ],
        "notes": None,
    },
    "orphan_supersedes": {
        "operation": "orphan_supersedes",
        "tool": "rka_query",
        "category": "decision",
        "summary": "List decisions marked superseded with no replacement pointer.",
        "signature": 'rka_query(args={"operation": "orphan_supersedes", "project_id": "prj_..."})',
        "required_fields": ["project_id"],
        "optional_fields": [],
        "enums": {},
        "examples": [
            {
                "description": "Find chains a reader cannot follow forward",
                "call": {"operation": "orphan_supersedes", "project_id": "prj_01ABC"},
            }
        ],
        "related_operations": ["link_supersession", "decision_tree", "staleness_impact"],
        "role_tag": "ANY",
        "notes": (
            "A decision reading 'superseded' with an empty superseded_by says it is "
            "dead but not what replaced it. The record carries no automatic pointer — "
            "a human must name the replacement, then link_supersession reconnects it."
        ),
    },
    "link_supersession": {
        "operation": "link_supersession",
        "tool": "rka_execute",
        "category": "decision",
        "summary": "Reconnect two existing decisions as superseded -> replacement.",
        "signature": (
            'rka_execute(args={"operation": "link_supersession", "project_id": "prj_...", '
            '"old_decision_id": "dec_...", "new_decision_id": "dec_...", "apply": false})'
        ),
        "required_fields": ["project_id", "old_decision_id", "new_decision_id"],
        "optional_fields": ["apply", "actor"],
        "enums": {"actor": ["pi", "brain", "executor", "system"]},
        "examples": [
            {
                "description": "Preview the repair first (apply defaults to false)",
                "call": {
                    "operation": "link_supersession",
                    "project_id": "prj_01ABC",
                    "old_decision_id": "dec_01OLD",
                    "new_decision_id": "dec_01NEW",
                },
            },
            {
                "description": "Perform it once the pairing is confirmed",
                "call": {
                    "operation": "link_supersession",
                    "project_id": "prj_01ABC",
                    "old_decision_id": "dec_01OLD",
                    "new_decision_id": "dec_01NEW",
                    "apply": True,
                },
            },
        ],
        "related_operations": ["supersede_decision", "orphan_supersedes", "decision_tree"],
        "role_tag": "BRAIN",
        "notes": (
            "NOT the same as supersede_decision, which CREATES the replacement. Use this "
            "only when both rows already exist and only the chain between them is missing. "
            "Replays the full sequence without creating anything: scope-version bump, the "
            "superseded_by pointer, the 'supersedes' entity_link, the staleness cascade "
            "over claims and clusters sourced from the old decision's journal entries, a "
            "re-distill review row, and the decision_superseded event. Idempotent. "
            "apply defaults to False because a wrong pointer is worse than a missing one — "
            "it sends a reader confidently to an unrelated decision."
        ),
    },
    "supersede_decision": {
        "operation": "supersede_decision",
        "tool": "rka_execute",
        "category": "decision",
        "role_tag": "BRAIN",
        "summary": "Replace an old decision with a new one (atomically links the two).",
        "signature": (
            "rka_execute(operation='supersede_decision', *, project_id, "
            "old_decision_id, question, chosen, rationale, related_journal, "
            "decided_by='brain', phase='', kind='decision')"
        ),
        "required_fields": [
            "project_id",
            "old_decision_id",
            "question",
            "chosen",
            "rationale",
            "related_journal",
        ],
        "optional_fields": ["decided_by", "phase", "kind"],
        "enums": _e("decided_by", "decision_kind"),
        "examples": [
            {
                "description": "Overturn a prior decision.",
                "call": {
                    "operation": "supersede_decision",
                    "project_id": "prj_01ABC...",
                    "old_decision_id": "dec_01OLD...",
                    "question": "Which embedding model for v2 (revised)?",
                    "chosen": "bge-m3",
                    "rationale": "New benchmarks reverse the prior choice.",
                    "related_journal": ["jrn_01XYZ..."],
                    "decided_by": "brain",
                    "kind": "design_choice",
                },
            },
        ],
        "related_operations": ["record_decision", "update_decision"],
        "notes": (
            "Also reachable as rka_execute(operation='record_decision', "
            "supersedes_decision_id='dec_...')."
        ),
    },
    "present_decision": {
        "operation": "present_decision",
        "tool": "rka_execute",
        "category": "decision",
        "role_tag": "BRAIN",
        "summary": "Present a decision to the PI for ratification.",
        "signature": (
            "rka_execute(operation='present_decision', *, project_id, "
            "decision_id, confirmation_brief, options, pi_preference=None)"
        ),
        "required_fields": [
            "project_id",
            "decision_id",
            "confirmation_brief",
            "options",
        ],
        "optional_fields": ["pi_preference"],
        "enums": {},
        "examples": [
            {
                "description": "Brain presents a decision for PI selection.",
                "call": {
                    "operation": "present_decision",
                    "project_id": "prj_01ABC...",
                    "decision_id": "dec_01XYZ...",
                    "confirmation_brief": "Choose embedding model for v2.",
                    # No "id": the server assigns option ids and returns
                    # them as presented_option_ids. Every other field here is
                    # required — DecisionOptionCreate forbids extras and
                    # accepts exactly three pros and three cons.
                    "options": [
                        {
                            "label": "nomic-embed-v1.5",
                            "summary": "Swap the embedding backend to nomic-embed-v1.5.",
                            "justification": "Best recall/latency trade-off measured on our corpus.",
                            "explanation": "768-dim, runs locally, no per-call cost.",
                            "pros": ["Highest recall", "Local inference", "No API cost"],
                            "cons": ["Reindex required", "768-dim storage", "Newer, less battle-tested"],
                            "confidence_verbal": "high",
                            "confidence_numeric": 0.8,
                            "confidence_evidence_strength": "strong",
                            "confidence_known_unknowns": ["Behaviour on non-English entries"],
                            "effort_time": "M",
                            "effort_reversibility": "reversible",
                            "presentation_order_seed": 1,
                        },
                        {
                            "label": "bge-m3",
                            "summary": "Swap the embedding backend to bge-m3.",
                            "justification": "Stronger multilingual coverage.",
                            "explanation": "1024-dim, multilingual, larger index.",
                            "pros": ["Multilingual", "Mature", "Long-context"],
                            "cons": ["Larger index", "Slower", "Higher memory"],
                            "confidence_verbal": "moderate",
                            "confidence_numeric": 0.6,
                            "confidence_evidence_strength": "moderate",
                            "confidence_known_unknowns": ["Latency at our corpus size"],
                            "effort_time": "L",
                            "effort_reversibility": "reversible",
                            "presentation_order_seed": 2,
                        },
                    ],
                },
            },
        ],
        "related_operations": ["record_pi_selection", "record_decision"],
        "notes": (
            "In orchestrator-driven flows, ratification is performed via "
            "the pi_decision_select interrupt (TWO-TAP gate), not via "
            "direct presentation."
        ),
    },
    "record_pi_selection": {
        "operation": "record_pi_selection",
        "tool": "rka_execute",
        "category": "decision",
        "role_tag": "ANY",
        "summary": "Record the PI's selection on a presented decision.",
        "signature": (
            "rka_execute(operation='record_pi_selection', *, project_id, "
            "decision_id, selected_option_id=None, override_rationale=None)"
        ),
        "required_fields": ["project_id", "decision_id"],
        "optional_fields": ["selected_option_id", "override_rationale"],
        "enums": {},
        "examples": [
            {
                "description": "PI selects option B with rationale.",
                "call": {
                    "operation": "record_pi_selection",
                    "project_id": "prj_01ABC...",
                    "decision_id": "dec_01XYZ...",
                    "selected_option_id": "B",
                    "override_rationale": "B aligns with the latency budget.",
                },
            },
        ],
        "related_operations": ["present_decision"],
        "notes": None,
    },
    "record_outcome": {
        "operation": "record_outcome",
        "tool": "rka_execute",
        "category": "decision",
        "role_tag": "PI",
        "summary": "Record the calibration outcome of a past decision.",
        "signature": (
            "rka_execute(operation='record_outcome', *, project_id, "
            "decision_id, outcome, outcome_details=None, recorded_by='pi')"
        ),
        "required_fields": ["project_id", "decision_id", "outcome"],
        "optional_fields": ["outcome_details", "recorded_by"],
        "enums": _e("outcome"),
        "examples": [
            {
                "description": "Mark a decision succeeded with details.",
                "call": {
                    "operation": "record_outcome",
                    "project_id": "prj_01ABC...",
                    "decision_id": "dec_01XYZ...",
                    "outcome": "succeeded",
                    "outcome_details": "Latency budget met.",
                },
            },
        ],
        "related_operations": ["calibration_metrics", "record_decision"],
        "notes": None,
    },
    # --- literature -----------------------------------------------------
    "record_literature": {
        "operation": "record_literature",
        "tool": "rka_execute",
        "category": "literature",
        "role_tag": "ANY",
        "summary": "Add a literature entry from a title or DOI.",
        "signature": (
            "rka_execute(operation='record_literature', *, project_id, "
            "title=None, authors=None, year=None, venue=None, doi=None, "
            "url=None, abstract=None, status='to_read', tags=None, "
            "related_decisions=None)"
        ),
        "required_fields": ["project_id"],
        "optional_fields": [
            "title",
            "authors",
            "year",
            "venue",
            "doi",
            "url",
            "abstract",
            "status",
            "tags",
            "related_decisions",
        ],
        "enums": _e("lit_status"),
        "examples": [
            {
                "description": "Bootstrap a paper from title + DOI.",
                "call": {
                    "operation": "record_literature",
                    "project_id": "prj_01ABC...",
                    "title": "Attention Is All You Need",
                    "doi": "10.48550/arXiv.1706.03762",
                    "status": "to_read",
                },
            },
        ],
        "related_operations": [
            "update_literature",
            "import_bibtex",
            "enrich_doi",
            "link_literature_to_zotero",
        ],
        "notes": "At least one of `title` or `doi` is required.",
    },
    "update_literature": {
        "operation": "update_literature",
        "tool": "rka_execute",
        "category": "literature",
        "role_tag": "BRAIN",
        "summary": "Update fields on a literature entry.",
        "signature": (
            "rka_execute(operation='update_literature', *, project_id, id, "
            "title=None, authors=None, year=None, venue=None, doi=None, "
            "url=None, bibtex=None, pdf_path=None, abstract=None, "
            "status=None, key_findings=None, methodology_notes=None, "
            "relevance=None, relevance_score=None, related_decisions=None, "
            "notes=None, tags=None)"
        ),
        "required_fields": ["project_id", "id"],
        "optional_fields": [
            "title",
            "authors",
            "year",
            "venue",
            "doi",
            "url",
            "bibtex",
            "pdf_path",
            "abstract",
            "status",
            "key_findings",
            "methodology_notes",
            "relevance",
            "relevance_score",
            "related_decisions",
            "notes",
            "tags",
        ],
        "enums": _e("lit_status"),
        "examples": [
            {
                "description": "Mark a paper as read with findings.",
                "call": {
                    "operation": "update_literature",
                    "project_id": "prj_01ABC...",
                    "id": "lit_01XYZ...",
                    "status": "read",
                    "key_findings": "Self-attention scales O(n^2).",
                },
            },
        ],
        "related_operations": ["record_literature", "process_paper"],
        "notes": None,
    },
    "import_bibtex": {
        "operation": "import_bibtex",
        "tool": "rka_execute",
        "category": "literature",
        "role_tag": "ANY",
        "summary": "Bulk-import literature entries from BibTeX.",
        "signature": (
            "rka_execute(operation='import_bibtex', *, project_id, bibtex, "
            "default_status='to_read')"
        ),
        "required_fields": ["project_id", "bibtex"],
        "optional_fields": ["default_status"],
        "enums": _e("lit_status"),
        "examples": [
            {
                "description": "Import a BibTeX library.",
                "call": {
                    "operation": "import_bibtex",
                    "project_id": "prj_01ABC...",
                    "bibtex": "@article{...}",
                },
            },
        ],
        "related_operations": ["record_literature", "batch_import"],
        "notes": "Preserves added_by='import' with execution actor='system'. Inspect errors even on HTTP success; malformed libraries create no entries. Base installs support a bounded BibTeX subset; macros/concatenation require the academic extra.",
    },
    "enrich_doi": {
        "operation": "enrich_doi",
        "tool": "rka_execute",
        "category": "literature",
        "role_tag": "ANY",
        "summary": "Enrich a literature entry's metadata from its DOI.",
        "signature": ("rka_execute(operation='enrich_doi', *, project_id, lit_id)"),
        "required_fields": ["project_id", "lit_id"],
        "optional_fields": [],
        "enums": {},
        "examples": [
            {
                "description": "Fill in metadata from DOI.",
                "call": {
                    "operation": "enrich_doi",
                    "project_id": "prj_01ABC...",
                    "lit_id": "lit_01XYZ...",
                },
            },
        ],
        "related_operations": ["record_literature", "update_literature"],
        "notes": None,
    },
    "link_literature_to_zotero": {
        "operation": "link_literature_to_zotero",
        "tool": "rka_execute",
        "category": "literature",
        "role_tag": "ANY",
        "summary": "Link a literature entry to a Zotero item.",
        "signature": (
            "rka_execute(operation='link_literature_to_zotero', *, "
            "project_id, lit_id, zotero_key=None)"
        ),
        "required_fields": ["project_id", "lit_id"],
        "optional_fields": ["zotero_key"],
        "enums": {},
        "examples": [
            {
                "description": "Link the literature row to Zotero.",
                "call": {
                    "operation": "link_literature_to_zotero",
                    "project_id": "prj_01ABC...",
                    "lit_id": "lit_01XYZ...",
                },
            },
        ],
        "related_operations": ["record_literature", "update_literature"],
        "notes": None,
    },
    "process_paper": {
        "operation": "process_paper",
        "tool": "rka_execute",
        "category": "literature",
        "role_tag": "ANY",
        "summary": "Ingest paper annotations into the literature row.",
        "signature": (
            "rka_execute(operation='process_paper', *, project_id, lit_id, "
            "annotations, summary=None)"
        ),
        "required_fields": ["project_id", "lit_id", "annotations"],
        "optional_fields": ["summary"],
        "enums": {},
        "examples": [
            {
                "description": "Process a paper's annotations.",
                "call": {
                    "operation": "process_paper",
                    "project_id": "prj_01ABC...",
                    "lit_id": "lit_01XYZ...",
                    "annotations": [{"text": "...", "page": 3}],
                    "summary": "Self-attention enables global context.",
                },
            },
        ],
        "related_operations": ["update_literature", "extract_claims"],
        "notes": None,
    },
    "batch_import": {
        "operation": "batch_import",
        "tool": "rka_execute",
        "category": "ingestion",
        "role_tag": "ANY",
        "summary": "Bulk import mixed entity types in one call.",
        "signature": (
            "rka_execute(operation='batch_import', *, project_id, entries, actor='system')"
        ),
        "required_fields": ["project_id", "entries"],
        "optional_fields": ["actor"],
        "enums": _e("source"),
        "examples": [
            {
                "description": "Import a batch of journal entries.",
                "call": {
                    "operation": "batch_import",
                    "project_id": "prj_01ABC...",
                    "entries": [{"type": "note", "content": "..."}],
                    "actor": "system",
                },
            },
        ],
        "related_operations": ["ingest_document", "import_bibtex"],
        "notes": (
            "actor='import' is auto-normalized to actor='system' "
            "(system is the canonical value for programmatic ingestion)."
        ),
    },
    "register_manuscript": {
        "operation": "register_manuscript",
        "tool": "rka_execute",
        "category": "manuscript",
        "role_tag": "ANY",
        "summary": "Compatibility-register legacy and canonical manuscript IDs.",
        "signature": (
            "rka_execute(operation='register_manuscript', *, project_id, "
            "venue, title, abstract=None, sections=None)"
        ),
        "required_fields": ["project_id", "venue", "title"],
        "optional_fields": ["abstract", "sections"],
        "enums": {},
        "examples": [
            {
                "description": "Register a NeurIPS submission.",
                "call": {
                    "operation": "register_manuscript",
                    "project_id": "prj_01ABC...",
                    "venue": "NeurIPS-2026",
                    "title": "Edge LLM latency budgets",
                },
            },
        ],
        "related_operations": [
            "manuscript",
            "create_manuscript",
        ],
        "notes": (
            "Compatibility operation. The response contains deprecated jrn_ "
            "id plus canonical man_ id; new callers should create_manuscript."
        ),
    },
    "create_manuscript": {
        "operation": "create_manuscript",
        "tool": "rka_execute",
        "category": "manuscript",
        "role_tag": "ANY",
        "summary": "Create the canonical native manuscript aggregate.",
        "signature": (
            "rka_execute(operation='create_manuscript', *, project_id, title, "
            "abstract=None, venue=None, phase='planning', state='active', "
            "workspace_ref=None, "
            "legacy_journal_id=None)"
        ),
        "required_fields": ["project_id", "title"],
        "optional_fields": [
            "abstract",
            "venue",
            "phase",
            "state",
            "workspace_ref",
            "legacy_journal_id",
        ],
        "enums": {
            "phase": ["planning"],
            "state": ["active"],
        },
        "examples": [
            {
                "description": "Create a native manuscript.",
                "call": {
                    "operation": "create_manuscript",
                    "project_id": "prj_01ABC...",
                    "title": "Evidence-grounded system security",
                    "venue": "USENIX Security",
                },
            },
        ],
        "related_operations": [
            "update_manuscript",
            "upsert_argument_spine",
            "manuscript_context",
        ],
        "notes": (
            "phase and state are explicit typed fields whose only accepted "
            "initial values are planning and active; creation never infers "
            "PI ratification."
        ),
    },
    "update_manuscript": {
        "operation": "update_manuscript",
        "tool": "rka_execute",
        "category": "manuscript",
        "role_tag": "ANY",
        "summary": "Update native manuscript metadata with revision precondition.",
        "signature": (
            "rka_execute(operation='update_manuscript', *, project_id, id, "
            "expected_revision, title? abstract? venue? workspace_ref?)"
        ),
        "required_fields": ["project_id", "id", "expected_revision"],
        "optional_fields": [
            "title",
            "abstract",
            "venue",
            "workspace_ref",
        ],
        "enums": {},
        "examples": [
            {
                "description": "Set a venue at revision 2.",
                "call": {
                    "operation": "update_manuscript",
                    "project_id": "prj_01ABC...",
                    "id": "man_01XYZ...",
                    "expected_revision": 2,
                    "venue": "IEEE S&P",
                },
            },
        ],
        "related_operations": ["manuscript", "manuscript_context"],
        "notes": (
            "At least one update field is required. Nullable metadata can be "
            "cleared explicitly with null; omitted fields remain unchanged. "
            "Lifecycle phase/state changes require transition_manuscript_phase."
        ),
    },
    "upsert_argument_spine": {
        "operation": "upsert_argument_spine",
        "tool": "rka_execute",
        "category": "manuscript",
        # role_tag remains an actor-routing contract. Lifecycle status is
        # conveyed by the summary/notes and the structured dispatch result.
        "role_tag": "ANY",
        "summary": (
            "Deprecated agent-direct mutation; returns a structured migration "
            "error and performs no write."
        ),
        "signature": (
            "rka_execute(operation='upsert_argument_spine', *, project_id, "
            "id, expected_revision, spine={'claims': [...], 'units': [...]})"
        ),
        "required_fields": [
            "project_id",
            "id",
            "expected_revision",
            "spine",
        ],
        "optional_fields": [],
        "enums": _e(
            "manuscript_claim_kind",
            "manuscript_claim_state",
            "manuscript_unit_kind",
            "manuscript_unit_status",
            "manuscript_unit_role",
            "manuscript_rhetorical_move",
            "manuscript_evidence_role",
            "manuscript_claim_unit_relationship",
            "manuscript_citation_role",
            "manuscript_citation_verification_state",
        ),
        "examples": [
            {
                "description": (
                    "Legacy payload retained only so older clients receive the "
                    "structured migration error."
                ),
                "call": {
                    "operation": "upsert_argument_spine",
                    "project_id": "prj_01ABC...",
                    "id": "man_01XYZ...",
                    "expected_revision": 1,
                    "spine": {
                        "claims": [
                            {
                                "local_key": "C1",
                                "kind": "empirical",
                                "state": "candidate",
                                "exact_wording": "The system reduces attack time.",
                                "allowed_wording": "Measured attack time decreased.",
                                "prohibited_wording": ["The system prevents attacks."],
                                "unit_links": [
                                    {
                                        "unit_key": "R1",
                                        "relationship": "tests",
                                    },
                                ],
                            },
                        ],
                        "units": [
                            {
                                "local_key": "R1",
                                "kind": "result",
                                "location": "results.attack-time",
                                "artifact_ref": "art_01RESULT...",
                                "allowed_interpretation": "Time decreased.",
                                "prohibited_interpretation": "All attacks fail.",
                            },
                        ],
                    },
                },
            },
        ],
        "related_operations": [
            "prepare_semantic_patch_context",
            "create_semantic_patch_proposal",
            "apply_semantic_patch_proposal",
            "ratify_manuscript_claim",
            "manuscript_spine",
            "manuscript_readiness",
        ],
        "notes": (
            "This legacy operation performs no write. Use "
            "prepare_semantic_patch_context followed by an attributed "
            "create_semantic_patch_proposal containing argument_spine_replace. "
            "A PI or web user must apply that full-replacement proposal "
            "separately; omitted claims are retired and omitted units are removed."
        ),
    },
    "replace_manuscript_reference_manifest": {
        "operation": "replace_manuscript_reference_manifest",
        "tool": "rka_execute",
        "category": "manuscript",
        "role_tag": "ANY",
        "summary": "Atomically replace the authoritative citation-key set.",
        "signature": (
            "rka_execute(operation='replace_manuscript_reference_manifest', "
            "*, project_id, id, expected_revision, members=["
            "{'citation_key', 'literature_id'}, ...])"
        ),
        "required_fields": [
            "project_id",
            "id",
            "expected_revision",
            "members",
        ],
        "optional_fields": [],
        "enums": {},
        "examples": [
            {
                "description": "Register two exact citations at revision 3.",
                "call": {
                    "operation": "replace_manuscript_reference_manifest",
                    "project_id": "prj_01ABC...",
                    "id": "man_01XYZ...",
                    "expected_revision": 3,
                    "members": [
                        {
                            "citation_key": "vaswani2017attention",
                            "literature_id": "lit_01ATTENTION...",
                        },
                        {
                            "citation_key": "carlini2024stealing",
                            "literature_id": "lit_01STEALING...",
                        },
                    ],
                },
            },
        ],
        "related_operations": [
            "manuscript_reference_manifest",
            "manuscript_readiness",
        ],
        "notes": (
            "This is a full-set, revision-guarded replacement. Omitted active "
            "members are retired, never deleted. Citation keys are unique "
            "case-insensitively, literature bindings are unique, and every "
            "literature row must belong to the manuscript project."
        ),
    },
    "ratify_manuscript_claim": {
        "operation": "ratify_manuscript_claim",
        "tool": "rka_execute",
        "category": "manuscript",
        "role_tag": "PI",
        "summary": "Bind exact claim wording to an explicit PI decision.",
        "signature": (
            "rka_execute(operation='ratify_manuscript_claim', *, project_id, "
            "id, claim_ref, expected_revision, decision_id, claim_version=None, "
            "ratified_at=None)"
        ),
        "required_fields": [
            "project_id",
            "id",
            "claim_ref",
            "expected_revision",
            "decision_id",
        ],
        "optional_fields": ["claim_version", "ratified_at"],
        "enums": {},
        "examples": [
            {
                "description": "Ratify the current C1 wording.",
                "call": {
                    "operation": "ratify_manuscript_claim",
                    "project_id": "prj_01ABC...",
                    "id": "man_01XYZ...",
                    "claim_ref": "C1",
                    "expected_revision": 2,
                    "decision_id": "dec_01PI...",
                },
            },
        ],
        "related_operations": [
            "record_decision",
            "upsert_argument_spine",
            "record_verification_attestation",
        ],
        "notes": (
            "The decision must be same-project, active, and PI-authored. "
            "Ratification is never inferred from notes or legacy tags."
        ),
    },
    "transition_manuscript_phase": {
        "operation": "transition_manuscript_phase",
        "tool": "rka_execute",
        "category": "manuscript",
        "role_tag": "ANY",
        "summary": "Run readiness gates and transition manuscript lifecycle.",
        "signature": (
            "rka_execute(operation='transition_manuscript_phase', *, "
            "project_id, id, expected_revision, target_phase, "
            "target_state=None)"
        ),
        "required_fields": [
            "project_id",
            "id",
            "expected_revision",
            "target_phase",
        ],
        "optional_fields": ["target_state"],
        "enums": {
            "target_phase": list(_ENUMS["manuscript_phase"]),
            "target_state": list(_ENUMS["manuscript_state"]),
        },
        "examples": [
            {
                "description": "Advance a ready manuscript into drafting.",
                "call": {
                    "operation": "transition_manuscript_phase",
                    "project_id": "prj_01ABC...",
                    "id": "man_01XYZ...",
                    "expected_revision": 4,
                    "target_phase": "drafting",
                },
            },
        ],
        "related_operations": [
            "manuscript_readiness",
            "create_manuscript_checkpoint",
        ],
        "notes": "The server rejects transitions with BLOCK or ERROR findings.",
    },
    "create_manuscript_checkpoint": {
        "operation": "create_manuscript_checkpoint",
        "tool": "rka_execute",
        "category": "manuscript",
        "role_tag": "PI",
        "summary": "Create a pending native manuscript checkpoint.",
        "signature": (
            "rka_execute(operation='create_manuscript_checkpoint', *, "
            "project_id, id, expected_revision, kind, unit_id=None, "
            "supersedes_id=None)"
        ),
        "required_fields": [
            "project_id",
            "id",
            "expected_revision",
            "kind",
        ],
        "optional_fields": ["unit_id", "supersedes_id"],
        "enums": {"kind": list(_ENUMS["manuscript_checkpoint_kind"])},
        "examples": [
            {
                "description": "Create an outline checkpoint.",
                "call": {
                    "operation": "create_manuscript_checkpoint",
                    "project_id": "prj_01ABC...",
                    "id": "man_01XYZ...",
                    "expected_revision": 3,
                    "kind": "outline",
                },
            },
        ],
        "related_operations": [
            "resolve_manuscript_checkpoint",
            "manuscript_readiness",
        ],
        "notes": "draft_section checkpoints require unit_id; other kinds forbid it.",
    },
    "resolve_manuscript_checkpoint": {
        "operation": "resolve_manuscript_checkpoint",
        "tool": "rka_execute",
        "category": "manuscript",
        "role_tag": "PI",
        "summary": "Resolve a pending manuscript checkpoint via a PI decision.",
        "signature": (
            "rka_execute(operation='resolve_manuscript_checkpoint', *, "
            "project_id, checkpoint_id, expected_revision, decision_id, "
            "status, resolved_at)"
        ),
        "required_fields": [
            "project_id",
            "checkpoint_id",
            "expected_revision",
            "decision_id",
            "status",
            "resolved_at",
        ],
        "optional_fields": [],
        "enums": {
            "status": list(_ENUMS["manuscript_checkpoint_resolution_status"]),
        },
        "examples": [
            {
                "description": "Resolve an outline checkpoint.",
                "call": {
                    "operation": "resolve_manuscript_checkpoint",
                    "project_id": "prj_01ABC...",
                    "checkpoint_id": "mck_01CHECK...",
                    "expected_revision": 4,
                    "decision_id": "dec_01PI...",
                    "status": "resolved",
                    "resolved_at": "2026-07-22T12:00:00Z",
                },
            },
        ],
        "related_operations": [
            "create_manuscript_checkpoint",
            "record_decision",
        ],
        "notes": "Only pending checkpoints can be resolved.",
    },
    "record_verification_attestation": {
        "operation": "record_verification_attestation",
        "tool": "rka_execute",
        "category": "manuscript",
        "role_tag": "ANY",
        "summary": "Append immutable multidimensional claim verification.",
        "signature": (
            "rka_execute(operation='record_verification_attestation', *, "
            "project_id, id, expected_revision, claim_id, claim_version, "
            "overall_verdict, grounding_verdict, evidence_verdict, "
            "contradiction_verdict, currency_verdict, ratification_verdict, "
            "unit_coverage_verdict, full_json_payload, started_at, completed_at, "
            "changelog_cursor=None, dependency_snapshot={}, "
            "validator_version=None)"
        ),
        "required_fields": [
            "project_id",
            "id",
            "expected_revision",
            "claim_id",
            "claim_version",
            "overall_verdict",
            "grounding_verdict",
            "evidence_verdict",
            "contradiction_verdict",
            "currency_verdict",
            "ratification_verdict",
            "unit_coverage_verdict",
            "full_json_payload",
            "started_at",
            "completed_at",
        ],
        "optional_fields": [
            "changelog_cursor",
            "dependency_snapshot",
            "validator_version",
        ],
        "enums": {
            "overall_verdict": list(_ENUMS["manuscript_verification_verdict"]),
            "dimension_verdicts": list(_ENUMS["manuscript_verification_dimension_verdict"]),
        },
        "examples": [
            {
                "description": "Record a blocking evidence check.",
                "call": {
                    "operation": "record_verification_attestation",
                    "project_id": "prj_01ABC...",
                    "id": "man_01XYZ...",
                    "expected_revision": 5,
                    "claim_id": "mcl_01CLAIM...",
                    "claim_version": 1,
                    "overall_verdict": "block",
                    "grounding_verdict": "pass",
                    "evidence_verdict": "block",
                    "contradiction_verdict": "pass",
                    "currency_verdict": "pass",
                    "ratification_verdict": "not_checked",
                    "unit_coverage_verdict": "pass",
                    "full_json_payload": {"findings": []},
                    "started_at": "2026-07-22T12:00:00Z",
                    "completed_at": "2026-07-22T12:00:01Z",
                },
            },
        ],
        "related_operations": [
            "manuscript_readiness",
            "ratify_manuscript_claim",
        ],
        "notes": (
            "Attestations are append-only. Per-dimension verdicts remain "
            "separate so a single pass/fail flag cannot hide the blocking cause."
        ),
    },
    "update_status": {
        "operation": "update_status",
        "tool": "rka_execute",
        "category": "core",
        "role_tag": "BRAIN",
        "summary": "Update the project status (phase, focus, blockers).",
        "signature": (
            "rka_execute(operation='update_status', *, project_id, "
            "current_phase=None, summary=None, blockers=None, metrics=None)"
        ),
        "required_fields": ["project_id"],
        "optional_fields": [
            "current_phase",
            "summary",
            "blockers",
            "metrics",
        ],
        "enums": {},
        "examples": [
            {
                "description": "Move to evaluation phase.",
                "call": {
                    "operation": "update_status",
                    "project_id": "prj_01ABC...",
                    "current_phase": "evaluation",
                    "summary": "Running benchmark suite.",
                },
            },
        ],
        "related_operations": ["status"],
        "notes": None,
    },
    "bulk_update": {
        "operation": "bulk_update",
        "tool": "rka_execute",
        "category": "core",
        "role_tag": "BRAIN",
        "summary": "Update many entities as one validated best-effort batch.",
        "signature": ("rka_execute(operation='bulk_update', *, project_id, updates)"),
        "required_fields": ["project_id", "updates"],
        "optional_fields": [],
        "enums": {"updates[].entity_type": list(_ENUMS["bulk_entity_type"])},
        "examples": [
            {
                "description": "Bulk-tag many entities.",
                "call": {
                    "operation": "bulk_update",
                    "project_id": "prj_01ABC...",
                    "updates": [
                        {
                            "entity_type": "journal",
                            "id": "jrn_01...",
                            "tags": ["v2"],
                        },
                        {
                            "entity_type": "journal",
                            "id": "jrn_02...",
                            "tags": ["v2"],
                        },
                    ],
                },
            },
        ],
        "related_operations": ["update_note", "update_decision"],
        "notes": (
            "The canonical item shape is flat: entity_type + id + update fields. "
            "Legacy nested data={...} is accepted, but mixing both shapes or "
            "sending an empty update is rejected before any write. Preflight is "
            "whole-batch; HTTP execution is best-effort and can partially succeed, "
            "so this operation is not transactional across entities. "
            "Journal source/verbatim_input updates are rejected during preflight; "
            "use correct_note_attribution for each reasoned correction."
        ),
    },
    # --- missions -------------------------------------------------------
    "create_mission": {
        "operation": "create_mission",
        "tool": "rka_execute",
        "category": "mission",
        "role_tag": "BRAIN",
        "summary": "Create a mission. Requires motivated_by_decision.",
        "signature": (
            "rka_execute(operation='create_mission', *, project_id, "
            "objective, motivated_by_decision, phase='execution', "
            "tasks=None, context=None, acceptance_criteria=None, "
            "scope_boundaries=None, checkpoint_triggers=None, "
            "depends_on=None, tags=None)"
        ),
        "required_fields": [
            "project_id",
            "objective",
            "motivated_by_decision",
        ],
        "optional_fields": [
            "phase",
            "tasks",
            "context",
            "acceptance_criteria",
            "scope_boundaries",
            "checkpoint_triggers",
            "depends_on",
            "tags",
        ],
        "enums": {},
        "examples": [
            {
                "description": "Create an execution mission tied to a decision.",
                "call": {
                    "operation": "create_mission",
                    "project_id": "prj_01ABC...",
                    "objective": "Implement and benchmark embedding-model swap.",
                    "motivated_by_decision": "dec_01XYZ...",
                    "phase": "execution",
                },
            },
        ],
        "related_operations": [
            "update_mission",
            "update_mission_status",
            "submit_report",
            "mission",
        ],
        "notes": (
            "Provenance discipline: motivated_by_decision is REQUIRED "
            "to preserve the decision -> mission causality chain."
        ),
    },
    "update_mission": {
        "operation": "update_mission",
        "tool": "rka_execute",
        "category": "mission",
        "role_tag": "BRAIN",
        "summary": "Update mission fields (objective, context, criteria, etc.).",
        "signature": (
            "rka_execute(operation='update_mission', *, project_id, "
            "mission_id, phase=None, objective=None, context=None, "
            "acceptance_criteria=None, scope_boundaries=None, "
            "checkpoint_triggers=None, depends_on=None, "
            "parent_mission_id=None, motivated_by_decision=None, tags=None)"
        ),
        "required_fields": ["project_id", "mission_id"],
        "optional_fields": [
            "phase",
            "objective",
            "context",
            "acceptance_criteria",
            "scope_boundaries",
            "checkpoint_triggers",
            "depends_on",
            "parent_mission_id",
            "motivated_by_decision",
            "tags",
        ],
        "enums": {},
        "examples": [
            {
                "description": "Refine acceptance criteria.",
                "call": {
                    "operation": "update_mission",
                    "project_id": "prj_01ABC...",
                    "mission_id": "mis_01XYZ...",
                    "acceptance_criteria": "p99 < 2s on benchmark X.",
                },
            },
        ],
        "related_operations": ["create_mission", "update_mission_status"],
        "notes": None,
    },
    "update_mission_status": {
        "operation": "update_mission_status",
        "tool": "rka_execute",
        "category": "mission",
        "role_tag": "EXECUTOR",
        "summary": "Move a mission through its lifecycle states.",
        "signature": (
            "rka_execute(operation='update_mission_status', *, project_id, "
            "mission_id, status, tasks=None)"
        ),
        "required_fields": ["project_id", "mission_id", "status"],
        "optional_fields": ["tasks"],
        "enums": _e("mission_status"),
        "examples": [
            {
                "description": "Activate a pending mission.",
                "call": {
                    "operation": "update_mission_status",
                    "project_id": "prj_01ABC...",
                    "mission_id": "mis_01XYZ...",
                    "status": "active",
                },
            },
        ],
        "related_operations": ["mission", "submit_report"],
        "notes": (
            "Lifecycle: pending -> active -> complete (via submit_report). "
            "Also: partial, blocked, cancelled."
        ),
    },
    "submit_report": {
        "operation": "submit_report",
        "tool": "rka_execute",
        "category": "mission",
        "role_tag": "EXECUTOR",
        "summary": "Submit a mission's final report (closes the mission).",
        "signature": (
            "rka_execute(operation='submit_report', *, project_id, "
            "mission_id, summary=None, content=None, findings=None, "
            "anomalies=None, questions=None, codebase_state=None, "
            "recommended_next=None)"
        ),
        "required_fields": [
            "project_id",
            "mission_id",
        ],
        "optional_fields": [
            "summary",
            "content",
            "findings",
            "anomalies",
            "questions",
            "codebase_state",
            "recommended_next",
        ],
        "enums": {},
        "examples": [
            {
                "description": "Close a mission with a structured report.",
                "call": {
                    "operation": "submit_report",
                    "project_id": "prj_01ABC...",
                    "mission_id": "mis_01XYZ...",
                    "summary": "Embedding-model swap landed; latency budget met.",
                    # A list: the typed model declares list[str], matching
                    # MissionReportCreate. A bare string is refused before
                    # the call is emitted.
                    "findings": ["p99 dropped 30%.", "cold-start regressed 2x."],
                    "recommended_next": "Run scale test.",
                },
            },
        ],
        "related_operations": ["mission", "update_mission_status", "report"],
        "notes": (
            "Canonical field is `summary` (the report's narrative body); "
            "Phase-X²' adapter also accepts `content` as an alias. Report "
            "submission does not infer task outcomes: reconcile tasks with "
            "update_mission_status first. A completed mission with pending, "
            "in-progress, or blocked tasks returns consistency_warnings."
        ),
    },
    "advance_rq": {
        "operation": "advance_rq",
        "tool": "rka_execute",
        "category": "mission",
        "role_tag": "BRAIN",
        "summary": "Advance a research question through its lifecycle.",
        "signature": (
            "rka_execute(operation='advance_rq', *, project_id, "
            "rq_id, status, conclusion=None, evidence_cluster_ids=None)"
        ),
        "required_fields": ["project_id", "rq_id", "status"],
        "optional_fields": ["conclusion", "evidence_cluster_ids"],
        "enums": _e("rq_status"),
        "examples": [
            {
                "description": "Mark a research question answered.",
                "call": {
                    "operation": "advance_rq",
                    "project_id": "prj_01ABC...",
                    "rq_id": "dec_01RQ...",
                    "status": "answered",
                    "conclusion": "Embedding model nomic-v1.5 wins.",
                },
            },
        ],
        "related_operations": ["evidence", "clusters"],
        "notes": None,
    },
    # --- checkpoints + gates -------------------------------------------
    "submit_checkpoint": {
        "operation": "submit_checkpoint",
        "tool": "rka_execute",
        "category": "checkpoint",
        "role_tag": "EXECUTOR",
        "summary": "Raise a checkpoint when blocked or needing input.",
        "signature": (
            "rka_execute(operation='submit_checkpoint', *, project_id, "
            "mission_id, type, description=None, content=None, "
            "task_reference=None, context=None, options=None, "
            "recommendation=None, blocking=True)"
        ),
        "required_fields": [
            "project_id",
            "mission_id",
            "type",
        ],
        "optional_fields": [
            "description",
            "content",
            "task_reference",
            "context",
            "options",
            "recommendation",
            "blocking",
        ],
        "enums": _e("checkpoint_type"),
        "examples": [
            {
                "description": "Raise a decision checkpoint.",
                "call": {
                    "operation": "submit_checkpoint",
                    "project_id": "prj_01ABC...",
                    "mission_id": "mis_01XYZ...",
                    "type": "decision",
                    "description": "Need PI input on benchmark thresholds.",
                    "blocking": True,
                },
            },
        ],
        "related_operations": [
            "resolve_checkpoint",
            "checkpoints",
            "create_gate",
        ],
        "notes": (
            "One of `description` or `content` is required. `description` is "
            "canonical; `content` is the legacy alias."
        ),
    },
    "resolve_checkpoint": {
        "operation": "resolve_checkpoint",
        "tool": "rka_execute",
        "category": "checkpoint",
        "role_tag": "PI",
        "summary": "Resolve a checkpoint with a resolution + rationale.",
        "signature": (
            "rka_execute(operation='resolve_checkpoint', *, project_id, id, "
            "resolution, resolved_by, rationale=None, create_decision=False)"
        ),
        "required_fields": [
            "project_id",
            "id",
            "resolution",
            "resolved_by",
        ],
        "optional_fields": ["rationale", "create_decision"],
        "enums": _e("resolved_by"),
        "examples": [
            {
                "description": "PI resolves a checkpoint.",
                "call": {
                    "operation": "resolve_checkpoint",
                    "project_id": "prj_01ABC...",
                    "id": "chk_01XYZ...",
                    "resolution": "Proceed with option B.",
                    "resolved_by": "pi",
                    "rationale": "B fits the latency budget.",
                },
            },
        ],
        "related_operations": ["submit_checkpoint", "checkpoints"],
        "notes": None,
    },
    "create_gate": {
        "operation": "create_gate",
        "tool": "rka_execute",
        "category": "checkpoint",
        "role_tag": "BRAIN",
        "summary": "Create a validation gate (problem framing, evidence review, etc.).",
        "signature": (
            "rka_execute(operation='create_gate', *, project_id, "
            "mission_id, gate_type, deliverables, pass_criteria, "
            "assumptions_to_verify=None)"
        ),
        "required_fields": [
            "project_id",
            "mission_id",
            "gate_type",
            "deliverables",
            "pass_criteria",
        ],
        "optional_fields": ["assumptions_to_verify"],
        "enums": _e("gate_type"),
        "examples": [
            {
                "description": "Create a plan_validation gate.",
                "call": {
                    "operation": "create_gate",
                    "project_id": "prj_01ABC...",
                    "mission_id": "mis_01XYZ...",
                    "gate_type": "plan_validation",
                    "deliverables": ["plan doc"],
                    "pass_criteria": ["plan reviewed by PI"],
                },
            },
        ],
        "related_operations": ["evaluate_gate"],
        "notes": None,
    },
    "evaluate_gate": {
        "operation": "evaluate_gate",
        "tool": "rka_execute",
        "category": "checkpoint",
        "role_tag": "BRAIN",
        "summary": "Evaluate a gate with a verdict + notes.",
        "signature": (
            "rka_execute(operation='evaluate_gate', *, project_id, "
            "gate_id, verdict, notes, assumption_status=None)"
        ),
        "required_fields": [
            "project_id",
            "gate_id",
            "verdict",
            "notes",
        ],
        "optional_fields": ["assumption_status"],
        "enums": _e("verdict"),
        "examples": [
            {
                "description": "Evaluate a gate as go.",
                "call": {
                    "operation": "evaluate_gate",
                    "project_id": "prj_01ABC...",
                    "gate_id": "chk_01GATE...",
                    "verdict": "go",
                    "notes": "Plan meets pass criteria.",
                },
            },
        ],
        "related_operations": ["create_gate"],
        "notes": None,
    },
    # --- claims / clusters ---------------------------------------------
    "extract_claims": {
        "operation": "extract_claims",
        "tool": "rka_execute",
        "category": "claims",
        "role_tag": "BRAIN",
        "summary": "Stage atomic interpretations from an existing entry.",
        "signature": ("rka_execute(operation='extract_claims', *, project_id, entry_id, claims)"),
        "required_fields": ["project_id", "entry_id", "claims"],
        "optional_fields": [],
        "enums": _e("claim_type"),
        "examples": [
            {
                "description": "Extract evidence + assumption claims.",
                "call": {
                    "operation": "extract_claims",
                    "project_id": "prj_01ABC...",
                    "entry_id": "jrn_01XYZ...",
                    "claims": [
                        {"text": "Latency improves linearly.", "claim_type": "evidence"},
                    ],
                },
            },
        ],
        "related_operations": [
            "interpretation_candidates",
            "triage_interpretation_candidate",
            "claims",
        ],
        "notes": (
            "Compatibility name retained. This operation creates icd_ candidates, "
            "not clm_ records; explicit reviewed promotion is required."
        ),
    },
    "register_source": {
        "operation": "register_source",
        "tool": "rka_execute",
        "category": "sources",
        "role_tag": "ANY",
        "summary": "Register local bytes or a stable locator with hashes and provenance.",
        "signature": (
            "rka_execute(operation='register_source', *, project_id, source_kind, "
            "registered_by, filepath=None, pasted_text=None, stable_locator=None, ...)"
        ),
        "required_fields": ["project_id", "source_kind", "registered_by"],
        "optional_fields": [
            "title",
            "filepath",
            "pasted_text",
            "stable_locator",
            "mime",
            "expected_content_hash",
            "ownership_kind",
            "ownership_note",
            "provenance",
        ],
        "enums": _e(
            "registered_source_kind",
            "registered_source_ownership",
            "registered_source_actor",
        ),
        "examples": [
            {
                "description": "Register pasted research notes without admitting them.",
                "call": {
                    "operation": "register_source",
                    "project_id": "prj_01ABC...",
                    "source_kind": "pasted_text",
                    "pasted_text": "Observed behavior to review.",
                    "registered_by": "pi",
                    "ownership_kind": "researcher",
                    "provenance": {"collection": "lab notes"},
                },
            }
        ],
        "related_operations": ["sources", "create_interpretation_candidate"],
        "notes": (
            "Never fetches a URL, clones a repository, calls an LLM, or writes "
            "journal/claim/decision records. MCP filepath inputs are read on the host "
            "and transferred as bounded bytes; Docker needs no host-path mount. The "
            "result includes artifact_id for staging. Path reads require operator-configured "
            "RKA_HOST_FILE_ROOTS on the MCP host (RKA_SERVER_FILE_ROOTS for direct REST filepath). "
            "Both default to disabled; pasted text and explicit bytes remain available."
        ),
    },
    "admit_source_interpretation": {
        "operation": "admit_source_interpretation",
        "tool": "rka_execute",
        "category": "sources",
        "role_tag": "BRAIN",
        "summary": "Explicitly admit a grounded artifact interpretation to an existing target.",
        "signature": (
            "rka_execute(operation='admit_source_interpretation', *, project_id, "
            "source_id, candidate_id, expected_revision, target_type, target_id, "
            "actor, reason, grounding_verified=true)"
        ),
        "required_fields": [
            "project_id",
            "source_id",
            "candidate_id",
            "expected_revision",
            "target_type",
            "target_id",
            "actor",
            "reason",
            "grounding_verified",
        ],
        "optional_fields": [],
        "enums": _e("source_admission_target", "source_admission_actor"),
        "examples": [
            {
                "description": "Admit a reviewed source statement to an existing journal entry.",
                "call": {
                    "operation": "admit_source_interpretation",
                    "project_id": "prj_01ABC...",
                    "source_id": "src_01SOURCE...",
                    "candidate_id": "icd_01CANDIDATE...",
                    "expected_revision": 1,
                    "target_type": "journal",
                    "target_id": "jrn_01TARGET...",
                    "actor": "pi",
                    "reason": "Verified against the registered bytes.",
                    "grounding_verified": True,
                },
            }
        ],
        "related_operations": ["sources", "create_interpretation_candidate"],
        "notes": (
            "The target must already exist. Admission is revision guarded and auditable; "
            "it never generates canonical prose automatically."
        ),
    },
    "create_interpretation_candidate": {
        "operation": "create_interpretation_candidate",
        "tool": "rka_execute",
        "category": "claims",
        "role_tag": "BRAIN",
        "summary": "Stage one source-grounded atomic interpretation for review.",
        "signature": (
            "rka_execute(operation='create_interpretation_candidate', *, "
            "project_id, source_type, source_id, locator_kind, statement, "
            "epistemic_kind, created_by, extraction_tool, ...)"
        ),
        "required_fields": [
            "project_id",
            "source_type",
            "source_id",
            "locator_kind",
            "statement",
            "epistemic_kind",
            "created_by",
            "extraction_tool",
        ],
        "optional_fields": [
            "locator_start",
            "locator_end",
            "locator_value",
            "scope_conditions",
            "uncertainty",
            "uncertainty_note",
            "falsifier",
            "proposed_claim_type",
            "extraction_model",
        ],
        "enums": _e(
            "interpretation_source",
            "interpretation_locator",
            "epistemic_kind",
            "interpretation_actor",
            "interpretation_uncertainty",
            "claim_type",
        ),
        "examples": [
            {
                "description": "Stage an exact journal observation.",
                "call": {
                    "operation": "create_interpretation_candidate",
                    "project_id": "prj_01ABC...",
                    "source_type": "journal",
                    "source_id": "jrn_01XYZ...",
                    "locator_kind": "text_offset",
                    "locator_start": 40,
                    "locator_end": 78,
                    "statement": "The run measured 42 ms.",
                    "epistemic_kind": "observation",
                    "created_by": "brain",
                    "extraction_tool": "manual_review",
                    "proposed_claim_type": "result",
                },
            },
        ],
        "related_operations": [
            "interpretation_candidates",
            "add_interpretation_hint",
            "triage_interpretation_candidate",
        ],
        "notes": "Creation never promotes a canonical claim.",
    },
    "add_interpretation_hint": {
        "operation": "add_interpretation_hint",
        "tool": "rka_execute",
        "category": "claims",
        "role_tag": "BRAIN",
        "summary": "Record a typed duplicate or conflict hint between candidates.",
        "signature": (
            "rka_execute(operation='add_interpretation_hint', *, project_id, "
            "id, related_candidate_id, kind, rationale, created_by, "
            "expected_revision, confidence=0.5)"
        ),
        "required_fields": [
            "project_id",
            "id",
            "related_candidate_id",
            "kind",
            "rationale",
            "created_by",
            "expected_revision",
        ],
        "optional_fields": ["confidence"],
        "enums": _e("interpretation_hint_kind", "interpretation_actor"),
        "examples": [
            {
                "description": "Flag two candidates as likely duplicates.",
                "call": {
                    "operation": "add_interpretation_hint",
                    "project_id": "prj_01ABC...",
                    "id": "icd_01A...",
                    "related_candidate_id": "icd_01B...",
                    "kind": "duplicate",
                    "rationale": "Same measurement and scope.",
                    "created_by": "brain",
                    "expected_revision": 1,
                },
            },
        ],
        "related_operations": ["interpretation_candidates"],
        "notes": "Hints inform review; they do not merge or reject candidates.",
    },
    "triage_interpretation_candidate": {
        "operation": "triage_interpretation_candidate",
        "tool": "rka_execute",
        "category": "claims",
        "role_tag": "BRAIN",
        "summary": "Review, promote, reopen, or revoke one candidate.",
        "signature": (
            "rka_execute(operation='triage_interpretation_candidate', *, "
            "project_id, id, action, expected_revision, actor, reason=None, ...)"
        ),
        "required_fields": [
            "project_id",
            "id",
            "action",
            "expected_revision",
            "actor",
        ],
        "optional_fields": [
            "reason",
            "target_candidate_id",
            "target_entity_id",
            "grounding_verified",
            "claim_confidence",
            "evidence_role",
        ],
        "enums": _e(
            "interpretation_triage_action",
            "interpretation_review_actor",
            "claim_evidence_role",
        ),
        "examples": [
            {
                "description": "Promote after checking the exact source span.",
                "call": {
                    "operation": "triage_interpretation_candidate",
                    "project_id": "prj_01ABC...",
                    "id": "icd_01XYZ...",
                    "action": "promote",
                    "expected_revision": 2,
                    "actor": "brain",
                    "reason": "Checked exact grounding.",
                    "grounding_verified": True,
                },
            },
        ],
        "related_operations": ["interpretation_candidates", "claims"],
        "notes": (
            "Promotion sets grounding fidelity but leaves scientific evidence "
            "status unassessed. Revoke preserves the claim and marks it stale."
        ),
    },
    "create_experiment": {
        "operation": "create_experiment",
        "tool": "rka_execute",
        "category": "experiments",
        "role_tag": "BRAIN",
        "summary": "Create an experiment with immutable plan version 1.",
        "signature": (
            "rka_execute(operation='create_experiment', *, project_id, title, "
            "objective, protocol, created_by, reason, ...)"
        ),
        "required_fields": [
            "project_id", "objective", "protocol", "title", "created_by", "reason"
        ],
        "optional_fields": [
            "hypothesis", "conditions", "variables", "metrics", "baselines",
            "success_criteria", "failure_criteria", "repository_url", "commit_sha",
            "working_tree_state",
        ],
        "enums": _e("experiment_actor", "working_tree_state"),
        "examples": [{
            "description": "Create a reproducible experiment plan.",
            "call": {
                "operation": "create_experiment",
                "project_id": "prj_01ABC...",
                "title": "Evaluate detector latency",
                "objective": "Measure latency under the frozen workload.",
                "protocol": "Run 30 repetitions and retain raw outputs.",
                "created_by": "brain",
                "reason": "Test the latency claim.",
            },
        }],
        "related_operations": ["experiments", "create_experiment_run"],
        "notes": "Creation records a plan, not a result.",
    },
    "append_experiment_plan": {
        "operation": "append_experiment_plan",
        "tool": "rka_execute",
        "category": "experiments",
        "role_tag": "BRAIN",
        "summary": "Append a revision-guarded immutable experiment plan version.",
        "signature": (
            "rka_execute(operation='append_experiment_plan', *, project_id, id, "
            "expected_revision, objective, protocol, created_by, reason, ...)"
        ),
        "required_fields": [
            "project_id", "objective", "protocol", "id", "expected_revision",
            "created_by", "reason",
        ],
        "optional_fields": [
            "hypothesis", "conditions", "variables", "metrics", "baselines",
            "success_criteria", "failure_criteria", "repository_url", "commit_sha",
            "working_tree_state",
        ],
        "enums": _e("experiment_actor", "working_tree_state"),
        "examples": [{
            "description": "Refine a protocol without overwriting plan version 1.",
            "call": {
                "operation": "append_experiment_plan",
                "project_id": "prj_01ABC...",
                "id": "exp_01XYZ...",
                "expected_revision": 1,
                "objective": "Measure latency under the frozen workload.",
                "protocol": "Run 50 repetitions and retain raw outputs.",
                "created_by": "brain",
                "reason": "Increase statistical power.",
            },
        }],
        "related_operations": ["experiments"],
        "notes": "Older plan versions remain addressable by existing runs.",
    },
    "transition_experiment": {
        "operation": "transition_experiment",
        "tool": "rka_execute",
        "category": "experiments",
        "role_tag": "BRAIN",
        "summary": "Transition a revision-guarded experiment lifecycle.",
        "signature": (
            "rka_execute(operation='transition_experiment', *, project_id, id, "
            "expected_revision, target_status, actor, reason)"
        ),
        "required_fields": [
            "project_id", "id", "expected_revision", "target_status", "actor", "reason"
        ],
        "optional_fields": [],
        "enums": _e("experiment_status", "experiment_actor"),
        "examples": [{
            "description": "Activate a reviewed experiment plan.",
            "call": {
                "operation": "transition_experiment",
                "project_id": "prj_01ABC...",
                "id": "exp_01XYZ...",
                "expected_revision": 1,
                "target_status": "active",
                "actor": "pi",
                "reason": "Plan approved.",
            },
        }],
        "related_operations": ["experiments"],
        "notes": "Completion requires terminal run evidence but does not assess claims.",
    },
    "create_experiment_run": {
        "operation": "create_experiment_run",
        "tool": "rka_execute",
        "category": "experiments",
        "role_tag": "EXECUTOR",
        "summary": "Queue a run bound to one immutable plan version.",
        "signature": (
            "rka_execute(operation='create_experiment_run', *, project_id, "
            "experiment_id, plan_version, label, runner, created_by, reason, ...)"
        ),
        "required_fields": [
            "project_id", "experiment_id", "plan_version", "label", "runner",
            "created_by", "reason",
        ],
        "optional_fields": [
            "command", "config", "environment", "repository_url", "commit_sha",
            "working_tree_state",
        ],
        "enums": _e("experiment_run_kind", "experiment_actor", "working_tree_state"),
        "examples": [{
            "description": "Queue a local run against plan version 1.",
            "call": {
                "operation": "create_experiment_run",
                "project_id": "prj_01ABC...",
                "experiment_id": "exp_01XYZ...",
                "plan_version": 1,
                "label": "seed-1",
                "runner": "local",
                "created_by": "executor",
                "reason": "Execute the approved protocol.",
            },
        }],
        "related_operations": ["experiment_runs", "transition_experiment_run"],
        "notes": "A run cannot silently float to a newer plan.",
    },
    "transition_experiment_run": {
        "operation": "transition_experiment_run",
        "tool": "rka_execute",
        "category": "experiments",
        "role_tag": "EXECUTOR",
        "summary": "Append a revision-guarded run lifecycle event.",
        "signature": (
            "rka_execute(operation='transition_experiment_run', *, project_id, id, "
            "expected_revision, action, actor, reason, ...)"
        ),
        "required_fields": [
            "project_id", "id", "expected_revision", "action", "actor", "reason"
        ],
        "optional_fields": [
            "started_at", "completed_at", "exit_code", "failure_summary"
        ],
        "enums": _e("experiment_run_action", "experiment_actor"),
        "examples": [{
            "description": "Start a queued run.",
            "call": {
                "operation": "transition_experiment_run",
                "project_id": "prj_01ABC...",
                "id": "run_01XYZ...",
                "expected_revision": 1,
                "action": "start",
                "actor": "executor",
                "reason": "Execution started.",
            },
        }],
        "related_operations": ["experiment_runs", "record_experiment_observation"],
        "notes": "Run success means execution completed, not that a claim is supported.",
    },
    "record_experiment_observation": {
        "operation": "record_experiment_observation",
        "tool": "rka_execute",
        "category": "experiments",
        "role_tag": "EXECUTOR",
        "summary": "Record one immutable positive, negative, or inconclusive observation.",
        "signature": (
            "rka_execute(operation='record_experiment_observation', *, project_id, "
            "run_id, name, kind, direction, summary, observed_at, recorded_by, ...)"
        ),
        "required_fields": [
            "project_id", "run_id", "name", "kind", "direction", "summary",
            "observed_at", "recorded_by",
        ],
        "optional_fields": [
            "value_real", "value_text", "unit", "sample_size", "uncertainty_note"
        ],
        "enums": _e(
            "experiment_observation_kind",
            "experiment_observation_direction",
            "experiment_actor",
        ),
        "examples": [{
            "description": "Record an inconclusive comparison without recasting it as support.",
            "call": {
                "operation": "record_experiment_observation",
                "project_id": "prj_01ABC...",
                "run_id": "run_01XYZ...",
                "name": "latency difference",
                "kind": "comparison",
                "direction": "inconclusive",
                "summary": "Confidence interval crosses zero.",
                "value_text": "95% CI [-1.2, 0.8] ms",
                "observed_at": "2026-08-15T12:00:00Z",
                "recorded_by": "executor",
            },
        }],
        "related_operations": ["experiment_observations", "add_evidence_locator"],
        "notes": (
            "Observations are append-only and do not update claim evidence "
            "status. Provide at most one of value_real and value_text. "
            "metric, comparison, and test require one of those value fields; "
            "qualitative and failure require value_text. The REST/domain "
            "validator is authoritative."
        ),
    },
    "add_evidence_locator": {
        "operation": "add_evidence_locator",
        "tool": "rka_execute",
        "category": "experiments",
        "role_tag": "EXECUTOR",
        "summary": "Bind an observation to exact immutable evidence bytes.",
        "signature": (
            "rka_execute(operation='add_evidence_locator', *, project_id, "
            "observation_id, source_kind, locator_kind, created_by, ...)"
        ),
        "required_fields": [
            "project_id", "observation_id", "source_kind", "locator_kind", "created_by"
        ],
        "optional_fields": [
            "artifact_id", "repository_url", "commit_sha", "relative_path",
            "locator_start", "locator_end", "locator_value", "content_hash", "label"
        ],
        "enums": _e("evidence_source_kind", "evidence_locator_kind", "experiment_actor"),
        "examples": [{
            "description": "Bind an observation to a content-hashed repository file.",
            "call": {
                "operation": "add_evidence_locator",
                "project_id": "prj_01ABC...",
                "observation_id": "obs_01XYZ...",
                "source_kind": "repository",
                "repository_url": "https://github.com/example/project",
                "commit_sha": "0123456789abcdef0123456789abcdef01234567",
                "relative_path": "results/metrics.json",
                "locator_kind": "json_pointer",
                "locator_value": "/latency/median",
                "content_hash": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
                "created_by": "executor",
            },
        }],
        "related_operations": ["experiment_observations"],
        "notes": "Artifact locators copy the registered artifact hash; repository locators require one.",
    },
    "set_claim_scope": {
        "operation": "set_claim_scope",
        "tool": "rka_execute",
        "category": "claims",
        "role_tag": "BRAIN",
        "summary": "Append a versioned applicability contract to a canonical claim.",
        "signature": (
            "rka_execute(operation='set_claim_scope', *, project_id, claim_id, "
            "expected_revision, actor, reason, conditions=[], ...)"
        ),
        "required_fields": [
            "project_id",
            "claim_id",
            "expected_revision",
            "actor",
            "reason",
        ],
        "optional_fields": [
            "conditions",
            "uncertainty",
            "uncertainty_note",
            "extension_policy",
            "allowed_extensions",
            "prohibited_extensions",
            "falsifier_status",
            "falsifier",
            "falsifier_rationale",
            "disconfirming_claim_ids",
            "review_status",
        ],
        "enums": _e(
            "claim_scope_actor",
            "claim_scope_uncertainty",
            "claim_scope_extension_policy",
            "claim_falsifier_status",
            "claim_scope_review_status",
            "claim_condition_kind",
            "claim_condition_operator",
        ),
        "examples": [
            {
                "description": "Ratify a bounded empirical result scope.",
                "call": {
                    "operation": "set_claim_scope",
                    "project_id": "prj_01ABC...",
                    "claim_id": "clm_01XYZ...",
                    "expected_revision": 0,
                    "actor": "brain",
                    "reason": "Reviewed against the source and evaluation design.",
                    "conditions": [
                        {
                            "kind": "dataset",
                            "key": "evaluation_dataset",
                            "operator": "equals",
                            "value": "Dataset A",
                        },
                    ],
                    "uncertainty": "low",
                    "extension_policy": "exact_only",
                    "prohibited_extensions": ["other datasets without evidence"],
                    "falsifier_status": "applicable",
                    "falsifier": "Independent replication fails on Dataset A.",
                    "review_status": "reviewed",
                },
            },
        ],
        "related_operations": ["claim_scope", "claims", "review_claims"],
        "notes": (
            "Reviewed contracts require typed conditions, resolved uncertainty, "
            "an extension policy, prohibited extensions, and resolved falsifier "
            "applicability. expected_revision prevents stale writes."
        ),
    },
    "review_claims": {
        "operation": "review_claims",
        "tool": "rka_execute",
        "category": "claims",
        "role_tag": "BRAIN",
        "summary": "Curate claim grounding and explicit evidence assessment.",
        "signature": (
            "rka_execute(operation='review_claims', *, project_id, "
            "claim_ids, action='approve', confidence_override=None, "
            "evidence_status=None)"
        ),
        "required_fields": ["project_id", "claim_ids"],
        "optional_fields": ["action", "confidence_override", "evidence_status"],
        "enums": _e("review_action", "evidence_status"),
        "examples": [
            {
                "description": "Approve a batch of claims.",
                "call": {
                    "operation": "review_claims",
                    "project_id": "prj_01ABC...",
                    "claim_ids": ["clm_01...", "clm_02..."],
                    "action": "approve",
                },
            },
        ],
        "related_operations": ["claims", "review_cluster"],
        "notes": (
            "verified records source-grounding fidelity only. Set evidence_status "
            "explicitly for scientific support; approve/reject never infer it."
        ),
    },
    "create_cluster": {
        "operation": "create_cluster",
        "tool": "rka_execute",
        "category": "claims",
        "role_tag": "BRAIN",
        "summary": "Create a new evidence cluster.",
        "signature": (
            "rka_execute(operation='create_cluster', *, project_id, label, "
            "research_question_id=None, synthesis=None, "
            "confidence='emerging', claim_ids=None)"
        ),
        "required_fields": ["project_id", "label"],
        "optional_fields": [
            "research_question_id",
            "synthesis",
            "confidence",
            "claim_ids",
        ],
        "enums": _e("cluster_confidence"),
        "examples": [
            {
                "description": "Bootstrap a cluster tied to an RQ.",
                "call": {
                    "operation": "create_cluster",
                    "project_id": "prj_01ABC...",
                    "label": "Latency wins from embedding swap",
                    "research_question_id": "dec_01RQ...",
                    "confidence": "emerging",
                },
            },
        ],
        "related_operations": [
            "clusters",
            "assign_claims_to_cluster",
            "review_cluster",
        ],
        "notes": None,
    },
    "assign_claims_to_cluster": {
        "operation": "assign_claims_to_cluster",
        "tool": "rka_execute",
        "category": "claims",
        "role_tag": "BRAIN",
        "summary": "Attach a set of claims to an existing cluster.",
        "signature": (
            "rka_execute(operation='assign_claims_to_cluster', *, "
            "project_id, cluster_id, claim_ids)"
        ),
        "required_fields": ["project_id", "cluster_id", "claim_ids"],
        "optional_fields": [],
        "enums": {},
        "examples": [
            {
                "description": "Add 2 claims to a cluster.",
                "call": {
                    "operation": "assign_claims_to_cluster",
                    "project_id": "prj_01ABC...",
                    "cluster_id": "ecl_01XYZ...",
                    "claim_ids": ["clm_01...", "clm_02..."],
                },
            },
        ],
        "related_operations": [
            "create_cluster",
            "split_cluster",
            "merge_clusters",
        ],
        "notes": None,
    },
    "split_cluster": {
        "operation": "split_cluster",
        "tool": "rka_execute",
        "category": "claims",
        "role_tag": "BRAIN",
        "summary": "Split a cluster into multiple smaller clusters.",
        "signature": (
            "rka_execute(operation='split_cluster', *, project_id, source_id, new_clusters)"
        ),
        "required_fields": ["project_id", "source_id", "new_clusters"],
        "optional_fields": [],
        "enums": {},
        "examples": [
            {
                "description": "Split a cluster.",
                "call": {
                    "operation": "split_cluster",
                    "project_id": "prj_01ABC...",
                    "source_id": "ecl_01SRC...",
                    "new_clusters": [
                        {"label": "A", "claim_ids": ["clm_01..."]},
                        {"label": "B", "claim_ids": ["clm_02..."]},
                    ],
                },
            },
        ],
        "related_operations": [
            "merge_clusters",
            "assign_claims_to_cluster",
        ],
        "notes": None,
    },
    "merge_clusters": {
        "operation": "merge_clusters",
        "tool": "rka_execute",
        "category": "claims",
        "role_tag": "BRAIN",
        "summary": "Merge multiple clusters into a new combined cluster.",
        "signature": (
            "rka_execute(operation='merge_clusters', *, project_id, "
            "source_ids, target_label, target_synthesis=None, "
            "research_question_id=None)"
        ),
        "required_fields": ["project_id", "source_ids", "target_label"],
        "optional_fields": ["target_synthesis", "research_question_id"],
        "enums": {},
        "examples": [
            {
                "description": "Merge two clusters.",
                "call": {
                    "operation": "merge_clusters",
                    "project_id": "prj_01ABC...",
                    "source_ids": ["ecl_01A...", "ecl_01B..."],
                    "target_label": "Unified latency findings",
                },
            },
        ],
        "related_operations": ["split_cluster", "create_cluster"],
        "notes": None,
    },
    "review_cluster": {
        "operation": "review_cluster",
        "tool": "rka_execute",
        "category": "claims",
        "role_tag": "BRAIN",
        "summary": "Write the definitive synthesis on a cluster.",
        "signature": (
            "rka_execute(operation='review_cluster', *, project_id, "
            "cluster_id, confidence, synthesis, gaps=None, "
            "contradictions=None, resolve_queue_items=None, "
            "research_question_id=None)"
        ),
        "required_fields": [
            "project_id",
            "cluster_id",
            "confidence",
            "synthesis",
        ],
        "optional_fields": [
            "gaps",
            "contradictions",
            "resolve_queue_items",
            "research_question_id",
        ],
        "enums": _e("cluster_confidence"),
        "examples": [
            {
                "description": "Synthesize a strong-confidence cluster.",
                "call": {
                    "operation": "review_cluster",
                    "project_id": "prj_01ABC...",
                    "cluster_id": "ecl_01XYZ...",
                    "confidence": "strong",
                    "synthesis": "Embedding swap yields 30% p99 reduction.",
                },
            },
        ],
        "related_operations": [
            "clusters",
            "review_claims",
            "resolve_contradiction",
        ],
        "notes": None,
    },
    "resolve_contradiction": {
        "operation": "resolve_contradiction",
        "tool": "rka_execute",
        "category": "claims",
        "role_tag": "BRAIN",
        "summary": "Resolve a contradiction inside an evidence cluster.",
        "signature": (
            "rka_execute(operation='resolve_contradiction', *, project_id, "
            "cluster_id, resolution, claim_actions=None)"
        ),
        "required_fields": ["project_id", "cluster_id", "resolution"],
        "optional_fields": ["claim_actions"],
        "enums": {},
        "examples": [
            {
                "description": "Resolve a contradiction by retiring a claim.",
                "call": {
                    "operation": "resolve_contradiction",
                    "project_id": "prj_01ABC...",
                    "cluster_id": "ecl_01XYZ...",
                    "resolution": "Newer benchmark supersedes the older.",
                    "claim_actions": [
                        {"id": "clm_01OLD...", "action": "retire"},
                    ],
                },
            },
        ],
        "related_operations": ["review_cluster", "contradictions"],
        "notes": None,
    },
    # --- hooks ----------------------------------------------------------
    "hook_add": {
        "operation": "hook_add",
        "tool": "rka_execute",
        "category": "hooks",
        "role_tag": "PI",
        "summary": "Add a brain_notify lifecycle hook.",
        "signature": (
            "rka_execute(operation='hook_add', *, project_id, "
            "event, handler_type, handler_config, name, "
            "enabled=True, created_by='pi')"
        ),
        "required_fields": [
            "project_id",
            "event",
            "handler_type",
            "handler_config",
            "name",
        ],
        "optional_fields": ["enabled", "created_by"],
        "enums": {},
        "examples": [
            {
                "description": "Record a notification when a session starts.",
                "call": {
                    "operation": "hook_add",
                    "project_id": "prj_01ABC...",
                    "event": "session_start",
                    "handler_type": "brain_notify",
                    "handler_config": {"content_template": {"message": "Review project context"}},
                    "name": "notify-pi",
                },
            },
        ],
        "related_operations": [
            "hooks",
            "hook_enable",
            "hook_disable",
            "hook_delete",
        ],
        "notes": "Only brain_notify is supported. SQL and scheduled-only MCP handlers are rejected; legacy rows remain readable and can be disabled.",
    },
    "hook_enable": {
        "operation": "hook_enable",
        "tool": "rka_execute",
        "category": "hooks",
        "role_tag": "PI",
        "summary": "Enable a hook.",
        "signature": ("rka_execute(operation='hook_enable', *, project_id, hook_id)"),
        "required_fields": ["project_id", "hook_id"],
        "optional_fields": [],
        "enums": {},
        "examples": [
            {
                "description": "Enable a hook.",
                "call": {
                    "operation": "hook_enable",
                    "project_id": "prj_01ABC...",
                    "hook_id": "hk_01XYZ...",
                },
            },
        ],
        "related_operations": ["hook_add", "hook_disable"],
        "notes": None,
    },
    "hook_disable": {
        "operation": "hook_disable",
        "tool": "rka_execute",
        "category": "hooks",
        "role_tag": "PI",
        "summary": "Disable a hook (preserves config).",
        "signature": ("rka_execute(operation='hook_disable', *, project_id, hook_id)"),
        "required_fields": ["project_id", "hook_id"],
        "optional_fields": [],
        "enums": {},
        "examples": [
            {
                "description": "Disable a hook.",
                "call": {
                    "operation": "hook_disable",
                    "project_id": "prj_01ABC...",
                    "hook_id": "hk_01XYZ...",
                },
            },
        ],
        "related_operations": ["hook_enable", "hook_delete"],
        "notes": None,
    },
    "hook_delete": {
        "operation": "hook_delete",
        "tool": "rka_execute",
        "category": "hooks",
        "role_tag": "PI",
        "summary": "Permanently delete a hook.",
        "signature": ("rka_execute(operation='hook_delete', *, project_id, hook_id)"),
        "required_fields": ["project_id", "hook_id"],
        "optional_fields": [],
        "enums": {},
        "examples": [
            {
                "description": "Delete a hook.",
                "call": {
                    "operation": "hook_delete",
                    "project_id": "prj_01ABC...",
                    "hook_id": "hk_01XYZ...",
                },
            },
        ],
        "related_operations": ["hook_disable"],
        "notes": None,
    },
    "brain_notifications_clear": {
        "operation": "brain_notifications_clear",
        "tool": "rka_execute",
        "category": "notifications",
        "role_tag": "BRAIN",
        "summary": "Clear Brain notifications by ID.",
        "signature": ("rka_execute(operation='brain_notifications_clear', *, project_id, ids)"),
        "required_fields": ["project_id", "ids"],
        "optional_fields": [],
        "enums": {},
        "examples": [
            {
                "description": "Clear two notifications.",
                "call": {
                    "operation": "brain_notifications_clear",
                    "project_id": "prj_01ABC...",
                    "ids": ["bn_01...", "bn_02..."],
                },
            },
        ],
        "related_operations": ["brain_notifications"],
        "notes": None,
    },
    # --- workspace + maintenance ---------------------------------------
    "bootstrap_workspace": {
        "operation": "bootstrap_workspace",
        "tool": "rka_execute",
        "category": "workspace",
        "role_tag": "ANY",
        "summary": "Bootstrap a workspace into the knowledge base.",
        "signature": (
            "rka_execute(operation='bootstrap_workspace', *, project_id, "
            "folder_path, phase=None, override_tags=None, skip_files=None, "
            "use_llm=True, dry_run=False)"
        ),
        "required_fields": ["project_id", "folder_path"],
        "optional_fields": [
            "phase",
            "override_tags",
            "skip_files",
            "use_llm",
            "dry_run",
        ],
        "enums": {},
        "examples": [
            {
                "description": "Dry-run bootstrap of a workspace folder.",
                "call": {
                    "operation": "bootstrap_workspace",
                    "project_id": "prj_01ABC...",
                    "folder_path": "/Users/me/Research/proj",
                    "dry_run": True,
                },
            },
        ],
        "related_operations": [
            "workspace_scan",
            "workspace_tree",
            "bootstrap_review",
        ],
        "notes": "Requires operator-configured RKA_HOST_FILE_ROOTS. Sends selected host content to the configured Core server; dry_run still scans and sends previews but does not create records. A server reply cannot nominate unscanned host files.",
    },
    "scan_workspace": {
        "operation": "scan_workspace",
        "tool": "rka_execute",
        "category": "workspace",
        "role_tag": "ANY",
        "summary": "Execute a workspace scan (writes scn_... row).",
        "signature": (
            "rka_execute(operation='scan_workspace', *, project_id, "
            "folder_path, ignore_patterns=None, max_file_size_mb=50.0, "
            "use_llm=True)"
        ),
        "required_fields": ["project_id", "folder_path"],
        "optional_fields": [
            "ignore_patterns",
            "max_file_size_mb",
            "use_llm",
        ],
        "enums": {},
        "examples": [
            {
                "description": "Scan a workspace.",
                "call": {
                    "operation": "scan_workspace",
                    "project_id": "prj_01ABC...",
                    "folder_path": "/Users/me/Research/proj",
                },
            },
        ],
        "related_operations": ["workspace_scan", "bootstrap_workspace"],
        "notes": None,
    },
    "flag_stale": {
        "operation": "flag_stale",
        "tool": "rka_execute",
        "category": "maintenance",
        "role_tag": "BRAIN",
        "summary": "Flag a knowledge entity as stale (yellow/red).",
        "signature": (
            "rka_execute(operation='flag_stale', *, project_id, "
            "entity_id, reason, staleness='yellow', propagate=True)"
        ),
        "required_fields": ["project_id", "entity_id", "reason"],
        "optional_fields": ["staleness", "propagate"],
        "enums": _e("staleness"),
        "examples": [
            {
                "description": "Flag a finding as stale.",
                "call": {
                    "operation": "flag_stale",
                    "project_id": "prj_01ABC...",
                    "entity_id": "jrn_01XYZ...",
                    "reason": "Superseded by new benchmark.",
                    "staleness": "red",
                },
            },
        ],
        "related_operations": ["freshness", "eviction_sweep"],
        "notes": None,
    },
    "eviction_sweep": {
        "operation": "eviction_sweep",
        "tool": "rka_execute",
        "category": "maintenance",
        "role_tag": "BRAIN",
        "summary": "Evict knowledge per policy (defaults to dry_run).",
        "signature": ("rka_execute(operation='eviction_sweep', *, project_id, dry_run=True)"),
        "required_fields": ["project_id"],
        "optional_fields": ["dry_run"],
        "enums": {},
        "examples": [
            {
                "description": "Preview an eviction sweep.",
                "call": {
                    "operation": "eviction_sweep",
                    "project_id": "prj_01ABC...",
                    "dry_run": True,
                },
            },
        ],
        "related_operations": ["flag_stale", "freshness"],
        "notes": None,
    },
    # --- provisional manuscript planning -------------------------------
    "planning_branches": {
        "operation": "planning_branches",
        "tool": "rka_query",
        "category": "manuscript_planning",
        "role_tag": "ANY",
        "summary": "List planning branches or fetch one exact effective snapshot.",
        "signature": (
            "rka_query(operation='planning_branches', *, project_id, id=None, "
            "manuscript_id=None, include_archived=True)"
        ),
        "required_fields": ["project_id"],
        "optional_fields": ["id", "manuscript_id", "include_archived"],
        "enums": {},
        "examples": [{
            "description": "List project-only planning branches.",
            "call": {"operation": "planning_branches", "project_id": "prj_01ABC..."},
        }],
        "related_operations": ["planning_resume", "planning_compare"],
        "notes": "These provisional artifacts never ratify canonical manuscript semantics.",
    },
    "planning_resume": {
        "operation": "planning_resume",
        "tool": "rka_query",
        "category": "manuscript_planning",
        "role_tag": "ANY",
        "summary": "Resume the selected branch at its exact persisted head.",
        "signature": (
            "rka_query(operation='planning_resume', *, project_id, manuscript_id=None)"
        ),
        "required_fields": ["project_id"],
        "optional_fields": ["manuscript_id"],
        "enums": {},
        "examples": [{
            "description": "Resume project-only planning.",
            "call": {"operation": "planning_resume", "project_id": "prj_01ABC..."},
        }],
        "related_operations": ["planning_branches"],
        "notes": "Returns null when no selected branch exists for the context.",
    },
    "planning_compare": {
        "operation": "planning_compare",
        "tool": "rka_query",
        "category": "manuscript_planning",
        "role_tag": "ANY",
        "summary": "Compare two revision-pinned planning branches deterministically.",
        "signature": (
            "rka_query(operation='planning_compare', *, project_id, "
            "base_branch_id, other_branch_id)"
        ),
        "required_fields": ["project_id", "base_branch_id", "other_branch_id"],
        "optional_fields": [],
        "enums": {},
        "examples": [{
            "description": "Compare an alternative against the primary framing.",
            "call": {
                "operation": "planning_compare",
                "project_id": "prj_01ABC...",
                "base_branch_id": "mpb_01BASE...",
                "other_branch_id": "mpb_01OTHER...",
            },
        }],
        "related_operations": ["planning_branches", "transition_planning_branch"],
        "notes": "Comparison never selects or mutates either branch.",
    },
    "planning_artifact_versions": {
        "operation": "planning_artifact_versions",
        "tool": "rka_query",
        "category": "manuscript_planning",
        "role_tag": "ANY",
        "summary": "Read all immutable versions and bindings for one planning artifact.",
        "signature": (
            "rka_query(operation='planning_artifact_versions', *, project_id, id)"
        ),
        "required_fields": ["project_id", "id"],
        "optional_fields": [],
        "enums": {},
        "examples": [{
            "description": "Inspect recoverable artifact history.",
            "call": {
                "operation": "planning_artifact_versions",
                "project_id": "prj_01ABC...",
                "id": "pla_01XYZ...",
            },
        }],
        "related_operations": ["append_planning_artifact_version"],
        "notes": None,
    },
    "planning_argument_workflow": {
        "operation": "planning_argument_workflow",
        "tool": "rka_query",
        "category": "manuscript_planning",
        "role_tag": "ANY",
        "summary": "Read deterministic seed-to-contribution guidance and quick-reader slots.",
        "signature": (
            "rka_query(operation='planning_argument_workflow', *, project_id, id)"
        ),
        "required_fields": ["project_id", "id"],
        "optional_fields": [],
        "enums": {},
        "examples": [{
            "description": "Inspect stage blockers and the next useful decision.",
            "call": {
                "operation": "planning_argument_workflow",
                "project_id": "prj_01ABC...",
                "id": "mpb_01XYZ...",
            },
        }],
        "related_operations": [
            "planning_branches",
            "append_planning_artifact_version",
        ],
        "notes": "This is a read-only projection and performs no LLM call or promotion.",
    },
    "planning_promotions": {
        "operation": "planning_promotions",
        "tool": "rka_query",
        "category": "manuscript_planning",
        "role_tag": "ANY",
        "summary": "Read append-only RQ and contribution promotion lineage.",
        "signature": (
            "rka_query(operation='planning_promotions', *, project_id, id)"
        ),
        "required_fields": ["project_id", "id"],
        "optional_fields": [],
        "enums": {},
        "examples": [{
            "description": "Audit all promotion actions for one branch.",
            "call": {
                "operation": "planning_promotions",
                "project_id": "prj_01ABC...",
                "id": "mpb_01XYZ...",
            },
        }],
        "related_operations": [
            "promote_planning_rq",
            "prepare_planning_contribution",
            "ratify_planning_contribution",
        ],
        "notes": "The ledger is append-only and includes exact source artifact versions.",
    },
    "planning_evaluation_workflow": {
        "operation": "planning_evaluation_workflow",
        "tool": "rka_query",
        "category": "manuscript_planning",
        "role_tag": "ANY",
        "summary": "Resolve exact claim, experiment, observation, and locator readiness.",
        "signature": (
            "rka_query(operation='planning_evaluation_workflow', *, project_id, id)"
        ),
        "required_fields": ["project_id", "id"],
        "optional_fields": [],
        "enums": {},
        "examples": [{
            "description": "Inspect categorical evidence readiness and adverse outcomes.",
            "call": {
                "operation": "planning_evaluation_workflow",
                "project_id": "prj_01ABC...",
                "id": "mpb_01XYZ...",
            },
        }],
        "related_operations": [
            "planning_evaluation_events",
            "create_planning_evaluation_mission",
            "prepare_planning_evaluation_result",
        ],
        "notes": "The projection performs no LLM call and never infers support from direction.",
    },
    "planning_evaluation_events": {
        "operation": "planning_evaluation_events",
        "tool": "rka_query",
        "category": "manuscript_planning",
        "role_tag": "ANY",
        "summary": "Read immutable evaluation mission and result-unit action lineage.",
        "signature": (
            "rka_query(operation='planning_evaluation_events', *, project_id, id)"
        ),
        "required_fields": ["project_id", "id"],
        "optional_fields": [],
        "enums": {},
        "examples": [{
            "description": "Audit all canonical evaluation actions for one branch.",
            "call": {
                "operation": "planning_evaluation_events",
                "project_id": "prj_01ABC...",
                "id": "mpb_01XYZ...",
            },
        }],
        "related_operations": ["planning_evaluation_workflow"],
        "notes": "The ledger is append-only and pins exact planning versions.",
    },
    "create_planning_branch": {
        "operation": "create_planning_branch",
        "tool": "rka_execute",
        "category": "manuscript_planning",
        "role_tag": "BRAIN",
        "summary": "Create a project- or manuscript-scoped revision-pinned branch.",
        "signature": (
            "rka_execute(operation='create_planning_branch', *, project_id, "
            "name, purpose, created_by, reason, ...)"
        ),
        "required_fields": ["project_id", "name", "purpose", "created_by", "reason"],
        "optional_fields": ["manuscript_id", "parent_branch_id"],
        "enums": _e("planning_actor"),
        "examples": [{
            "description": "Fork a framing alternative.",
            "call": {
                "operation": "create_planning_branch",
                "project_id": "prj_01ABC...",
                "name": "mechanism-first",
                "purpose": "Explore a mechanism-first paper framing.",
                "created_by": "pi",
                "reason": "Keep the alternative recoverable.",
            },
        }],
        "related_operations": ["planning_branches", "append_planning_artifact_version"],
        "notes": "The first branch in a context becomes selected; later branches start active.",
    },
    "transition_planning_branch": {
        "operation": "transition_planning_branch",
        "tool": "rka_execute",
        "category": "manuscript_planning",
        "role_tag": "PI",
        "summary": "Select, activate, archive, or supersede a planning branch.",
        "signature": (
            "rka_execute(operation='transition_planning_branch', *, project_id, id, "
            "expected_revision, target_state, actor, reason)"
        ),
        "required_fields": [
            "project_id", "id", "expected_revision", "target_state", "actor", "reason"
        ],
        "optional_fields": [],
        "enums": _e("planning_branch_state", "planning_actor"),
        "examples": [{
            "description": "Select a reviewed alternative.",
            "call": {
                "operation": "transition_planning_branch",
                "project_id": "prj_01ABC...",
                "id": "mpb_01XYZ...",
                "expected_revision": 4,
                "target_state": "selected",
                "actor": "pi",
                "reason": "This branch has the clearest argument.",
            },
        }],
        "related_operations": ["planning_compare", "planning_resume"],
        "notes": "Select a replacement before archiving the currently selected branch.",
    },
    "append_planning_artifact_version": {
        "operation": "append_planning_artifact_version",
        "tool": "rka_execute",
        "category": "manuscript_planning",
        "role_tag": "BRAIN",
        "summary": "Append a schema-validated immutable planning-artifact version.",
        "signature": (
            "rka_execute(operation='append_planning_artifact_version', *, project_id, "
            "id, expected_branch_revision, local_key, stage_type, summary, payload, "
            "origin, created_by, reason, ...)"
        ),
        "required_fields": [
            "project_id", "id", "expected_branch_revision", "local_key", "stage_type",
            "summary", "payload", "origin", "created_by", "reason",
        ],
        "optional_fields": [
            "expected_previous_version", "lifecycle", "provider", "model",
            "context_hash", "unresolved_items", "readiness_state",
            "readiness_missing", "readiness_notes", "promotion_target_type",
            "promotion_target_id", "evidence_bindings",
        ],
        "enums": _e(
            "planning_stage", "planning_lifecycle", "planning_origin",
            "planning_readiness", "planning_actor",
        ),
        "examples": [{
            "description": "Record the one-sentence insight with no evidence laundering.",
            "call": {
                "operation": "append_planning_artifact_version",
                "project_id": "prj_01ABC...",
                "id": "mpb_01XYZ...",
                "expected_branch_revision": 1,
                "local_key": "core-insight",
                "stage_type": "seed",
                "summary": "A composable timing primitive changes the security trade-off.",
                "payload": {"insight": "Treat timing as a composable security primitive."},
                "origin": "user",
                "created_by": "pi",
                "reason": "Preserve the initial insight before expansion.",
            },
        }],
        "related_operations": ["planning_artifact_versions", "planning_compare"],
        "notes": (
            "origin='ai_suggested' additionally requires provider, model, and context_hash. "
            "Use lifecycle='parked' for the recoverable parking lot."
        ),
    },
    "promote_planning_rq": {
        "operation": "promote_planning_rq",
        "tool": "rka_execute",
        "category": "manuscript_planning",
        "role_tag": "PI",
        "summary": "Promote one selected, ready RQ candidate into a PI decision.",
        "signature": (
            "rka_execute(operation='promote_planning_rq', *, project_id, id, "
            "expected_branch_revision, artifact_id, expected_artifact_version, "
            "candidate_key, phase, reason, confirmed_by='pi')"
        ),
        "required_fields": [
            "project_id", "id", "expected_branch_revision", "artifact_id",
            "expected_artifact_version", "candidate_key", "phase", "reason",
        ],
        "optional_fields": ["confirmed_by"],
        "enums": {},
        "examples": [{
            "description": "Ratify the selected research question as a decision.",
            "call": {
                "operation": "promote_planning_rq",
                "project_id": "prj_01ABC...",
                "id": "mpb_01XYZ...",
                "expected_branch_revision": 7,
                "artifact_id": "pla_01RQ...",
                "expected_artifact_version": 2,
                "candidate_key": "rq-main",
                "phase": "paper_framing",
                "reason": "PI selected this bounded question.",
            },
        }],
        "related_operations": ["planning_argument_workflow", "planning_promotions"],
        "notes": "Promotion is explicit, revision guarded, and rejects duplicate actions.",
    },
    "prepare_planning_contribution": {
        "operation": "prepare_planning_contribution",
        "tool": "rka_execute",
        "category": "manuscript_planning",
        "role_tag": "BRAIN",
        "summary": "Prepare a semantic proposal for one selected contribution candidate.",
        "signature": (
            "rka_execute(operation='prepare_planning_contribution', *, project_id, id, "
            "expected_branch_revision, artifact_id, expected_artifact_version, "
            "candidate_key, manuscript_id, expected_manuscript_revision, reason, ...)"
        ),
        "required_fields": [
            "project_id", "id", "expected_branch_revision", "artifact_id",
            "expected_artifact_version", "candidate_key", "manuscript_id",
            "expected_manuscript_revision", "reason",
        ],
        "optional_fields": ["claim_local_key", "actor"],
        "enums": {},
        "examples": [{
            "description": "Prepare the exact candidate wording for review without applying it.",
            "call": {
                "operation": "prepare_planning_contribution",
                "project_id": "prj_01ABC...",
                "id": "mpb_01XYZ...",
                "expected_branch_revision": 7,
                "artifact_id": "pla_01RQ...",
                "expected_artifact_version": 2,
                "candidate_key": "contribution-main",
                "manuscript_id": "man_01ABC...",
                "expected_manuscript_revision": 3,
                "reason": "Prepare exact wording for human review.",
            },
        }],
        "related_operations": [
            "semantic_patch_proposals",
            "apply_semantic_patch_proposal",
            "planning_promotions",
        ],
        "notes": "This action never mutates the manuscript; apply remains a separate review step.",
    },
    "ratify_planning_contribution": {
        "operation": "ratify_planning_contribution",
        "tool": "rka_execute",
        "category": "manuscript_planning",
        "role_tag": "PI",
        "summary": "Ratify exact applied contribution wording against a PI decision.",
        "signature": (
            "rka_execute(operation='ratify_planning_contribution', *, project_id, id, "
            "expected_branch_revision, artifact_id, expected_artifact_version, "
            "candidate_key, manuscript_id, claim_ref, expected_manuscript_revision, "
            "proposal_id, decision_id, reason, confirmed_by='pi')"
        ),
        "required_fields": [
            "project_id", "id", "expected_branch_revision", "artifact_id",
            "expected_artifact_version", "candidate_key", "manuscript_id",
            "claim_ref", "expected_manuscript_revision", "proposal_id",
            "decision_id", "reason",
        ],
        "optional_fields": ["confirmed_by"],
        "enums": {},
        "examples": [{
            "description": "Bind the applied wording to an existing PI decision.",
            "call": {
                "operation": "ratify_planning_contribution",
                "project_id": "prj_01ABC...",
                "id": "mpb_01XYZ...",
                "expected_branch_revision": 7,
                "artifact_id": "pla_01RQ...",
                "expected_artifact_version": 2,
                "candidate_key": "contribution-main",
                "manuscript_id": "man_01ABC...",
                "claim_ref": "main-contribution",
                "expected_manuscript_revision": 4,
                "proposal_id": "spp_01ABC...",
                "decision_id": "dec_01ABC...",
                "reason": "PI ratified the exact applied wording.",
            },
        }],
        "related_operations": [
            "planning_promotions",
            "ratify_manuscript_claim",
        ],
        "notes": "The selected candidate, applied claim version, and PI decision must agree exactly.",
    },
    "create_planning_evaluation_mission": {
        "operation": "create_planning_evaluation_mission",
        "tool": "rka_execute",
        "category": "manuscript_planning",
        "role_tag": "BRAIN",
        "summary": "Create one canonical mission from a selected missing-evidence slot.",
        "signature": (
            "rka_execute(operation='create_planning_evaluation_mission', *, project_id, "
            "id, expected_branch_revision, artifact_id, expected_artifact_version, "
            "commitment_key, requirement_key, reason, ...)"
        ),
        "required_fields": [
            "project_id", "id", "expected_branch_revision", "artifact_id",
            "expected_artifact_version", "commitment_key", "requirement_key", "reason",
        ],
        "optional_fields": ["phase", "motivated_by_decision", "actor"],
        "enums": {},
        "examples": [{
            "description": "Turn an unresolved primary-effect slot into executable work.",
            "call": {
                "operation": "create_planning_evaluation_mission",
                "project_id": "prj_01ABC...",
                "id": "mpb_01XYZ...",
                "expected_branch_revision": 8,
                "artifact_id": "pla_01EVAL...",
                "expected_artifact_version": 2,
                "commitment_key": "claim-primary-evaluation",
                "requirement_key": "primary-effect",
                "reason": "Collect the exact missing evidence.",
            },
        }],
        "related_operations": ["planning_evaluation_workflow", "mission"],
        "notes": "Creation is idempotent for one artifact version and requirement.",
    },
    "prepare_planning_evaluation_result": {
        "operation": "prepare_planning_evaluation_result",
        "tool": "rka_execute",
        "category": "manuscript_planning",
        "role_tag": "BRAIN",
        "summary": "Prepare one bounded native result-unit semantic proposal.",
        "signature": (
            "rka_execute(operation='prepare_planning_evaluation_result', *, project_id, "
            "id, expected_branch_revision, artifact_id, expected_artifact_version, "
            "commitment_key, manuscript_id, expected_manuscript_revision, "
            "result_unit_local_key, location, title, artifact_ref, reason, ...)"
        ),
        "required_fields": [
            "project_id", "id", "expected_branch_revision", "artifact_id",
            "expected_artifact_version", "commitment_key", "manuscript_id",
            "expected_manuscript_revision", "result_unit_local_key", "location",
            "title", "artifact_ref", "reason",
        ],
        "optional_fields": ["actor"],
        "enums": {},
        "examples": [{
            "description": "Prepare a result unit from exact located observations.",
            "call": {
                "operation": "prepare_planning_evaluation_result",
                "project_id": "prj_01ABC...",
                "id": "mpb_01XYZ...",
                "expected_branch_revision": 9,
                "artifact_id": "pla_01EVAL...",
                "expected_artifact_version": 3,
                "commitment_key": "claim-primary-evaluation",
                "manuscript_id": "man_01ABC...",
                "expected_manuscript_revision": 5,
                "result_unit_local_key": "result-primary-effect",
                "location": "sections/results.tex#primary-effect",
                "title": "Primary effect",
                "artifact_ref": "art_01RESULT...",
                "reason": "Prepare the exact bounded result for review.",
            },
        }],
        "related_operations": [
            "planning_evaluation_workflow",
            "semantic_patch_proposals",
            "apply_semantic_patch_proposal",
        ],
        "notes": "Preparation never mutates the manuscript; apply remains separate.",
    },
    "prepare_manuscript_outline_proposal": {
        "operation": "prepare_manuscript_outline_proposal",
        "tool": "rka_execute",
        "category": "manuscript",
        "role_tag": "PI",
        "summary": "Prepare an outline edit, expansion, condensation, or reorder proposal.",
        "signature": (
            "rka_execute(operation='prepare_manuscript_outline_proposal', *, "
            "project_id, id, expected_revision, action, reason, ...)"
        ),
        "required_fields": [
            "project_id", "id", "expected_revision", "action", "reason",
            "origin", "provider", "model", "boundary", "context_manifest_id",
        ],
        "optional_fields": [
            "unit_key", "patch", "children", "descendant_keys", "ordered_unit_keys",
        ],
        "enums": {
            "action": list(_ENUMS["outline_action"]),
            "origin": list(_ENUMS["semantic_patch_ai_origin"]),
            "boundary": list(_ENUMS["semantic_patch_ai_boundary"]),
        },
        "examples": [{
            "description": "Prepare a complete-set reorder for semantic review.",
            "call": {
                "operation": "prepare_manuscript_outline_proposal",
                "project_id": "prj_01ABC...",
                "id": "man_01XYZ...",
                "expected_revision": 6,
                "action": "reorder",
                "reason": "Lead with the mechanism for this audience.",
                "origin": "host_agent",
                "provider": "openai",
                "model": "gpt-5.6",
                "boundary": "host_conversation",
                "context_manifest_id": "pcm_01ABC...",
                "ordered_unit_keys": ["METHOD", "INTRO", "RESULT"],
            },
        }],
        "related_operations": [
            "manuscript_outline", "semantic_patch_proposals",
            "apply_semantic_patch_proposal",
        ],
        "notes": (
            "Preparation never mutates the manuscript or resolves its Outline checkpoint. "
            "AI-origin proposals require provider, model, boundary, and a matching context manifest."
        ),
    },
    # --- unified semantic patch proposals ------------------------------
    "semantic_patch_proposals": {
        "operation": "semantic_patch_proposals",
        "tool": "rka_query",
        "category": "semantic_patches",
        "role_tag": "ANY",
        "summary": "List semantic proposals or fetch one exact diff and event history.",
        "signature": (
            "rka_query(operation='semantic_patch_proposals', *, project_id, "
            "id=None, status=None, limit=100)"
        ),
        "required_fields": ["project_id"],
        "optional_fields": ["id", "status", "limit"],
        "enums": {"status": list(_ENUMS["semantic_patch_status"])},
        "examples": [{
            "description": "List proposals waiting for review.",
            "call": {
                "operation": "semantic_patch_proposals",
                "project_id": "prj_01ABC...",
                "status": "proposed",
            },
        }],
        "related_operations": [
            "create_semantic_patch_proposal", "apply_semantic_patch_proposal",
            "reject_semantic_patch_proposal",
        ],
        "notes": "A proposed record has not mutated planning or manuscript state.",
    },
    "semantic_patch_schema": {
        "operation": "semantic_patch_schema",
        "tool": "rka_query",
        "category": "semantic_patches",
        "role_tag": "ANY",
        "summary": "Return the exact JSON schema expected from a host-agent suggestion.",
        "signature": "rka_query(operation='semantic_patch_schema', *, project_id)",
        "required_fields": ["project_id"],
        "optional_fields": [],
        "enums": {},
        "examples": [{
            "description": "Load the schema before generating a host-agent draft.",
            "call": {"operation": "semantic_patch_schema", "project_id": "prj_01ABC..."},
        }],
        "related_operations": ["prepare_semantic_patch_context"],
        "notes": None,
    },
    "prepare_semantic_patch_context": {
        "operation": "prepare_semantic_patch_context",
        "tool": "rka_execute",
        "category": "semantic_patches",
        "role_tag": "ANY",
        "summary": "Persist an exact disclosure manifest before an AI provider call.",
        "signature": (
            "rka_execute(operation='prepare_semantic_patch_context', *, project_id, "
            "origin, provider, model, boundary, ...)"
        ),
        "required_fields": ["origin", "provider", "model", "boundary", "project_id"],
        "optional_fields": [
            "selected_context", "include_source_closure", "targets", "constraints",
            "omissions", "truncation_notes",
        ],
        "enums": {
            "origin": list(_ENUMS["semantic_patch_ai_origin"]),
            "boundary": list(_ENUMS["semantic_patch_ai_boundary"]),
        },
        "examples": [{
            "description": "Prepare selected RKA context for the current host conversation.",
            "call": {
                "operation": "prepare_semantic_patch_context",
                "project_id": "prj_01ABC...",
                "origin": "host_agent",
                "provider": "chatgpt",
                "model": "host-model",
                "boundary": "host_conversation",
                "selected_context": [{"entity_id": "clm_01XYZ...", "role": "support"}],
                "targets": [{"target_type": "manuscript", "target_id": "man_01ABC..."}],
            },
        }],
        "related_operations": ["semantic_patch_schema", "create_semantic_patch_proposal"],
        "notes": (
            "Call this before generation and disclose every mutable target and referenced "
            "evidence entity; credentials are never included."
        ),
    },
    "create_semantic_patch_proposal": {
        "operation": "create_semantic_patch_proposal",
        "tool": "rka_execute",
        "category": "semantic_patches",
        "role_tag": "ANY",
        "summary": "Validate and store a human or AI semantic edit without applying it.",
        "signature": (
            "rka_execute(operation='create_semantic_patch_proposal', *, project_id, "
            "origin, intent, reason, created_by, operations, ...)"
        ),
        "required_fields": [
            "origin", "intent", "reason", "created_by", "operations", "project_id",
            "provider", "model", "boundary", "context_manifest_id",
        ],
        "optional_fields": [
            "supersedes_proposal_id",
        ],
        "enums": {
            "origin": list(_ENUMS["semantic_patch_ai_origin"]),
            "created_by": list(_ENUMS["semantic_patch_actor"]),
            "boundary": list(_ENUMS["semantic_patch_ai_boundary"]),
        },
        "examples": [{
            "description": "Propose a metadata change for review.",
            "call": {
                "operation": "create_semantic_patch_proposal",
                "project_id": "prj_01ABC...",
                "origin": "host_agent",
                "intent": "Clarify the title.",
                "reason": "Improve quick-reader comprehension.",
                "created_by": "executor",
                "provider": "openai",
                "model": "gpt-5.6",
                "boundary": "host_conversation",
                "context_manifest_id": "pcm_01ABC...",
                "operations": [{
                    "operation": "manuscript_metadata_update",
                    "manuscript_id": "man_01XYZ...",
                    "expected_revision": 3,
                    "title": "A clearer title",
                }],
            },
        }],
        "related_operations": ["semantic_patch_proposals", "apply_semantic_patch_proposal"],
        "notes": (
            "No target is mutated until a separate explicit apply. Typed MCP callers "
            "must disclose an AI provider boundary; human proposals use local REST/UI."
        ),
    },
    "apply_semantic_patch_proposal": {
        "operation": "apply_semantic_patch_proposal",
        "tool": "rka_execute",
        "category": "semantic_patches",
        "role_tag": "PI",
        "summary": "Explicitly apply one reviewed proposal under optimistic guards.",
        "signature": (
            "rka_execute(operation='apply_semantic_patch_proposal', *, project_id, "
            "id, expected_revision, actor, reason)"
        ),
        "required_fields": ["expected_revision", "actor", "reason", "project_id", "id"],
        "optional_fields": [],
        "enums": {"actor": list(_ENUMS["semantic_patch_actor"])},
        "examples": [{
            "description": "Apply after reviewing the semantic diff and warnings.",
            "call": {
                "operation": "apply_semantic_patch_proposal",
                "project_id": "prj_01ABC...",
                "id": "spp_01XYZ...",
                "expected_revision": 1,
                "actor": "pi",
                "reason": "Approved after preview.",
            },
        }],
        "related_operations": ["semantic_patch_proposals"],
        "notes": "A stale base becomes a preserved conflict; no target is partially changed.",
    },
    "reject_semantic_patch_proposal": {
        "operation": "reject_semantic_patch_proposal",
        "tool": "rka_execute",
        "category": "semantic_patches",
        "role_tag": "PI",
        "summary": "Reject a proposal without changing any target.",
        "signature": (
            "rka_execute(operation='reject_semantic_patch_proposal', *, project_id, "
            "id, expected_revision, actor, reason)"
        ),
        "required_fields": ["expected_revision", "actor", "reason", "project_id", "id"],
        "optional_fields": [],
        "enums": {"actor": list(_ENUMS["semantic_patch_actor"])},
        "examples": [{
            "description": "Reject while preserving the audit record.",
            "call": {
                "operation": "reject_semantic_patch_proposal",
                "project_id": "prj_01ABC...",
                "id": "spp_01XYZ...",
                "expected_revision": 1,
                "actor": "pi",
                "reason": "The framing is not suitable.",
            },
        }],
        "related_operations": ["semantic_patch_proposals"],
        "notes": None,
    },
    "generate_lm_studio_semantic_patch": {
        "operation": "generate_lm_studio_semantic_patch",
        "tool": "rka_execute",
        "category": "semantic_patches",
        "role_tag": "ANY",
        "summary": "Generate a schema-validated proposal through local LM Studio.",
        "signature": (
            "rka_execute(operation='generate_lm_studio_semantic_patch', *, project_id, "
            "instruction, created_by, ...)"
        ),
        "required_fields": ["instruction", "created_by", "project_id"],
        "optional_fields": [
            "selected_context", "targets", "include_source_closure", "constraints",
            "omissions", "truncation_notes", "model",
        ],
        "enums": {"created_by": list(_ENUMS["semantic_patch_actor"])},
        "examples": [{
            "description": "Ask a local model for a proposal, not a direct edit.",
            "call": {
                "operation": "generate_lm_studio_semantic_patch",
                "project_id": "prj_01ABC...",
                "instruction": "Suggest a clearer one-paragraph spine.",
                "created_by": "pi",
            },
        }],
        "related_operations": ["semantic_patch_proposals"],
        "notes": (
            "The endpoint must be loopback or Docker's exact host gateway; "
            "generation never applies the result."
        ),
    },
    # --- session (unscoped + project lifecycle) ------------------------
    "create_project": {
        "operation": "create_project",
        "tool": "rka_execute",
        "category": "session",
        "role_tag": "PI",
        "summary": "Bootstrap a new RKA project (UNSCOPED — no project_id required).",
        "signature": ("rka_execute(operation='create_project', *, name, description=None)"),
        "required_fields": ["name"],
        "optional_fields": ["description"],
        "enums": {},
        "examples": [
            {
                "description": "Create a new project.",
                "call": {
                    "operation": "create_project",
                    "name": "iot-edge-llm",
                    "description": "Edge-deployed LLM latency study.",
                },
            },
        ],
        "related_operations": ["list_projects", "status"],
        "notes": "UNSCOPED — does not require project_id.",
    },
    "reset_session": {
        "operation": "reset_session",
        "tool": "rka_execute",
        "category": "session",
        "role_tag": "ANY",
        "summary": "Reset the in-process session tracker (UNSCOPED).",
        "signature": "rka_execute(operation='reset_session')",
        "required_fields": [],
        "optional_fields": [],
        "enums": {},
        "examples": [
            {
                "description": "Reset session tracking.",
                "call": {"operation": "reset_session"},
            },
        ],
        "related_operations": ["status"],
        "notes": "UNSCOPED.",
    },
    "session_digest": {
        "operation": "session_digest",
        "tool": "rka_execute",
        "category": "session",
        "role_tag": "ANY",
        "summary": "Compact session summary (mutates session state).",
        "signature": ("rka_execute(operation='session_digest', *, project_id)"),
        "required_fields": ["project_id"],
        "optional_fields": [],
        "enums": {},
        "examples": [
            {
                "description": "Fetch a session digest.",
                "call": {
                    "operation": "session_digest",
                    "project_id": "prj_01ABC...",
                },
            },
        ],
        "related_operations": ["reset_session", "status"],
        "notes": None,
    },
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def get_operation_schema(operation: str) -> dict[str, Any] | None:
    """Return the schema dict for ``operation`` or None if unknown."""
    return OPERATIONS_SCHEMA.get(operation)


def list_operations_grouped() -> dict[str, list[dict[str, Any]]]:
    """Return operations grouped by tool (rka_query / rka_execute), then category.

    Each leaf entry: ``{operation, category, summary, role_tag, required_fields}``.
    Used by ``rka_describe()`` with no argument to produce the operations index.
    """
    out: dict[str, dict[str, list[dict[str, Any]]]] = {
        "rka_query": {},
        "rka_execute": {},
    }
    for op, entry in OPERATIONS_SCHEMA.items():
        bucket = out.setdefault(entry["tool"], {})
        cat_list = bucket.setdefault(entry["category"], [])
        cat_list.append(
            {
                "operation": op,
                "category": entry["category"],
                "summary": entry["summary"],
                "role_tag": entry["role_tag"],
                "required_fields": entry["required_fields"],
            }
        )
    # Sort within each category
    for tool_bucket in out.values():
        for cat_list in tool_bucket.values():
            cat_list.sort(key=lambda d: d["operation"])
    return out


# ---------------------------------------------------------------------------
# Maturity — which operations belong in an agent's default choice space
# ---------------------------------------------------------------------------
# Measured 2026-08-23 against a five-month-old production store holding 5178
# entities: these subsystems had ZERO rows. They are not broken and may be
# deliberately ahead of use, but listing them alongside the core operations
# means ~41% of what an agent reads while choosing is unreachable in practice.
# Derived from category (plus a few named operations) rather than stamped on
# all entries, so the rule stays auditable and one edit re-classifies a
# subsystem once it starts carrying data.
PREVIEW_CATEGORIES: frozenset[str] = frozenset({
    "manuscript",           # 3 manuscripts, 0 units / 0 claims / 0 bindings
    "manuscript_planning",  # 0 branches
    "experiments",          # 0 experiments
    "semantic_patches",     # 0 proposals
    "hooks",                # 0 hooks
})

# Preview operations inside an otherwise-stable category.
PREVIEW_OPERATIONS: frozenset[str] = frozenset({
    # interpretation-candidate pipeline: 0 candidates in production
    "interpretation_candidates",
    "create_interpretation_candidate",
    "triage_interpretation_candidate",
    "add_interpretation_hint",
    # claim-scope contracts: 0 scope versions in production
    "claim_scope",
    "set_claim_scope",
    # v2.4.0 removed the LLM path; this is a stub until it is re-wired
    "generate_summary",
})


# Explicit operation deprecations are a public compatibility signal, not a
# proxy for production usage. Keep them separate from ``operation_maturity``:
# one operation can be both usage-preview and deprecated.
#
# E2.4 freezes the still-present Writer/Workbench branches as compatibility
# adapters. They remain callable so existing projects can read, audit, and
# migrate legacy state, but new authoring work belongs in the standalone Writer
# project. Derive the set from the ownership categories so a future operation
# cannot accidentally look like a supported Core feature merely because a
# developer forgot to add it to another hand-maintained list. The exact current
# membership is locked in tests.
WRITER_COMPATIBILITY_CATEGORIES: frozenset[str] = frozenset({
    "manuscript",
    "manuscript_planning",
    "semantic_patches",
})
WRITER_COMPATIBILITY_OPERATIONS: frozenset[str] = frozenset(
    op_name
    for op_name, entry in OPERATIONS_SCHEMA.items()
    if entry.get("category") in WRITER_COMPATIBILITY_CATEGORIES
    or op_name == "reference_validation_status"
)


def _writer_compatibility_deprecation() -> dict[str, Any]:
    """Return fresh metadata for one frozen Writer compatibility operation."""

    return {
        "status": "deprecated_compatibility",
        "reason": (
            "This manuscript/Workbench operation is retained only for legacy "
            "RKA Core compatibility, audit, and migration. New authoring "
            "development belongs in the standalone RKA Writer project."
        ),
        "owner": "rka-writer",
        "migration_target": "https://github.com/rka-project/rka-writer",
        "migration_issue": "https://github.com/rka-project/rka-core/issues/129",
        "compatibility": "behavior_preserved",
        "removal_milestone": "E5",
        "removal_version": "not_scheduled",
    }


DEPRECATED_OPERATIONS: dict[str, dict[str, Any]] = {
    op_name: _writer_compatibility_deprecation()
    for op_name in WRITER_COMPATIBILITY_OPERATIONS
}
DEPRECATED_OPERATIONS["upsert_argument_spine"].update(
    {
        "reason": (
            "Agent-direct argument-spine replacement is disabled. Existing "
            "Core callers may use the attributed semantic-proposal transition "
            "during the compatibility window; new authoring development "
            "belongs in the standalone RKA Writer project."
        ),
        "replacement_operations": [
            "prepare_semantic_patch_context",
            "create_semantic_patch_proposal",
            "apply_semantic_patch_proposal",
        ],
    }
)


def operation_maturity(op_name: str) -> str:
    """``'stable'`` or ``'preview'`` for one operation."""
    entry = OPERATIONS_SCHEMA.get(op_name)
    if entry is None:
        return "unknown"
    if op_name in PREVIEW_OPERATIONS or entry.get("category") in PREVIEW_CATEGORIES:
        return "preview"
    return "stable"


def operation_deprecation(op_name: str) -> dict[str, Any] | None:
    """Return explicit compatibility guidance for a deprecated operation."""

    return DEPRECATED_OPERATIONS.get(op_name)


def operation_contract_disposition(op_name: str) -> str:
    """Return product ownership independently of usage maturity."""

    if op_name not in OPERATIONS_SCHEMA:
        return "unknown"
    return mcp_operation_disposition(
        op_name,
        writer_operations=WRITER_COMPATIBILITY_OPERATIONS,
    )


def suggest_operations(query: str, *, top_n: int = 5) -> list[str]:
    """Return up to ``top_n`` best fuzzy matches for an unknown operation."""
    if not query:
        return []
    return difflib.get_close_matches(
        query.lower().strip(),
        list(OPERATIONS_SCHEMA),
        n=top_n,
        cutoff=0.4,
    )


def list_operations_compact(*, include_preview: bool = False) -> dict[str, list[str]]:
    """v2.7.0 NO-COMPROMISE compromise-#3 mitigation.

    Returns a flat ``{tool: [op_name, ...]}`` map for the
    ``rka_describe('')`` browse mode. Strips summaries, required_fields,
    enums, examples, related_operations from the response — those
    surfaces are now visible directly in the Pydantic-derived
    ``inputSchema`` of ``rka_query`` / ``rka_execute`` (v2.7.0
    discriminated union), so the browse-mode index can collapse to
    <250 tokens.

    Callers wanting per-operation summary + examples pass the
    operation name explicitly: ``rka_describe('record_decision')``.
    """
    out: dict[str, list[str]] = {"rka_query": [], "rka_execute": []}
    for op_name, entry in OPERATIONS_SCHEMA.items():
        if not include_preview and (
            operation_maturity(op_name) == "preview"
            or operation_contract_disposition(op_name) != CORE
        ):
            continue
        tool = entry["tool"]
        out.setdefault(tool, []).append(op_name)
    for ops_list in out.values():
        ops_list.sort()
    return out


async def dispatch_describe(
    operation: str | None, *, include_preview: bool = False
) -> str:
    """Render the rka_describe response as a JSON string.

    Behavior:
      - operation in OPERATIONS_SCHEMA -> return full schema as indented JSON.
      - operation is None or empty -> return the compact operations index
        (v2.7.0 NO-COMPROMISE compromise-#3 mitigation: stripped to
        operation name + ≤12-word summary, grouped by tool; target
        <250 tokens total). Full per-op schema is reachable via
        ``rka_describe('<op_name>')`` or via the Pydantic-derived
        inputSchema FastMCP renders for ``rka_query`` / ``rka_execute``.
      - operation is unknown -> return ``{error, operation, did_you_mean}``.
    """
    if not operation:
        # NO-COMPROMISE: shrink to under ~250 tokens by joining names
        # into a single comma-separated string per tool. Full per-op
        # schema is reachable via the FastMCP-rendered inputSchema or
        # via `rka_describe('<op_name>')`.
        compact = list_operations_compact(include_preview=include_preview)
        listed = sum(len(v) for v in compact.values())
        payload: dict[str, Any] = {
            "rka_query": ", ".join(compact.get("rka_query", [])),
            "rka_execute": ", ".join(compact.get("rka_execute", [])),
            "listed": listed,
            "total": len(OPERATIONS_SCHEMA),
            # Preserve the pre-E2.4 discovery field for existing callers.
            # Deprecated names are separate from the stable tool indexes.
            "deprecated_operations": sorted(DEPRECATED_OPERATIONS),
            "unsupported_operations": sorted(
                op_name
                for op_name in OPERATIONS_SCHEMA
                if operation_contract_disposition(op_name) == AGENTIC_UNSUPPORTED
            ),
            "legacy_operations": sorted(
                op_name
                for op_name in OPERATIONS_SCHEMA
                if operation_contract_disposition(op_name) == CORE_LEGACY
            ),
            "hint": (
                "rka_describe('<op>') for schema; rka_query/_execute "
                "inputSchema already carries per-branch enums."
            ),
        }
        if not include_preview:
            preview_hidden = sum(
                operation_maturity(op_name) == "preview"
                and operation_contract_disposition(op_name) == CORE
                for op_name in OPERATIONS_SCHEMA
            )
            payload["preview_hidden"] = preview_hidden
            payload["preview_hint"] = (
                f"{preview_hidden} usage-preview operations are omitted "
                "(experiments, hooks, interpretation staging, and claim "
                "scope). Pass include_preview=True to see them."
            )
            payload["deprecated_hidden"] = len(DEPRECATED_OPERATIONS)
            payload["deprecated_hint"] = (
                f"{len(DEPRECATED_OPERATIONS)} frozen Writer compatibility "
                "operations are omitted from the stable tool indexes and "
                "listed separately. Use exact describe for migration guidance."
            )
            payload["unsupported_hidden"] = len(payload["unsupported_operations"])
            payload["unsupported_hint"] = (
                "Shelved Agentic operations are omitted from the stable Core "
                "index; exact describe remains available for compatibility."
            )
            payload["legacy_hidden"] = len(payload["legacy_operations"])
            payload["legacy_hint"] = "Core-legacy operations are omitted from rka-mcp/v1."
        return json.dumps(payload, indent=2)

    op = operation.strip()
    entry = OPERATIONS_SCHEMA.get(op)
    if entry is None:
        return json.dumps(
            {
                "error": "unknown_operation",
                "operation": operation,
                "did_you_mean": suggest_operations(op),
                "hint": (
                    "Pass operation='' (empty string) to list every known "
                    "operation grouped by tool and category."
                ),
            },
            indent=2,
        )

    deprecation = operation_deprecation(op)
    disposition = operation_contract_disposition(op)
    payload = {
        **entry,
        "maturity": operation_maturity(op),
        "deprecated": deprecation is not None,
    }
    if disposition != CORE:
        payload["contract_disposition"] = disposition
    if deprecation is not None:
        payload["deprecation"] = deprecation
    return json.dumps(payload, indent=2)


__all__ = [
    "OPERATIONS_SCHEMA",
    "DEPRECATED_OPERATIONS",
    "PREVIEW_CATEGORIES",
    "PREVIEW_OPERATIONS",
    "WRITER_COMPATIBILITY_CATEGORIES",
    "WRITER_COMPATIBILITY_OPERATIONS",
    "dispatch_describe",
    "operation_maturity",
    "operation_contract_disposition",
    "operation_deprecation",
    "get_operation_schema",
    "list_operations_grouped",
    "list_operations_compact",
    "suggest_operations",
]
