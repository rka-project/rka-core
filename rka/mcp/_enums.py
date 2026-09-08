"""Shared Literal enum type aliases for the RKA MCP surface.

Single source of truth for RKA enum surfaces. Promotes
``Annotated[Literal[...]]`` enum sets from ``rka/mcp/server.py`` module-level
aliases into a shared module so v2.7.0 verbs and any future v2.6.x patches
consume the same source. The orchestrator-side mirror at
``orchestrator/orchestrator/rka_enums.py`` MUST stay in sync — bookkeeper
invariant forbids the orchestrator from importing from ``rka``.

Each name here is a **plain** ``typing.Literal[...]`` alias — NOT
``Annotated``. Wrappers like
``Annotated[ConfidenceLit, Field(description="...")]`` live at the call
site (in ``server.py`` verbs) per FastMCP norms; the description belongs
with the parameter, not with the type.

Source of truth (reconcile drift against these files):
  - ``rka/db/schema.sql`` (SQLite CHECK constraints — migrations
    009 + 015 + 019 cover journal / decision / checkpoint columns)
  - ``rka/models/journal.py`` (JournalEntry — accepts the schema's CHECK
    set plus a legacy-normalized superset that the server silently maps
    via ``JOURNAL_TYPE_MAP`` for back-compat)
  - ``rka/models/decision.py`` (Decision — kind / status / decided_by)
  - ``rka/models/checkpoint.py`` (Checkpoint — type / resolved_by)
  - ``rka/models/literature.py`` (Literature — status / added_by)
  - ``rka/models/claim.py`` (Claim / EvidenceCluster — claim_type /
    cluster confidence)
  - ``rka/models/calibration.py`` (Outcome on rka_record_outcome)
  - ``rka/api/missions.py`` (mission lifecycle status enum)
  - ``rka/services/researcher_tools.py`` (RQ lifecycle, staleness
    levels, gate verdicts — string sets validated at the service
    layer, mirrored here for FastMCP enum rendering)

If you bump RKA's schema or Pydantic enums, search this file for the
affected Literal and update both. The orchestrator-side mirror at
``orchestrator/orchestrator/rka_enums.py`` is independently maintained
under the bookkeeper invariant — coordinate updates manually.
"""

from __future__ import annotations

from typing import Literal

from rka.models.journal import JournalCaptureMode

JournalCaptureModeLit = JournalCaptureMode


# Safe external-source registration and explicit interpretation admission.
RegisteredSourceKindLit = Literal[
    "file", "pasted_text", "url", "repository", "zotero"
]
RegisteredSourceOwnershipLit = Literal[
    "researcher", "institution", "third_party", "public_domain", "unknown"
]
RegisteredSourceActorLit = Literal[
    "pi", "brain", "executor", "web_ui", "llm", "import", "system"
]
SourceAdmissionTargetLit = Literal["journal", "claim", "decision"]
SourceAdmissionActorLit = Literal["pi", "brain", "executor", "web_ui"]


# ---------------------------------------------------------------------------
# Journal entries (rka_record_note / rka_add_note / rka_update_note)
# ---------------------------------------------------------------------------

# Confidence on journal entries — see ``rka/db/schema.sql`` CHECK on
# ``journal.confidence`` and ``rka/models/journal.py``. Run-5's empirical
# Brain hallucination was ``'confirmed'`` — NOT in this set.
ConfidenceLit = Literal[
    "hypothesis",
    "tested",
    "verified",
    "superseded",
    "retracted",
]

# Importance on journal entries — ``rka/db/schema.sql`` CHECK on
# ``journal.importance``.
ImportanceLit = Literal[
    "critical",
    "high",
    "normal",
    "low",
    "archived",
]

# Actor-of-record across journal / decision / literature writes —
# ``rka/db/schema.sql`` CHECK on ``journal.source`` (note: ``import`` is
# not in the journal-source set; the document-ingestion path widens it
# via ``IngestSourceLit`` below).
SourceLit = Literal["brain", "executor", "pi", "web_ui", "llm"]
JournalActorLit = Literal["brain", "executor", "pi", "llm", "web_ui", "system"]

