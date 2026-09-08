"""Claim and evidence cluster models (v2.0)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


# ── Claims ──────────────────────────────────────────────────

ClaimType = Literal["hypothesis", "evidence", "method", "result", "observation", "assumption"]

EvidenceStatus = Literal[
    "unassessed",
    "supported",
    "partially_supported",
    "inconclusive",
    "contradicted",
]

ScopeUncertainty = Literal["none", "low", "medium", "high", "unknown"]
ScopeExtensionPolicy = Literal["exact_only", "bounded"]
FalsifierStatus = Literal["unknown", "applicable", "not_applicable"]
ClaimScopeReviewStatus = Literal["draft", "reviewed"]
ClaimScopeReadiness = Literal["missing", "stale", "incomplete", "needs_review", "ready"]
ClaimScopeActor = Literal["pi", "brain", "executor", "web_ui", "llm"]
ClaimConditionKind = Literal[
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
ClaimConditionOperator = Literal[
    "equals",
    "one_of",
    "range",
    "at_least",
    "at_most",
    "present",
    "absent",
    "described_by",
]
ConditionScalar = str | int | float | bool


class ClaimScopeCondition(BaseModel):
    """One machine-labeled applicability condition for a canonical claim."""

    model_config = ConfigDict(extra="forbid")

    kind: ClaimConditionKind
    key: str = Field(min_length=1, max_length=128)
    operator: ClaimConditionOperator
    value: ConditionScalar | list[ConditionScalar]
    unit: str | None = Field(default=None, max_length=64)
    note: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def validate_operator_value(self) -> "ClaimScopeCondition":
        self.key = self.key.strip()
        if isinstance(self.value, str):
            self.value = self.value.strip()
            if not self.value:
                raise ValueError("condition value cannot be blank")
        elif isinstance(self.value, list):
            self.value = [item.strip() if isinstance(item, str) else item for item in self.value]
            if any(isinstance(item, str) and not item for item in self.value):
                raise ValueError("condition values cannot be blank")

        if self.operator == "one_of":
            if not isinstance(self.value, list) or not self.value:
                raise ValueError("one_of requires a non-empty list value")
        elif self.operator == "range":
            if not isinstance(self.value, list) or len(self.value) != 2:
                raise ValueError("range requires exactly two boundary values")
        elif self.operator in {"present", "absent"}:
            if not isinstance(self.value, bool):
                raise ValueError(f"{self.operator} requires a boolean value")
        elif isinstance(self.value, list):
            raise ValueError(f"{self.operator} requires a scalar value")

        if self.unit is not None:
            self.unit = self.unit.strip() or None
        if self.note is not None:
            self.note = self.note.strip() or None
        return self


class ClaimScopeWrite(BaseModel):
    """Append one immutable scope version with optimistic revision control."""

    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=0)
    actor: ClaimScopeActor
    reason: str = Field(min_length=1, max_length=10_000)
    conditions: list[ClaimScopeCondition] = Field(default_factory=list, max_length=100)
    uncertainty: ScopeUncertainty = "unknown"
    uncertainty_note: str | None = Field(default=None, max_length=4000)
    extension_policy: ScopeExtensionPolicy | None = None
    allowed_extensions: list[str] = Field(default_factory=list, max_length=100)
    prohibited_extensions: list[str] = Field(default_factory=list, max_length=100)
    falsifier_status: FalsifierStatus = "unknown"
    falsifier: str | None = Field(default=None, max_length=10_000)
    falsifier_rationale: str | None = Field(default=None, max_length=4000)
    disconfirming_claim_ids: list[str] = Field(default_factory=list, max_length=200)
    review_status: ClaimScopeReviewStatus = "draft"
    source_candidate_id: str | None = Field(default=None, max_length=128)

    @field_validator(
        "allowed_extensions",
        "prohibited_extensions",
        "disconfirming_claim_ids",
        mode="after",
    )
    @classmethod
    def normalize_unique_strings(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        for raw in values:
            value = raw.strip()
            if value and value not in seen:
                normalized.append(value)
                seen.add(value)
        return normalized

    @model_validator(mode="after")
    def validate_contract(self) -> "ClaimScopeWrite":
        self.reason = self.reason.strip()
        for field in (
            "uncertainty_note",
            "falsifier",
            "falsifier_rationale",
            "source_candidate_id",
        ):
            value = getattr(self, field)
            if isinstance(value, str):
                setattr(self, field, value.strip() or None)

        if self.extension_policy == "exact_only" and self.allowed_extensions:
            raise ValueError("exact_only scope cannot contain allowed_extensions")
        if self.falsifier_status == "applicable" and not self.falsifier:
            raise ValueError("applicable falsifier requires a falsifier statement")
        if self.falsifier_status == "not_applicable" and not self.falsifier_rationale:
            raise ValueError("not_applicable falsifier requires a rationale")

        if self.review_status == "reviewed":
            if self.actor not in {"pi", "brain", "web_ui"}:
                raise ValueError("reviewed scope requires pi, brain, or web_ui actor")
            if not self.conditions:
                raise ValueError("reviewed scope requires at least one condition")
            if self.uncertainty == "unknown":
                raise ValueError("reviewed scope must resolve uncertainty")
            if self.extension_policy is None:
                raise ValueError("reviewed scope requires an extension_policy")
            if self.extension_policy == "bounded" and not self.allowed_extensions:
                raise ValueError("bounded scope requires allowed_extensions")
            if not self.prohibited_extensions:
                raise ValueError("reviewed scope requires prohibited_extensions")
            if self.falsifier_status == "unknown":
                raise ValueError("reviewed scope must resolve falsifier applicability")
        return self


class ClaimScopeVersion(BaseModel):
    id: str
    claim_id: str
    project_id: str
    revision: int
    claim_content_hash: str
    conditions: list[ClaimScopeCondition] = Field(default_factory=list)
    uncertainty: ScopeUncertainty = "unknown"
    uncertainty_note: str | None = None
    extension_policy: ScopeExtensionPolicy | None = None
    allowed_extensions: list[str] = Field(default_factory=list)
    prohibited_extensions: list[str] = Field(default_factory=list)
    falsifier_status: FalsifierStatus = "unknown"
    falsifier: str | None = None
    falsifier_rationale: str | None = None
    disconfirming_claim_ids: list[str] = Field(default_factory=list)
    review_status: ClaimScopeReviewStatus = "draft"
    created_by: str
    reason: str
    source_candidate_id: str | None = None
    supersedes_scope_id: str | None = None
    created_at: str | None = None


class ClaimScopeFinding(BaseModel):
    code: str
    severity: Literal["block", "warn", "info"]
    message: str


class ClaimScopeHistory(BaseModel):
    claim_id: str
    project_id: str
    current_revision: int = 0
    scope_readiness: ClaimScopeReadiness = "missing"
    findings: list[ClaimScopeFinding] = Field(default_factory=list)
    current: ClaimScopeVersion | None = None
    versions: list[ClaimScopeVersion] = Field(default_factory=list)


class ClaimCreate(BaseModel):
    """Create a new claim (typically by the distillation pipeline).

    extra="forbid" defense-in-depth — see Mission C
    (mis_01KR43RX9KY11GAPTPPGK9XSDE) for context.
    """

    model_config = ConfigDict(extra="forbid")

    source_entry_id: str
    claim_type: ClaimType
    content: str
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    verified: bool = Field(
        default=False,
        description=(
            "Whether the extracted proposition is faithfully grounded in its "
            "source entry. This is extraction/grounding fidelity, not evidence support."
        ),
    )
    evidence_status: EvidenceStatus = Field(
        default="unassessed",
        description=(
            "Independent scientific evidence assessment. Defaults to unassessed; "
            "never infer support from verified=True."
        ),
    )
    source_offset_start: int | None = None
    source_offset_end: int | None = None


class ClaimUpdate(BaseModel):
    """Partial update for a claim.

    extra="forbid": undeclared fields raise 422 instead of silently stripping.
    See mis_01KQJH9MB65AR0GSVPQBT8707X (silent-write-failure fix) for context.
    """

    model_config = ConfigDict(extra="forbid")

    content: str | None = None
    claim_type: ClaimType | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    verified: bool | None = Field(
        default=None,
        description=(
            "Update extraction/grounding fidelity only; this field does not "
            "represent scientific evidence support."
        ),
    )
    evidence_status: EvidenceStatus | None = None
    stale: bool | None = None


class FreshnessProjection(BaseModel):
    staleness: str = "green"
    stale_reason: str | None = None
    staleness_reviewed_at: str | None = None
    staleness_verdict: str | None = None
    staleness_resolution: str | None = None
    staleness_resolution_journal_id: str | None = None
    staleness_resolved_by: str | None = None
    currentness: dict = Field(default_factory=dict)


class Claim(FreshnessProjection):
    """Full claim record from database."""

    id: str
    source_entry_id: str
    claim_type: str
    content: str
    confidence: float = 0.5
    verified: bool = Field(
        default=False,
        description=(
            "Extraction/grounding fidelity against source_entry_id; not a "
            "scientific-support verdict."
        ),
    )
    evidence_status: EvidenceStatus = "unassessed"
    contradicted: bool = Field(
        description=(
            "Server-derived graph projection. True when a same-project "
            "claim_edges row with relation=contradicts touches this claim."
        ),
    )
    stale: bool = False
    source_offset_start: int | None = None
    source_offset_end: int | None = None
    scope_revision: int = 0
    scope_readiness: ClaimScopeReadiness = "missing"
    scope_contract: ClaimScopeVersion | None = None
    scope_findings: list[ClaimScopeFinding] = Field(default_factory=list)
    project_id: str
    created_at: str | None = None
    updated_at: str | None = None


# ── Evidence Clusters ───────────────────────────────────────

ClusterConfidence = Literal["strong", "moderate", "emerging", "contested", "refuted"]


class EvidenceClusterCreate(BaseModel):
    """Create a new evidence cluster.

    extra="forbid" defense-in-depth — see Mission C
    (mis_01KR43RX9KY11GAPTPPGK9XSDE) for context.
    """

    model_config = ConfigDict(extra="forbid")

    research_question_id: str | None = None
    label: str
    synthesis: str | None = None
    confidence: ClusterConfidence = "emerging"


class EvidenceClusterUpdate(BaseModel):
    """Partial update for an evidence cluster.

    extra="forbid": undeclared fields raise 422 instead of silently stripping.
    See mis_01KQJH9MB65AR0GSVPQBT8707X (silent-write-failure fix) for context.
    """

    model_config = ConfigDict(extra="forbid")

    label: str | None = None
    synthesis: str | None = None
    confidence: ClusterConfidence | None = None
    needs_reprocessing: bool | None = None
    synthesized_by: Literal["llm", "brain"] | None = None
    research_question_id: str | None = None


class EvidenceCluster(FreshnessProjection):
    """Full evidence cluster record from database."""

    id: str
    research_question_id: str | None = None
    label: str
    synthesis: str | None = None
    confidence: str = "emerging"
    claim_count: int = 0
    gap_count: int = 0
    needs_reprocessing: bool = False
    synthesized_by: str = "llm"
    project_id: str
    created_at: str | None = None
    updated_at: str | None = None


# ── Claim Edges ─────────────────────────────────────────────

ClaimRelationType = Literal["member_of", "supports", "contradicts", "qualifies", "supersedes"]


class ClaimEdgeCreate(BaseModel):
    """Create a claim edge (relationship between claims or claim-to-cluster).

    extra="forbid" defense-in-depth — see Mission C
    (mis_01KR43RX9KY11GAPTPPGK9XSDE) for context.
    """

    model_config = ConfigDict(extra="forbid")

    source_claim_id: str
    target_claim_id: str | None = None
    cluster_id: str | None = None
    relation: ClaimRelationType
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)


class ClaimEdge(BaseModel):
    """Full claim edge record from database."""

    id: str
    source_claim_id: str
    target_claim_id: str | None = None
    cluster_id: str | None = None
    relation: str
    confidence: float = 0.5
    project_id: str = "proj_default"
    created_at: str | None = None
