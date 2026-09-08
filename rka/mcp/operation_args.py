"""v2.7.0 — Pydantic discriminated-union argument models for rka_query / rka_execute.

This module is the **typed surface** for the v2.7.0 always-on verbs. Each
operation gets its own Pydantic ``Args`` model that:

1. **Locks enums at the JSON Schema layer.** Every per-operation enum field
   uses an ``Annotated[Literal[...]]`` alias imported from ``rka.mcp._enums``.
   FastMCP renders the model as ``oneOf`` with per-branch ``enum: [...]``
   arrays — the LLM cannot emit a bad enum value (the tool surface rejects it
   pre-dispatch).
2. **Locks required fields per operation.** Each branch's required-fields
   array is explicit; the LLM-emission-time gap that Run-5 exploited is
   closed. Canonical/legacy aliases such as checkpoint ``description`` and
   ``content`` are handled by explicit cross-field validators.
3. **Locks cross-field invariants** via ``model_validator(mode='after')``
   for the empirically-observed Brain hallucination + omission classes
   (``related_journal=[]``, ``source='pi'`` without ``verbatim_input``,
   missing ``motivated_by_decision``).

Naming convention: ``{operation_pascal_case}Args``. For query ops whose verb
name collides with a write sibling (``mission``, ``manuscript``), the model
is prefixed ``Query...`` to disambiguate (``QueryMissionArgs`` vs
``CreateMissionArgs``).

Discriminator: each model declares
``operation: Literal['<op>'] = '<op>'`` — FastMCP keys the discriminated
union by this field.

Configuration: every model sets
``model_config = ConfigDict(extra='forbid', use_enum_values=True)`` so
unknown kwargs are rejected at the schema layer (closes the silent-typo
class identified in the v2.7.0 pre-mortem).

Source of truth (drift-detection):
  - ``rka/mcp/operations_schema.py`` ``OPERATIONS_SCHEMA`` — canonical
    required/optional/enum lists per operation.
  - ``rka/mcp/_enums.py`` — module-level Literal aliases.
  - ``rka/models/*.py`` — service-layer Pydantic shapes.

The lock-tests in ``tests/test_mcp/test_v270_typed_args_lock.py`` pin each
model's field set against ``OPERATIONS_SCHEMA`` and assert each Literal in
each model is ``is`` the imported alias from ``_enums.py``.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator
from rka.models.claim import ClaimScopeCondition
from rka.models.manuscript_native import ManuscriptReferenceMemberInput
from rka.models.planning import (
    PlanningArtifactVersionAppend,
    PlanningBranchCreate,
    PlanningBranchTransition,
    PlanningContributionProposalPrepare,
    PlanningContributionRatification,
    PlanningEvaluationMissionCreate,
    PlanningEvaluationResultProposalPrepare,
    PlanningResearchQuestionPromotion,
)
from rka.models.semantic_patch import (
    ContextManifestCreate,
    LMStudioProposalRequest,
    SemanticPatchProposalCreate,
    SemanticPatchProposalTransition,
)
from rka.models.outline import OutlineProposalRequest
from rka.models.decision import DecisionUpdate
from rka.models.journal import JournalAttributionCorrection, JournalEntryCreate, JournalEntryUpdate
from rka.mcp._enums import JournalCaptureModeLit
from rka.models.literature import LiteratureUpdate

# NOTE: enum aliases from ``rka.mcp._enums`` are imported on a per-batch
# basis. Batch A (query ops) doesn't directly type any enum field — query
# enums live inside the ``filters: dict[str, Any]`` payload. Batches B/C/D
# (write ops) add per-field Literal aliases. The lock-tests in
# ``tests/test_mcp/test_v270_typed_args_lock.py`` audit that each
# Annotated[Literal[...]] in a write model uses the imported alias.

# Batch D imports — claim/cluster review + maintenance enums.
from rka.mcp._enums import (  # noqa: E402  (intentional post-docstring batch import)
    ClusterConfLit,
    ConfidenceLit,
    EvidenceStatusLit,
    ReviewActionLit,
    StalenessLit,
)

# Batch B imports — Record/Create write op enums. ConfidenceLit / ClusterConfLit
# are re-imported in their own block for batch-locality reading even though
# Batch D's import above already pulled them; Python's import machinery
# deduplicates so this is no cost. The lock-tests audit per-model that each
# Annotated[Literal[...]] uses the imported alias.
from rka.mcp._enums import (  # noqa: E402, F811
    ClaimFalsifierStatusLit,
    ClaimScopeActorLit,
    ClaimScopeExtensionPolicyLit,
    ClaimScopeReviewStatusLit,
    ClaimScopeUncertaintyLit,
    ClaimTypeLit,
    DecidedByLit,
    DecisionKindLit,
    GateTypeLit,
    ImportanceLit,
    IngestSourceLit,
    EpistemicKindLit,
    InterpretationActorLit,
    InterpretationHintKindLit,
    InterpretationLocatorLit,
    InterpretationReviewActorLit,
    InterpretationSourceLit,
    InterpretationTriageActionLit,
    InterpretationUncertaintyLit,
    ClaimEvidenceRoleLit,
    EvidenceLocatorKindLit,
    EvidenceSourceKindLit,
    ExperimentActorLit,
    ExperimentObservationDirectionLit,
    ExperimentObservationKindLit,
    ExperimentRunActionLit,
    ExperimentRunKindLit,
    ExperimentStatusLit,
    WorkingTreeStateLit,
    PlanningActorLit,
    PlanningBranchStateLit,
    PlanningLifecycleLit,
    PlanningOriginLit,
    PlanningReadinessLit,
    PlanningStageLit,
    LitStatusLit,
    NoteTypeLit,
    JournalActorLit,
    SourceLit,
    SemanticPatchAIOriginLit,
    SemanticPatchAIBoundaryLit,
    SemanticPatchActorLit,
    SemanticPatchReviewerLit,
    SemanticPatchStatusLit,
    RegisteredSourceActorLit,
    RegisteredSourceKindLit,
    RegisteredSourceOwnershipLit,
    SourceAdmissionActorLit,
    SourceAdmissionTargetLit,
)

# Batch C imports — Update/Lifecycle/Submit enums.
from rka.mcp._enums import (  # noqa: E402, F811
    BulkEntityTypeLit,
    CheckpointResolvedByLit,
    ChkTypeLit,
    DecisionStatusLit,
    JournalStatusLit,
    MissionStatusLit,
    OutcomeLit,
    RecordedByLit,
    RQStatusLit,
    VerdictLit,
)

# Batch B extras — actor aliases for batch_import + hook_add (promoted
# from inline Literals to public aliases in Phase 3 so the drift-test
# infra catches future divergence).
from rka.mcp._enums import (  # noqa: E402, F811
    BatchImportActorLit,
    HookCreatedByLit,
    ManuscriptCheckpointKindLit,
    ManuscriptCheckpointResolutionStatusLit,
    ManuscriptClaimKindLit,
    ManuscriptClaimStateLit,
    ManuscriptClaimUnitRelationshipLit,
    ManuscriptCitationRoleLit,
    ManuscriptCitationVerificationStateLit,
    ManuscriptEvidenceRoleLit,
    ManuscriptInitialPhaseLit,
    ManuscriptInitialStateLit,
    ManuscriptOutlineActionLit,
    ManuscriptReadinessPhaseLit,
    ManuscriptRhetoricalMoveLit,
    ManuscriptStateLit,
    ManuscriptUnitKindLit,
    ManuscriptUnitRoleLit,
    ManuscriptUnitStatusLit,
    ManuscriptVerificationDimensionVerdictLit,
    ManuscriptVerificationVerdictLit,
)

# ---------------------------------------------------------------------------
# Shared base classes
# ---------------------------------------------------------------------------


class ProjectScopedArgs(BaseModel):
    """Base for project-scoped operations (the vast majority).

    Subclasses inherit ``project_id: str`` and the strict ``ConfigDict``.
    """

    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    project_id: Annotated[
        str,
        Field(description="Project ID (prj_...). REQUIRED."),
    ]


class UnscopedArgs(BaseModel):
    """Base for operations that don't require a project_id.

    Used by ``list_projects``, ``capabilities``, ``health``, ``create_project``,
    and ``reset_session``.
    """

    model_config = ConfigDict(extra="forbid", use_enum_values=True)


# ---------------------------------------------------------------------------
# Reusable mixins (composed via multiple inheritance where appropriate)
# ---------------------------------------------------------------------------


class PaginatedFiltersMixin(BaseModel):
    """List-mode reads: optional ``limit`` + ``filters`` dict."""

    limit: Annotated[
        Optional[int],
        Field(default=None, description="Cap on result count."),
    ] = None
    filters: Annotated[
        Optional[dict[str, Any]],
        Field(default=None, description="Operation-specific filter dict."),
    ] = None


# =============================================================================
# Batch A — QUERY operations (56 models)
# =============================================================================

# ---------------------------------------------------------------------------
# Core read operations (status / context / search / entity)
# ---------------------------------------------------------------------------


class QueryStatusArgs(ProjectScopedArgs):
    """[ANY] Current phase, focus, blockers — the minimal session-start probe.

    Related: ``context``, ``pending_maintenance``, ``checkpoints``.
    """

    operation: Literal["status"] = "status"


class QueryContextArgs(ProjectScopedArgs):
    """[ANY] Load current project state + recent knowledge for a topic.

    Related: ``status``, ``summarize``, ``search``.
    """

    operation: Literal["context"] = "context"

    query: Annotated[
        Optional[str],
        Field(default=None, description="Optional topic to scope the context pull."),
    ] = None
    filters: Annotated[
        Optional[dict[str, Any]],
        Field(default=None, description="Optional filters (e.g. {'phase': str})."),
    ] = None
    # No `options`: it carried only `depth`, and once that was removed
    # nothing in dispatch_query read it. See test_context_surface_is_honest.


class QuerySearchArgs(ProjectScopedArgs):
    """[ANY] Full-text + semantic search across journal/decision/literature/clusters.

    Pass 2-4 keyword terms; longer queries reduce recall.
    """

    operation: Literal["search"] = "search"

    query: Annotated[
        str,
        Field(description="Search query (2-4 keyword terms recommended)."),
    ]
    limit: Annotated[
        Optional[int],
        Field(default=None, description="Cap on result count (default 20)."),
    ] = None
    filters: Annotated[
        Optional[dict[str, Any]],
        Field(
            default=None,
            description=("Optional filters (e.g. {'entity_types': ['decision', 'journal']})."),
        ),
    ] = None


class QueryEntityArgs(ProjectScopedArgs):
    """[ANY] Fetch a single entity by ID (jrn_/dec_/lit_/mis_/clm_/ecl_/chk_).

    Entity prefix is auto-routed.
    """

    operation: Literal["entity"] = "entity"

    id: Annotated[
        str,
        Field(description="Entity ID (jrn_/dec_/lit_/mis_/clm_/ecl_/chk_)."),
    ]


# ---------------------------------------------------------------------------
# List-mode reads (journal / literature / mission / report / checkpoints)
# ---------------------------------------------------------------------------


class QueryJournalArgs(ProjectScopedArgs, PaginatedFiltersMixin):
    """[ANY] List journal entries (notes, logs, directives).

    Filter keys: ``type``, ``phase``, ``confidence``, ``status``, ``since``,
    ``source``, ``tags``.
    """

    operation: Literal["journal"] = "journal"


class QueryLiteratureArgs(ProjectScopedArgs, PaginatedFiltersMixin):
    """[ANY] List literature entries.

    Filter keys: ``status``, ``tag``.
    """

    operation: Literal["literature"] = "literature"

    query: Annotated[
        Optional[str],
        Field(default=None, description="Optional free-text search over literature."),
    ] = None


class QueryMissionArgs(ProjectScopedArgs):
    """[ANY] Fetch a specific mission (by ID) or the current active/pending mission.

    Omit ``id`` to auto-locate the most recent active or pending mission.
    """

    operation: Literal["mission"] = "mission"

    id: Annotated[
        Optional[str],
        Field(
            default=None,
            description=(
                "Mission ID (mis_...). Omit to auto-locate the current active or pending mission."
            ),
        ),
    ] = None


class QueryReportArgs(ProjectScopedArgs):
    """[ANY] Fetch a mission's submitted report.

    ``id`` is the mission_id, NOT a report_id.
    """

    operation: Literal["report"] = "report"

    id: Annotated[
        str,
        Field(description="Mission ID (mis_...) whose report you want."),
    ]


class QueryCheckpointsArgs(ProjectScopedArgs):
    """[ANY] List checkpoints (default: open).

    Filter: ``{'status': 'open' | 'resolved' | 'all'}``.
    """

    operation: Literal["checkpoints"] = "checkpoints"

    filters: Annotated[
        Optional[dict[str, Any]],
        Field(
            default=None,
            description="Optional filters (e.g. {'status': 'open'}).",
        ),
    ] = None


# ---------------------------------------------------------------------------
# Decision / calibration / hooks / notifications
# ---------------------------------------------------------------------------


class QueryDecisionTreeArgs(ProjectScopedArgs):
    """[ANY] Render the decision tree, optionally rooted at a decision.

    Filter keys: ``phase``, ``active_only``.
    """

    operation: Literal["decision_tree"] = "decision_tree"

    id: Annotated[
        Optional[str],
        Field(
            default=None,
            description="Optional root decision ID; omit for full project tree.",
        ),
    ] = None
    filters: Annotated[
        Optional[dict[str, Any]],
        Field(
            default=None,
            description="Optional filters (e.g. {'active_only': True, 'phase': '...'}).",
        ),
    ] = None


class OrphanSupersedesArgs(ProjectScopedArgs):
    """[ANY] List decisions marked superseded with no pointer to a replacement."""

    operation: Literal["orphan_supersedes"] = "orphan_supersedes"


class QueryCalibrationMetricsArgs(ProjectScopedArgs):
    """[BRAIN] Aggregate calibration outcomes for decisions you've made.

    Related: ``record_outcome``.
    """

    operation: Literal["calibration_metrics"] = "calibration_metrics"


class QueryHooksArgs(ProjectScopedArgs):
    """[ANY] List configured hooks.

    Filter keys: ``event``, ``enabled_only``.
    """

    operation: Literal["hooks"] = "hooks"

    filters: Annotated[
        Optional[dict[str, Any]],
        Field(default=None, description="Optional filters."),
    ] = None


class QueryHookExecutionsArgs(ProjectScopedArgs, PaginatedFiltersMixin):
    """[ANY] Recent hook execution history.

    Filter keys: ``hook_id``, ``since``, ``status``.
    """

    operation: Literal["hook_executions"] = "hook_executions"


class QueryBrainNotificationsArgs(ProjectScopedArgs, PaginatedFiltersMixin):
    """[BRAIN] Notifications queued for the Brain.

    Filter keys: ``since``, ``include_cleared``.
    """

    operation: Literal["brain_notifications"] = "brain_notifications"


# ---------------------------------------------------------------------------
# Research map / review / claims / clusters / manuscript
# ---------------------------------------------------------------------------


class QueryResearchMapArgs(ProjectScopedArgs):
    """[ANY] Top-level research map: RQs -> clusters -> claims with synthesis."""

    operation: Literal["research_map"] = "research_map"


class QueryReviewQueueArgs(ProjectScopedArgs, PaginatedFiltersMixin):
    """[BRAIN] Pending review items (claims, clusters needing synthesis, etc.).

    Filter keys: ``status`` (``pending`` | ``reviewed`` | ``all``), ``target_type``.
    """

    operation: Literal["review_queue"] = "review_queue"


class QueryClustersArgs(ProjectScopedArgs, PaginatedFiltersMixin):
    """[ANY] List evidence clusters.

    Filter keys: ``research_question_id``, ``confidence``.
    """

    operation: Literal["clusters"] = "clusters"


class QueryClaimsArgs(ProjectScopedArgs, PaginatedFiltersMixin):
    """[ANY] List claims.

    Filter keys: ``source_entry_id``, ``cluster_id``, ``claim_type``,
    ``verified`` (source-grounding fidelity), ``evidence_status``
    (scientific evidence assessment), ``stale``.
    """

    operation: Literal["claims"] = "claims"


class QueryClaimScopeArgs(ProjectScopedArgs):
    """[ANY] Fetch immutable scope history and readiness for one claim."""

    operation: Literal["claim_scope"] = "claim_scope"
    id: Annotated[str, Field(description="Canonical clm_ claim id.")]


class QueryInterpretationCandidatesArgs(ProjectScopedArgs, PaginatedFiltersMixin):
    """[ANY] List staged interpretations or fetch one candidate detail.

    ``id`` selects the detailed candidate with immutable review history.
    Filter keys: ``review_status``, ``disposition``, ``epistemic_kind``,
    ``source_type``, and ``source_id``.
    """

    operation: Literal["interpretation_candidates"] = "interpretation_candidates"
    id: Annotated[
        Optional[str],
        Field(default=None, description="Optional icd_ id for detailed history."),
    ] = None


class QuerySourcesArgs(ProjectScopedArgs, PaginatedFiltersMixin):
    """[ANY] List registered sources or fetch one provenance detail."""

    operation: Literal["sources"] = "sources"
    id: Annotated[
        Optional[str],
        Field(default=None, description="Optional src_ id for provenance detail."),
    ] = None


class QueryNoteAttributionHistoryArgs(ProjectScopedArgs):
    """[ANY] Read ordered, immutable attribution corrections; not a full note edit history."""

    operation: Literal["note_attribution_history"] = "note_attribution_history"
    id: str
    after_revision: int = Field(default=0, ge=0)
    limit: int = Field(default=50, ge=1, le=200)


class QueryExperimentsArgs(ProjectScopedArgs, PaginatedFiltersMixin):
    """[ANY] List experiments or fetch one experiment with plan and run history.

    ``id`` selects a detailed experiment. Filter key: ``status``.
    """

    operation: Literal["experiments"] = "experiments"
    id: Annotated[Optional[str], Field(default=None, description="Optional exp_ id.")] = None


class QueryExperimentRunsArgs(ProjectScopedArgs, PaginatedFiltersMixin):
    """[ANY] List experiment runs or fetch one run with events and observations.

    ``id`` selects a detailed run. Filter keys: ``experiment_id`` and ``status``.
    """

    operation: Literal["experiment_runs"] = "experiment_runs"
    id: Annotated[Optional[str], Field(default=None, description="Optional run_ id.")] = None


class QueryExperimentObservationsArgs(ProjectScopedArgs, PaginatedFiltersMixin):
    """[ANY] List immutable observations or fetch one with evidence lineage.

    ``id`` selects a detailed observation. Filter keys: ``run_id``, ``direction``,
    ``kind``, and ``claim_id``.
    """

    operation: Literal["experiment_observations"] = "experiment_observations"
    id: Annotated[Optional[str], Field(default=None, description="Optional obs_ id.")] = None


class QueryPlanningBranchesArgs(ProjectScopedArgs):
    """[ANY] List planning branches or fetch one effective branch snapshot."""

    operation: Literal["planning_branches"] = "planning_branches"
    id: Annotated[Optional[str], Field(default=None, description="Optional mpb_ id.")] = None
    manuscript_id: Annotated[
        Optional[str],
        Field(default=None, description="Optional canonical man_ context; omit for project-only."),
    ] = None
    include_archived: bool = True


class QueryPlanningResumeArgs(ProjectScopedArgs):
    """[ANY] Resume the exact selected planning branch for one context."""

    operation: Literal["planning_resume"] = "planning_resume"
    manuscript_id: Annotated[
        Optional[str],
        Field(default=None, description="Optional canonical man_ context; omit for project-only."),
    ] = None


class QueryPlanningCompareArgs(ProjectScopedArgs):
    """[ANY] Compare two planning branches in the same context."""

    operation: Literal["planning_compare"] = "planning_compare"
    base_branch_id: Annotated[str, Field(min_length=1, description="Base mpb_ id.")]
    other_branch_id: Annotated[str, Field(min_length=1, description="Comparison mpb_ id.")]


class QueryPlanningArtifactVersionsArgs(ProjectScopedArgs):
    """[ANY] Read every immutable version of one planning artifact."""

    operation: Literal["planning_artifact_versions"] = "planning_artifact_versions"
    id: Annotated[str, Field(min_length=1, description="Canonical pla_ id.")]


class QueryPlanningArgumentWorkflowArgs(ProjectScopedArgs):
    """[ANY] Read deterministic seed-to-contribution guidance for one branch."""

    operation: Literal["planning_argument_workflow"] = "planning_argument_workflow"
    id: Annotated[str, Field(min_length=1, description="Canonical mpb_ id.")]


class QueryPlanningPromotionsArgs(ProjectScopedArgs):
    """[ANY] Read append-only promotion lineage for one planning branch."""

    operation: Literal["planning_promotions"] = "planning_promotions"
    id: Annotated[str, Field(min_length=1, description="Canonical mpb_ id.")]


class QueryPlanningEvaluationWorkflowArgs(ProjectScopedArgs):
    """[ANY] Resolve exact claim-to-result readiness for one planning branch."""

    operation: Literal["planning_evaluation_workflow"] = "planning_evaluation_workflow"
    id: Annotated[str, Field(min_length=1, description="Canonical mpb_ id.")]


class QueryPlanningEvaluationEventsArgs(ProjectScopedArgs):
    """[ANY] Read immutable evaluation mission/result lineage for one branch."""

    operation: Literal["planning_evaluation_events"] = "planning_evaluation_events"
    id: Annotated[str, Field(min_length=1, description="Canonical mpb_ id.")]


class QuerySemanticPatchProposalsArgs(ProjectScopedArgs):
    """[ANY] List proposals or fetch one exact semantic diff and event history."""

    operation: Literal["semantic_patch_proposals"] = "semantic_patch_proposals"
    id: Annotated[Optional[str], Field(default=None, description="Optional spp_ id.")] = None
    status: Annotated[
        Optional[SemanticPatchStatusLit],
        Field(default=None, description="Optional lifecycle filter."),
    ] = None
    limit: Annotated[int, Field(default=100, ge=1, le=500)] = 100


class QuerySemanticPatchSchemaArgs(ProjectScopedArgs):
    """[ANY] Get the exact host-agent generated-proposal JSON schema."""

    operation: Literal["semantic_patch_schema"] = "semantic_patch_schema"


class QueryManuscriptArgs(ProjectScopedArgs):
    """[ANY] Fetch a manuscript by canonical ``man_`` or legacy ``jrn_`` id."""

    operation: Literal["manuscript"] = "manuscript"

    id: Annotated[
        str,
        Field(description="Canonical man_ id or compatibility jrn_ alias."),
    ]


class QueryReferenceValidationStatusArgs(ProjectScopedArgs):
    """[ANY] Read one historical manuscript-reference validation job.

    Core no longer initiates or executes reference validation. External Writer
    or client workflows may verify separately. Core preserves this scoped
    reader for jobs recorded before the split.
    """

    operation: Literal["reference_validation_status"] = "reference_validation_status"

    manuscript_id: Annotated[
        str,
        Field(
            min_length=1,
            max_length=128,
            description="Canonical man_ id or compatibility jrn_ alias.",
        ),
    ]
    job_id: Annotated[
        str,
        Field(
            min_length=1,
            max_length=128,
            description="Historical reference-validation job id.",
        ),
    ]


class ResolveEntitiesArgs(ProjectScopedArgs):
    """[ANY] Resolve heterogeneous RKA IDs with project attestation.

    The server returns one outcome for every requested ID.  Set
    ``include_sources`` for direct terminal claim-source closure and
    ``include_edges`` for same-project edge rows.
    """

    operation: Literal["resolve_entities"] = "resolve_entities"

    ids: Annotated[
        list[str],
        Field(
            min_length=1,
            max_length=1000,
            description="One to 1,000 heterogeneous RKA entity IDs.",
        ),
    ]
    include_sources: Annotated[
        bool,
        Field(default=False, description="Include direct claim-source closure."),
    ] = False
    include_edges: Annotated[
        bool,
        Field(default=False, description="Include same-project typed edges."),
    ] = False

    @model_validator(mode="after")
    def _validate_ids(self) -> "ResolveEntitiesArgs":
        for entity_id in self.ids:
            if not entity_id or entity_id != entity_id.strip():
                raise ValueError(
                    "resolve_entities ids must be non-empty and contain no surrounding whitespace"
                )
        return self


class QueryManuscriptContextArgs(ProjectScopedArgs):
    """[ANY] Read the authoritative native manuscript aggregate."""

    operation: Literal["manuscript_context"] = "manuscript_context"

    id: Annotated[
        str,
        Field(description="Canonical man_ id or compatibility jrn_ alias."),
    ]


class QueryManuscriptReferenceManifestArgs(ProjectScopedArgs):
    """[ANY] Read active citation membership and validation currency."""

    operation: Literal["manuscript_reference_manifest"] = "manuscript_reference_manifest"

    id: Annotated[
        str,
        Field(description="Canonical man_ id or compatibility jrn_ alias."),
    ]


class QueryManuscriptReadinessArgs(ProjectScopedArgs):
    """[ANY] Evaluate evidence-linked readiness for one lifecycle phase."""

    operation: Literal["manuscript_readiness"] = "manuscript_readiness"

    id: Annotated[
        str,
        Field(description="Canonical man_ id or compatibility jrn_ alias."),
    ]
    target_phase: Annotated[
        ManuscriptReadinessPhaseLit,
        Field(
            default="drafting",
            description="Lifecycle phase whose mechanical gates to evaluate.",
        ),
    ] = "drafting"


class QueryManuscriptSpineArgs(ProjectScopedArgs):
    """[ANY] Export the deterministic Writer projection of the RKA spine."""

    operation: Literal["manuscript_spine"] = "manuscript_spine"

    id: Annotated[
        str,
        Field(description="Canonical man_ id or compatibility jrn_ alias."),
    ]


class QueryManuscriptOutlineArgs(ProjectScopedArgs):
    """[ANY] Read L2-L5 outline rationale, bindings, and checkpoint state."""

    operation: Literal["manuscript_outline"] = "manuscript_outline"

    id: Annotated[
        str,
        Field(description="Canonical man_ id or compatibility jrn_ alias."),
    ]


class QueryManuscriptWritingCandidatesArgs(ProjectScopedArgs):
    """[ANY] Smooth claims through reviewed clusters and research questions."""

    operation: Literal["manuscript_writing_candidates"] = "manuscript_writing_candidates"

    id: Annotated[
        str,
        Field(description="Canonical man_ id or compatibility jrn_ alias."),
    ]


class QueryChangesSinceArgs(ProjectScopedArgs):
    """[ANY] Page the durable project-scoped semantic change ledger."""

    operation: Literal["changes_since"] = "changes_since"

    cursor: Annotated[
        int,
        Field(
            default=0,
            ge=0,
            description="Opaque monotonic cursor; return rows strictly after it.",
        ),
    ] = 0
    limit: Annotated[
        int,
        Field(default=100, ge=1, le=1000, description="Page size."),
    ] = 100


class QueryManuscriptImpactArgs(ProjectScopedArgs):
    """[ANY] Map changed dependencies to manuscript claims and units."""

    operation: Literal["manuscript_impact"] = "manuscript_impact"

    id: Annotated[
        str,
        Field(description="Canonical man_ id or compatibility jrn_ alias."),
    ]
    since_cursor: Annotated[
        int,
        Field(
            default=0,
            ge=0,
            description="Opaque cursor from the last synchronized projection.",
        ),
    ] = 0
    limit: Annotated[
        int,
        Field(default=100, ge=1, le=1000, description="Change-page size."),
    ] = 100


# ---------------------------------------------------------------------------
# Graph operations
# ---------------------------------------------------------------------------


class QueryGraphArgs(ProjectScopedArgs, PaginatedFiltersMixin):
    """[ANY] Subset of the research-knowledge graph (nodes + edges).

    Filter keys: ``include_types``, ``phase``.
    """

    operation: Literal["graph"] = "graph"


class QueryEgoGraphArgs(ProjectScopedArgs):
    """[ANY] Local neighborhood graph centered on one entity."""

    operation: Literal["ego_graph"] = "ego_graph"

    id: Annotated[
        str,
        Field(description="Center-entity ID."),
    ]
    filters: Annotated[
        Optional[dict[str, Any]],
        Field(
            default=None,
            description="Optional filters (e.g. {'depth': 1}).",
        ),
    ] = None


class QueryGraphStatsArgs(ProjectScopedArgs):
    """[ANY] Top-level node/edge counts."""

    operation: Literal["graph_stats"] = "graph_stats"


class QueryGraphMermaidArgs(ProjectScopedArgs):
    """[ANY] Render the decision tree as Mermaid graph syntax.

    Filter keys: ``phase``, ``active_only``.
    """

    operation: Literal["graph_mermaid"] = "graph_mermaid"

    filters: Annotated[
        Optional[dict[str, Any]],
        Field(default=None, description="Optional filters."),
    ] = None


class QueryProvenanceArgs(ProjectScopedArgs):
    """[ANY] Trace the reasoning chain that produced an entity.

    Filter keys: ``direction`` (``forward`` | ``backward`` | ``both``),
    ``max_depth`` (integer 1-3, default 3). ``forward`` follows what the
    entity led to; ``backward`` follows what led to the entity. Legacy
    ``downstream`` / ``upstream`` direction aliases remain accepted.
    Contradictions are returned separately as non-causal context.
    """

    operation: Literal["provenance"] = "provenance"

    id: Annotated[
        str,
        Field(description="Entity ID whose provenance to trace."),
    ]
    filters: Annotated[
        Optional[dict[str, Any]],
        Field(
            default=None,
            description=(
                "Optional filters: direction is forward, backward, or both "
                "(legacy downstream/upstream aliases accepted); max_depth is "
                "an integer from 1 through 3 and defaults to 3."
            ),
        ),
    ] = None


class QueryMultiHopArgs(ProjectScopedArgs):
    """[BRAIN] Multi-hop graph retrieval seeded by query or explicit seed IDs.

    Pass ``filters.seeds=[...]`` to override the query-based seeding.
    """

    operation: Literal["multi_hop"] = "multi_hop"

    query: Annotated[
        str,
        Field(description="Seed query for multi-hop retrieval."),
    ]
    filters: Annotated[
        Optional[dict[str, Any]],
        Field(
            default=None,
            description=(
                "Optional filters (e.g. {'seeds': [...], 'max_depth': 3, 'max_nodes': 50})."
            ),
        ),
    ] = None


class QueryCollectReportContextArgs(ProjectScopedArgs):
    """[ANY] Collect the node set relevant to a report described in prose.

    Composite retrieval for the "I want to write a report about X"
    workflow: seeds from every angle query, BFS-expands through the
    typed link graph, protects seeds from cap displacement, and tags
    every node with inclusion provenance (``included_via``).

    ALWAYS provide ``filters.angle_queries`` — 3-5 short (1-4 word)
    queries decomposing the description from different angles
    (components, bugs/fixes, decisions, evaluations). Paragraph-only
    seeding measured 0.32 mean cohort recall vs 0.80 for
    angle-decomposed retrieval (eval-v3).
    """

    operation: Literal["collect_report_context"] = "collect_report_context"

    query: Annotated[
        str,
        Field(
            description=(
                "The report description — the PI's prose paragraph describing "
                "what the report should cover."
            )
        ),
    ]
    filters: Annotated[
        Optional[dict[str, Any]],
        Field(
            default=None,
            description=(
                "Optional filters: {'angle_queries': ['short query', ...], "
                "'max_depth': 2, 'max_nodes': 60, 'seed_limit': 8}. "
                "angle_queries is STRONGLY recommended."
            ),
        ),
    ] = None


# ---------------------------------------------------------------------------
# Summarization
# ---------------------------------------------------------------------------


class QueryStalenessImpactArgs(ProjectScopedArgs):
    """[ANY] Downstream blast-radius of a stale (or about-to-be-stale) entity.

    Walks dependent-direction links only: everything whose reasoning rests
    on the entity. Use before/after a supersede or retraction.
    """

    operation: Literal["staleness_impact"] = "staleness_impact"

    id: Annotated[
        str,
        Field(description="Entity ID whose downstream impact to compute."),
    ]
    filters: Annotated[
        Optional[dict[str, Any]],
        Field(default=None, description="Optional filters: {'max_depth': 3}."),
    ] = None


class QueryMissionGuardArgs(ProjectScopedArgs):
    """[EXECUTOR] Negative knowledge relevant to a mission at pickup.

    Retracted/superseded findings and unresolved contradictions overlapping
    the mission objective: approaches already falsified or contested. Call
    alongside mission context at pickup.
    """

    operation: Literal["mission_guard"] = "mission_guard"

    id: Annotated[
        str,
        Field(description="Mission ID to guard."),
    ]


class QueryBeliefAsOfArgs(ProjectScopedArgs):
    """[ANY] Reconstruct the believed-current knowledge state at a past date.

    'What did we believe in March, and what changed since?' Supersession
    transitions are exact; retraction times are approximate (updated_at).
    """

    operation: Literal["belief_as_of"] = "belief_as_of"

    query: Annotated[
        str,
        Field(description="ISO date or timestamp, e.g. '2026-03-15'."),
    ]


class QuerySummarizeArgs(ProjectScopedArgs):
    """[ANY] Topic-scoped summarization across the knowledge graph.

    Filter keys: ``topic``, ``phase``, ``entity_ids``.
    """

    operation: Literal["summarize"] = "summarize"

    query: Annotated[
        Optional[str],
        Field(default=None, description="Optional topic query."),
    ] = None
    filters: Annotated[
        Optional[dict[str, Any]],
        Field(default=None, description="Optional filters."),
    ] = None


class QueryGenerateSummaryArgs(ProjectScopedArgs):
    """[ANY] Render a structured summary (project / mission / decision scope).

    Filter keys: ``scope_type`` (``project`` | ``mission`` | ``decision``),
    ``scope_id``, ``granularity`` (``paragraph`` | ``outline`` | ``bullet_list``).

    Note: v2.4.0 removed the LLM-driven path; this scope is a stub until
    re-wired through the orchestrator.
    """

    operation: Literal["generate_summary"] = "generate_summary"

    id: Annotated[
        Optional[str],
        Field(default=None, description="Optional scope-entity ID."),
    ] = None
    filters: Annotated[
        Optional[dict[str, Any]],
        Field(default=None, description="Optional filters."),
    ] = None


# ---------------------------------------------------------------------------
# Evidence / maintenance / freshness / contradictions
# ---------------------------------------------------------------------------


class QueryEvidenceArgs(ProjectScopedArgs):
    """[BRAIN] Assemble structured evidence supporting a research question.

    ``id`` is the research_question_id (stored as a decision id with
    kind=``research_question``).
    """

    operation: Literal["evidence"] = "evidence"

    id: Annotated[
        str,
        Field(description="Research question ID (dec_... with kind='research_question')."),
    ]
    filters: Annotated[
        Optional[dict[str, Any]],
        Field(
            default=None,
            description=(
                "Optional filters (e.g. {'format': 'progress_report'|'briefing'|'audit'})."
            ),
        ),
    ] = None


class QueryFreshnessArgs(ProjectScopedArgs):
    """[BRAIN] Check freshness/staleness of claims and clusters.

    Filter keys: ``days_threshold``.
    """

    operation: Literal["freshness"] = "freshness"

    filters: Annotated[
        Optional[dict[str, Any]],
        Field(
            default=None,
            description="Optional filters (e.g. {'days_threshold': 30}).",
        ),
    ] = None


class QueryContradictionsArgs(ProjectScopedArgs):
    """[BRAIN] Detect contradicting evidence near a given entity.

    Filter keys: ``similarity_threshold``, ``max_results``.
    """

    operation: Literal["contradictions"] = "contradictions"

    id: Annotated[
        str,
        Field(description="Anchor entity ID for contradiction search."),
    ]
    filters: Annotated[
        Optional[dict[str, Any]],
        Field(default=None, description="Optional filters."),
    ] = None


class QueryIntegrityArgs(ProjectScopedArgs):
    """[BRAIN] Database integrity probe (orphans, broken links)."""

    operation: Literal["integrity"] = "integrity"


class QueryPendingMaintenanceArgs(ProjectScopedArgs):
    """[BRAIN] Provenance gaps, untagged entries, orphans, etc."""

    operation: Literal["pending_maintenance"] = "pending_maintenance"


# ---------------------------------------------------------------------------
# Changelog / workspace / bootstrap_review
# ---------------------------------------------------------------------------


class QueryChangelogArgs(ProjectScopedArgs):
    """[ANY] Project changelog since a given date.

    ``filters.since`` is REQUIRED (ISO8601 date string).
    """

    operation: Literal["changelog"] = "changelog"

    limit: Annotated[
        Optional[int],
        Field(default=None, description="Cap on result count (default 50)."),
    ] = None
    filters: Annotated[
        dict[str, Any],
        Field(
            description=(
                "Required filter dict; MUST contain 'since' as an ISO8601 "
                "date string (e.g. {'since': '2026-05-01'})."
            ),
        ),
    ]

    @model_validator(mode="after")
    def _require_since(self) -> "QueryChangelogArgs":
        if self.filters is None or "since" not in self.filters:
            raise ValueError("changelog requires filters.since as an ISO8601 date string")
        since_val = self.filters["since"]
        if not isinstance(since_val, str) or not since_val.strip():
            raise ValueError("changelog filters.since must be a non-empty ISO8601 string")
        return self


class QueryBootstrapReviewArgs(ProjectScopedArgs):
    """[ANY] Review the proposed bootstrap result for a workspace scan.

    ``id`` is the scan_id (scn_...).
    """

    operation: Literal["bootstrap_review"] = "bootstrap_review"

    id: Annotated[
        str,
        Field(description="Scan ID (scn_...)."),
    ]


class QueryWorkspaceTreeArgs(ProjectScopedArgs):
    """[ANY] Read-only file-tree probe of a workspace folder.

    The ``folder_path`` lives in ``filters``; the optional ``max_depth`` is
    a top-level kwarg for ergonomic access.
    """

    operation: Literal["workspace_tree"] = "workspace_tree"

    filters: Annotated[
        dict[str, Any],
        Field(
            description=(
                "Required filter dict; MUST contain 'folder_path' as a string. "
                "Optional 'max_depth' int."
            ),
        ),
    ]
    max_depth: Annotated[
        Optional[int],
        Field(default=None, description="Optional max recursion depth."),
    ] = None

    @model_validator(mode="after")
    def _require_folder_path(self) -> "QueryWorkspaceTreeArgs":
        if self.filters is None or "folder_path" not in self.filters:
            raise ValueError("workspace_tree requires filters.folder_path as an absolute path")
        fp = self.filters["folder_path"]
        if not isinstance(fp, str) or not fp.strip():
            raise ValueError("workspace_tree filters.folder_path must be a non-empty string")
        return self


class QueryWorkspaceScanArgs(ProjectScopedArgs):
    """[ANY] Deep workspace scan (full file contents for ingestion).

    The ``folder_path`` lives in ``filters``; ``ignore_patterns``,
    ``max_file_size_mb``, and ``use_llm`` are top-level optional kwargs.
    """

    operation: Literal["workspace_scan"] = "workspace_scan"

    filters: Annotated[
        dict[str, Any],
        Field(
            description=("Required filter dict; MUST contain 'folder_path' as a string."),
        ),
    ]
    ignore_patterns: Annotated[
        Optional[list[str]],
        Field(default=None, description="Optional list of glob patterns to ignore."),
    ] = None
    max_file_size_mb: Annotated[
        Optional[int],
        Field(default=None, description="Optional max per-file size in MB."),
    ] = None
    use_llm: Annotated[
        Optional[bool],
        Field(default=None, description="Optional flag to enable LLM-assisted scan."),
    ] = None

    @model_validator(mode="after")
    def _require_folder_path(self) -> "QueryWorkspaceScanArgs":
        if self.filters is None or "folder_path" not in self.filters:
            raise ValueError("workspace_scan requires filters.folder_path as an absolute path")
        fp = self.filters["folder_path"]
        if not isinstance(fp, str) or not fp.strip():
            raise ValueError("workspace_scan filters.folder_path must be a non-empty string")
        return self


# ---------------------------------------------------------------------------
# Unscoped session operations (list_projects / capabilities / health)
# ---------------------------------------------------------------------------


class QueryListProjectsArgs(UnscopedArgs):
    """[ANY] List all available projects (UNSCOPED — does not require project_id)."""

    operation: Literal["list_projects"] = "list_projects"


class QueryCapabilitiesArgs(UnscopedArgs):
    """[ANY] Discover Core and interface contracts (UNSCOPED)."""

    operation: Literal["capabilities"] = "capabilities"
    required_contract: Annotated[
        Optional[str],
        Field(
            default=None,
            description="Optional Core contract required by the caller.",
        ),
    ] = None
    required_capabilities: Annotated[
        Optional[list[str]],
        Field(
            default=None,
            description=(
                "Optional runtime capabilities that must be available (rest, mcp, embedding)."
            ),
        ),
    ] = None


class QueryHealthArgs(UnscopedArgs):
    """[ANY] API health probe (UNSCOPED)."""

    operation: Literal["health"] = "health"


# ---------------------------------------------------------------------------
# Union (discriminated by `operation`)
# ---------------------------------------------------------------------------


QueryArgsUnion = Annotated[
    Union[
        # Core
        QueryStatusArgs,
        QueryContextArgs,
        QuerySearchArgs,
        QueryEntityArgs,
        # List-mode
        QueryJournalArgs,
        QueryNoteAttributionHistoryArgs,
        QueryLiteratureArgs,
        QueryMissionArgs,
        QueryReportArgs,
        QueryCheckpointsArgs,
        # Decision / calibration / hooks / notifications
        QueryDecisionTreeArgs,
        OrphanSupersedesArgs,
        QueryCalibrationMetricsArgs,
        QueryHooksArgs,
        QueryHookExecutionsArgs,
        QueryBrainNotificationsArgs,
        # Research map / review / claims / clusters / manuscript
        QueryResearchMapArgs,
        QueryReviewQueueArgs,
        QueryClustersArgs,
        QueryClaimsArgs,
        QueryClaimScopeArgs,
        QueryInterpretationCandidatesArgs,
        QuerySourcesArgs,
        QueryExperimentsArgs,
        QueryExperimentRunsArgs,
        QueryExperimentObservationsArgs,
        QueryPlanningBranchesArgs,
        QueryPlanningResumeArgs,
        QueryPlanningCompareArgs,
        QueryPlanningArtifactVersionsArgs,
        QueryPlanningArgumentWorkflowArgs,
        QueryPlanningPromotionsArgs,
        QueryPlanningEvaluationWorkflowArgs,
        QueryPlanningEvaluationEventsArgs,
        QuerySemanticPatchProposalsArgs,
        QuerySemanticPatchSchemaArgs,
        QueryManuscriptArgs,
        QueryReferenceValidationStatusArgs,
        ResolveEntitiesArgs,
        QueryManuscriptContextArgs,
        QueryManuscriptReferenceManifestArgs,
        QueryManuscriptReadinessArgs,
        QueryManuscriptSpineArgs,
        QueryManuscriptOutlineArgs,
        QueryManuscriptWritingCandidatesArgs,
        QueryChangesSinceArgs,
        QueryManuscriptImpactArgs,
        # Graph
        QueryGraphArgs,
        QueryEgoGraphArgs,
        QueryGraphStatsArgs,
        QueryGraphMermaidArgs,
        QueryProvenanceArgs,
        QueryMultiHopArgs,
        QueryCollectReportContextArgs,
        QueryStalenessImpactArgs,
        QueryMissionGuardArgs,
        QueryBeliefAsOfArgs,
        # Summarization
        QuerySummarizeArgs,
        QueryGenerateSummaryArgs,
        # Evidence / maintenance
        QueryEvidenceArgs,
        QueryFreshnessArgs,
        QueryContradictionsArgs,
        QueryIntegrityArgs,
        QueryPendingMaintenanceArgs,
        # Changelog / workspace / bootstrap_review
        QueryChangelogArgs,
        QueryBootstrapReviewArgs,
        QueryWorkspaceTreeArgs,
        QueryWorkspaceScanArgs,
        # Unscoped session
        QueryListProjectsArgs,
        QueryCapabilitiesArgs,
        QueryHealthArgs,
    ],
    Field(discriminator="operation"),
]


# =============================================================================
# Batch B — RECORD/CREATE + native manuscript operations (26 models)
# =============================================================================
#
# record_note, ingest_document, record_decision, record_literature,
# import_bibtex, batch_import, register_manuscript, create_mission,
# create_cluster, create_project, create_gate, hook_add, extract_claims.
#
# Provenance discipline (mirrors verb_dispatch.dispatch_record_note +
# dispatch_record_decision + dispatch_mission(action='create')) is
# encoded as Pydantic ``model_validator(mode='after')`` raisers:
#   - RecordNoteArgs: source='pi' -> verbatim_input required
#   - RecordDecisionArgs: related_journal min_length=1
#   - CreateMissionArgs: motivated_by_decision required (non-Optional)
#   - RecordLiteratureArgs: at least one of {title, doi} required
#   - ExtractClaimsArgs: claims min_length=1
#   - CreateGateArgs: deliverables + pass_criteria min_length=1


# --- journal --------------------------------------------------------------


class _NoteProvenance(BaseModel):
    """Nested provenance dict shape accepted by ``record_note`` /
    ``ingest_document`` per Graft A — flattened into the legacy REST
    call by the dispatcher.

    Fields mirror ``verb_dispatch._NOTE_PROVENANCE_KEYS``.
    """

    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    related_decisions: Annotated[
        Optional[list[str]],
        Field(default=None, description="Decision IDs justifying this note."),
    ] = None
    related_literature: Annotated[
        Optional[list[str]],
        Field(default=None, description="Literature IDs cited by this note."),
    ] = None
    related_mission: Annotated[
        Optional[str],
        Field(default=None, description="Mission ID this note belongs to."),
    ] = None
    supersedes: Annotated[
        Optional[str],
        Field(default=None, description="Journal ID this note supersedes."),
    ] = None


class RecordNoteArgs(ProjectScopedArgs):
    """[ANY] RECORD a journal entry (note, log, directive).

    PI input requires ``verbatim_input`` unless explicit ``raw_capture``
    snapshots supplied content on creation. The
    typed-args layer rejects calls without it pre-flight (PI provenance
    discipline — preserves PI's exact wording for intellectual attribution).

    Brain hallucination class closed: ``confidence='confirmed'`` is NOT
    in the allowed set; use one of
    {hypothesis, tested, verified, superseded, retracted}.
    """

    operation: Literal["record_note"] = "record_note"
    capture_mode: JournalCaptureModeLit = Field(
        default="unknown",
        description="Omission makes no capture claim. Use raw_capture only for supplied "
        "original text; absent verbatim_input snapshots content. unknown makes no capture claim.",
    )

    # Required body
    content: Annotated[
        str,
        Field(description="Journal entry body (PRIMARY field)."),
    ]

    # Optional enum-typed fields
    source: Annotated[
        SourceLit,
        Field(default="executor", description="Actor of record."),
    ] = "executor"
    type: Annotated[
        NoteTypeLit,
        Field(default="note", description=(
            "Journal entry type. Only `note`, `log` and `directive` are stored: the other nine are legacy aliases, normalized on write (`methodology`→`log`, `pi_instruction`→`directive`, and the rest→`note`). Reading an entry back returns the canonical type, not the alias you sent."
        )),
    ] = "note"
    confidence: Annotated[
        ConfidenceLit,
        Field(default="hypothesis", description="Confidence level."),
    ] = "hypothesis"
    importance: Annotated[
        ImportanceLit,
        Field(default="normal", description="Importance level."),
    ] = "normal"

    # Optional provenance / metadata
    verbatim_input: Annotated[
        Optional[str],
        Field(
            default=None,
            description=(
                "Exact original text. PI source requires it unless explicit raw_capture "
                "snapshots supplied content on creation. Never use a paraphrase here."
            ),
        ),
    ] = None
    phase: Annotated[
        Optional[str],
        Field(default=None, description="Project phase tag."),
    ] = None
    tags: Annotated[
        Optional[list[str]],
        Field(default=None, description="Free-form tags."),
    ] = None
    # Phase-X²' polish: surface the previously-hidden JournalEntryCreate
    # source fields through the typed-args layer so Brain can set them
    # explicitly instead of relying on REST-layer defaults.
    summary: Annotated[
        Optional[str],
        Field(
            default=None,
            description=(
                "Optional short summary of the entry (matches "
                "rka/models/journal.py JournalEntryCreate.summary)."
            ),
        ),
    ] = None
    status: Annotated[
        Optional[JournalStatusLit],
        Field(
            default=None,
            description=(
                "Journal entry lifecycle status (draft|active|superseded|"
                "retracted). Defaults to 'active' at the REST layer when "
                "omitted; matches rka/db/schema.sql migration 009."
            ),
        ),
    ] = None
    pinned: Annotated[
        Optional[bool],
        Field(
            default=None,
            description=(
                "Whether the entry is pinned for quick access. Matches "
                "rka/models/journal.py JournalEntryCreate.pinned."
            ),
        ),
    ] = None
    provenance: Annotated[
        Optional[_NoteProvenance],
        Field(
            default=None,
            description=(
                "Nested provenance dict (Graft A): "
                "{related_decisions, related_literature, related_mission, supersedes}."
            ),
        ),
    ] = None

    @model_validator(mode="after")
    def _enforce_pi_verbatim(self) -> "RecordNoteArgs":
        if self.capture_mode != "raw_capture" and self.source == "pi" and (
            self.verbatim_input is None or not str(self.verbatim_input).strip()
        ):
            raise ValueError(
                "rka_record_note(source='pi'): verbatim_input is required "
                "(preserves PI's exact wording for intellectual attribution)"
            )
        JournalEntryCreate(
            content=self.content, source=self.source, capture_mode=self.capture_mode,
            verbatim_input=self.verbatim_input,
        )
        return self


class IngestDocumentArgs(ProjectScopedArgs):
    """[ANY] Ingest a markdown document into many journal entries.

    Splits the input by H1/H2 headings (when ``split_by_headings=True``).
    Each section becomes its own journal row carrying the shared
    provenance + default_type.
    """

    operation: Literal["ingest_document"] = "ingest_document"

    # Required body
    content: Annotated[
        str,
        Field(description="Markdown document body (PRIMARY field)."),
    ]

    # Optional enum-typed fields
    source: Annotated[
        IngestSourceLit,
        Field(default="executor", description="Actor of record."),
    ] = "executor"
    default_type: Annotated[
        NoteTypeLit,
        Field(
            default="finding",
            description=(
                "Default type for emitted entries. Note that the default is a "
                "legacy alias: `finding` is stored as `note`. Only `note`, "
                "`log` and `directive` are stored; the other nine normalize on "
                "write (`methodology`→`log`, `pi_instruction`→`directive`, the "
                "rest→`note`)."
            ),
        ),
    ] = "finding"
    split_by_headings: Annotated[
        bool,
        Field(default=True, description="Split markdown body on H1/H2 headings."),
    ] = True
    phase: Annotated[
        Optional[str],
        Field(default=None, description="Project phase tag."),
    ] = None
    tags: Annotated[
        Optional[list[str]],
        Field(default=None, description="Tags applied to every emitted entry."),
    ] = None
    provenance: Annotated[
        Optional[_NoteProvenance],
        Field(
            default=None,
            description="Shared provenance applied to every emitted entry.",
        ),
    ] = None


# --- decisions ------------------------------------------------------------


class RecordDecisionArgs(ProjectScopedArgs):
    """[BRAIN/PI] RECORD a decision node. Provenance-required.

    Brain hallucination classes closed:
      - ``confidence='confirmed'`` is NOT in the allowed set; use one of
        {hypothesis, tested, verified, superseded, retracted}.
      - ``decided_by='SUPERVISOR'`` is NOT valid; use {pi, brain, executor}.
      - ``kind='research_question'`` is reserved for advanceable RQs;
        most decisions are 'decision' or 'design_choice'.
      - ``related_journal=[]`` is rejected at the Pydantic layer
        (``min_length=1``) AND in the post-validator
        (defense-in-depth for programmatic callers bypassing the schema).
    """

    operation: Literal["record_decision"] = "record_decision"

    # Required body
    question: Annotated[
        str,
        Field(description="Decision question (PRIMARY field)."),
    ]
    chosen: Annotated[
        str,
        Field(description="Chosen option label."),
    ]
    rationale: Annotated[
        str,
        Field(description="Why this was chosen."),
    ]

    # Required enums
    decided_by: Annotated[
        DecidedByLit,
        Field(
            description=(
                "Who decided this. PI for ratified, Brain for proposed, "
                "Executor for self-reported operational decisions."
            ),
        ),
    ]
    kind: Annotated[
        DecisionKindLit,
        Field(description="Kind of decision."),
    ]

    # Required provenance
    related_journal: Annotated[
        list[str],
        Field(
            min_length=1,
            description=(
                "Journal IDs justifying this decision. REQUIRED non-empty "
                "(provenance discipline preserved from Phase-X²)."
            ),
        ),
    ]

    # Optional fields
    options: Annotated[
        Optional[list[dict]],
        Field(
            default=None,
            description="Options considered (each: {id, label, ...}).",
        ),
    ] = None
    supersedes_decision_id: Annotated[
        Optional[str],
        Field(
            default=None,
            description=(
                "If set, this decision supersedes the given decision ID "
                "atomically (POST /api/decisions/{old}/supersede)."
            ),
        ),
    ] = None
    confidence: Annotated[
        ConfidenceLit,
        Field(default="tested", description="Confidence level."),
    ] = "tested"
    tags: Annotated[
        Optional[list[str]],
        Field(default=None, description="Free-form tags."),
    ] = None
    phase: Annotated[
        str,
        Field(
            description=(
                "Project phase tag. REQUIRED — decisions are phase-scoped and "
                "the REST DecisionCreate model requires it. v2.7.0.7 promoted "
                "this from optional to required so a missing phase fails at the "
                "typed-args boundary with a clear message instead of as an "
                "opaque 422 at the API. (The dedicated `supersede_decision` "
                "operation still allows omitting phase — it inherits the old "
                "decision's phase.)"
            ),
        ),
    ]
    parent_id: Annotated[
        Optional[str],
        Field(default=None, description="Parent decision ID (decision tree)."),
    ] = None
    related_literature: Annotated[
        Optional[list[str]],
        Field(default=None, description="Literature IDs cited by this decision."),
    ] = None
    related_missions: Annotated[
        Optional[list[str]],
        Field(
            default=None,
            description=(
                "Mission IDs related to this decision (surfaces the "
                "DecisionCreate.related_missions source field through the "
                "typed-args layer)."
            ),
        ),
    ] = None
    status: Annotated[
        Optional[DecisionStatusLit],
        Field(
            default=None,
            description=(
                "Decision lifecycle status; defaults to 'active' at the REST layer when omitted."
            ),
        ),
    ] = None
    assumptions: Annotated[
        Optional[list[str]],
        Field(default=None, description="Assumptions underlying this decision."),
    ] = None

    @model_validator(mode="after")
    def _enforce_related_journal_nonempty(self) -> "RecordDecisionArgs":
        # ``min_length=1`` enforces this at the Pydantic schema layer; this
        # validator is defense-in-depth for any caller bypassing the
        # schema (e.g. an orchestrator constructing the model
        # programmatically via __dict__ mutation).
        if not self.related_journal:
            raise ValueError(
                "rka_record_decision: related_journal must be a non-empty "
                "list — decisions need justifying journal entries "
                "(provenance discipline preserved from Phase-X²)"
            )
        return self


# --- literature -----------------------------------------------------------


class RecordLiteratureArgs(ProjectScopedArgs):
    """[ANY] Add a literature entry. Title-based create is the canonical mode.

    For BibTeX bulk-import, use ``import_bibtex`` (separate op). For
    semantic-scholar / arxiv search modes, use Batch A's literature
    query path with the appropriate search_source. This op exists for
    title-based create OR doi-only bootstrap (where the row is created
    with a placeholder title to be enriched later via ``enrich_doi``).
    """

    operation: Literal["record_literature"] = "record_literature"

    # Body: title is the canonical PRIMARY field; doi is the fallback for
    # bootstrap-from-DOI mode.
    title: Annotated[
        Optional[str],
        Field(
            default=None,
            description=(
                "Paper title (PRIMARY for title-mode). Either title OR doi must be provided."
            ),
        ),
    ] = None
    doi: Annotated[
        Optional[str],
        Field(
            default=None,
            description=(
                "DOI (e.g. '10.48550/arXiv.1706.03762'). When only doi is "
                "set (no title), bootstraps a row to enrich later."
            ),
        ),
    ] = None

    # Optional metadata
    authors: Annotated[
        Optional[list[str]],
        Field(default=None, description="Authors list."),
    ] = None
    year: Annotated[
        Optional[int],
        Field(default=None, description="Publication year."),
    ] = None
    venue: Annotated[
        Optional[str],
        Field(default=None, description="Publication venue."),
    ] = None
    url: Annotated[
        Optional[str],
        Field(default=None, description="URL to the paper."),
    ] = None
    abstract: Annotated[
        Optional[str],
        Field(default=None, description="Abstract text."),
    ] = None
    status: Annotated[
        LitStatusLit,
        Field(default="to_read", description="Reading lifecycle status."),
    ] = "to_read"
    tags: Annotated[
        Optional[list[str]],
        Field(default=None, description="Tags."),
    ] = None
    related_decisions: Annotated[
        Optional[list[str]],
        Field(default=None, description="Decision IDs citing this paper."),
    ] = None

    @model_validator(mode="after")
    def _enforce_title_or_doi(self) -> "RecordLiteratureArgs":
        if not self.title and not self.doi:
            raise ValueError(
                "rka_record_literature: provide one of title or doi "
                "(title-based create or doi-only bootstrap)"
            )
        return self


class ImportBibtexArgs(ProjectScopedArgs):
    """[ANY] Bulk-import literature entries from BibTeX."""

    operation: Literal["import_bibtex"] = "import_bibtex"

    bibtex: Annotated[
        str,
        Field(
            min_length=1,
            description="BibTeX library source text (non-empty).",
        ),
    ]
    default_status: Annotated[
        LitStatusLit,
        Field(
            default="to_read",
            description="Reading status applied to every imported entry.",
        ),
    ] = "to_read"


# --- multi-entity ingestion ----------------------------------------------


class BatchImportArgs(ProjectScopedArgs):
    """[ANY] Bulk import mixed entity types in one call.

    Each entry must have ``entity_type`` (note | literature | decision) and
    ``data`` fields. ``actor='import'`` is auto-normalized to
    ``actor='system'`` at the legacy tier (system is the canonical value
    for programmatic ingestion per CLAUDE.md).

    Phase-X²' polish: each entry's structural shape is validated at the
    typed surface (``entity_type`` in the allowed set, ``data`` is a
    dict) so Brain can't ship ``[{}]`` past the schema and defer the
    failure to the REST layer.
    """

    operation: Literal["batch_import"] = "batch_import"

    entries: Annotated[
        list[dict],
        Field(
            min_length=1,
            description=(
                "Non-empty list of {entity_type: 'note'|'literature'|"
                "'decision', data: {...}} entries to import."
            ),
        ),
    ]
    actor: Annotated[
        BatchImportActorLit,
        Field(
            default="system",
            description=(
                "Actor of record. 'import' is auto-normalized to 'system' "
                "(system is the canonical value for programmatic ingestion)."
            ),
        ),
    ] = "system"

    @model_validator(mode="after")
    def _enforce_entry_shape(self) -> "BatchImportArgs":
        allowed_types = {"note", "literature", "decision"}
        for idx, entry in enumerate(self.entries):
            if not isinstance(entry, dict):
                raise ValueError(f"batch_import: entries[{idx}] is not a dict.")
            entity_type = entry.get("entity_type")
            if entity_type not in allowed_types:
                raise ValueError(
                    f"batch_import: entries[{idx}].entity_type must be one "
                    f"of {sorted(allowed_types)} (got {entity_type!r})."
                )
            data = entry.get("data")
            if not isinstance(data, dict):
                raise ValueError(
                    f"batch_import: entries[{idx}].data must be a dict (got {type(data).__name__})."
                )
        return self


class RegisterManuscriptArgs(ProjectScopedArgs):
    """[ANY] Compatibility register for legacy Writer callers.

    The response includes both the deprecated ``jrn_`` manifest ID and the
    canonical native ``man_`` ID.  New callers should use
    ``create_manuscript``.
    """

    operation: Literal["register_manuscript"] = "register_manuscript"

    venue: Annotated[
        str,
        Field(description="Submission venue (e.g. 'NeurIPS-2026')."),
    ]
    title: Annotated[
        str,
        Field(description="Manuscript title."),
    ]
    abstract: Annotated[
        Optional[str],
        Field(default=None, description="Abstract draft."),
    ] = None
    sections: Annotated[
        Optional[list[str]],
        Field(
            default=None,
            description="Section names for the manuscript skeleton.",
        ),
    ] = None


class _ManuscriptSpineEvidenceArgs(BaseModel):
    """Typed evidence-role buckets used by claim and unit spine records."""

    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    support: list[str] = Field(default_factory=list)
    qualifier: list[str] = Field(default_factory=list)
    counterevidence: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _require_claim_ids(self) -> "_ManuscriptSpineEvidenceArgs":
        for role in ManuscriptEvidenceRoleLit.__args__:
            values = getattr(self, role)
            if len(values) != len(set(values)):
                raise ValueError(f"duplicate evidence ID in {role}")
            if any(not value.startswith("clm_") for value in values):
                raise ValueError(f"{role} evidence must contain only clm_ IDs")
        return self


class _ManuscriptSpineEvidenceUseArgs(BaseModel):
    """One unit-local use of an RKA evidence claim."""

    model_config = ConfigDict(extra="forbid")

    evidence_claim_id: Annotated[str, Field(pattern=r"^clm_")]
    supported_proposition: Annotated[Optional[str], Field(min_length=1)] = None
    warrant: Annotated[Optional[str], Field(min_length=1)] = None


class _ManuscriptSpineUnitEvidenceArgs(BaseModel):
    """Evidence-use buckets; strings remain a compatibility input."""

    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    support: list[Union[str, _ManuscriptSpineEvidenceUseArgs]] = Field(
        default_factory=list
    )
    qualifier: list[Union[str, _ManuscriptSpineEvidenceUseArgs]] = Field(
        default_factory=list
    )
    counterevidence: list[Union[str, _ManuscriptSpineEvidenceUseArgs]] = Field(
        default_factory=list
    )

    @model_validator(mode="after")
    def _require_unique_claim_ids(self) -> "_ManuscriptSpineUnitEvidenceArgs":
        for role in ManuscriptEvidenceRoleLit.__args__:
            values = getattr(self, role)
            ids = [
                value if isinstance(value, str) else value.evidence_claim_id
                for value in values
            ]
            if len(ids) != len(set(ids)):
                raise ValueError(f"duplicate evidence ID in {role}")
            if any(not value.startswith("clm_") for value in ids):
                raise ValueError(f"{role} evidence must contain only clm_ IDs")
        return self


class _ManuscriptSpineCitationUseArgs(BaseModel):
    """One typed use of an existing manuscript reference member."""

    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    citation_key: Annotated[str, Field(min_length=1)]
    citation_role: ManuscriptCitationRoleLit
    supported_proposition: Annotated[str, Field(min_length=1)]
    verification_state: ManuscriptCitationVerificationStateLit = "unverified"
    comparison_axis: Annotated[Optional[str], Field(min_length=1)] = None


class _ManuscriptSpineUnitLinkArgs(BaseModel):
    """Typed claim-to-unit relationship in an argument-spine replacement."""

    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    unit_key: Annotated[str, Field(min_length=1)]
    relationship: Annotated[
        ManuscriptClaimUnitRelationshipLit,
        Field(default="advances"),
    ] = "advances"


class _ManuscriptSpineClaimArgs(BaseModel):
    """One stable claim identity plus its current immutable wording."""

    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    local_key: Annotated[str, Field(min_length=1)]
    kind: ManuscriptClaimKindLit
    state: ManuscriptClaimStateLit = "candidate"
    exact_wording: Annotated[str, Field(min_length=1)]
    allowed_wording: Annotated[str, Field(min_length=1)]
    prohibited_wording: Annotated[list[str], Field(min_length=1)]
    conditions: list[Annotated[str, Field(min_length=1)]] = Field(default_factory=list)
    falsification_criteria: list[Annotated[str, Field(min_length=1)]] = Field(
        default_factory=list
    )
    evidence: _ManuscriptSpineEvidenceArgs = Field(default_factory=_ManuscriptSpineEvidenceArgs)
    unit_links: list[_ManuscriptSpineUnitLinkArgs] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_wording_and_links(self) -> "_ManuscriptSpineClaimArgs":
        if any(not value.strip() for value in self.prohibited_wording):
            raise ValueError("prohibited_wording entries must be non-empty")
        linked = [(item.unit_key, item.relationship) for item in self.unit_links]
        if len(linked) != len(set(linked)):
            raise ValueError("duplicate claim-to-unit relationship")
        return self


class _ManuscriptSpineUnitArgs(BaseModel):
    """One claim-sized Writer unit in the authoritative RKA spine."""

    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    local_key: Annotated[str, Field(min_length=1)]
    kind: ManuscriptUnitKindLit
    location: Annotated[str, Field(min_length=1)]
    title: Optional[str] = None
    artifact_ref: Optional[str] = None
    allowed_interpretation: Optional[str] = None
    prohibited_interpretation: Optional[str] = None
    sequence: Annotated[int, Field(ge=0)] = 0
    status: ManuscriptUnitStatusLit = "planned"
    outline_level: Annotated[int, Field(ge=2, le=5)] = 4
    unit_role: ManuscriptUnitRoleLit = "unspecified"
    rhetorical_move: ManuscriptRhetoricalMoveLit = "unspecified"
    parent_unit_key: Optional[str] = None
    communicative_job: Optional[str] = None
    intended_takeaway: Optional[str] = None
    transition_from_previous: Optional[str] = None
    quick_reader_role: Optional[str] = None
    evidence_plan: list[str] = Field(default_factory=list)
    figure_intentions: list[str] = Field(default_factory=list)
    table_intentions: list[str] = Field(default_factory=list)
    citation_intentions: list[str] = Field(default_factory=list)
    blocker: Optional[str] = None
    evidence: _ManuscriptSpineUnitEvidenceArgs = Field(
        default_factory=_ManuscriptSpineUnitEvidenceArgs
    )
    citations: list[_ManuscriptSpineCitationUseArgs] = Field(default_factory=list)

    @model_validator(mode="after")
    def _require_result_boundaries(self) -> "_ManuscriptSpineUnitArgs":
        if self.kind != "result":
            return self
        for field_name in (
            "artifact_ref",
            "allowed_interpretation",
            "prohibited_interpretation",
        ):
            value = getattr(self, field_name)
            if value is None or not value.strip():
                raise ValueError(f"result units require {field_name}")
        return self


class _ManuscriptArgumentSpineArgs(BaseModel):
    """Complete replacement payload for the native argument spine.

    Both lists are required because omission has retirement/removal semantics.
    An explicit empty list is permitted when the caller intentionally retires
    every record in that category.
    """

    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    claims: list[_ManuscriptSpineClaimArgs]
    units: list[_ManuscriptSpineUnitArgs]

    @model_validator(mode="after")
    def _require_unique_local_keys(self) -> "_ManuscriptArgumentSpineArgs":
        for label, records in (("claim", self.claims), ("unit", self.units)):
            keys = [record.local_key for record in records]
            if len(keys) != len(set(keys)):
                raise ValueError(f"duplicate {label} local_key")
        unit_keys = {unit.local_key for unit in self.units}
        missing = sorted(
            {
                link.unit_key
                for claim in self.claims
                for link in claim.unit_links
                if link.unit_key not in unit_keys
            }
        )
        if missing:
            raise ValueError("claim unit_links reference unknown unit keys: " + ", ".join(missing))
        return self


class CreateManuscriptArgs(ProjectScopedArgs):
    """[ANY] Create the canonical native manuscript aggregate."""

    operation: Literal["create_manuscript"] = "create_manuscript"

    title: Annotated[str, Field(min_length=1)]
    abstract: Optional[str] = None
    venue: Optional[str] = None
    phase: ManuscriptInitialPhaseLit = "planning"
    state: ManuscriptInitialStateLit = "active"
    workspace_ref: Optional[str] = None
    legacy_journal_id: Optional[str] = None


class UpdateManuscriptArgs(ProjectScopedArgs):
    """[ANY] Update native manuscript metadata with revision precondition."""

    operation: Literal["update_manuscript"] = "update_manuscript"

    id: Annotated[
        str,
        Field(description="Canonical man_ id or compatibility jrn_ alias."),
    ]
    expected_revision: Annotated[int, Field(ge=1)]
    title: Annotated[Optional[str], Field(min_length=1)] = None
    abstract: Optional[str] = None
    venue: Optional[str] = None
    workspace_ref: Optional[str] = None

    @model_validator(mode="after")
    def _require_metadata_change(self) -> "UpdateManuscriptArgs":
        changes = self.model_fields_set - {
            "operation",
            "project_id",
            "id",
            "expected_revision",
        }
        if not changes:
            raise ValueError("update_manuscript requires at least one change")
        for required_value in ("title",):
            if required_value in changes and getattr(self, required_value) is None:
                raise ValueError(f"update_manuscript {required_value} cannot be null")
        return self


class UpsertArgumentSpineArgs(ProjectScopedArgs):
    """[DEPRECATED] Human REST/CLI compatibility only; MCP callers use proposals."""

    operation: Literal["upsert_argument_spine"] = "upsert_argument_spine"

    id: Annotated[
        str,
        Field(description="Canonical man_ id or compatibility jrn_ alias."),
    ]
    expected_revision: Annotated[int, Field(ge=1)]
    spine: _ManuscriptArgumentSpineArgs


class ReplaceManuscriptReferenceManifestArgs(ProjectScopedArgs):
    """[ANY] Replace the active citation-key manifest transactionally."""

    operation: Literal["replace_manuscript_reference_manifest"] = (
        "replace_manuscript_reference_manifest"
    )

    id: Annotated[
        str,
        Field(description="Canonical man_ id or compatibility jrn_ alias."),
    ]
    expected_revision: Annotated[int, Field(ge=1)]
    members: Annotated[
        list[ManuscriptReferenceMemberInput],
        Field(
            max_length=2_000,
            description=("Complete active citation set; omission retires a prior member."),
        ),
    ]

    @model_validator(mode="after")
    def _require_unique_reference_bindings(
        self,
    ) -> "ReplaceManuscriptReferenceManifestArgs":
        folded_keys: set[str] = set()
        literature_ids: set[str] = set()
        for member in self.members:
            folded_key = member.citation_key.casefold()
            if folded_key in folded_keys:
                raise ValueError(
                    "replace_manuscript_reference_manifest citation keys "
                    "must be unique case-insensitively"
                )
            if member.literature_id in literature_ids:
                raise ValueError(
                    "replace_manuscript_reference_manifest may bind each "
                    "literature record only once"
                )
            folded_keys.add(folded_key)
            literature_ids.add(member.literature_id)
        return self


class RatifyManuscriptClaimArgs(ProjectScopedArgs):
    """[PI] Bind exact manuscript-claim wording to an active PI decision."""

    operation: Literal["ratify_manuscript_claim"] = "ratify_manuscript_claim"

    id: Annotated[
        str,
        Field(description="Canonical man_ id or compatibility jrn_ alias."),
    ]
    claim_ref: Annotated[
        str,
        Field(
            min_length=1,
            description="Stable mcl_ id or manuscript-local claim key.",
        ),
    ]
    expected_revision: Annotated[int, Field(ge=1)]
    decision_id: Annotated[str, Field(min_length=1)]
    claim_version: Annotated[Optional[int], Field(default=None, ge=1)] = None
    ratified_at: Optional[str] = None


class TransitionManuscriptPhaseArgs(ProjectScopedArgs):
    """[ANY] Run readiness gates and transition the manuscript lifecycle."""

    operation: Literal["transition_manuscript_phase"] = "transition_manuscript_phase"

    id: Annotated[
        str,
        Field(description="Canonical man_ id or compatibility jrn_ alias."),
    ]
    expected_revision: Annotated[int, Field(ge=1)]
    target_phase: ManuscriptReadinessPhaseLit
    target_state: Optional[ManuscriptStateLit] = None


class CreateManuscriptCheckpointArgs(ProjectScopedArgs):
    """[PI] Create a pending native manuscript checkpoint."""

    operation: Literal["create_manuscript_checkpoint"] = "create_manuscript_checkpoint"

    id: Annotated[
        str,
        Field(description="Canonical man_ id or compatibility jrn_ alias."),
    ]
    expected_revision: Annotated[int, Field(ge=1)]
    kind: ManuscriptCheckpointKindLit
    unit_id: Optional[str] = None
    supersedes_id: Optional[str] = None

    @model_validator(mode="after")
    def _validate_checkpoint_scope(self) -> "CreateManuscriptCheckpointArgs":
        if self.kind == "draft_section" and self.unit_id is None:
            raise ValueError("draft_section checkpoints require unit_id")
        if self.kind != "draft_section" and self.unit_id is not None:
            raise ValueError("only draft_section checkpoints may set unit_id")
        return self


class ResolveManuscriptCheckpointArgs(ProjectScopedArgs):
    """[PI] Resolve a pending manuscript checkpoint through a PI decision."""

    operation: Literal["resolve_manuscript_checkpoint"] = "resolve_manuscript_checkpoint"

    checkpoint_id: Annotated[str, Field(min_length=1)]
    expected_revision: Annotated[int, Field(ge=1)]
    decision_id: Annotated[str, Field(min_length=1)]
    status: ManuscriptCheckpointResolutionStatusLit
    resolved_at: Annotated[str, Field(min_length=1)]


class RecordVerificationAttestationArgs(ProjectScopedArgs):
    """[ANY] Append one immutable multidimensional claim verification."""

    operation: Literal["record_verification_attestation"] = "record_verification_attestation"

    id: Annotated[
        str,
        Field(description="Canonical man_ id or compatibility jrn_ alias."),
    ]
    expected_revision: Annotated[int, Field(ge=1)]
    claim_id: Annotated[str, Field(min_length=1)]
    claim_version: Annotated[int, Field(ge=1)]
    overall_verdict: ManuscriptVerificationVerdictLit
    grounding_verdict: ManuscriptVerificationDimensionVerdictLit
    evidence_verdict: ManuscriptVerificationDimensionVerdictLit
    contradiction_verdict: ManuscriptVerificationDimensionVerdictLit
    currency_verdict: ManuscriptVerificationDimensionVerdictLit
    ratification_verdict: ManuscriptVerificationDimensionVerdictLit
    unit_coverage_verdict: ManuscriptVerificationDimensionVerdictLit
    changelog_cursor: Optional[str] = None
    dependency_snapshot: dict[str, Any] = Field(default_factory=dict)
    full_json_payload: dict[str, Any]
    validator_version: Optional[str] = None
    started_at: Annotated[str, Field(min_length=1)]
    completed_at: Annotated[str, Field(min_length=1)]


# --- missions -------------------------------------------------------------


class CreateMissionArgs(ProjectScopedArgs):
    """[BRAIN] Create a mission. Requires ``motivated_by_decision``.

    Provenance discipline: ``motivated_by_decision`` REQUIRED to preserve
    the decision -> mission causality chain (verified by
    ``verb_dispatch.dispatch_mission(action='create')``).
    """

    operation: Literal["create_mission"] = "create_mission"

    # Required body
    objective: Annotated[
        str,
        Field(description="Mission objective (PRIMARY field)."),
    ]
    motivated_by_decision: Annotated[
        str,
        Field(
            description=(
                "Decision ID this mission is grounded in. REQUIRED to "
                "preserve the decision -> mission causality chain."
            ),
        ),
    ]

    # Optional fields
    phase: Annotated[
        Optional[str],
        Field(default="execution", description="Project phase tag."),
    ] = "execution"
    tasks: Annotated[
        Optional[list[dict]],
        Field(
            default=None,
            description="Task breakdown (each: {id, description, ...}).",
        ),
    ] = None
    context: Annotated[
        Optional[str],
        Field(default=None, description="Context paragraph."),
    ] = None
    acceptance_criteria: Annotated[
        Optional[str],
        Field(default=None, description="Acceptance criteria text."),
    ] = None
    scope_boundaries: Annotated[
        Optional[str],
        Field(default=None, description="Scope boundary text."),
    ] = None
    checkpoint_triggers: Annotated[
        Optional[str],
        Field(
            default=None,
            description=(
                "Conditions that should trigger a checkpoint (free-form "
                "string). NOTE: ``rka/models/mission.py`` MissionCreate "
                "declares this as ``str | None`` — list-of-strings will "
                "fail HTTP 422 at the REST boundary."
            ),
        ),
    ] = None
    depends_on: Annotated[
        Optional[str],
        Field(
            default=None,
            description=(
                "Upstream mission dependencies (free-form string). NOTE: "
                "``rka/models/mission.py`` MissionCreate declares this as "
                "``str | None`` — list-of-strings will fail HTTP 422 at "
                "the REST boundary."
            ),
        ),
    ] = None
    tags: Annotated[
        Optional[list[str]],
        Field(default=None, description="Free-form tags."),
    ] = None

    @model_validator(mode="after")
    def _enforce_motivated_by_decision(self) -> "CreateMissionArgs":
        # The non-Optional declaration above already enforces this at the
        # Pydantic schema layer. This defense-in-depth check mirrors
        # dispatch_mission(action='create')'s contract for any caller
        # bypassing the schema.
        if not self.motivated_by_decision:
            raise ValueError(
                "rka_create_mission: motivated_by_decision required to "
                "preserve decision->mission causality"
            )
        return self


# --- claims/clusters ------------------------------------------------------


class CreateClusterArgs(ProjectScopedArgs):
    """[BRAIN] Create a new evidence cluster."""

    operation: Literal["create_cluster"] = "create_cluster"

    label: Annotated[
        str,
        Field(description="Cluster label (PRIMARY field)."),
    ]
    research_question_id: Annotated[
        Optional[str],
        Field(
            default=None,
            description="Research-question decision ID this cluster supports.",
        ),
    ] = None
    synthesis: Annotated[
        Optional[str],
        Field(default=None, description="Initial synthesis text."),
    ] = None
    confidence: Annotated[
        ClusterConfLit,
        Field(default="emerging", description="Cluster confidence level."),
    ] = "emerging"
    claim_ids: Annotated[
        Optional[list[str]],
        Field(default=None, description="Claim IDs to attach at creation."),
    ] = None


class _ExtractClaimItem(BaseModel):
    """One item in an ``extract_claims`` call.

    Each claim has ``text`` (required) and ``claim_type`` (Literal-typed
    enum). Additional metadata fields (e.g. ``confidence``,
    ``related_literature``) are passed through to the legacy REST adapter
    via ``extra='allow'``.
    """

    model_config = ConfigDict(extra="allow", use_enum_values=True)

    text: Annotated[
        str,
        Field(description="Claim text."),
    ]
    claim_type: Annotated[
        ClaimTypeLit,
        Field(
            description=(
                "Type of claim: hypothesis | evidence | method | result | observation | assumption."
            ),
        ),
    ]
    epistemic_kind: Annotated[
        Optional[EpistemicKindLit],
        Field(
            default=None,
            description="Explicit interpretation kind; inferred from claim_type when omitted.",
        ),
    ] = None
    source_offset_start: Annotated[Optional[int], Field(default=None, ge=0)] = None
    source_offset_end: Annotated[Optional[int], Field(default=None, ge=0)] = None
    scope_conditions: Annotated[Optional[list[str]], Field(default=None)] = None
    uncertainty: Annotated[
        InterpretationUncertaintyLit,
        Field(default="unknown", description="Known uncertainty at extraction time."),
    ] = "unknown"
    uncertainty_note: Annotated[Optional[str], Field(default=None)] = None
    falsifier: Annotated[Optional[str], Field(default=None)] = None
    extraction_model: Annotated[Optional[str], Field(default=None)] = None


class ExtractClaimsArgs(ProjectScopedArgs):
    """[BRAIN] Extract claims from an existing entry."""

    operation: Literal["extract_claims"] = "extract_claims"

    entry_id: Annotated[
        str,
        Field(description="Journal entry ID to extract claims from."),
    ]
    claims: Annotated[
        list[_ExtractClaimItem],
        Field(
            min_length=1,
            description=(
                "Non-empty list of claim items. Each item has 'text' + "
                "'claim_type' (hypothesis | evidence | method | result | "
                "observation | assumption)."
            ),
        ),
    ]


class CreateInterpretationCandidateArgs(ProjectScopedArgs):
    """[BRAIN/EXECUTOR/PI] Stage one atomic source interpretation."""

    operation: Literal["create_interpretation_candidate"] = "create_interpretation_candidate"
    source_type: Annotated[InterpretationSourceLit, Field(description="Source entity type.")]
    source_id: Annotated[str, Field(min_length=1, max_length=128)]
    locator_kind: Annotated[InterpretationLocatorLit, Field(description="Exact locator shape.")]
    statement: Annotated[str, Field(min_length=1, max_length=20_000)]
    epistemic_kind: Annotated[
        EpistemicKindLit, Field(description="Meaning of this staged statement.")
    ]
    created_by: Annotated[InterpretationActorLit, Field(description="Candidate author/extractor.")]
    extraction_tool: Annotated[str, Field(min_length=1, max_length=256)]
    locator_start: Annotated[Optional[int], Field(default=None, ge=0)] = None
    locator_end: Annotated[Optional[int], Field(default=None, ge=0)] = None
    locator_value: Annotated[Optional[str], Field(default=None, max_length=2048)] = None
    scope_conditions: Annotated[Optional[list[str]], Field(default=None, max_length=100)] = None
    uncertainty: Annotated[InterpretationUncertaintyLit, Field(default="unknown")] = "unknown"
    uncertainty_note: Annotated[Optional[str], Field(default=None, max_length=4000)] = None
    falsifier: Annotated[Optional[str], Field(default=None, max_length=10_000)] = None
    proposed_claim_type: Annotated[Optional[ClaimTypeLit], Field(default=None)] = None
    extraction_model: Annotated[Optional[str], Field(default=None, max_length=256)] = None

    @model_validator(mode="after")
    def _validate_locator(self) -> "CreateInterpretationCandidateArgs":
        if self.locator_kind in {"text_offset", "page", "line_range"}:
            if self.locator_start is None:
                raise ValueError(f"{self.locator_kind} requires locator_start")
            if self.locator_end is not None and self.locator_end < self.locator_start:
                raise ValueError("locator_end must be >= locator_start")
        elif not self.locator_value or not self.locator_value.strip():
            raise ValueError(f"{self.locator_kind} requires locator_value")
        return self


class RegisterSourceArgs(ProjectScopedArgs):
    """[ANY] Register local bytes or a stable locator without canonical admission."""

    operation: Literal["register_source"] = "register_source"
    source_kind: Annotated[RegisteredSourceKindLit, Field(description="Source class.")]
    registered_by: Annotated[RegisteredSourceActorLit, Field(description="Registration actor.")]
    title: Annotated[Optional[str], Field(default=None, max_length=1000)] = None
    filepath: Annotated[Optional[str], Field(default=None, max_length=8192)] = None
    pasted_text: Annotated[Optional[str], Field(default=None, max_length=10_000_000)] = None
    stable_locator: Annotated[Optional[str], Field(default=None, max_length=8192)] = None
    mime: Annotated[Optional[str], Field(default=None, max_length=256)] = None
    expected_content_hash: Annotated[Optional[str], Field(default=None, max_length=64)] = None
    ownership_kind: Annotated[
        RegisteredSourceOwnershipLit, Field(default="unknown")
    ] = "unknown"
    ownership_note: Annotated[Optional[str], Field(default=None, max_length=10_000)] = None
    provenance: Annotated[Optional[dict[str, Any]], Field(default=None)] = None

    @model_validator(mode="after")
    def _validate_source_input(self) -> "RegisterSourceArgs":
        supplied = int(self.filepath is not None) + int(self.pasted_text is not None)
        if supplied > 1:
            raise ValueError("provide at most one of filepath and pasted_text")
        if self.source_kind == "file" and not self.filepath:
            raise ValueError("file sources require filepath")
        if self.source_kind == "pasted_text" and self.pasted_text is None:
            raise ValueError("pasted_text sources require pasted_text")
        if self.pasted_text is not None and self.source_kind != "pasted_text":
            raise ValueError(
                "pasted_text bytes are only valid for source_kind='pasted_text'"
            )
        if self.source_kind in {"url", "repository", "zotero"} and not self.stable_locator:
            raise ValueError(f"{self.source_kind} sources require stable_locator")
        if supplied == 0 and not self.stable_locator:
            raise ValueError("a source requires bytes or a stable_locator")
        return self


class AdmitSourceInterpretationArgs(ProjectScopedArgs):
    """[BRAIN/PI] Admit one grounded source interpretation to an existing target."""

    operation: Literal["admit_source_interpretation"] = "admit_source_interpretation"
    source_id: Annotated[str, Field(description="Registered src_ source id.")]
    candidate_id: Annotated[str, Field(description="Artifact-backed icd_ candidate id.")]
    expected_revision: Annotated[int, Field(ge=1)]
    target_type: Annotated[SourceAdmissionTargetLit, Field(description="Canonical target type.")]
    target_id: Annotated[str, Field(description="Existing canonical target id.")]
    actor: Annotated[SourceAdmissionActorLit, Field(description="Reviewing actor.")]
    reason: Annotated[str, Field(min_length=1, max_length=10_000)]
    grounding_verified: Annotated[
        Literal[True], Field(description="Must be true after source review.")
    ]


class AddInterpretationHintArgs(ProjectScopedArgs):
    """[BRAIN/PI] Attach a duplicate/conflict review hint."""

    operation: Literal["add_interpretation_hint"] = "add_interpretation_hint"
    id: Annotated[str, Field(description="Candidate receiving the hint.")]
    related_candidate_id: Annotated[str, Field(description="Related candidate id.")]
    kind: Annotated[InterpretationHintKindLit, Field(description="Duplicate or conflict.")]
    rationale: Annotated[str, Field(min_length=1, max_length=10_000)]
    created_by: Annotated[InterpretationActorLit, Field(description="Hint author.")]
    expected_revision: Annotated[int, Field(ge=1)]
    confidence: Annotated[float, Field(default=0.5, ge=0.0, le=1.0)] = 0.5


class TriageInterpretationCandidateArgs(ProjectScopedArgs):
    """[BRAIN/PI] Review, promote, reopen, or revoke one candidate."""

    operation: Literal["triage_interpretation_candidate"] = "triage_interpretation_candidate"
    id: Annotated[str, Field(description="Interpretation candidate id.")]
    action: Annotated[InterpretationTriageActionLit, Field(description="Review action.")]
    expected_revision: Annotated[int, Field(ge=1)]
    actor: Annotated[InterpretationReviewActorLit, Field(description="Reviewing actor.")]
    reason: Annotated[Optional[str], Field(default=None, max_length=10_000)] = None
    target_candidate_id: Annotated[Optional[str], Field(default=None, max_length=128)] = None
    target_entity_id: Annotated[Optional[str], Field(default=None, max_length=128)] = None
    grounding_verified: Annotated[bool, Field(default=False)] = False
    claim_confidence: Annotated[float, Field(default=0.5, ge=0.0, le=1.0)] = 0.5
    evidence_role: Annotated[Optional[ClaimEvidenceRoleLit], Field(default=None)] = None

    @model_validator(mode="after")
    def _validate_triage(self) -> "TriageInterpretationCandidateArgs":
        if self.action != "start_review" and not self.reason:
            raise ValueError(f"{self.action} requires reason")
        if self.action == "merge" and not self.target_candidate_id:
            raise ValueError("merge requires target_candidate_id")
        if self.action == "promote" and not self.grounding_verified:
            raise ValueError("promote requires grounding_verified=true")
        if self.action == "classify_evidence":
            if not self.target_entity_id:
                raise ValueError("classify_evidence requires target_entity_id")
            if not self.evidence_role:
                raise ValueError("classify_evidence requires evidence_role")
        if self.action == "revoke_evidence" and not self.target_entity_id:
            raise ValueError("revoke_evidence requires target_entity_id")
        return self


class _ExperimentPlanArgs(BaseModel):
    """Shared exact plan and repository-snapshot fields."""

    objective: Annotated[str, Field(min_length=1, max_length=20_000)]
    hypothesis: Annotated[Optional[str], Field(default=None, max_length=20_000)] = None
    protocol: Annotated[str, Field(min_length=1, max_length=100_000)]
    conditions: Annotated[list[str], Field(default_factory=list, max_length=500)]
    variables: Annotated[list[str], Field(default_factory=list, max_length=500)]
    metrics: Annotated[list[str], Field(default_factory=list, max_length=500)]
    baselines: Annotated[list[str], Field(default_factory=list, max_length=500)]
    success_criteria: Annotated[list[str], Field(default_factory=list, max_length=500)]
    failure_criteria: Annotated[list[str], Field(default_factory=list, max_length=500)]
    repository_url: Annotated[Optional[str], Field(default=None, max_length=2048)] = None
    commit_sha: Annotated[Optional[str], Field(default=None, max_length=64)] = None
    working_tree_state: Annotated[Optional[WorkingTreeStateLit], Field(default=None)] = None


class CreateExperimentArgs(ProjectScopedArgs, _ExperimentPlanArgs):
    """[BRAIN/EXECUTOR/PI] Create an experiment with immutable plan version 1."""

    operation: Literal["create_experiment"] = "create_experiment"
    title: Annotated[str, Field(min_length=1, max_length=1000)]
    created_by: Annotated[ExperimentActorLit, Field(description="Experiment author.")]
    reason: Annotated[str, Field(min_length=1, max_length=10_000)]


class AppendExperimentPlanArgs(ProjectScopedArgs, _ExperimentPlanArgs):
    """[BRAIN/EXECUTOR/PI] Append a revision-guarded immutable plan version."""

    operation: Literal["append_experiment_plan"] = "append_experiment_plan"
    id: Annotated[str, Field(description="Canonical exp_ id.")]
    expected_revision: Annotated[int, Field(ge=1)]
    created_by: Annotated[ExperimentActorLit, Field(description="Plan author.")]
    reason: Annotated[str, Field(min_length=1, max_length=10_000)]


class TransitionExperimentArgs(ProjectScopedArgs):
    """[BRAIN/EXECUTOR/PI] Transition an experiment lifecycle state."""

    operation: Literal["transition_experiment"] = "transition_experiment"
    id: Annotated[str, Field(description="Canonical exp_ id.")]
    expected_revision: Annotated[int, Field(ge=1)]
    target_status: Annotated[ExperimentStatusLit, Field(description="Target lifecycle state.")]
    actor: Annotated[ExperimentActorLit, Field(description="Transition actor.")]
    reason: Annotated[str, Field(min_length=1, max_length=10_000)]


class CreateExperimentRunArgs(ProjectScopedArgs):
    """[EXECUTOR] Queue a run bound to one immutable experiment plan version."""

    operation: Literal["create_experiment_run"] = "create_experiment_run"
    experiment_id: Annotated[str, Field(description="Canonical exp_ id.")]
    plan_version: Annotated[int, Field(ge=1)]
    label: Annotated[str, Field(min_length=1, max_length=1000)]
    runner: Annotated[ExperimentRunKindLit, Field(description="Execution environment kind.")]
    command: Annotated[Optional[str], Field(default=None, max_length=100_000)] = None
    config: Annotated[dict[str, Any], Field(default_factory=dict)]
    environment: Annotated[dict[str, Any], Field(default_factory=dict)]
    repository_url: Annotated[Optional[str], Field(default=None, max_length=2048)] = None
    commit_sha: Annotated[Optional[str], Field(default=None, max_length=64)] = None
    working_tree_state: Annotated[Optional[WorkingTreeStateLit], Field(default=None)] = None
    created_by: Annotated[ExperimentActorLit, Field(description="Run creator.")]
    reason: Annotated[str, Field(min_length=1, max_length=10_000)]


class TransitionExperimentRunArgs(ProjectScopedArgs):
    """[EXECUTOR] Append a revision-guarded run lifecycle event."""

    operation: Literal["transition_experiment_run"] = "transition_experiment_run"
    id: Annotated[str, Field(description="Canonical run_ id.")]
    expected_revision: Annotated[int, Field(ge=1)]
    action: Annotated[ExperimentRunActionLit, Field(description="Lifecycle action.")]
    actor: Annotated[ExperimentActorLit, Field(description="Transition actor.")]
    reason: Annotated[str, Field(min_length=1, max_length=10_000)]
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    exit_code: Optional[int] = None
    failure_summary: Annotated[Optional[str], Field(default=None, max_length=20_000)] = None


class RecordExperimentObservationArgs(ProjectScopedArgs):
    """[EXECUTOR/BRAIN/PI] Record one immutable observation from a run.

    Value shape is enforced authoritatively by the experiment domain model:
    provide at most one of ``value_real`` and ``value_text``. ``metric``,
    ``comparison``, and ``test`` observations require one of them;
    ``qualitative`` and ``failure`` observations require ``value_text``.
    """

    operation: Literal["record_experiment_observation"] = "record_experiment_observation"
    run_id: Annotated[str, Field(description="Canonical run_ id.")]
    name: Annotated[str, Field(min_length=1, max_length=1000)]
    kind: Annotated[
        ExperimentObservationKindLit,
        Field(
            description=(
                "Observation shape. metric/comparison/test require one value; "
                "qualitative/failure require value_text."
            )
        ),
    ]
    direction: Annotated[
        ExperimentObservationDirectionLit,
        Field(description="Observed direction; independent from run success."),
    ]
    summary: Annotated[str, Field(min_length=1, max_length=50_000)]
    value_real: Annotated[
        Optional[float],
        Field(
            default=None,
            description=(
                "Numeric value. Mutually exclusive with value_text; required "
                "as one of the two value fields for metric/comparison/test."
            ),
        ),
    ] = None
    value_text: Annotated[
        Optional[str],
        Field(
            default=None,
            max_length=100_000,
            description=(
                "Text value. Mutually exclusive with value_real; required for "
                "qualitative/failure and accepted as the value for "
                "metric/comparison/test."
            ),
        ),
    ] = None
    unit: Annotated[Optional[str], Field(default=None, max_length=256)] = None
    sample_size: Annotated[Optional[int], Field(default=None, ge=0)] = None
    uncertainty_note: Annotated[Optional[str], Field(default=None, max_length=20_000)] = None
    observed_at: Annotated[str, Field(min_length=1)]
    recorded_by: Annotated[ExperimentActorLit, Field(description="Observation recorder.")]


class AddEvidenceLocatorArgs(ProjectScopedArgs):
    """[EXECUTOR/BRAIN/PI] Bind an observation to exact immutable evidence bytes."""

    operation: Literal["add_evidence_locator"] = "add_evidence_locator"
    observation_id: Annotated[str, Field(description="Canonical obs_ id.")]
    source_kind: Annotated[EvidenceSourceKindLit, Field(description="Artifact or repository.")]
    artifact_id: Optional[str] = None
    repository_url: Optional[str] = None
    commit_sha: Optional[str] = None
    relative_path: Optional[str] = None
    locator_kind: Annotated[EvidenceLocatorKindLit, Field(description="Exact sub-resource locator.")]
    locator_start: Annotated[Optional[int], Field(default=None, ge=0)] = None
    locator_end: Annotated[Optional[int], Field(default=None, ge=0)] = None
    locator_value: Optional[str] = None
    content_hash: Optional[str] = None
    label: Optional[str] = None
    created_by: Annotated[ExperimentActorLit, Field(description="Locator author.")]


class CreatePlanningBranchArgs(ProjectScopedArgs, PlanningBranchCreate):
    """[BRAIN/EXECUTOR/PI] Create a revision-pinned manuscript-planning branch."""

    operation: Literal["create_planning_branch"] = "create_planning_branch"
    created_by: Annotated[PlanningActorLit, Field(description="Branch author.")]


class TransitionPlanningBranchArgs(ProjectScopedArgs, PlanningBranchTransition):
    """[BRAIN/EXECUTOR/PI] Select, activate, archive, or supersede a branch."""

    operation: Literal["transition_planning_branch"] = "transition_planning_branch"
    id: Annotated[str, Field(min_length=1, description="Canonical mpb_ id.")]
    target_state: Annotated[PlanningBranchStateLit, Field(description="Target branch state.")]
    actor: Annotated[PlanningActorLit, Field(description="Transition actor.")]


class AppendPlanningArtifactVersionArgs(
    ProjectScopedArgs, PlanningArtifactVersionAppend
):
    """[BRAIN/EXECUTOR/PI] Append one immutable typed planning-artifact version."""

    operation: Literal["append_planning_artifact_version"] = (
        "append_planning_artifact_version"
    )
    id: Annotated[str, Field(min_length=1, description="Canonical mpb_ branch id.")]
    stage_type: Annotated[PlanningStageLit, Field(description="Closed planning stage.")]
    lifecycle: Annotated[PlanningLifecycleLit, Field(description="Artifact lifecycle.")] = (
        "candidate"
    )
    origin: Annotated[PlanningOriginLit, Field(description="Version origin.")]
    readiness_state: Annotated[
        PlanningReadinessLit, Field(description="Mechanical readiness state.")
    ] = "in_progress"
    created_by: Annotated[PlanningActorLit, Field(description="Version author.")]


class PromotePlanningResearchQuestionArgs(
    ProjectScopedArgs, PlanningResearchQuestionPromotion
):
    """[PI] Promote one selected RQ candidate into a PI decision."""

    operation: Literal["promote_planning_rq"] = "promote_planning_rq"
    id: Annotated[str, Field(min_length=1, description="Canonical mpb_ branch id.")]


class PreparePlanningContributionArgs(
    ProjectScopedArgs, PlanningContributionProposalPrepare
):
    """[BRAIN/EXECUTOR/PI] Prepare, but never apply, a contribution proposal."""

    operation: Literal["prepare_planning_contribution"] = (
        "prepare_planning_contribution"
    )
    id: Annotated[str, Field(min_length=1, description="Canonical mpb_ branch id.")]


class RatifyPlanningContributionArgs(
    ProjectScopedArgs, PlanningContributionRatification
):
    """[PI] Ratify exact applied contribution wording against a PI decision."""

    operation: Literal["ratify_planning_contribution"] = (
        "ratify_planning_contribution"
    )
    id: Annotated[str, Field(min_length=1, description="Canonical mpb_ branch id.")]


class CreatePlanningEvaluationMissionArgs(
    ProjectScopedArgs, PlanningEvaluationMissionCreate
):
    """[BRAIN/EXECUTOR/PI] Create one mission from a missing evidence slot."""

    operation: Literal["create_planning_evaluation_mission"] = (
        "create_planning_evaluation_mission"
    )
    id: Annotated[str, Field(min_length=1, description="Canonical mpb_ branch id.")]


class PreparePlanningEvaluationResultArgs(
    ProjectScopedArgs, PlanningEvaluationResultProposalPrepare
):
    """[BRAIN/EXECUTOR/PI] Prepare one exact bounded result-unit proposal."""

    operation: Literal["prepare_planning_evaluation_result"] = (
        "prepare_planning_evaluation_result"
    )
    id: Annotated[str, Field(min_length=1, description="Canonical mpb_ branch id.")]


class PrepareManuscriptOutlineProposalArgs(ProjectScopedArgs, OutlineProposalRequest):
    """[BRAIN/EXECUTOR] Prepare an attributed outline proposal for human review."""

    operation: Literal["prepare_manuscript_outline_proposal"] = (
        "prepare_manuscript_outline_proposal"
    )
    origin: Annotated[
        SemanticPatchAIOriginLit,
        Field(description="AI provider path; typed MCP calls cannot claim human origin."),
    ]
    provider: Annotated[str, Field(min_length=1, description="AI provider name.")]
    model: Annotated[str, Field(min_length=1, description="AI model identifier.")]
    boundary: Annotated[
        SemanticPatchAIBoundaryLit,
        Field(description="Outbound data boundary recorded by the context manifest."),
    ]
    context_manifest_id: Annotated[
        str,
        Field(min_length=1, description="Matching pcm_ context disclosure manifest."),
    ]
    action: Annotated[
        ManuscriptOutlineActionLit,
        Field(description="Deterministic structural outline transformation."),
    ]
    id: Annotated[str, Field(min_length=1, description="Canonical man_ manuscript id.")]


class PrepareSemanticPatchContextArgs(ProjectScopedArgs, ContextManifestCreate):
    """[ANY] Persist the exact context disclosure before an AI call."""

    operation: Literal["prepare_semantic_patch_context"] = "prepare_semantic_patch_context"
    origin: Annotated[SemanticPatchAIOriginLit, Field(description="AI provider path.")]
    boundary: Annotated[
        SemanticPatchAIBoundaryLit, Field(description="Outbound data boundary.")
    ]


class CreateSemanticPatchProposalArgs(ProjectScopedArgs, SemanticPatchProposalCreate):
    """[BRAIN/EXECUTOR] Persist an attributed AI proposal without applying it."""

    operation: Literal["create_semantic_patch_proposal"] = "create_semantic_patch_proposal"
    origin: Annotated[
        SemanticPatchAIOriginLit,
        Field(description="AI provider path; typed MCP calls cannot claim human origin."),
    ]
    created_by: Annotated[SemanticPatchActorLit, Field(description="Proposal author.")]
    provider: Annotated[str, Field(min_length=1, description="AI provider name.")]
    model: Annotated[str, Field(min_length=1, description="AI model identifier.")]
    boundary: Annotated[
        SemanticPatchAIBoundaryLit, Field(description="Outbound data boundary.")
    ]
    context_manifest_id: Annotated[
        str,
        Field(min_length=1, description="Matching pcm_ context disclosure manifest."),
    ]


class ApplySemanticPatchProposalArgs(ProjectScopedArgs, SemanticPatchProposalTransition):
    """[PI/WEB_UI] Explicitly apply one reviewed, current proposal."""

    operation: Literal["apply_semantic_patch_proposal"] = "apply_semantic_patch_proposal"
    id: Annotated[str, Field(min_length=1, description="Canonical spp_ id.")]
    actor: Annotated[SemanticPatchReviewerLit, Field(description="Applying reviewer.")]


class RejectSemanticPatchProposalArgs(ProjectScopedArgs, SemanticPatchProposalTransition):
    """[PI/WEB_UI] Reject a proposal without mutating its targets."""

    operation: Literal["reject_semantic_patch_proposal"] = "reject_semantic_patch_proposal"
    id: Annotated[str, Field(min_length=1, description="Canonical spp_ id.")]
    actor: Annotated[SemanticPatchReviewerLit, Field(description="Rejecting reviewer.")]


class GenerateLMStudioSemanticPatchArgs(ProjectScopedArgs, LMStudioProposalRequest):
    """[ANY] Generate a local-machine proposal; never auto-apply it."""

    operation: Literal["generate_lm_studio_semantic_patch"] = (
        "generate_lm_studio_semantic_patch"
    )
    created_by: Annotated[SemanticPatchActorLit, Field(description="Proposal author.")]


class SetClaimScopeArgs(ProjectScopedArgs):
    """[BRAIN/PI] Append a revision-guarded canonical claim-scope contract."""

    operation: Literal["set_claim_scope"] = "set_claim_scope"
    claim_id: Annotated[str, Field(min_length=1, description="Canonical clm_ id.")]
    expected_revision: Annotated[int, Field(ge=0)]
    actor: Annotated[ClaimScopeActorLit, Field(description="Scope contract author.")]
    reason: Annotated[str, Field(min_length=1, max_length=10_000)]
    conditions: Annotated[
        list[ClaimScopeCondition],
        Field(default_factory=list, max_length=100),
    ]
    uncertainty: Annotated[
        ClaimScopeUncertaintyLit,
        Field(default="unknown"),
    ] = "unknown"
    uncertainty_note: Annotated[
        Optional[str],
        Field(default=None, max_length=4000),
    ] = None
    extension_policy: Annotated[
        Optional[ClaimScopeExtensionPolicyLit],
        Field(default=None),
    ] = None
    allowed_extensions: Annotated[
        list[str],
        Field(default_factory=list, max_length=100),
    ]
    prohibited_extensions: Annotated[
        list[str],
        Field(default_factory=list, max_length=100),
    ]
    falsifier_status: Annotated[
        ClaimFalsifierStatusLit,
        Field(default="unknown"),
    ] = "unknown"
    falsifier: Annotated[
        Optional[str],
        Field(default=None, max_length=10_000),
    ] = None
    falsifier_rationale: Annotated[
        Optional[str],
        Field(default=None, max_length=4000),
    ] = None
    disconfirming_claim_ids: Annotated[
        list[str],
        Field(default_factory=list, max_length=200),
    ]
    review_status: Annotated[
        ClaimScopeReviewStatusLit,
        Field(default="draft"),
    ] = "draft"

    @model_validator(mode="after")
    def _validate_scope_contract(self) -> "SetClaimScopeArgs":
        # Keep MCP and REST cross-field semantics identical.
        from rka.models.claim import ClaimScopeWrite

        ClaimScopeWrite.model_validate(
            self.model_dump(exclude={"operation", "project_id", "claim_id"})
        )
        return self


# --- checkpoint / gates ---------------------------------------------------


class CreateGateArgs(ProjectScopedArgs):
    """[BRAIN] Create a validation gate (problem_framing, plan_validation,
    evidence_review, synthesis_validation).
    """

    operation: Literal["create_gate"] = "create_gate"

    mission_id: Annotated[
        str,
        Field(description="Mission ID this gate belongs to."),
    ]
    gate_type: Annotated[
        GateTypeLit,
        Field(description="Validation-gate subtype."),
    ]
    deliverables: Annotated[
        list[str],
        Field(min_length=1, description="Deliverables required for the gate."),
    ]
    pass_criteria: Annotated[
        list[str],
        Field(min_length=1, description="Criteria for passing the gate."),
    ]
    assumptions_to_verify: Annotated[
        Optional[list[str]],
        Field(default=None, description="Assumptions to verify at the gate."),
    ] = None


# --- session / project ----------------------------------------------------


class CreateProjectArgs(UnscopedArgs):
    """[PI] Bootstrap a new RKA project (UNSCOPED — does not require
    project_id).
    """

    operation: Literal["create_project"] = "create_project"

    name: Annotated[
        str,
        Field(description="Project name (PRIMARY field)."),
    ]
    description: Annotated[
        Optional[str],
        Field(default=None, description="Free-form project description."),
    ] = None


# --- hooks ----------------------------------------------------------------


class HookAddArgs(ProjectScopedArgs):
    """[PI] Add a brain_notify lifecycle hook.

    The service rejects unsupported handler types for every transport.
    The string input and historical validator remain for wire compatibility;
    they do not authorize SQL, webhooks, or scheduled-only MCP execution.
    """

    operation: Literal["hook_add"] = "hook_add"

    event: Annotated[
        str,
        Field(description="Lifecycle event, e.g. 'session_start' or 'post_journal_create'."),
    ]
    handler_type: Annotated[
        str,
        Field(description="Only 'brain_notify' is executable; other handlers are unsupported."),
    ]
    handler_config: Annotated[
        dict[str, Any],
        Field(description="Handler-specific configuration."),
    ]
    name: Annotated[
        str,
        Field(description="Human-readable hook name."),
    ]
    enabled: Annotated[
        bool,
        Field(
            default=True,
            description="Whether the hook is enabled at creation.",
        ),
    ] = True
    created_by: Annotated[
        HookCreatedByLit,
        Field(default="pi", description="Actor of record for hook creation."),
    ] = "pi"

    @model_validator(mode="after")
    def _enforce_handler_config_shape(self) -> "HookAddArgs":
        # Historical shape validation is retained for compatibility.
        # Passing this validator does not grant execution: the service
        # rejects every handler except brain_notify.
        required_by_type: dict[str, tuple[str, ...]] = {
            "webhook": ("url",),
        }
        required_keys = required_by_type.get(self.handler_type)
        if required_keys is None:
            return self
        missing = [k for k in required_keys if not self.handler_config.get(k)]
        if missing:
            raise ValueError(
                f"hook_add: handler_type={self.handler_type!r} requires "
                f"handler_config keys {list(required_keys)} "
                f"(missing: {missing})."
            )
        return self


# ---------------------------------------------------------------------------
# Batch B partial union (record/create + native manuscript domain)
# ---------------------------------------------------------------------------
#
# The full rka_execute union is assembled below
# from per-batch contributions. This partial union exposes Batch B's
# slice so unit tests can verify it in isolation.

BatchBExecuteUnion = Annotated[
    Union[
        RecordNoteArgs,
        IngestDocumentArgs,
        RecordDecisionArgs,
        RecordLiteratureArgs,
        ImportBibtexArgs,
        BatchImportArgs,
        RegisterManuscriptArgs,
        CreateManuscriptArgs,
        UpdateManuscriptArgs,
        UpsertArgumentSpineArgs,
        ReplaceManuscriptReferenceManifestArgs,
        RatifyManuscriptClaimArgs,
        TransitionManuscriptPhaseArgs,
        CreateManuscriptCheckpointArgs,
        ResolveManuscriptCheckpointArgs,
        RecordVerificationAttestationArgs,
        CreateMissionArgs,
        CreateClusterArgs,
        CreateProjectArgs,
        CreateGateArgs,
        HookAddArgs,
        ExtractClaimsArgs,
        CreateInterpretationCandidateArgs,
        RegisterSourceArgs,
        AdmitSourceInterpretationArgs,
        AddInterpretationHintArgs,
        TriageInterpretationCandidateArgs,
        SetClaimScopeArgs,
        CreateExperimentArgs,
        AppendExperimentPlanArgs,
        TransitionExperimentArgs,
        CreateExperimentRunArgs,
        TransitionExperimentRunArgs,
        RecordExperimentObservationArgs,
        AddEvidenceLocatorArgs,
        CreatePlanningBranchArgs,
        TransitionPlanningBranchArgs,
        AppendPlanningArtifactVersionArgs,
        PromotePlanningResearchQuestionArgs,
        PreparePlanningContributionArgs,
        RatifyPlanningContributionArgs,
        CreatePlanningEvaluationMissionArgs,
        PreparePlanningEvaluationResultArgs,
        PrepareManuscriptOutlineProposalArgs,
        PrepareSemanticPatchContextArgs,
        CreateSemanticPatchProposalArgs,
        ApplySemanticPatchProposalArgs,
        RejectSemanticPatchProposalArgs,
        GenerateLMStudioSemanticPatchArgs,
    ],
    Field(discriminator="operation"),
]


# =============================================================================
# Batch D — REVIEW / MAINTENANCE / HOOKS / WORKSPACE operations (14 models)
# =============================================================================
#
# Operations (all rka_execute, all project-scoped):
#   Claims:           assign_claims_to_cluster, split_cluster,
#                     merge_clusters, review_claims, review_cluster,
#                     resolve_contradiction
#   Hooks lifecycle:  hook_enable, hook_disable, hook_delete
#   Notifications:    brain_notifications_clear
#   Workspace:        bootstrap_workspace, scan_workspace
#   Maintenance:      flag_stale, eviction_sweep
#
# Source-of-truth signatures: ``rka/mcp/operations_schema.py`` lines
# 2180-2664. Each model below mirrors the per-op
# ``required_fields + optional_fields + enums`` from that schema.


# ---------------------------------------------------------------------------
# Claims curation — assign / split / merge / review_claims / review_cluster /
# resolve_contradiction
# ---------------------------------------------------------------------------


class AssignClaimsToClusterArgs(ProjectScopedArgs):
    """[BRAIN] Attach a set of claims to an existing evidence cluster.

    Non-empty ``claim_ids`` is enforced by Pydantic ``min_length=1`` so
    the no-op case (``claim_ids=[]``) is rejected pre-dispatch.

    Related: ``create_cluster``, ``split_cluster``, ``merge_clusters``.
    """

    operation: Literal["assign_claims_to_cluster"] = "assign_claims_to_cluster"

    cluster_id: Annotated[
        str,
        Field(description="Target cluster ID (``ecl_...``)."),
    ]
    claim_ids: Annotated[
        list[str],
        Field(
            min_length=1,
            description=(
                "Claim IDs to attach (``clm_...``). Must be non-empty; "
                "an empty list would be a no-op."
            ),
        ),
    ]


class SplitClusterArgs(ProjectScopedArgs):
    """[BRAIN] Split a cluster into multiple smaller clusters.

    ``new_clusters`` is a list of ``{label, claim_ids}`` dicts; the
    service layer validates each entry's shape. Pydantic ``min_length=2``
    enforces the structural minimum — splitting into a single bucket is
    a rename, not a split.

    Related: ``merge_clusters``, ``assign_claims_to_cluster``.
    """

    operation: Literal["split_cluster"] = "split_cluster"

    source_id: Annotated[
        str,
        Field(description="Source cluster ID to split (``ecl_...``)."),
    ]
    new_clusters: Annotated[
        list[dict[str, Any]],
        Field(
            min_length=2,
            description=(
                "List of ``{label, claim_ids}`` dicts describing the new "
                "clusters. Must contain at least 2 entries (splitting "
                "into 1 bucket is a rename, not a split)."
            ),
        ),
    ]


class MergeClustersArgs(ProjectScopedArgs):
    """[BRAIN] Merge multiple clusters into a new combined cluster.

    ``source_ids`` requires >= 2 entries — merging a single cluster is a
    no-op. Pydantic ``min_length=2`` enforces this at the schema layer,
    closing the spec's named ``MergeClustersArgs.model_validator``
    structural-minimum invariant.

    Related: ``split_cluster``, ``create_cluster``.
    """

    operation: Literal["merge_clusters"] = "merge_clusters"

    source_ids: Annotated[
        list[str],
        Field(
            min_length=2,
            description=(
                "Source cluster IDs to merge (``ecl_...``). Must contain "
                "at least 2 entries (single-cluster merge is a no-op)."
            ),
        ),
    ]
    target_label: Annotated[
        str,
        Field(description="Label for the merged target cluster."),
    ]

    target_synthesis: Annotated[
        Optional[str],
        Field(
            default=None,
            description="Optional synthesis statement for the merged cluster.",
        ),
    ] = None
    research_question_id: Annotated[
        Optional[str],
        Field(
            default=None,
            description=(
                "Optional research-question ID to link the merged cluster "
                "to (``dec_...`` with kind=``research_question``)."
            ),
        ),
    ] = None


class ReviewClaimsArgs(ProjectScopedArgs):
    """[BRAIN] Curate grounding and/or evidence assessment for claims.

    ``action`` is the newly-promoted ``ReviewActionLit`` (``approve`` /
    ``reject`` / ``adjust``). Legacy ``approve`` and ``reject`` retain
    their extraction-curation behavior on ``verified``/``stale``.
    Scientific support is never inferred from those actions; callers must
    set ``evidence_status`` explicitly.

    Related: ``claims``, ``review_cluster``.
    """

    operation: Literal["review_claims"] = "review_claims"

    claim_ids: Annotated[
        list[str],
        Field(
            min_length=1,
            description=("Claim IDs to review (``clm_...``). Must be non-empty."),
        ),
    ]

    action: Annotated[
        ReviewActionLit,
        Field(
            default="approve",
            description=(
                "Review action. ``approve`` confirms the claim is grounded "
                "in its source, ``reject`` retires the "
                "extraction, and ``adjust`` applies one or both explicit "
                "overrides. None of these actions implicitly asserts "
                "scientific evidence support."
            ),
        ),
    ] = "approve"
    confidence_override: Annotated[
        Optional[float],
        Field(
            default=None,
            ge=0.0,
            le=1.0,
            description=(
                "Override the claim's numeric confidence (0.0-1.0). Used with action='adjust'."
            ),
        ),
    ] = None
    evidence_status: Annotated[
        Optional[EvidenceStatusLit],
        Field(
            default=None,
            description=(
                "Explicit scientific evidence assessment. Independent from "
                "verified, which only records source-grounding fidelity."
            ),
        ),
    ] = None

    @model_validator(mode="after")
    def require_adjustment(self) -> "ReviewClaimsArgs":
        """An adjust action must carry at least one concrete change."""
        if (
            self.action == "adjust"
            and self.confidence_override is None
            and self.evidence_status is None
        ):
            raise ValueError("action='adjust' requires confidence_override or evidence_status")
        return self


class ReviewClusterArgs(ProjectScopedArgs):
    """[BRAIN] Write the definitive synthesis on an evidence cluster.

    The Brain's primary act of cluster curation. ``confidence`` is the
    ``ClusterConfLit`` set (``strong`` / ``moderate`` / ``emerging`` /
    ``contested`` / ``refuted``) — distinct from the journal
    ``ConfidenceLit`` set despite the parameter sharing a name with the
    field on ``record_decision``.

    Related: ``clusters``, ``review_claims``, ``resolve_contradiction``.
    """

    operation: Literal["review_cluster"] = "review_cluster"

    cluster_id: Annotated[
        str,
        Field(description="Target cluster ID (``ecl_...``)."),
    ]
    confidence: Annotated[
        ClusterConfLit,
        Field(
            description=(
                "Cluster confidence level. Use ``strong`` only when the "
                "evidence base is robust and the synthesis has been "
                "ratified."
            ),
        ),
    ]
    synthesis: Annotated[
        str,
        Field(
            description=(
                "Definitive synthesis statement for the cluster. This "
                "becomes the cluster's canonical interpretation."
            ),
        ),
    ]

    gaps: Annotated[
        Optional[list[str]],
        Field(
            default=None,
            description="Known evidence gaps to flag for follow-up.",
        ),
    ] = None
    contradictions: Annotated[
        Optional[list[str]],
        Field(
            default=None,
            description=(
                "Contradiction descriptions to surface for ``resolve_contradiction`` follow-up."
            ),
        ),
    ] = None
    resolve_queue_items: Annotated[
        Optional[list[str]],
        Field(
            default=None,
            description=(
                "Review-queue item IDs that this synthesis resolves "
                "(mark-done plumbing for the review queue)."
            ),
        ),
    ] = None
    research_question_id: Annotated[
        Optional[str],
        Field(
            default=None,
            description=(
                "Optional research-question ID this cluster contributes "
                "to (``dec_...`` with kind=``research_question``)."
            ),
        ),
    ] = None


class ResolveContradictionArgs(ProjectScopedArgs):
    """[BRAIN] Resolve a contradiction inside an evidence cluster.

    ``claim_actions`` is an optional list of ``{id, action}`` dicts
    where ``action`` is one of ``retire`` / ``demote`` / ``keep`` — the
    service layer validates the enum (not pinned at the Pydantic layer
    because v2.7.0a3 OPERATIONS_SCHEMA doesn't enumerate it).

    Related: ``review_cluster``, ``contradictions``.
    """

    operation: Literal["resolve_contradiction"] = "resolve_contradiction"

    cluster_id: Annotated[
        str,
        Field(description="Cluster containing the contradiction (``ecl_...``)."),
    ]
    resolution: Annotated[
        str,
        Field(
            description=(
                "Resolution statement explaining how the contradiction "
                "is resolved (e.g., 'Newer benchmark supersedes')."
            ),
        ),
    ]

    claim_actions: Annotated[
        Optional[list[dict[str, Any]]],
        Field(
            default=None,
            description=(
                "Optional list of ``{id, action}`` dicts where action is "
                "one of ``retire`` / ``demote`` / ``keep``."
            ),
        ),
    ] = None


# ---------------------------------------------------------------------------
# Hooks lifecycle — enable / disable / delete
# ---------------------------------------------------------------------------


class HookEnableArgs(ProjectScopedArgs):
    """[PI] Enable a previously-defined hook.

    Only brain_notify is supported. Legacy SQL/MCP handlers return HTTP 422,
    even if already enabled. Enabling a supported hook is idempotent.

    Related: ``hook_add``, ``hook_disable``.
    """

    operation: Literal["hook_enable"] = "hook_enable"

    hook_id: Annotated[
        str,
        Field(description="Hook identifier (``hk_...``)."),
    ]


class HookDisableArgs(ProjectScopedArgs):
    """[PI] Disable a hook (preserves config, including unsupported legacy handlers).

    Idempotent — disabling an already-disabled hook is a no-op at the
    service layer.

    Related: ``hook_enable``, ``hook_delete``.
    """

    operation: Literal["hook_disable"] = "hook_disable"

    hook_id: Annotated[
        str,
        Field(description="Hook identifier (``hk_...``)."),
    ]


class HookDeleteArgs(ProjectScopedArgs):
    """[PI] Permanently delete a hook.

    Destructive — the hook row and its handler_config are removed.
    Use ``hook_disable`` instead if you may want to re-enable later.

    Related: ``hook_disable``.
    """

    operation: Literal["hook_delete"] = "hook_delete"

    hook_id: Annotated[
        str,
        Field(description="Hook identifier (``hk_...``)."),
    ]


# ---------------------------------------------------------------------------
# Notifications — brain_notifications_clear
# ---------------------------------------------------------------------------


class BrainNotificationsClearArgs(ProjectScopedArgs):
    """[BRAIN] Clear Brain notifications by ID.

    Non-empty ``ids`` enforced at the Pydantic layer — clearing an
    empty list is a no-op the typed surface rejects pre-dispatch.

    Related: ``brain_notifications``.
    """

    operation: Literal["brain_notifications_clear"] = "brain_notifications_clear"

    ids: Annotated[
        list[str],
        Field(
            min_length=1,
            description=("Notification IDs to clear (``bn_...``). Must be non-empty."),
        ),
    ]


# ---------------------------------------------------------------------------
# Workspace — bootstrap_workspace / scan_workspace
# ---------------------------------------------------------------------------


class BootstrapWorkspaceArgs(ProjectScopedArgs):
    """[ANY] Bootstrap a workspace folder into the knowledge base.

    Walks ``folder_path``, extracts metadata, mints knowledge entries.
    ``dry_run=True`` simulates the bootstrap without writing.
    ``use_llm`` toggles LLM-driven categorisation; defaults True per the
    v2.7.0a3 schema (service falls back to deterministic rules when no
    LLM is configured).

    The ``folder_path`` MUST be an absolute path; tilde-prefixed paths
    (``~/...``) are rejected (mirror of the Phase D2.1 orchestrator
    fix — inside the daemon container ``~`` resolves to ``/root`` and
    the HOST_WORKSPACE_ROOT bind mount would miss).

    Related: ``workspace_scan``, ``workspace_tree``, ``bootstrap_review``.
    """

    operation: Literal["bootstrap_workspace"] = "bootstrap_workspace"

    folder_path: Annotated[
        str,
        Field(
            description=(
                "Absolute path to the workspace folder to bootstrap. "
                "Tilde-prefixed paths (``~/...``) are NOT accepted — "
                "use the fully-resolved absolute path."
            ),
        ),
    ]

    phase: Annotated[
        Optional[str],
        Field(
            default=None,
            description="Optional phase identifier to tag generated entries with.",
        ),
    ] = None
    override_tags: Annotated[
        Optional[list[str]],
        Field(
            default=None,
            description=(
                "Tags to apply to every generated entry (overrides the default tagging policy)."
            ),
        ),
    ] = None
    skip_files: Annotated[
        Optional[list[str]],
        Field(
            default=None,
            description=("List of file paths or glob patterns to skip during the walk."),
        ),
    ] = None
    use_llm: Annotated[
        bool,
        Field(
            default=True,
            description=(
                "Enable LLM-driven categorisation. Defaults to True; "
                "falls back to deterministic rules when no LLM is "
                "configured."
            ),
        ),
    ] = True
    dry_run: Annotated[
        bool,
        Field(
            default=False,
            description=(
                "When True, simulate the bootstrap and emit a preview without writing any rows."
            ),
        ),
    ] = False


class ScanWorkspaceArgs(ProjectScopedArgs):
    """[ANY] Execute a workspace scan (writes a ``scn_...`` row).

    Distinct from ``bootstrap_workspace`` — scan records a snapshot of
    files + hashes (used by drift detection) WITHOUT inferring
    knowledge entries from contents.

    Related: ``workspace_scan`` (query), ``bootstrap_workspace``.
    """

    operation: Literal["scan_workspace"] = "scan_workspace"

    folder_path: Annotated[
        str,
        Field(
            description=(
                "Absolute path to the workspace folder to scan. "
                "Tilde-prefixed paths (``~/...``) are NOT accepted."
            ),
        ),
    ]

    ignore_patterns: Annotated[
        Optional[list[str]],
        Field(
            default=None,
            description=(
                "Glob patterns to skip (extends the built-in "
                "``.git``/``node_modules``/``.venv`` ignore list)."
            ),
        ),
    ] = None
    max_file_size_mb: Annotated[
        float,
        Field(
            default=50.0,
            gt=0.0,
            description=(
                "Per-file size cap in megabytes. Files above this size "
                "are hashed but their contents are NOT inspected."
            ),
        ),
    ] = 50.0
    use_llm: Annotated[
        bool,
        Field(
            default=True,
            description="Enable LLM-driven file-type inference. Defaults to True.",
        ),
    ] = True


# ---------------------------------------------------------------------------
# Maintenance — flag_stale / eviction_sweep
# ---------------------------------------------------------------------------


class FlagStaleArgs(ProjectScopedArgs):
    """[BRAIN] Flag a knowledge entity as stale.

    ``staleness`` is the ``StalenessLit`` set (``yellow`` for soft
    warning, ``red`` for definitive). ``propagate=True`` (default)
    cascades the flag through provenance edges so downstream entries
    inherit the staleness signal.

    Related: ``freshness``, ``eviction_sweep``.
    """

    operation: Literal["flag_stale"] = "flag_stale"

    entity_id: Annotated[
        str,
        Field(
            description=(
                "Target entity ID. Most commonly ``jrn_`` / ``dec_`` / "
                "``clm_`` / ``ecl_`` — the service layer validates the "
                "prefix is one of the flag-supported types."
            ),
        ),
    ]
    reason: Annotated[
        str,
        Field(
            description=(
                "Free-form reason for the staleness flag (e.g., 'Superseded by new benchmark')."
            ),
        ),
    ]

    staleness: Annotated[
        StalenessLit,
        Field(
            default="yellow",
            description=(
                "Staleness severity. ``yellow`` is a soft warning; "
                "``red`` indicates definitively superseded."
            ),
        ),
    ] = "yellow"
    propagate: Annotated[
        bool,
        Field(
            default=True,
            description=(
                "When True, cascade the staleness flag through "
                "provenance edges so downstream entries inherit the "
                "signal."
            ),
        ),
    ] = True


class EvictionSweepArgs(ProjectScopedArgs):
    """[BRAIN] Evict knowledge entries per the configured policy.

    Defaults to ``dry_run=True`` — the destructive ``dry_run=False``
    path is opt-in. Under the orchestrator's TWO-TAP autonomy contract,
    a ``dry_run=False`` invocation requires explicit PI ratification
    via ``pi_decision_select``.

    Related: ``flag_stale``, ``freshness``.
    """

    operation: Literal["eviction_sweep"] = "eviction_sweep"

    dry_run: Annotated[
        bool,
        Field(
            default=True,
            description=(
                "When True (default), preview what would be evicted. "
                "When False, perform the destructive sweep — requires "
                "explicit ratification under the orchestrator's TWO-TAP "
                "autonomy contract."
            ),
        ),
    ] = True


# ---------------------------------------------------------------------------
# Batch D partial union (for the rka_execute discriminated union)
# ---------------------------------------------------------------------------
#
# Mirrors the Batch B pattern: the per-batch partial union is exposed so
# unit tests can verify Batch D's slice in isolation. Phase 3 composes
# the final ExecuteArgsUnion = BatchBExecuteUnion ∪ BatchCExecuteUnion ∪
# BatchDExecuteUnion (plus the orphan ``HealthArgs`` / ``ListProjectsArgs``
# already in QueryArgsUnion).

BatchDExecuteUnion = Annotated[
    Union[
        # Claims curation
        AssignClaimsToClusterArgs,
        SplitClusterArgs,
        MergeClustersArgs,
        ReviewClaimsArgs,
        ReviewClusterArgs,
        ResolveContradictionArgs,
        # Hooks lifecycle
        HookEnableArgs,
        HookDisableArgs,
        HookDeleteArgs,
        # Notifications
        BrainNotificationsClearArgs,
        # Workspace
        BootstrapWorkspaceArgs,
        ScanWorkspaceArgs,
        # Maintenance
        FlagStaleArgs,
        EvictionSweepArgs,
    ],
    Field(discriminator="operation"),
]


# =============================================================================
# Batch C — UPDATE / LIFECYCLE / SUBMIT operations (21 models)
# =============================================================================
#
# Operations:
#   Updates:           update_note, update_decision, update_literature,
#                      update_mission, update_status, update_mission_status,
#                      bulk_update, supersede_decision
#   Decision lifecycle: present_decision, record_pi_selection, record_outcome
#   Literature lifecycle: enrich_doi, link_literature_to_zotero,
#                         process_paper
#   Mission lifecycle:  submit_report, advance_rq
#   Checkpoint lifecycle: submit_checkpoint, resolve_checkpoint, evaluate_gate
#   Session lifecycle:  reset_session, session_digest


# ---------------------------------------------------------------------------
# UPDATES (8)
# ---------------------------------------------------------------------------


class CorrectNoteAttributionArgs(ProjectScopedArgs, JournalAttributionCorrection):
    """Correct asserted source/original with reason, revision and exact-retry request ID.

    Actor is caller-asserted, not authenticated. Read the note's attribution_revision
    first. Both source and verbatim_input (nullable for non-PI) are required.
    """

    operation: Literal["correct_note_attribution"] = "correct_note_attribution"
    id: str
    actor: JournalActorLit


class UpdateNoteArgs(ProjectScopedArgs):
    """Update a journal entry (content / type / confidence / links).

    Phase-X²' note: every update tool has a "no-op update" guard — at
    least one mutable field must be non-None — to keep Brain from
    dispatching id-only updates that change nothing.

    Phase-X²' regression: ``importance`` MUST be the ``ImportanceLit``
    set (bare ``str`` lets Brain emit ``importance='URGENT'`` and bypass
    the journal.importance CHECK at DB INSERT time).

    Attribution changes require ``correct_note_attribution``. Ordinary edits
    cannot overwrite source or original text without correction history.
    """

    operation: Literal["update_note"] = "update_note"

    id: Annotated[
        str,
        Field(description="Journal entry id (jrn_*) to update."),
    ]

    content: Annotated[
        Optional[str],
        Field(default=None, description="New content body."),
    ] = None
    summary: Annotated[
        Optional[str],
        Field(
            default=None,
            description=(
                "New short summary. RecordNoteArgs could write this and "
                "UpdateNoteArgs could not correct it, so a summary written "
                "through the agent surface was uneditable there."
            ),
        ),
    ] = None
    type: Annotated[
        Optional[NoteTypeLit],
        Field(default=None, description=(
            "New journal type. Only `note`, `log` and `directive` are stored; "
            "the other nine are legacy aliases normalized on write "
            "(`methodology`→`log`, `pi_instruction`→`directive`, the rest→"
            "`note`), so a read-back returns the canonical type."
        )),
    ] = None
    confidence: Annotated[
        Optional[ConfidenceLit],
        Field(default=None, description="New confidence level."),
    ] = None
    importance: Annotated[
        Optional[ImportanceLit],
        Field(default=None, description="New importance level."),
    ] = None
    status: Annotated[
        Optional[JournalStatusLit],
        Field(
            default=None,
            description=(
                "New journal lifecycle status "
                "(draft|active|superseded|retracted)."
            ),
        ),
    ] = None
    pinned: Annotated[
        Optional[bool],
        Field(default=None, description="Whether the journal entry is pinned."),
    ] = None
    tags: Annotated[
        Optional[list[str]],
        Field(default=None, description="Replacement tag list."),
    ] = None
    phase: Annotated[
        Optional[str],
        Field(default=None, description="New phase label."),
    ] = None
    verbatim_input: Annotated[
        Optional[str],
        Field(
            default=None,
            description="Deprecated: use correct_note_attribution; non-null values rejected.",
            deprecated=True,
        ),
    ] = None
    source: Annotated[
        Optional[SourceLit],
        Field(default=None, description="Deprecated: use correct_note_attribution.", deprecated=True),
    ] = None
    related_decisions: Annotated[
        Optional[list[str]],
        Field(default=None, description="Replacement related-decision ids."),
    ] = None
    related_literature: Annotated[
        Optional[list[str]],
        Field(default=None, description="Replacement related-literature ids."),
    ] = None
    related_mission: Annotated[
        Optional[str],
        Field(default=None, description="Replacement related-mission id."),
    ] = None

    @model_validator(mode="after")
    def _enforce_non_empty_update(self) -> "UpdateNoteArgs":
        mutable_fields = (
            self.content,
            self.summary,
            self.type,
            self.confidence,
            self.importance,
            self.status,
            self.pinned,
            self.tags,
            self.phase,
            self.__dict__.get("verbatim_input"),
            self.__dict__.get("source"),
            self.related_decisions,
            self.related_literature,
            self.related_mission,
        )
        if all(f is None for f in mutable_fields):
            raise ValueError(
                "update_note: at least one mutable field must be non-None "
                "(content, summary, type, confidence, importance, status, pinned, "
                "tags, phase, "
                "verbatim_input, source, related_decisions, related_literature, "
                "related_mission)."
            )
        return self

    @model_validator(mode="after")
    def _require_attribution_correction(self) -> "UpdateNoteArgs":
        if self.__dict__.get("source") is not None or self.__dict__.get("verbatim_input") is not None:
            raise ValueError(
                "source/verbatim_input require correct_note_attribution with reason and revision"
            )
        return self


class UpdateDecisionArgs(ProjectScopedArgs):
    """Update fields on an existing decision.

    Phase-X²' regression: ``status`` MUST be the ``DecisionStatusLit``
    set (bare ``str`` lets Brain emit invalid statuses that fail the
    ``decisions.status`` CHECK at DB INSERT time — HTTP 500 instead of
    422 at the typed-args layer).
    """

    operation: Literal["update_decision"] = "update_decision"

    id: Annotated[
        str,
        Field(description="Decision id (dec_*) to update."),
    ]

    status: Annotated[
        Optional[DecisionStatusLit],
        Field(
            default=None,
            description="New decision lifecycle status (active|abandoned|superseded|merged|revisit).",
        ),
    ] = None
    chosen: Annotated[
        Optional[str],
        Field(default=None, description="Revised chosen option."),
    ] = None
    rationale: Annotated[
        Optional[str],
        Field(default=None, description="Revised rationale."),
    ] = None
    kind: Annotated[
        Optional[DecisionKindLit],
        Field(default=None, description="Revised decision kind."),
    ] = None
    related_journal: Annotated[
        Optional[list[str]],
        Field(default=None, description="Replacement related-journal ids."),
    ] = None
    parent_id: Annotated[
        Optional[str],
        Field(default=None, description="Parent decision id."),
    ] = None
    related_literature: Annotated[
        Optional[list[str]],
        Field(default=None, description="Replacement related-literature ids."),
    ] = None
    related_missions: Annotated[
        Optional[list[str]],
        Field(default=None, description="Replacement related-mission ids."),
    ] = None
    phase: Annotated[
        Optional[str],
        Field(default=None, description="New phase label."),
    ] = None
    tags: Annotated[
        Optional[list[str]],
        Field(default=None, description="Replacement tag list."),
    ] = None
    assumptions: Annotated[
        Optional[list[str]],
        Field(default=None, description="Revised assumption list."),
    ] = None
    abandonment_reason: Annotated[
        Optional[str],
        Field(default=None, description="Reason for abandoning (when status transitions)."),
    ] = None

    @model_validator(mode="after")
    def _enforce_non_empty_update(self) -> "UpdateDecisionArgs":
        mutable_fields = (
            self.status,
            self.chosen,
            self.rationale,
            self.kind,
            self.related_journal,
            self.parent_id,
            self.related_literature,
            self.related_missions,
            self.phase,
            self.tags,
            self.assumptions,
            self.abandonment_reason,
        )
        if all(f is None for f in mutable_fields):
            raise ValueError("update_decision: at least one mutable field must be non-None.")
        return self


class UpdateLiteratureArgs(ProjectScopedArgs):
    """Update fields on a literature entry.

    Phase-X²' polish: ``key_findings`` is ``list[str]`` to match
    ``rka/models/literature.py`` ``LiteratureUpdate.key_findings`` —
    previously typed as ``str`` here, which fails REST PATCH with HTTP
    422 ``Input should be a valid list``.
    """

    operation: Literal["update_literature"] = "update_literature"

    id: Annotated[
        str,
        Field(description="Literature entry id (lit_*) to update."),
    ]

    title: Annotated[
        Optional[str],
        Field(default=None, description="Revised title."),
    ] = None
    authors: Annotated[
        Optional[list[str]],
        Field(default=None, description="Replacement author list."),
    ] = None
    year: Annotated[
        Optional[int],
        Field(default=None, description="Publication year."),
    ] = None
    venue: Annotated[
        Optional[str],
        Field(default=None, description="Publication venue."),
    ] = None
    doi: Annotated[
        Optional[str],
        Field(default=None, description="DOI string."),
    ] = None
    url: Annotated[
        Optional[str],
        Field(default=None, description="Canonical URL."),
    ] = None
    bibtex: Annotated[
        Optional[str],
        Field(default=None, description="Raw BibTeX entry."),
    ] = None
    pdf_path: Annotated[
        Optional[str],
        Field(default=None, description="Local PDF path."),
    ] = None
    abstract: Annotated[
        Optional[str],
        Field(default=None, description="Abstract text."),
    ] = None
    status: Annotated[
        Optional[LitStatusLit],
        Field(default=None, description="New reading-lifecycle status."),
    ] = None
    key_findings: Annotated[
        Optional[list[str]],
        Field(default=None, description="Reader-noted key findings (list of strings)."),
    ] = None
    methodology_notes: Annotated[
        Optional[str],
        Field(default=None, description="Methodology notes."),
    ] = None
    relevance: Annotated[
        Optional[str],
        Field(default=None, description="Relevance prose."),
    ] = None
    relevance_score: Annotated[
        Optional[float],
        Field(default=None, description="Numeric relevance score."),
    ] = None
    related_decisions: Annotated[
        Optional[list[str]],
        Field(default=None, description="Linked decision ids."),
    ] = None
    notes: Annotated[
        Optional[str],
        Field(default=None, description="Free-form notes."),
    ] = None
    tags: Annotated[
        Optional[list[str]],
        Field(default=None, description="Replacement tag list."),
    ] = None

    @model_validator(mode="after")
    def _enforce_non_empty_update(self) -> "UpdateLiteratureArgs":
        mutable_fields = (
            self.title,
            self.authors,
            self.year,
            self.venue,
            self.doi,
            self.url,
            self.bibtex,
            self.pdf_path,
            self.abstract,
            self.status,
            self.key_findings,
            self.methodology_notes,
            self.relevance,
            self.relevance_score,
            self.related_decisions,
            self.notes,
            self.tags,
        )
        if all(f is None for f in mutable_fields):
            raise ValueError("update_literature: at least one mutable field must be non-None.")
        return self


class UpdateMissionArgs(ProjectScopedArgs):
    """Update mission fields (objective, context, criteria, etc.)."""

    operation: Literal["update_mission"] = "update_mission"

    mission_id: Annotated[
        str,
        Field(description="Mission id (mis_*) to update."),
    ]

    phase: Annotated[
        Optional[str],
        Field(default=None, description="New mission phase label."),
    ] = None
    objective: Annotated[
        Optional[str],
        Field(default=None, description="Revised objective."),
    ] = None
    context: Annotated[
        Optional[str],
        Field(default=None, description="Revised context."),
    ] = None
    acceptance_criteria: Annotated[
        Optional[str],
        Field(default=None, description="Revised acceptance criteria."),
    ] = None
    scope_boundaries: Annotated[
        Optional[str],
        Field(default=None, description="Revised scope boundaries."),
    ] = None
    checkpoint_triggers: Annotated[
        Optional[list[str]],
        Field(default=None, description="Revised checkpoint-trigger conditions."),
    ] = None
    depends_on: Annotated[
        Optional[list[str]],
        Field(default=None, description="Replacement upstream-dependency ids."),
    ] = None
    parent_mission_id: Annotated[
        Optional[str],
        Field(default=None, description="Parent mission id (for hierarchy)."),
    ] = None
    motivated_by_decision: Annotated[
        Optional[str],
        Field(default=None, description="Decision id that motivates this mission."),
    ] = None
    tags: Annotated[
        Optional[list[str]],
        Field(default=None, description="Replacement tag list."),
    ] = None

    @model_validator(mode="after")
    def _enforce_non_empty_update(self) -> "UpdateMissionArgs":
        mutable_fields = (
            self.phase,
            self.objective,
            self.context,
            self.acceptance_criteria,
            self.scope_boundaries,
            self.checkpoint_triggers,
            self.depends_on,
            self.parent_mission_id,
            self.motivated_by_decision,
            self.tags,
        )
        if all(f is None for f in mutable_fields):
            raise ValueError("update_mission: at least one mutable field must be non-None.")
        return self


class UpdateStatusArgs(ProjectScopedArgs):
    """Update the project status (phase, focus, blockers)."""

    operation: Literal["update_status"] = "update_status"

    current_phase: Annotated[
        Optional[str],
        Field(default=None, description="New project-level phase label."),
    ] = None
    summary: Annotated[
        Optional[str],
        Field(default=None, description="Status summary text."),
    ] = None
    blockers: Annotated[
        Optional[list[str]],
        Field(default=None, description="Replacement blockers list."),
    ] = None
    metrics: Annotated[
        Optional[dict[str, Any]],
        Field(default=None, description="Replacement metrics dict."),
    ] = None

    @model_validator(mode="after")
    def _enforce_non_empty_update(self) -> "UpdateStatusArgs":
        if (
            self.current_phase is None
            and self.summary is None
            and self.blockers is None
            and self.metrics is None
        ):
            raise ValueError(
                "update_status: at least one of (current_phase, summary, "
                "blockers, metrics) must be non-None."
            )
        return self


class UpdateMissionStatusArgs(ProjectScopedArgs):
    """Move a mission through its lifecycle states."""

    operation: Literal["update_mission_status"] = "update_mission_status"

    mission_id: Annotated[
        str,
        Field(description="Mission id (mis_*) whose status to transition."),
    ]
    status: Annotated[
        MissionStatusLit,
        Field(
            description="New lifecycle state (pending|active|complete|partial|blocked|cancelled)."
        ),
    ]

    tasks: Annotated[
        Optional[list[dict[str, Any]]],
        Field(
            default=None,
            description="Optional task list update accompanying the status transition.",
        ),
    ] = None


class BulkUpdateItem(BaseModel):
    """One compatibility-safe item in a best-effort bulk update.

    The typed contract historically advertised flat items while the legacy
    adapter expected fields inside ``data``. Accept both shapes so existing
    callers keep working, but reject mixed or empty payloads before any write.
    Entity-specific REST models validate and normalize the mutable fields.
    """

    model_config = ConfigDict(extra="allow", use_enum_values=True)

    entity_type: Annotated[
        BulkEntityTypeLit,
        Field(description="note|journal|decision|literature update target."),
    ]
    id: Annotated[str, Field(min_length=1, description="Entity id to update.")]
    data: Annotated[
        Optional[dict[str, Any]],
        Field(
            default=None,
            description=(
                "Legacy nested update fields. Prefer flat fields beside id and "
                "entity_type; do not combine both shapes."
            ),
        ),
    ] = None

    def normalized_data(self) -> dict[str, Any]:
        """Return entity-model-validated update data in JSON form."""
        flat_data = dict(self.model_extra or {})
        if self.data is not None and flat_data:
            raise ValueError(
                "bulk_update item cannot combine nested 'data' with flat update fields"
            )
        raw_data = self.data if self.data is not None else flat_data
        model_type = {
            "note": JournalEntryUpdate,
            "journal": JournalEntryUpdate,
            "decision": DecisionUpdate,
            "literature": LiteratureUpdate,
        }[self.entity_type]
        validated = model_type.model_validate(raw_data)
        normalized = validated.model_dump(mode="json", exclude_none=True)
        if not normalized:
            raise ValueError("bulk_update item must include at least one non-null update field")
        return normalized

    @model_validator(mode="after")
    def _validate_shape_and_data(self) -> "BulkUpdateItem":
        if not self.id.strip():
            raise ValueError("bulk_update item id cannot be blank")
        self.normalized_data()
        return self


class BulkUpdateArgs(ProjectScopedArgs):
    """Update many entities as a validated best-effort batch."""

    operation: Literal["bulk_update"] = "bulk_update"

    updates: Annotated[
        list[BulkUpdateItem],
        Field(
            min_length=1,
            description=(
                "List of validated updates. Each item requires entity_type and "
                "id plus one or more flat update fields. Legacy nested data is "
                "accepted for compatibility."
            ),
        ),
    ]


class SupersedeDecisionArgs(ProjectScopedArgs):
    """Replace an old decision with a new one (atomically links the two).

    Phase-X²' provenance discipline: ``related_journal`` REQUIRED non-empty
    on the supersede path — supersede CREATES a new decision and the same
    provenance rule that applies to ``RecordDecisionArgs.related_journal``
    must apply here. Without it, Brain can supersede a decision into a
    provenance-orphaned successor.
    """

    operation: Literal["supersede_decision"] = "supersede_decision"

    old_decision_id: Annotated[
        str,
        Field(description="The decision id (dec_*) being superseded."),
    ]
    question: Annotated[str, Field(description="Revised decision question.")]
    chosen: Annotated[str, Field(description="Newly chosen option label.")]
    rationale: Annotated[str, Field(description="Rationale for the new choice.")]

    # Required provenance (mirrors RecordDecisionArgs).
    related_journal: Annotated[
        list[str],
        Field(
            min_length=1,
            description=(
                "Journal IDs justifying the new (superseding) decision. "
                "REQUIRED non-empty (provenance discipline preserved from "
                "RecordDecisionArgs)."
            ),
        ),
    ]

    decided_by: Annotated[
        Optional[DecidedByLit],
        Field(
            default="brain",
            description="Who decided (PI for ratified, Brain for proposed).",
        ),
    ] = "brain"
    phase: Annotated[
        Optional[str],
        Field(default=None, description="Phase label."),
    ] = None
    kind: Annotated[
        Optional[DecisionKindLit],
        Field(
            default="decision",
            description="Decision kind (decision|design_choice|research_question|operational).",
        ),
    ] = "decision"

    @model_validator(mode="after")
    def _enforce_related_journal_nonempty(self) -> "SupersedeDecisionArgs":
        # Defense-in-depth (mirrors RecordDecisionArgs validator).
        if not self.related_journal:
            raise ValueError(
                "supersede_decision: related_journal must be a non-empty "
                "list — superseding decisions need justifying journal "
                "entries (provenance discipline preserved from Phase-X²)."
            )
        return self


# ---------------------------------------------------------------------------
# DECISION LIFECYCLE (3)
# ---------------------------------------------------------------------------


class PresentDecisionArgs(ProjectScopedArgs):
    """Present a decision to the PI for ratification.

    ``options`` MUST be non-empty — the TWO-TAP autonomy contract is
    semantically void with no choices to ratify.

    Option ids are assigned by the server, not supplied by the caller. This
    model used to require an ``id`` on every option so that
    ``record_pi_selection.selected_option_id`` would be dereferenceable, but
    the REST payload it feeds (``DecisionOptionCreate``) declares
    ``extra="forbid"`` and has no ``id`` field, so an option carrying one was
    rejected with HTTP 422. Requiring it here and forbidding it there made the
    operation impossible to call: without ``id`` this model refused, with
    ``id`` the API did.

    The ids to use in ``record_pi_selection`` come back in this operation's
    own response, as ``presented_option_ids``.

    Each option must otherwise match ``DecisionOptionCreate`` in full — see
    the field description below.
    """

    operation: Literal["present_decision"] = "present_decision"

    decision_id: Annotated[
        str,
        Field(description="Decision id (dec_*) being presented."),
    ]
    confirmation_brief: Annotated[
        str,
        Field(description="Short brief explaining the choice + tradeoffs to the PI."),
    ]
    options: Annotated[
        list[dict[str, Any]],
        Field(
            min_length=1,
            description=(
                "Non-empty list of DecisionOptionCreate-shaped dicts. Required "
                "per option: label, summary, justification, explanation, pros "
                "(exactly 3), cons (exactly 3), confidence_verbal "
                "(low|moderate|high), confidence_numeric (0.0-1.0), "
                "confidence_evidence_strength (weak|moderate|strong), "
                "confidence_known_unknowns (1-2 items), effort_time "
                "(S|M|L|XL), effort_reversibility "
                "(reversible|costly|irreversible), presentation_order_seed. "
                "Do NOT pass 'id' — the server assigns option ids and returns "
                "them as presented_option_ids."
            ),
        ),
    ]
    pi_preference: Annotated[
        Optional[str],
        Field(
            default=None,
            description="Optional PI-stated preference hint (verbatim).",
        ),
    ] = None

    @model_validator(mode="after")
    def _reject_caller_supplied_option_id(self) -> "PresentDecisionArgs":
        """Catch a caller-supplied ``id`` here rather than as a REST 422.

        ``DecisionOptionCreate`` forbids extra keys, so an option carrying an
        ``id`` fails at the API boundary with ``extra_forbidden`` — an error
        that reads like a schema mismatch rather than "you do not assign these".
        Rejecting it at the typed surface says which it is.
        """
        for idx, opt in enumerate(self.options):
            if not isinstance(opt, dict):
                raise ValueError(f"present_decision: options[{idx}] is not a dict.")
            if "id" in opt:
                raise ValueError(
                    f"present_decision: options[{idx}] must not carry an 'id' — "
                    "the server assigns option ids and returns them as "
                    "presented_option_ids, which is what record_pi_selection "
                    "takes as selected_option_id."
                )
        return self


class RecordPiSelectionArgs(ProjectScopedArgs):
    """Record the PI's selection on a presented decision."""

    operation: Literal["record_pi_selection"] = "record_pi_selection"

    decision_id: Annotated[
        str,
        Field(description="Decision id (dec_*) the PI is selecting on."),
    ]

    selected_option_id: Annotated[
        Optional[str],
        Field(
            default=None,
            description="The PI's chosen option id from the presented options.",
        ),
    ] = None
    override_rationale: Annotated[
        Optional[str],
        Field(
            default=None,
            description="PI-provided rationale (used to record an override or annotate the choice).",
        ),
    ] = None

    @model_validator(mode="after")
    def _require_selection_or_rationale(self) -> "RecordPiSelectionArgs":
        if self.selected_option_id is None and self.override_rationale is None:
            raise ValueError(
                "record_pi_selection: at least one of selected_option_id or "
                "override_rationale required."
            )
        return self


class RecordOutcomeArgs(ProjectScopedArgs):
    """Record the calibration outcome of a past decision.

    Phase-X²' polish: ``recorded_by`` typed as ``RecordedByLit`` (was
    bare ``str``) so Brain cannot emit ``recorded_by='SUPERVISOR'`` and
    pollute the calibration provenance. The calibration_outcomes table
    has no DB-level CHECK constraint on ``recorded_by``, so the typed
    surface is the authoritative gate.
    """

    operation: Literal["record_outcome"] = "record_outcome"

    decision_id: Annotated[
        str,
        Field(description="Decision id (dec_*) whose outcome is being recorded."),
    ]
    outcome: Annotated[
        OutcomeLit,
        Field(description="Calibration outcome (succeeded|failed|mixed|unresolved)."),
    ]

    outcome_details: Annotated[
        Optional[str],
        Field(
            default=None,
            description="Free-form details on the outcome.",
        ),
    ] = None
    recorded_by: Annotated[
        Optional[RecordedByLit],
        Field(
            default="pi",
            description="Actor recording the outcome (pi|brain|executor|system); typically 'pi'.",
        ),
    ] = "pi"


# ---------------------------------------------------------------------------
# LITERATURE LIFECYCLE (4)
# ---------------------------------------------------------------------------


class EnrichDoiArgs(ProjectScopedArgs):
    """Enrich a literature entry's metadata from its DOI."""

    operation: Literal["enrich_doi"] = "enrich_doi"

    lit_id: Annotated[
        str,
        Field(description="Literature entry id (lit_*) whose DOI is the lookup key."),
    ]


class LinkLiteratureToZoteroArgs(ProjectScopedArgs):
    """Link a literature entry to a Zotero item."""

    operation: Literal["link_literature_to_zotero"] = "link_literature_to_zotero"

    lit_id: Annotated[
        str,
        Field(description="Literature entry id (lit_*) to link."),
    ]

    zotero_key: Annotated[
        Optional[str],
        Field(
            default=None,
            description=(
                "Optional Zotero item key. If omitted, the service looks "
                "up by DOI / title via the configured Zotero adapter."
            ),
        ),
    ] = None


class ProcessPaperArgs(ProjectScopedArgs):
    """Ingest paper annotations into the literature row.

    Phase-X²' polish: ``annotations`` MUST be non-empty (zero annotations
    is a no-op); each annotation dict MUST carry a ``text`` key.
    """

    operation: Literal["process_paper"] = "process_paper"

    lit_id: Annotated[
        str,
        Field(description="Literature entry id (lit_*) receiving the annotations."),
    ]
    annotations: Annotated[
        list[dict[str, Any]],
        Field(
            min_length=1,
            description=(
                "Non-empty list of annotation dicts (each MUST include a "
                "'text' key; typical shape {text, page, ...})."
            ),
        ),
    ]

    summary: Annotated[
        Optional[str],
        Field(
            default=None,
            description="Optional narrative summary derived from the annotations.",
        ),
    ] = None

    @model_validator(mode="after")
    def _enforce_text_on_every_annotation(self) -> "ProcessPaperArgs":
        for idx, ann in enumerate(self.annotations):
            if not isinstance(ann, dict):
                raise ValueError(f"process_paper: annotations[{idx}] is not a dict.")
            if "text" not in ann or not ann["text"]:
                raise ValueError(
                    f"process_paper: annotations[{idx}] missing required "
                    "'text' key (annotations without text are meaningless)."
                )
        return self


# ---------------------------------------------------------------------------
# MISSION LIFECYCLE (2)
# ---------------------------------------------------------------------------


class SubmitReportArgs(ProjectScopedArgs):
    """Submit a mission's final report (closes the mission).

    Report submission does not infer or rewrite task outcomes. Reconcile the
    full task list with ``update_mission_status`` first; a completed mission
    returned with non-terminal task rows carries ``consistency_warnings``.

    Phase-X²' alias rule: canonical body field is ``summary``; legacy
    callers using ``content`` are accepted. Both None -> raise.

    Phase-X²' polish (drift fix): ``findings``/``anomalies``/``questions``
    are ``list[str]`` here to match ``rka/models/mission.py``
    ``MissionReportCreate``. ``codebase_state``/``recommended_next``
    default to ``None`` (not ``""``) so ``model_dump(exclude_none=True)``
    truly omits unsupplied fields per the dispatcher contract.
    """

    operation: Literal["submit_report"] = "submit_report"

    mission_id: Annotated[
        str,
        Field(description="Mission id (mis_*) being closed."),
    ]

    summary: Annotated[
        Optional[str],
        Field(
            default=None,
            description=(
                "Report narrative body (CANONICAL). One of summary or content is required."
            ),
        ),
    ] = None
    content: Annotated[
        Optional[str],
        Field(
            default=None,
            description=(
                "Phase-X²' alias for `summary`. If both are supplied, `summary` wins (canonical)."
            ),
        ),
    ] = None

    findings: Annotated[
        Optional[list[str]],
        Field(default=None, description="Findings (list of strings)."),
    ] = None
    anomalies: Annotated[
        Optional[list[str]],
        Field(default=None, description="Anomalies (list of strings)."),
    ] = None
    questions: Annotated[
        Optional[list[str]],
        Field(default=None, description="Open questions (list of strings)."),
    ] = None
    codebase_state: Annotated[
        Optional[str],
        Field(default=None, description="Codebase state notes."),
    ] = None
    recommended_next: Annotated[
        Optional[str],
        Field(default=None, description="Recommended next mission."),
    ] = None

    @model_validator(mode="after")
    def _require_summary_or_content(self) -> "SubmitReportArgs":
        if self.summary is None and self.content is None:
            raise ValueError(
                "submit_report: one of 'summary' or 'content' is required "
                "(summary is canonical; content is a Phase-X²' alias)."
            )
        # Canonical-wins normalisation: when both are supplied, null the
        # alias so model_dump(exclude_none=True) emits only `summary`.
        if self.summary is not None and self.content is not None:
            self.content = None
        return self


class LinkSupersessionArgs(ProjectScopedArgs):
    """[BRAIN/PI] Reconnect an existing decision pair as superseded -> replacement.

    Distinct from ``supersede_decision``, which CREATES the replacement. Use
    this when both rows already exist and only the chain between them is
    missing — a decision reading ``superseded`` with no ``superseded_by``
    tells a reader it is dead but not what replaced it, which is precisely
    what sends a session hunting for a successor it cannot reach.

    Replays the full supersede sequence without creating anything: scope
    version bump, the superseded_by pointer, the ``supersedes`` entity_link,
    the staleness cascade over claims and clusters sourced from the old
    decision's journal entries, a re-distill review row, and the
    decision_superseded event. Idempotent.

    ``apply`` defaults to False: a wrong pointer is worse than a missing one,
    because it sends a reader confidently to an unrelated decision. Preview
    first, then re-call with apply=True.
    """

    operation: Literal["link_supersession"] = "link_supersession"

    old_decision_id: Annotated[
        str,
        Field(description="The superseded decision (dec_*) that is missing its pointer."),
    ]
    new_decision_id: Annotated[
        str,
        Field(description="The existing decision (dec_*) that replaced it."),
    ]
    apply: Annotated[
        bool,
        Field(
            default=False,
            description="False (default) previews the steps; True performs them.",
        ),
    ] = False
    actor: Annotated[
        Literal["pi", "brain", "executor", "system"],
        Field(default="pi", description="Actor recorded on the backfilled rows."),
    ] = "pi"


class AdvanceRqArgs(ProjectScopedArgs):
    """Advance a research question through its lifecycle."""

    operation: Literal["advance_rq"] = "advance_rq"

    rq_id: Annotated[
        str,
        Field(description="Research-question id (dec_*) being advanced."),
    ]
    status: Annotated[
        RQStatusLit,
        Field(description="New RQ status (open|partially_answered|answered|reframed|closed)."),
    ]

    conclusion: Annotated[
        Optional[str],
        Field(
            default=None,
            description="Optional conclusion text (recommended when status='answered').",
        ),
    ] = None
    evidence_cluster_ids: Annotated[
        Optional[list[str]],
        Field(
            default=None,
            description="Optional evidence-cluster ids (ecl_*) supporting the advance.",
        ),
    ] = None


# ---------------------------------------------------------------------------
# CHECKPOINT LIFECYCLE (3)
# ---------------------------------------------------------------------------


class SubmitCheckpointArgs(ProjectScopedArgs):
    """Raise a checkpoint when blocked or needing input.

    Phase-X²' alias rule (2026-06-01 hyperscaler-auditing PA-2):
    canonical body field is ``description``; legacy callers using
    ``content`` are accepted. Both None -> raise.
    """

    operation: Literal["submit_checkpoint"] = "submit_checkpoint"

    mission_id: Annotated[
        str,
        Field(description="Mission id (mis_*) the checkpoint is raised on."),
    ]
    type: Annotated[
        ChkTypeLit,
        Field(description="Checkpoint type (decision|clarification|inspection|gate)."),
    ]

    description: Annotated[
        Optional[str],
        Field(
            default=None,
            description=(
                "Checkpoint description (CANONICAL). One of description or content is required."
            ),
        ),
    ] = None
    content: Annotated[
        Optional[str],
        Field(
            default=None,
            description=(
                "Phase-X²' alias for `description`. If both are supplied, "
                "`description` wins (canonical) and `content` is silently "
                "nulled. Common Brain hallucination class — alias provided "
                "for back-compat."
            ),
        ),
    ] = None

    task_reference: Annotated[
        Optional[str],
        Field(
            default=None,
            description="Optional task-reference string within the mission.",
        ),
    ] = None
    context: Annotated[
        Optional[str],
        Field(
            default=None,
            description="Optional context payload (free-form).",
        ),
    ] = None
    options: Annotated[
        Optional[list[dict[str, Any]]],
        Field(
            default=None,
            description="Optional list of option dicts presented to the resolver.",
        ),
    ] = None
    recommendation: Annotated[
        Optional[str],
        Field(
            default=None,
            description="Optional recommended resolution.",
        ),
    ] = None
    blocking: Annotated[
        Optional[bool],
        Field(
            default=True,
            description="Whether the checkpoint blocks the mission (default True).",
        ),
    ] = True

    @model_validator(mode="after")
    def _require_description_or_content(self) -> "SubmitCheckpointArgs":
        if self.description is None and self.content is None:
            raise ValueError(
                "submit_checkpoint: one of 'description' or 'content' is "
                "required (description is canonical; content is a Phase-X²' alias)."
            )
        # Canonical-wins normalisation: when both are supplied, null the
        # alias so model_dump(exclude_none=True) emits only `description`
        # and downstream callers don't see a stale `content` value.
        if self.description is not None and self.content is not None:
            self.content = None
        return self


class ResolveCheckpointArgs(ProjectScopedArgs):
    """Resolve a checkpoint with a resolution + rationale.

    Phase-X²' drift fix: ``resolved_by`` is the narrower
    ``CheckpointResolvedByLit`` (``pi``/``brain``) — matches both
    ``rka/models/checkpoint.py`` ``CheckpointResolve.resolved_by`` and
    the DB CHECK on ``checkpoints.resolved_by`` (schema.sql line 156 +
    migration 015). The previously-used ``DecidedByLit`` included
    ``'executor'`` which would pass the typed surface but fail the DB
    CHECK at INSERT time (HTTP 500 instead of 422).
    """

    operation: Literal["resolve_checkpoint"] = "resolve_checkpoint"

    id: Annotated[
        str,
        Field(description="Checkpoint id (chk_*) to resolve."),
    ]
    resolution: Annotated[
        str,
        Field(description="Resolution text."),
    ]
    resolved_by: Annotated[
        CheckpointResolvedByLit,
        Field(description="Who resolved this (pi|brain). 'executor' is rejected by the DB CHECK."),
    ]

    rationale: Annotated[
        Optional[str],
        Field(
            default=None,
            description="Optional rationale narrative.",
        ),
    ] = None
    create_decision: Annotated[
        Optional[bool],
        Field(
            default=False,
            description=(
                "If True, the resolution is also recorded as a Decision "
                "node with provenance back to the checkpoint."
            ),
        ),
    ] = False


class EvaluateGateArgs(ProjectScopedArgs):
    """Evaluate a gate with a verdict + notes."""

    operation: Literal["evaluate_gate"] = "evaluate_gate"

    gate_id: Annotated[
        str,
        Field(description="Gate id (typically chk_* for a gate-type checkpoint)."),
    ]
    verdict: Annotated[
        VerdictLit,
        Field(description="Gate verdict (go|kill|hold|recycle)."),
    ]
    notes: Annotated[
        str,
        Field(description="Evaluation notes."),
    ]

    assumption_status: Annotated[
        Optional[dict[str, Any]],
        Field(
            default=None,
            description=(
                "Optional per-assumption status dict (e.g., "
                "{'assumption_text': 'verified|refuted|untested'})."
            ),
        ),
    ] = None


# ---------------------------------------------------------------------------
# SESSION LIFECYCLE (2)
# ---------------------------------------------------------------------------


class ResetSessionArgs(UnscopedArgs):
    """Reset the in-process session tracker (UNSCOPED).

    Discriminator-only model.
    """

    operation: Literal["reset_session"] = "reset_session"


class SessionDigestArgs(ProjectScopedArgs):
    """Compact session summary (mutates session state)."""

    operation: Literal["session_digest"] = "session_digest"


# ---------------------------------------------------------------------------
# Batch C partial union (for the rka_execute discriminated union)
# ---------------------------------------------------------------------------

BatchCExecuteUnion = Annotated[
    Union[
        # Updates
        UpdateNoteArgs,
        CorrectNoteAttributionArgs,
        UpdateDecisionArgs,
        UpdateLiteratureArgs,
        UpdateMissionArgs,
        UpdateStatusArgs,
        UpdateMissionStatusArgs,
        BulkUpdateArgs,
        SupersedeDecisionArgs,
        # Decision lifecycle
        PresentDecisionArgs,
        RecordPiSelectionArgs,
        RecordOutcomeArgs,
        # Literature lifecycle
        EnrichDoiArgs,
        LinkLiteratureToZoteroArgs,
        ProcessPaperArgs,
        # Mission lifecycle
        SubmitReportArgs,
        AdvanceRqArgs,
        # Checkpoint lifecycle
        SubmitCheckpointArgs,
        ResolveCheckpointArgs,
        EvaluateGateArgs,
        # Session lifecycle
        ResetSessionArgs,
        SessionDigestArgs,
    ],
    Field(discriminator="operation"),
]


# =============================================================================
# Final ExecuteArgsUnion — composes B + C + D (83 models total)
# =============================================================================
#
# Phase 3 assembly: the discriminated union for `rka_execute`. FastMCP
# renders this as `oneOf` with per-branch enum + required arrays. The
# discriminator field (`operation: Literal['<op_name>']`) is the sole
# union key.

ExecuteArgsUnion = Annotated[
    Union[
        # ===== Batch B — RECORD/CREATE + NATIVE MANUSCRIPT =====
        RecordNoteArgs,
        IngestDocumentArgs,
        RecordDecisionArgs,
        RecordLiteratureArgs,
        ImportBibtexArgs,
        BatchImportArgs,
        RegisterManuscriptArgs,
        CreateManuscriptArgs,
        UpdateManuscriptArgs,
        UpsertArgumentSpineArgs,
        ReplaceManuscriptReferenceManifestArgs,
        RatifyManuscriptClaimArgs,
        TransitionManuscriptPhaseArgs,
        CreateManuscriptCheckpointArgs,
        ResolveManuscriptCheckpointArgs,
        RecordVerificationAttestationArgs,
        CreateMissionArgs,
        CreateClusterArgs,
        CreateProjectArgs,
        CreateGateArgs,
        HookAddArgs,
        ExtractClaimsArgs,
        CreateInterpretationCandidateArgs,
        RegisterSourceArgs,
        AdmitSourceInterpretationArgs,
        AddInterpretationHintArgs,
        TriageInterpretationCandidateArgs,
        SetClaimScopeArgs,
        CreateExperimentArgs,
        AppendExperimentPlanArgs,
        TransitionExperimentArgs,
        CreateExperimentRunArgs,
        TransitionExperimentRunArgs,
        RecordExperimentObservationArgs,
        AddEvidenceLocatorArgs,
        CreatePlanningBranchArgs,
        TransitionPlanningBranchArgs,
        AppendPlanningArtifactVersionArgs,
        PromotePlanningResearchQuestionArgs,
        PreparePlanningContributionArgs,
        RatifyPlanningContributionArgs,
        CreatePlanningEvaluationMissionArgs,
        PreparePlanningEvaluationResultArgs,
        PrepareManuscriptOutlineProposalArgs,
        PrepareSemanticPatchContextArgs,
        CreateSemanticPatchProposalArgs,
        ApplySemanticPatchProposalArgs,
        RejectSemanticPatchProposalArgs,
        GenerateLMStudioSemanticPatchArgs,
        # ===== Batch C — UPDATE/LIFECYCLE/SUBMIT (22) =====
        UpdateNoteArgs,
        CorrectNoteAttributionArgs,
        UpdateDecisionArgs,
        UpdateLiteratureArgs,
        UpdateMissionArgs,
        UpdateStatusArgs,
        UpdateMissionStatusArgs,
        BulkUpdateArgs,
        SupersedeDecisionArgs,
        LinkSupersessionArgs,
        PresentDecisionArgs,
        RecordPiSelectionArgs,
        RecordOutcomeArgs,
        EnrichDoiArgs,
        LinkLiteratureToZoteroArgs,
        ProcessPaperArgs,
        SubmitReportArgs,
        AdvanceRqArgs,
        SubmitCheckpointArgs,
        ResolveCheckpointArgs,
        EvaluateGateArgs,
        ResetSessionArgs,
        SessionDigestArgs,
        # ===== Batch D — REVIEW/MAINT/HOOKS/WORKSPACE (14) =====
        AssignClaimsToClusterArgs,
        SplitClusterArgs,
        MergeClustersArgs,
        ReviewClaimsArgs,
        ReviewClusterArgs,
        ResolveContradictionArgs,
        HookEnableArgs,
        HookDisableArgs,
        HookDeleteArgs,
        BrainNotificationsClearArgs,
        BootstrapWorkspaceArgs,
        ScanWorkspaceArgs,
        FlagStaleArgs,
        EvictionSweepArgs,
    ],
    Field(discriminator="operation"),
]


__all__ = [
    # Bases
    "ProjectScopedArgs",
    "UnscopedArgs",
    "PaginatedFiltersMixin",
    # Batch A — Query models
    "QueryStatusArgs",
    "QueryContextArgs",
    "QuerySearchArgs",
    "QueryEntityArgs",
    "QueryJournalArgs",
    "QueryLiteratureArgs",
    "QueryMissionArgs",
    "QueryReportArgs",
    "QueryCheckpointsArgs",
    "QueryDecisionTreeArgs",
    "QueryCalibrationMetricsArgs",
    "QueryHooksArgs",
    "QueryHookExecutionsArgs",
    "QueryBrainNotificationsArgs",
    "QueryResearchMapArgs",
    "QueryReviewQueueArgs",
    "QueryClustersArgs",
    "QueryClaimsArgs",
    "QueryClaimScopeArgs",
    "QueryInterpretationCandidatesArgs",
    "QuerySourcesArgs",
    "QueryNoteAttributionHistoryArgs",
    "QueryExperimentsArgs",
    "QueryExperimentRunsArgs",
    "QueryExperimentObservationsArgs",
    "QueryPlanningBranchesArgs",
    "QueryPlanningResumeArgs",
    "QueryPlanningCompareArgs",
    "QueryPlanningArtifactVersionsArgs",
    "QueryPlanningArgumentWorkflowArgs",
    "QueryPlanningEvaluationWorkflowArgs",
    "QueryPlanningEvaluationEventsArgs",
    "QueryPlanningPromotionsArgs",
    "QuerySemanticPatchProposalsArgs",
    "QuerySemanticPatchSchemaArgs",
    "QueryManuscriptArgs",
    "QueryReferenceValidationStatusArgs",
    "ResolveEntitiesArgs",
    "QueryManuscriptContextArgs",
    "QueryManuscriptOutlineArgs",
    "QueryManuscriptReferenceManifestArgs",
    "QueryManuscriptReadinessArgs",
    "QueryManuscriptSpineArgs",
    "QueryManuscriptWritingCandidatesArgs",
    "QueryChangesSinceArgs",
    "QueryManuscriptImpactArgs",
    "QueryGraphArgs",
    "QueryEgoGraphArgs",
    "QueryGraphStatsArgs",
    "QueryGraphMermaidArgs",
    "QueryProvenanceArgs",
    "QueryMultiHopArgs",
    "QueryCollectReportContextArgs",
    "QueryStalenessImpactArgs",
    "QueryMissionGuardArgs",
    "QueryBeliefAsOfArgs",
    "QuerySummarizeArgs",
    "QueryGenerateSummaryArgs",
    "QueryEvidenceArgs",
    "QueryFreshnessArgs",
    "QueryContradictionsArgs",
    "QueryIntegrityArgs",
    "QueryPendingMaintenanceArgs",
    "QueryChangelogArgs",
    "QueryBootstrapReviewArgs",
    "QueryWorkspaceTreeArgs",
    "QueryWorkspaceScanArgs",
    "QueryListProjectsArgs",
    "QueryCapabilitiesArgs",
    "QueryHealthArgs",
    # Union
    "QueryArgsUnion",
    # Batch B — Record/Create write models
    "RecordNoteArgs",
    "IngestDocumentArgs",
    "RecordDecisionArgs",
    "RecordLiteratureArgs",
    "ImportBibtexArgs",
    "BatchImportArgs",
    "RegisterManuscriptArgs",
    "CreateManuscriptArgs",
    "UpdateManuscriptArgs",
    "UpsertArgumentSpineArgs",
    "ReplaceManuscriptReferenceManifestArgs",
    "RatifyManuscriptClaimArgs",
    "TransitionManuscriptPhaseArgs",
    "CreateManuscriptCheckpointArgs",
    "ResolveManuscriptCheckpointArgs",
    "RecordVerificationAttestationArgs",
    "CreateMissionArgs",
    "CreateClusterArgs",
    "CreateProjectArgs",
    "CreateGateArgs",
    "HookAddArgs",
    "ExtractClaimsArgs",
    "CreateInterpretationCandidateArgs",
    "RegisterSourceArgs",
    "AdmitSourceInterpretationArgs",
    "AddInterpretationHintArgs",
    "TriageInterpretationCandidateArgs",
    "SetClaimScopeArgs",
    "CreateExperimentArgs",
    "AppendExperimentPlanArgs",
    "TransitionExperimentArgs",
    "CreateExperimentRunArgs",
    "TransitionExperimentRunArgs",
    "RecordExperimentObservationArgs",
    "AddEvidenceLocatorArgs",
    "CreatePlanningBranchArgs",
    "TransitionPlanningBranchArgs",
    "AppendPlanningArtifactVersionArgs",
    "CreatePlanningEvaluationMissionArgs",
    "PreparePlanningEvaluationResultArgs",
    "PrepareManuscriptOutlineProposalArgs",
    "PromotePlanningResearchQuestionArgs",
    "PreparePlanningContributionArgs",
    "RatifyPlanningContributionArgs",
    "PrepareSemanticPatchContextArgs",
    "CreateSemanticPatchProposalArgs",
    "ApplySemanticPatchProposalArgs",
    "RejectSemanticPatchProposalArgs",
    "GenerateLMStudioSemanticPatchArgs",
    # Batch B partial union
    "BatchBExecuteUnion",
    # Batch D — Review / Maintenance / Hooks / Workspace write models
    "AssignClaimsToClusterArgs",
    "SplitClusterArgs",
    "MergeClustersArgs",
    "ReviewClaimsArgs",
    "ReviewClusterArgs",
    "ResolveContradictionArgs",
    "HookEnableArgs",
    "HookDisableArgs",
    "HookDeleteArgs",
    "BrainNotificationsClearArgs",
    "BootstrapWorkspaceArgs",
    "ScanWorkspaceArgs",
    "FlagStaleArgs",
    "EvictionSweepArgs",
    # Batch D partial union
    "BatchDExecuteUnion",
    # Batch C — UPDATE / LIFECYCLE / SUBMIT write models
    "UpdateNoteArgs",
    "CorrectNoteAttributionArgs",
    "UpdateDecisionArgs",
    "UpdateLiteratureArgs",
    "UpdateMissionArgs",
    "UpdateStatusArgs",
    "UpdateMissionStatusArgs",
    "BulkUpdateItem",
    "BulkUpdateArgs",
    "SupersedeDecisionArgs",
    "PresentDecisionArgs",
    "RecordPiSelectionArgs",
    "RecordOutcomeArgs",
    "EnrichDoiArgs",
    "LinkLiteratureToZoteroArgs",
    "ProcessPaperArgs",
    "SubmitReportArgs",
    "AdvanceRqArgs",
    "OrphanSupersedesArgs",
    "LinkSupersessionArgs",
    "SubmitCheckpointArgs",
    "ResolveCheckpointArgs",
    "EvaluateGateArgs",
    "ResetSessionArgs",
    "SessionDigestArgs",
    # Batch C partial union
    "BatchCExecuteUnion",
    # Final execute union
    "ExecuteArgsUnion",
]