# Journal type — v2 canonical set plus legacy-accepted superset (silently
# normalized via ``JOURNAL_TYPE_MAP``). See ``rka/models/journal.py``
# ``AnyJournalType``.
NoteTypeLit = Literal[
    # v2 canonical
    "note",
    "log",
    "directive",
    # Legacy (accepted; normalized to one of the v2 set above)
    "finding",
    "insight",
    "pi_instruction",
    "exploration",
    "idea",
    "observation",
    "hypothesis",
    "methodology",
    "summary",
]


# ---------------------------------------------------------------------------
# Decisions (rka_record_decision / rka_add_decision / rka_update_decision)
# ---------------------------------------------------------------------------

# Who decided this — ``rka/db/schema.sql`` CHECK on ``decisions.decided_by``.
# PI for ratified, Brain for proposed; Executor for self-reported
# operational decisions.
DecidedByLit = Literal["pi", "brain", "executor"]

# Decision kind — ``rka/db/schema.sql`` and ``rka/models/decision.py``.
# ``research_question`` is reserved for advanceable RQs (see
# ``rka_advance_rq``); most decisions are ``decision`` or ``design_choice``.
DecisionKindLit = Literal[
    "research_question",
    "design_choice",
    "decision",
    "operational",
]

# Decision lifecycle status — ``rka/db/schema.sql`` CHECK on
# ``decisions.status`` (line 41) AND ``rka/models/decision.py``
# ``DecisionCreate.status`` / ``DecisionUpdate.status``. Canonical set
# is {active, abandoned, superseded, merged, revisit}; bare ``str`` on
# the typed-args surface lets Brain hallucinate values that fall the DB
# CHECK constraint at INSERT time (HTTP 500). Phase-X²' polish promotes
# this to a Literal alias so the typed-args layer rejects pre-dispatch.
DecisionStatusLit = Literal[
    "active",
    "abandoned",
    "superseded",
    "merged",
    "revisit",
]


# ---------------------------------------------------------------------------
# Literature (rka_record_literature / rka_add_literature / rka_update_literature)
# ---------------------------------------------------------------------------

# Reading lifecycle on a literature entry — ``rka/db/schema.sql`` CHECK
# on ``literature.status``.
LitStatusLit = Literal[
    "to_read",
    "reading",
    "read",
    "cited",
    "excluded",
]


# ---------------------------------------------------------------------------
# Missions (rka_mission)
# ---------------------------------------------------------------------------

# Mission lifecycle status — ``rka/db/schema.sql`` CHECK on
# ``missions.status``.
MissionStatusLit = Literal[
    "pending",
    "active",
    "complete",
    "partial",
    "blocked",
    "cancelled",
]


# ---------------------------------------------------------------------------
# Checkpoints + Gates (rka_checkpoint)
# ---------------------------------------------------------------------------

# Checkpoint type — ``rka/db/schema.sql`` CHECK on ``checkpoints.type``
# (note: schema lists 'decision', 'clarification', 'inspection'; the
# Pydantic ``CheckpointCreate`` in ``rka/models/checkpoint.py`` widens
# to include ``'gate'`` for the validation-gate subtype). The v2.7.0
# verb surface accepts the Pydantic-level set.
ChkTypeLit = Literal[
    "decision",
    "clarification",
    "inspection",
    "gate",
]

# Checkpoint resolver — ``rka/db/schema.sql`` CHECK on
# ``checkpoints.resolved_by`` (schema line 156 + migration 015 line 15)
# AND ``rka/models/checkpoint.py`` ``CheckpointResolve.resolved_by`` —
# narrower than ``DecidedByLit`` (which includes 'executor'). DB rejects
# 'executor' at INSERT time. Phase-X²' polish: separate alias so the
# typed-args surface rejects ``resolved_by='executor'`` pre-dispatch.
CheckpointResolvedByLit = Literal["pi", "brain"]

# Journal entry lifecycle status — ``rka/db/schema.sql`` migration 009
# line 24 CHECK on ``journal.status`` AND ``rka/models/journal.py``
# ``JournalEntryCreate.status`` / ``JournalEntryUpdate.status``.
JournalStatusLit = Literal[
    "draft",
    "active",
    "superseded",
    "retracted",
]

# Entity aliases accepted by the compatibility-safe bulk-update adapter.
# ``note`` is the historical MCP spelling; ``journal`` is the canonical
# entity name used by the typed/query surfaces. Both route to /api/notes.
BulkEntityTypeLit = Literal[
    "note",
    "journal",
    "decision",
    "literature",
]

# Validation-gate subtype — string-set validated in
# ``rka/services/researcher_tools.py`` and the legacy
# ``rka_create_gate`` MCP tool.
GateTypeLit = Literal[
    "problem_framing",
    "plan_validation",
    "evidence_review",
    "synthesis_validation",
]

# Gate verdict — string-set validated in
# ``rka/services/researcher_tools.py`` and the legacy
# ``rka_evaluate_gate`` MCP tool.
VerdictLit = Literal["go", "kill", "hold", "recycle"]


# ---------------------------------------------------------------------------
# Claims + Clusters (rka_review on claim / cluster targets)
# ---------------------------------------------------------------------------

# Claim type — ``rka/models/claim.py`` ``ClaimType``.
ClaimTypeLit = Literal[
    "hypothesis",
    "evidence",
    "method",
    "result",
    "observation",
    "assumption",
]

# Canonical claim-scope contracts (rka/models/claim.py). These are research-
# level applicability bounds on clm_ claims, distinct from source-bounded
# interpretation scope and manuscript-specific wording boundaries.
ClaimScopeUncertaintyLit = Literal["none", "low", "medium", "high", "unknown"]
ClaimScopeExtensionPolicyLit = Literal["exact_only", "bounded"]
ClaimFalsifierStatusLit = Literal["unknown", "applicable", "not_applicable"]
ClaimScopeReviewStatusLit = Literal["draft", "reviewed"]
ClaimScopeReadinessLit = Literal[
    "missing",
    "stale",
    "incomplete",
    "needs_review",
    "ready",
]
ClaimScopeActorLit = Literal["pi", "brain", "executor", "web_ui", "llm"]
ClaimConditionKindLit = Literal[
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
]
ClaimConditionOperatorLit = Literal[
    "equals",
    "one_of",
    "range",
    "at_least",
    "at_most",
    "present",
    "absent",
    "described_by",
]

# Interpretation Staging (rka/models/interpretation.py).
InterpretationSourceLit = Literal[
    "journal", "literature", "artifact", "experiment_observation"
]
InterpretationLocatorLit = Literal[
    "text_offset",
    "page",
    "line_range",
    "section",
    "url_fragment",
    "record",
]
EpistemicKindLit = Literal[
    "observation",
    "reported_fact",
    "inference",
    "hypothesis",
    "plan",
    "author_intent",
]
InterpretationUncertaintyLit = Literal["none", "low", "medium", "high", "unknown"]
InterpretationActorLit = Literal["pi", "brain", "executor", "web_ui", "llm", "import"]
InterpretationReviewActorLit = Literal["pi", "brain", "executor", "web_ui"]
InterpretationReviewStatusLit = Literal["pending", "in_review", "resolved"]
InterpretationDispositionLit = Literal[
    "promoted",
    "merged",
    "deferred",
    "rejected",
    "classified_decision",
    "classified_plan",
    "classified_author_intent",
    "evidence_mission_requested",
    "classified_evidence",
]
InterpretationHintKindLit = Literal["duplicate", "conflict"]
InterpretationTriageActionLit = Literal[
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
]

# Experiment evidence substrate (rka/models/experiment.py).
ExperimentActorLit = Literal["pi", "brain", "executor", "web_ui", "llm", "import"]
ExperimentStatusLit = Literal["planned", "active", "completed", "abandoned"]
WorkingTreeStateLit = Literal["clean", "dirty", "unknown"]
ExperimentRunStatusLit = Literal[
    "queued", "running", "succeeded", "failed", "cancelled"
]
ExperimentRunActionLit = Literal["start", "succeed", "fail", "cancel"]
ExperimentRunKindLit = Literal["local", "docker", "cluster", "manual", "import"]
ExperimentObservationKindLit = Literal[
    "metric", "comparison", "test", "qualitative", "failure", "artifact"
]
ExperimentObservationDirectionLit = Literal[
    "positive", "negative", "inconclusive", "neutral", "error"
]
EvidenceSourceKindLit = Literal["artifact", "repository"]
EvidenceLocatorKindLit = Literal[
    "whole_artifact",
    "page",
    "line_range",
    "table",
    "table_cell",
    "json_pointer",
    "notebook_cell",
    "record",
]
ClaimEvidenceRoleLit = Literal["support", "qualifier", "counterevidence", "context"]

# Provisional manuscript-planning branches (rka/models/planning.py).
PlanningActorLit = Literal["pi", "brain", "executor", "web_ui", "llm", "import"]
PIConfirmationLit = Literal["pi"]
PlanningBranchStateLit = Literal["active", "selected", "archived", "superseded"]
PlanningStageLit = Literal[
    "seed",
    "paragraph_spine",
    "problem_scope",
    "landscape_gap",
    "response_mechanism",
    "challenge_innovation",
    "rq_contribution",
    "evaluation",
    "outline",
    "review",
]
PlanningLifecycleLit = Literal[
    "candidate", "reviewed", "selected", "parked", "superseded", "archived"
]
PlanningOriginLit = Literal["user", "ai_suggested", "imported", "user_revised"]
PlanningReadinessLit = Literal["blocked", "in_progress", "ready"]
PlanningPromotionTargetLit = Literal[
    "manuscript", "manuscript_claim", "manuscript_unit", "experiment", "decision"
]

# Scientific evidence assessment on a claim. This is intentionally
# independent from ``claims.verified``, which records extraction/grounding
# fidelity against the source entry.
EvidenceStatusLit = Literal[
    "unassessed",
    "supported",
    "partially_supported",
    "inconclusive",
    "contradicted",
]

# Cluster confidence — ``rka/models/claim.py`` ``ClusterConfidence``.
ClusterConfLit = Literal[
    "strong",
    "moderate",
    "emerging",
    "contested",
    "refuted",
]


# ---------------------------------------------------------------------------
# Research questions (rka_mission action='advance_rq')
# ---------------------------------------------------------------------------

# RQ lifecycle status — string-set validated in
# ``rka/services/researcher_tools.py::advance_rq``. Stored as ``rq:*`` tag
# on the underlying decision row (the ``decisions.status`` column owns
# the decision-level lifecycle and is a different CHECK set).
RQStatusLit = Literal[
    "open",
    "partially_answered",
    "answered",
    "reframed",
    "closed",
]


# ---------------------------------------------------------------------------
# Calibration outcomes (rka_review target='outcome')
# ---------------------------------------------------------------------------

# Outcome on a calibration record — ``rka/models/calibration.py``
# ``Outcome`` (matches the ``calibration_outcomes`` CHECK constraint).
OutcomeLit = Literal["succeeded", "failed", "mixed", "unresolved"]

# Actor recording a calibration outcome — ``rka/models/calibration.py``
# ``CalibrationOutcomeCreate.recorded_by`` is currently bare ``str`` with
# no DB CHECK (calibration_outcomes schema has no constraint). Phase-X²'
# polish constrains the typed-args surface to the canonical actor set
# (matches the wider CLAUDE.md actor vocabulary minus the rare ones).
RecordedByLit = Literal["pi", "brain", "executor", "system"]


# ---------------------------------------------------------------------------
# Staleness (rka_review target='stale')
# ---------------------------------------------------------------------------

# Staleness severity — string-set validated in
# ``rka/services/researcher_tools.py::flag_stale``. Stored on
# ``claims.staleness`` / ``evidence_clusters.staleness`` columns.
StalenessLit = Literal["yellow", "red"]
StalenessVerdictLit = Literal["current", "historical", "retired", "superseded", "retracted", "dismissed"]


# ---------------------------------------------------------------------------
# Document ingestion (rka_ingest_document)
# ---------------------------------------------------------------------------

# Actor-of-record for the ingested document. Widens ``SourceLit`` with
# ``'import'`` for batch-import workflows (mirrors
# ``rka/db/schema.sql`` ``literature.added_by`` CHECK set).
IngestSourceLit = Literal["brain", "executor", "pi", "import", "web_ui"]


# ---------------------------------------------------------------------------
# Claim review (rka_execute operation='review_claims')
# ---------------------------------------------------------------------------

# Action verb for the review-claims operation. String-set validated in
# ``rka/services/researcher_tools.py::review_claims``. Promoted from the
# inlined ``OPERATIONS_SCHEMA['review_claims']['enums']['review_action']``
# in v2.7.0 so the typed-args module + orchestrator-side mirror have a
# single source of truth.
ReviewActionLit = Literal["approve", "reject", "adjust"]


# ---------------------------------------------------------------------------
# Batch-import actor (rka_execute operation='batch_import')
# ---------------------------------------------------------------------------

# Actor-of-record for the bulk-import endpoint. Widens ``SourceLit`` with
# ``'system'`` + ``'import'`` per the legacy ``rka_batch_import`` tool;
# CLAUDE.md notes that ``'import'`` is auto-normalized to ``'system'`` at
# the service layer.
BatchImportActorLit = Literal["brain", "executor", "pi", "system", "import"]


# ---------------------------------------------------------------------------
# Hook creator (rka_execute operation='hook_add')
# ---------------------------------------------------------------------------

# Actor-of-record for hook creation. Matches the legacy
# ``rka_add_hook(created_by=...)`` set: pi (the typical operator),
# brain, executor (rare), system (programmatic).
HookCreatedByLit = Literal["pi", "brain", "executor", "system"]


# ---------------------------------------------------------------------------
# Native manuscripts (rka_query / rka_execute manuscript operations)
# ---------------------------------------------------------------------------

# Canonical lifecycle phases understood by the native manuscript readiness
# engine.  Metadata-only create/update calls may still carry a custom phase
# string, but readiness checks and phase transitions are deliberately closed
# to this set.
ManuscriptReadinessPhaseLit = Literal[
    "planning",
    "drafting",
    "review",
    "final",
    "submitted",
]

# Native manuscript creation is intentionally narrower than later lifecycle
# transitions. Named singleton aliases keep that contract visible in the
# published typed surface and covered by the enum-drift audit.
ManuscriptInitialPhaseLit = Literal["planning"]

# Native manuscript lifecycle state — mirrors
# ``rka.models.manuscript_native.ManuscriptState``.
ManuscriptStateLit = Literal[
    "active",
    "on_hold",
    "submitted",
    "accepted",
    "rejected",
    "withdrawn",
    "archived",
]
ManuscriptInitialStateLit = Literal["active"]

# Argument-spine claim and unit enums.
ManuscriptClaimKindLit = Literal[
    "empirical",
    "methodological",
    "theoretical",
    "survey",
    "position",
]
ManuscriptClaimStateLit = Literal["candidate", "active", "retired"]
ManuscriptUnitKindLit = Literal[
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
]
ManuscriptUnitStatusLit = Literal[
    "planned",
    "drafted",
    "reviewed",
    "final",
    "removed",
]
ManuscriptUnitRoleLit = Literal[
    "unspecified",
    "section",
    "argument_block",
    "paragraph_plan",
    "result",
    "caption",
    "appendix",
    "other",
]
ManuscriptRhetoricalMoveLit = Literal[
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
]
ManuscriptOutlineActionLit = Literal["edit", "expand", "condense", "reorder"]
ManuscriptEvidenceRoleLit = Literal[
    "support",
    "qualifier",
    "counterevidence",
]
ManuscriptClaimUnitRelationshipLit = Literal[
    "advances",
    "tests",
    "bounds",
    "mentions",
]
ManuscriptCitationRoleLit = Literal[
    "imports",
    "bounds",
    "baseline",
    "extends",
    "refutes",
]
ManuscriptCitationVerificationStateLit = Literal[
    "unverified",
    "self_attested",
    "verified",
    "rejected",
]

# PI checkpoint lifecycle.
ManuscriptCheckpointKindLit = Literal[
    "venue",
    "outline",
    "table_figure_plan",
    "reference_set",
    "draft_section",
    "final_layout",
]
ManuscriptCheckpointResolutionStatusLit = Literal[
    "resolved",
    "rejected",
]

# Immutable multidimensional verification attestation verdicts.
ManuscriptVerificationVerdictLit = Literal["pass", "warn", "block", "error"]
ManuscriptVerificationDimensionVerdictLit = Literal[
    "pass",
    "warn",
    "block",
    "error",
    "not_checked",
]

# Unified manuscript-workbench proposal lifecycle.
SemanticPatchOriginLit = Literal["human", "host_agent", "lm_studio"]
SemanticPatchAIOriginLit = Literal["host_agent", "lm_studio"]
SemanticPatchBoundaryLit = Literal["none", "host_conversation", "local_loopback"]
SemanticPatchAIBoundaryLit = Literal["host_conversation", "local_loopback"]
SemanticPatchActorLit = Literal["pi", "brain", "executor", "web_ui"]
SemanticPatchReviewerLit = Literal["pi", "web_ui"]
SemanticPatchStatusLit = Literal[
    "proposed", "applied", "rejected", "conflicted", "superseded", "expired"
]
