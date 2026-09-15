// TypeScript interfaces matching RKA Pydantic models

// ---- Journal ----

// v2.0 canonical types
export type JournalType = "note" | "log" | "directive"
// Legacy types still accepted by the API (auto-mapped to v2 types)
export type LegacyJournalType =
  | "finding" | "insight" | "pi_instruction" | "exploration"
  | "idea" | "observation" | "hypothesis" | "methodology" | "summary"
export type AnyJournalType = JournalType | LegacyJournalType

export type Source = "brain" | "executor" | "pi" | "web_ui" | "llm"
export type Confidence = "hypothesis" | "tested" | "verified" | "superseded" | "retracted"
export type Importance = "critical" | "high" | "normal" | "low" | "archived"
export type JournalStatus = "draft" | "active" | "superseded" | "retracted"

export interface JournalEntry {
  id: string
  type: string
  content: string
  summary: string | null
  source: string
  phase: string | null
  related_decisions: string[] | null
  related_literature: string[] | null
  related_mission: string | null
  supersedes: string | null
  superseded_by: string | null
  confidence: string
  importance: string
  status: string
  pinned: boolean
  tags: string[]
  created_at: string | null
  updated_at: string | null
}

export interface JournalEntryCreate {
  content: string
  type?: AnyJournalType
  source?: Source
  phase?: string
  related_decisions?: string[]
  related_literature?: string[]
  related_mission?: string
  supersedes?: string
  confidence?: Confidence
  importance?: Importance
  status?: JournalStatus
  pinned?: boolean
  tags?: string[]
}

export interface JournalEntryUpdate {
  content?: string
  type?: AnyJournalType
  summary?: string
  confidence?: Confidence
  importance?: Importance
  status?: JournalStatus
  pinned?: boolean
  related_decisions?: string[]
  related_literature?: string[]
  related_mission?: string
  tags?: string[]
}

// ---- Decisions ----

export interface DecisionOption {
  label: string
  description?: string
  explored?: boolean
}

export type DecisionStatus = "active" | "abandoned" | "superseded" | "merged" | "revisit"
export type DecidedBy = "pi" | "brain" | "executor"
export type DecisionKind = "research_question" | "design_choice" | "decision" | "operational"

export interface Decision {
  id: string
  parent_id: string | null
  phase: string
  question: string
  options: DecisionOption[] | null
  chosen: string | null
  rationale: string | null
  decided_by: string
  status: string
  abandonment_reason: string | null
  related_missions: string[] | null
  related_literature: string[] | null
  related_journal: string[] | null
  superseded_by: string | null
  scope_version: number
  kind: string
  tags: string[]
  created_at: string | null
  updated_at: string | null
}

export interface DecisionCreate {
  question: string
  decided_by: DecidedBy
  phase: string
  options?: DecisionOption[]
  chosen?: string
  rationale?: string
  parent_id?: string
  related_missions?: string[]
  related_literature?: string[]
  related_journal?: string[]
  status?: DecisionStatus
  kind?: DecisionKind
  tags?: string[]
}

export interface DecisionUpdate {
  question?: string
  options?: DecisionOption[]
  chosen?: string
  rationale?: string
  status?: DecisionStatus
  abandonment_reason?: string
  related_missions?: string[]
  related_literature?: string[]
  related_journal?: string[]
  kind?: DecisionKind
  tags?: string[]
}

export interface DecisionTreeNode {
  id: string
  question: string
  status: string
  chosen: string | null
  phase: string
  children: DecisionTreeNode[]
}

// ---- Literature ----

export type LiteratureStatus = "to_read" | "reading" | "read" | "cited" | "excluded"

export interface Literature {
  id: string
  title: string
  authors: string[] | null
  year: number | null
  venue: string | null
  doi: string | null
  url: string | null
  bibtex: string | null
  pdf_path: string | null
  abstract: string | null
  status: string
  key_findings: string[] | null
  methodology_notes: string | null
  relevance: string | null
  relevance_score: number | null
  related_decisions: string[] | null
  added_by: string | null
  notes: string | null
  tags: string[]
  created_at: string | null
  updated_at: string | null
}

export interface LiteratureCreate {
  title: string
  authors?: string[]
  year?: number
  venue?: string
  doi?: string
  url?: string
  bibtex?: string
  abstract?: string
  status?: LiteratureStatus
  key_findings?: string[]
  methodology_notes?: string
  relevance?: string
  relevance_score?: number
  related_decisions?: string[]
  added_by?: "brain" | "executor" | "pi" | "import" | "web_ui"
  notes?: string
  tags?: string[]
}

export interface LiteratureUpdate {
  title?: string
  authors?: string[]
  year?: number
  venue?: string
  doi?: string
  url?: string
  abstract?: string
  status?: LiteratureStatus
  key_findings?: string[]
  methodology_notes?: string
  relevance?: string
  relevance_score?: number
  related_decisions?: string[]
  notes?: string
  tags?: string[]
}

// ---- Missions ----

export type TaskStatus = "pending" | "in_progress" | "complete" | "blocked" | "skipped"
export type MissionStatus = "pending" | "active" | "complete" | "partial" | "blocked" | "cancelled"

export interface MissionTask {
  description: string
  status?: TaskStatus
  commit_hash?: string | null
  completed_at?: string | null
}

export interface Mission {
  id: string
  phase: string
  objective: string
  tasks: MissionTask[] | null
  context: string | null
  acceptance_criteria: string | null
  scope_boundaries: string | null
  checkpoint_triggers: string | null
  status: string
  depends_on: string | null
  report: MissionReport | null
  iteration: number
  parent_mission_id: string | null
  motivated_by_decision: string | null
  tags: string[]
  created_at: string | null
  completed_at: string | null
}

export interface MissionCreate {
  phase: string
  objective: string
  tasks?: MissionTask[]
  context?: string
  acceptance_criteria?: string
  scope_boundaries?: string
  checkpoint_triggers?: string
  depends_on?: string
  motivated_by_decision?: string
  tags?: string[]
}

export interface MissionUpdate {
  status?: MissionStatus
  tasks?: MissionTask[]
  objective?: string
}

export interface MissionReport {
  mission_id: string
  tasks_completed: string[] | null
  findings: string[] | null
  anomalies: string[] | null
  questions: string[] | null
  codebase_state: string | null
  recommended_next: string | null
  submitted_at: string | null
}

export interface MissionReportCreate {
  tasks_completed?: string[]
  findings?: string[]
  anomalies?: string[]
  questions?: string[]
  codebase_state?: string
  recommended_next?: string
}

// ---- Checkpoints ----

export interface CheckpointOption {
  label: string
  description?: string
  consequence?: string
}

export interface Checkpoint {
  id: string
  mission_id: string | null
  task_reference: string | null
  type: string
  description: string
  context: string | null
  options: CheckpointOption[] | null
  recommendation: string | null
  blocking: boolean
  resolution: string | null
  resolved_by: string | null
  resolution_rationale: string | null
  linked_decision_id: string | null
  status: string
  created_at: string | null
  resolved_at: string | null
}

export interface CheckpointResolve {
  resolution: string
  resolved_by: "pi" | "brain"
  rationale?: string
  create_decision?: boolean
}

// ---- Events ----

export interface Event {
  id: string
  timestamp: string | null
  event_type: string
  entity_type: string
  entity_id: string
  actor: string
  summary: string
  caused_by_event: string | null
  caused_by_entity: string | null
  phase: string | null
  details: Record<string, unknown> | null
}

// ---- Project ----

export interface ProjectState {
  project_name: string
  project_description: string | null
  current_phase: string | null
  phases_config: string[] | null
  summary: string | null
  blockers: string | null
  metrics: Record<string, unknown> | null
  created_at: string | null
  updated_at: string | null
}

export interface ProjectInfo {
  id: string
  name: string
  description: string | null
  created_by: string | null
  created_at: string | null
  updated_at: string | null
}

export interface ProjectCreate {
  id?: string
  name: string
  description?: string
  phases_config?: string[]
}

export interface ProjectStateUpdate {
  project_name?: string
  project_description?: string
  current_phase?: string
  phases_config?: string[]
  summary?: string
  blockers?: string
  metrics?: Record<string, unknown>
}

export interface KnowledgePackImportResult {
  project_id: string
  project_name: string
  source_project_id: string
  imported_counts: Record<string, number>
  artifact_files_restored: number
}

export interface KnowledgePackDownload {
  blob: Blob
  filename: string
}

// ---- Context ----

export interface ContextRequest {
  topic?: string
  phase?: string
  depth?: "summary" | "detailed"
}

export interface ContextPackage {
  topic: string | null
  phase: string | null
  hot_entries: string[]
  warm_entries: string[]
  cold_entries: string[]
  sources: string[]
  narrative: string | null
  note: string | null
  token_estimate: number
}

// ---- Search ----

export interface SearchResult {
  entity_type: string
  entity_id: string
  title: string
  snippet: string
  score: number
}

export interface SearchRequest {
  query: string
  entity_types?: string[]
  limit?: number
}

// ---- Tags ----

export interface TagCount {
  tag: string
  count: number
}

// ---- Audit ----

export interface AuditEntry {
  id: number
  action: string
  entity_type: string
  entity_id: string | null
  actor: string | null
  details: Record<string, unknown> | null
  created_at: string | null
}

// ---- Health ----

/** Runtime retrieval status from the versioned public capability document. */
export interface CoreCapabilities {
  schema_version: string
  embedding: {
    available: boolean
    reason_unavailable: string | null
    search_mode: "lexical" | "hybrid"
  }
}

export interface HealthStatus {
  status: string
  version: string
  vec_available: boolean
}

// ---- Embedding configuration (v2.4.0, Mission D) ----

export type EmbeddingBackendKind = "fastembed" | "openai_compat" | "ollama"

export interface EmbeddingConfig {
  backend: EmbeddingBackendKind
  config: {
    base_url?: string
    model?: string
    model_name?: string
    api_key?: string
    dim?: number
    timeout_seconds?: number
    query_template?: string
    document_template?: string
    embedding_space_id?: string
    threads?: number
    cache_dir?: string
  }
  updated_at?: string | null
  updated_by?: string | null
}

export interface ConnectionTestResult {
  ok: boolean
  detail: string
  detected_dim: number | null
  latency_ms: number | null
}

export interface BackfillStatus {
  job_id: string | null
  state: "idle" | "pending" | "running" | "complete" | "failed"
  processed?: number
  total?: number
  started_at?: string
  elapsed_seconds?: number
  error?: string | null
}

export interface EmbeddingConfigError {
  error: "embedding_config_invalid"
  detail: string
  hint: string
}

// ---- Academic Import ----

export interface BibtexImportResult {
  total_parsed: number
  imported: Array<{ id: string; title: string }>
  skipped: Array<{ title: string; reason: string }>
  errors: Array<{ title: string; error: string }>
}

export interface MermaidExport {
  mermaid: string
}

// ---- Graph ----

export interface GraphNode {
  id: string
  type: string
  label: string
  status: string | null
  phase: string
  created_at: string
}

export interface GraphEdge {
  source: string
  target: string
  link_type: string
  created_at: string
}

export interface GraphData {
  nodes: GraphNode[]
  edges: GraphEdge[]
}

export interface GraphStats {
  node_counts: Record<string, number>
  total_nodes: number
  total_edges: number
  edge_counts_by_type: Record<string, number>
  claim_scope_readiness_counts: Partial<Record<ClaimScopeReadiness, number>>
}

// ---- Summaries ----

export interface SummaryResult {
  id: string
  scope_type: string
  scope_id: string | null
  granularity: string
  one_line: string
  paragraph: string
  narrative: string | null
  key_questions: string[]
  sources: Array<{ entity_type: string; entity_id: string; excerpt: string }>
  confidence: number
}

export interface ExplorationSummary {
  id: string
  scope_type: string
  scope_id: string | null
  granularity: string
  content: string
  produced_by: string | null
  confidence: number | null
  blessed: number
  source_refs: string | null
  created_at: string
  updated_at: string
}

// ---- QA ----

export interface QAResult {
  session_id: string
  log_id: string
  answer: string
  answer_type: string
  sources: Array<{ entity_type: string; entity_id: string; excerpt: string }>
  confidence: number
  followups: string[]
}

export interface QASession {
  id: string
  title: string | null
  created_by: string | null
  created_at: string
  logs: Array<{
    id: string
    question: string
    answer: string
    confidence: number | null
    created_at: string
  }>
}

// ---- v2.0: Claims & Research Map ----

export type ClaimType = "hypothesis" | "evidence" | "method" | "result" | "observation" | "assumption"
export type ClusterConfidence = "strong" | "moderate" | "emerging" | "contested" | "refuted"
export type ClaimScopeUncertainty = "none" | "low" | "medium" | "high" | "unknown"
export type ClaimScopeExtensionPolicy = "exact_only" | "bounded"
export type ClaimFalsifierStatus = "unknown" | "applicable" | "not_applicable"
export type ClaimScopeReviewStatus = "draft" | "reviewed"
export type ClaimScopeReadiness = "missing" | "stale" | "incomplete" | "needs_review" | "ready"
export type ClaimConditionKind =
  | "dataset"
  | "population"
  | "platform"
  | "environment"
  | "threat_model"
  | "baseline"
  | "workload"
  | "metric"
  | "parameter"
  | "assumption"
  | "time_window"
  | "other"
export type ClaimConditionOperator =
  | "equals"
  | "one_of"
  | "range"
  | "at_least"
  | "at_most"
  | "present"
  | "absent"
  | "described_by"

export interface ClaimScopeCondition {
  kind: ClaimConditionKind
  key: string
  operator: ClaimConditionOperator
  value: string | number | boolean | Array<string | number | boolean>
  unit?: string | null
  note?: string | null
}

export interface ClaimScopeVersion {
  id: string
  claim_id: string
  project_id: string
  revision: number
  claim_content_hash: string
  conditions: ClaimScopeCondition[]
  uncertainty: ClaimScopeUncertainty
  uncertainty_note: string | null
  extension_policy: ClaimScopeExtensionPolicy | null
  allowed_extensions: string[]
  prohibited_extensions: string[]
  falsifier_status: ClaimFalsifierStatus
  falsifier: string | null
  falsifier_rationale: string | null
  disconfirming_claim_ids: string[]
  review_status: ClaimScopeReviewStatus
  created_by: string
  reason: string
  source_candidate_id: string | null
  supersedes_scope_id: string | null
  created_at: string | null
}

export interface ClaimScopeFinding {
  code: string
  severity: "block" | "warn" | "info"
  message: string
}

export interface ClaimScopeHistory {
  claim_id: string
  project_id: string
  current_revision: number
  scope_readiness: ClaimScopeReadiness
  findings: ClaimScopeFinding[]
  current: ClaimScopeVersion | null
  versions: ClaimScopeVersion[]
}

export interface ClaimScopeWrite {
  expected_revision: number
  actor: "pi" | "brain" | "executor" | "web_ui" | "llm"
  reason: string
  conditions: ClaimScopeCondition[]
  uncertainty: ClaimScopeUncertainty
  uncertainty_note?: string
  extension_policy?: ClaimScopeExtensionPolicy
  allowed_extensions: string[]
  prohibited_extensions: string[]
  falsifier_status: ClaimFalsifierStatus
  falsifier?: string
  falsifier_rationale?: string
  disconfirming_claim_ids: string[]
  review_status: ClaimScopeReviewStatus
}

export interface Claim {
  id: string
  source_entry_id: string
  claim_type: string
  content: string
  confidence: number
  verified: boolean
  evidence_status: string
  contradicted: boolean
  stale: boolean
  source_offset_start: number | null
  source_offset_end: number | null
  source_type?: string | null
  source_actor?: string | null
  scope_revision: number
  scope_readiness: ClaimScopeReadiness
  scope_contract: ClaimScopeVersion | null
  scope_findings: ClaimScopeFinding[]
  project_id?: string
  created_at?: string | null
  updated_at?: string | null
}

// ---- M1: Interpretation staging upstream of canonical claims ----

export type InterpretationSourceType = "journal" | "literature" | "artifact"
export type InterpretationLocatorKind =
  | "text_offset"
  | "page"
  | "line_range"
  | "section"
  | "url_fragment"
  | "record"
export type EpistemicKind =
  | "observation"
  | "reported_fact"
  | "inference"
  | "hypothesis"
  | "plan"
  | "author_intent"
export type InterpretationReviewStatus = "pending" | "in_review" | "resolved"
export type InterpretationDisposition =
  | "promoted"
  | "merged"
  | "deferred"
  | "rejected"
  | "classified_decision"
  | "classified_plan"
  | "classified_author_intent"
  | "evidence_mission_requested"
export type InterpretationTriageAction =
  | "start_review"
  | "promote"
  | "merge"
  | "defer"
  | "reject"
  | "classify_decision"
  | "classify_plan"
  | "classify_author_intent"
  | "request_evidence_mission"
  | "reopen"
  | "revoke_promotion"

export interface InterpretationCandidate {
  id: string
  project_id: string
  source_type: InterpretationSourceType
  source_id: string
  locator_kind: InterpretationLocatorKind
  locator_start: number | null
  locator_end: number | null
  locator_value: string | null
  statement: string
  epistemic_kind: EpistemicKind
  scope_conditions: string[]
  uncertainty: "none" | "low" | "medium" | "high" | "unknown"
  uncertainty_note: string | null
  falsifier: string | null
  proposed_claim_type: ClaimType | null
  created_by: string
  extraction_tool: string
  extraction_model: string | null
  review_status: InterpretationReviewStatus
  disposition: InterpretationDisposition | null
  disposition_reason: string | null
  disposition_target_type: string | null
  disposition_target_id: string | null
  reviewed_by: string | null
  reviewed_at: string | null
  revision: number
  duplicate_hint_count: number
  conflict_hint_count: number
  active_claim_id: string | null
  created_at: string | null
  updated_at: string | null
}

export interface InterpretationHint {
  id: string
  project_id: string
  candidate_id: string
  related_candidate_id: string
  kind: "duplicate" | "conflict"
  confidence: number
  rationale: string
  created_by: string
  created_at: string | null
}

export interface InterpretationReviewEvent {
  id: string
  project_id: string
  candidate_id: string
  action: string
  from_status: InterpretationReviewStatus | null
  to_status: InterpretationReviewStatus
  disposition: InterpretationDisposition | null
  actor: string
  reason: string | null
  target_type: string | null
  target_id: string | null
  candidate_revision: number
  created_at: string | null
}

export interface InterpretationPromotion {
  id: string
  project_id: string
  candidate_id: string
  claim_id: string
  status: "active" | "revoked"
  promoted_by: string
  promotion_reason: string
  promoted_at: string | null
  revoked_by: string | null
  revocation_reason: string | null
  revoked_at: string | null
}

export interface InterpretationCandidateDetail extends InterpretationCandidate {
  hints: InterpretationHint[]
  review_events: InterpretationReviewEvent[]
  promotions: InterpretationPromotion[]
}

export interface InterpretationTriageRequest {
  action: InterpretationTriageAction
  expected_revision: number
  actor: "pi" | "brain" | "executor" | "web_ui"
  reason?: string
  target_candidate_id?: string
  target_entity_id?: string
  grounding_verified?: boolean
  claim_confidence?: number
}

export interface EvidenceCluster {
  id: string
  research_question_id: string | null
  label: string
  synthesis: string | null
  confidence: string
  claim_count: number
  gap_count: number
  needs_reprocessing: boolean
  synthesized_by: string
  project_id?: string
  created_at?: string | null
  updated_at?: string | null
}

export interface EvidenceClusterUpdateRequest {
  label?: string
  synthesis?: string | null
  confidence?: ClusterConfidence
  needs_reprocessing?: boolean
  synthesized_by?: "llm" | "brain"
  research_question_id?: string | null
}

export interface ResearchQuestion {
  id: string
  question: string
  status: string
  phase: string | null
  cluster_count: number
  total_claims: number
  gap_count: number
  contradiction_count: number
  created_at: string | null
  clusters?: Array<{
    id: string
    label: string
    confidence: string
    claim_count: number
    staleness: string
  }>
}

export interface ResearchMapData {
  research_questions: ResearchQuestion[]
  unassigned_clusters: Array<{
    id: string
    label: string
    claim_count: number
    confidence: string
  }>
  summary: {
    total_rqs: number
    total_clusters: number
    total_claims: number
    total_gaps: number
    total_contradictions: number
    pending_review: number
  }
}

// ---- Native manuscript workbench (read-only prototype) ----

export interface NativeManuscript {
  id: string
  project_id: string
  title: string
  abstract: string | null
  venue: string | null
  phase: string
  state: string
  workspace_ref: string | null
  revision: number
  legacy_journal_id: string | null
  created_at: string
  updated_at: string
}

export interface ManuscriptEvidenceBinding {
  role: "support" | "qualifier" | "counterevidence"
  evidence_claim_id: string
  content?: string | null
  source_entry_id?: string | null
  confidence?: number | null
  verified?: number | boolean | null
  evidence_status?: string | null
  stale?: number | boolean | null
  staleness?: string | null
  contradicted?: number | boolean | null
  source_current?: number | boolean | null
  source_is_manuscript?: number | boolean | null
  scope_readiness?: ClaimScopeReadiness
  scope_contract?: ClaimScopeVersion | null
  scope_findings?: ClaimScopeFinding[]
  supported_proposition?: string | null
  warrant?: string | null
}

export type ManuscriptUnitRole =
  | "unspecified" | "section" | "argument_block" | "paragraph_plan"
  | "result" | "caption" | "appendix" | "other"

export type ManuscriptRhetoricalMove =
  | "unspecified" | "frame_problem" | "establish_gap" | "state_insight"
  | "explain_mechanism" | "address_challenge" | "present_innovation"
  | "pose_research_question" | "state_contribution" | "describe_method"
  | "present_result" | "interpret_result" | "compare_prior_work"
  | "state_limitation" | "transition" | "summarize" | "other"

export interface ManuscriptCitationUse {
  id?: string
  reference_member_id?: string
  literature_id?: string
  citation_key: string
  citation_role: "imports" | "bounds" | "baseline" | "extends" | "refutes"
  supported_proposition: string
  verification_state: "unverified" | "self_attested" | "verified" | "rejected"
  verification_current?: boolean
  comparison_axis: string | null
  stable_identifier?: string | null
}

export interface ManuscriptClaimContext {
  id: string
  local_key: string
  kind: string
  state: string
  version: number | null
  exact_wording: string | null
  allowed_wording: string | null
  prohibited_wording: string[]
  conditions: string[]
  falsification_criteria: string[]
  evidence: ManuscriptEvidenceBinding[]
  ratifications: Array<{
    decision_id: string
    claim_version: number
    decision_status: string
    decided_by: string
    chosen: string | null
    superseded_by: string | null
  }>
  unit_links: Array<{
    unit_id: string
    unit_local_key: string
    unit_kind: string
    unit_location: string
    relationship: string
  }>
}

export interface ManuscriptUnitContext {
  id: string
  local_key: string
  kind: string
  location: string
  title: string | null
  artifact_ref: string | null
  allowed_interpretation: string | null
  prohibited_interpretation: string | null
  sequence: number
  status: string
  evidence: ManuscriptEvidenceBinding[]
  artifact_binding?: Record<string, unknown>
  outline_level: number
  unit_role: ManuscriptUnitRole
  rhetorical_move: ManuscriptRhetoricalMove
  parent_unit_key: string | null
  communicative_job: string | null
  intended_takeaway: string | null
  transition_from_previous: string | null
  quick_reader_role: string | null
  evidence_plan: string[]
  figure_intentions: string[]
  table_intentions: string[]
  citation_intentions: string[]
  blocker: string | null
  citations: ManuscriptCitationUse[]
}

export interface ManuscriptOutlineClaimLink {
  claim_id: string
  claim_key: string
  claim_version: number | null
  exact_wording: string | null
  relationship: string
  /** Private planning context; it is not manuscript prose. */
  evidence: ManuscriptEvidenceBinding[]
  unallocated_adverse_evidence: ManuscriptEvidenceBinding[]
}

export interface ManuscriptOutlineUnit extends ManuscriptUnitContext {
  claims: ManuscriptOutlineClaimLink[]
  child_unit_keys: string[]
  completeness: "complete" | "needs_review"
  missing: string[]
}

export interface ManuscriptOutline {
  schema_version: "rka.manuscript-outline/v1"
  project_id: string
  manuscript_id: string
  manuscript_revision: number
  units: ManuscriptOutlineUnit[]
  outline_checkpoint: Record<string, unknown> | null
  academic_readiness: {
    schema_version: "rka.academic-readiness/v2"
    ready: boolean
    dimensions: Array<{
      name: string
      verdict: "pass" | "warn" | "not_applicable"
      blocking: boolean
      findings: Array<{
        code: string
        message: string
        blocking: boolean
        unit_id?: string
        unit_key?: string
        claim_id?: string
        claim_key?: string
        evidence_claim_id?: string
        role?: "support" | "qualifier" | "counterevidence"
        citation_id?: string
        citation_key?: string
      }>
    }>
    policy: {
      deterministic_only: true
      judgment_checks: "advisory_only"
      verdicts: Array<"pass" | "warn" | "not_applicable">
    }
  }
  summary: {
    active_units: number
    complete_units: number
    units_needing_review: number
    levels: number[]
    rationale_complete: boolean
    /** @deprecated Use rationale_complete; this does not report checkpoint state. */
    checkpoint_ready: boolean
  }
  policy: {
    canonical_unit_identity: "mun_"
    mutation: "semantic_patch_then_explicit_apply"
    checkpoint_resolution: "explicit_pi_decision"
    file_writes: false
  }
}

// ---- Local manuscript Markdown/LaTeX source synchronization ----

export interface ManuscriptSourceFinding {
  severity: "error" | "warning"
  code: string
  message: string
  unit_id?: string
  line?: number
  kind?: "claim" | "evidence" | "citation"
  value?: string
}

export interface ManuscriptSourceAnchor {
  unit_id: string
  start_line: number
  content_start_line: number
  content_end_line: number
  end_line: number
  content_hash: string
}

export interface ManuscriptSourceProvenance {
  unit_id: string
  kind: "claim" | "evidence" | "citation"
  value: string
  line: number
  verified: boolean
}

export interface ManuscriptSourceFile {
  schema_version: "rka.manuscript-source/v1"
  project_id: string
  manuscript_id: string
  relative_path: string
  source_format: "markdown" | "latex"
  content: string
  content_hash: string
  size_bytes: number
  mode: number
  anchors: ManuscriptSourceAnchor[]
  provenance: ManuscriptSourceProvenance[]
  findings: ManuscriptSourceFinding[]
}

export interface ManuscriptSourceOverview {
  schema_version: "rka.manuscript-source/v1"
  project_id: string
  manuscript_id: string
  workspace_ref: string
  files: Array<{
    relative_path: string
    source_format: "markdown" | "latex"
    content_hash: string
    size_bytes: number
    anchor_count: number
    finding_count: number
    blocking: boolean
  }>
  warnings: Array<{ relative_path?: string; code: string; message: string }>
  quick_reader: Array<{
    unit_id: string
    local_key: string
    title: string | null
    communicative_job: string | null
    intended_takeaway: string | null
    quick_reader_role: string | null
    anchors: Array<{
      relative_path: string
      start_line: number
      end_line: number
      content_hash: string
    }>
    anchor_state: "linked" | "missing"
  }>
  private_reviewer_risks: Array<{
    kind: string
    unit_id: string
    message: string
    claim_id?: string
    evidence_claim_id?: string
    citation_key?: string
    content?: string | null
    items?: string[]
    verification_state?: string
    verification_current?: boolean
  }>
  public_private_boundary: {
    draft_source: string
    private_reviewer_risks: string
  }
}

export interface ManuscriptSourceProposal {
  schema_version: "rka.manuscript-source-proposal/v1"
  id: string
  project_id: string
  manuscript_id: string
  status: "proposed" | "applied" | "rejected" | "conflicted" | "superseded" | "expired"
  revision: number
  origin: "human" | "host_agent" | "lm_studio"
  relative_path: string
  source_format: "markdown" | "latex"
  base_content_hash: string | null
  proposed_content?: string
  proposed_content_hash: string
  created_by: "pi" | "brain" | "executor" | "web_ui"
  reason: string
  validation_findings: ManuscriptSourceFinding[]
  recovery_manifest_path: string | null
  created_at: string
  updated_at: string
  events?: Array<{
    id: string
    proposal_revision: number
    action: string
    actor: string
    reason: string
    details: Record<string, unknown>
    created_at: string
  }>
}

export interface ManuscriptSourceProposalCreate {
  origin: "human"
  relative_path: string
  expected_content_hash: string | null
  content: string
  created_by: "web_ui"
  reason: string
  supersedes_proposal_id?: string
}

export interface OutlineUnitPatch {
  title?: string | null
  location?: string
  outline_level?: number
  unit_role?: ManuscriptUnitRole
  rhetorical_move?: ManuscriptRhetoricalMove
  parent_unit_key?: string | null
  communicative_job?: string | null
  intended_takeaway?: string | null
  transition_from_previous?: string | null
  quick_reader_role?: string | null
  evidence_plan?: string[]
  figure_intentions?: string[]
  table_intentions?: string[]
  citation_intentions?: string[]
  blocker?: string | null
}

export interface OutlineChildDraft {
  local_key: string
  title: string
  location: string
  unit_role?: ManuscriptUnitRole
  rhetorical_move?: ManuscriptRhetoricalMove
  communicative_job: string
  intended_takeaway: string
  transition_from_previous?: string | null
  quick_reader_role?: string | null
  evidence_plan: string[]
  figure_intentions?: string[]
  table_intentions?: string[]
  citation_intentions?: string[]
  blocker?: string | null
  claim_keys?: string[]
  support_ids?: string[]
  qualifier_ids?: string[]
  counterevidence_ids?: string[]
}

export interface OutlineProposalRequest {
  expected_revision: number
  action: "edit" | "expand" | "condense" | "reorder"
  reason: string
  origin?: "human" | "host_agent" | "lm_studio"
  provider?: string | null
  model?: string | null
  boundary?: "none" | "host_conversation" | "local_loopback"
  context_manifest_id?: string | null
  unit_key?: string
  patch?: OutlineUnitPatch
  children?: OutlineChildDraft[]
  descendant_keys?: string[]
  ordered_unit_keys?: string[]
}

export interface OutlineProposalResult {
  schema_version: "rka.outline-proposal/v1"
  proposal: SemanticPatchProposal
  impact: {
    action: string
    affected_unit_keys: string[]
    canonical_mutation: false
    apply_operation: "semantic_patch_apply"
    [key: string]: unknown
  }
}

export interface ManuscriptContext {
  schema_version: "rka.manuscript-context/v1"
  project_id: string
  manuscript: NativeManuscript
  claims: ManuscriptClaimContext[]
  units: ManuscriptUnitContext[]
  checkpoints: Array<Record<string, unknown>>
  verification_attestations: Array<Record<string, unknown>>
  reference_validations: Array<Record<string, unknown>>
  reference_manifest: Record<string, unknown>
  authoritative_source: "rka"
}

export interface ManuscriptSpineClaim {
  claim_id: string
  rka_manuscript_claim_id: string
  version: number | null
  claim_type: string
  status: string
  text: string | null
  allowed_wording: string | null
  prohibited_wording: string[]
  conditions: string[]
  falsification_criteria: string[]
  ratified_by: string | null
  evidence_ids: string[]
  qualifier_ids: string[]
  counterevidence_ids: string[]
  manuscript_units: string[]
}

export interface ManuscriptSpineUnit {
  unit_id: string
  rka_manuscript_unit_id: string
  kind: string
  location: string
  artifact_ref: string | null
  allowed_interpretation: string | null
  prohibited_interpretation: string | null
  status: string
  evidence_ids: string[]
  qualifier_ids: string[]
  counterevidence_ids: string[]
  evidence: {
    support: Array<Pick<ManuscriptEvidenceBinding, "evidence_claim_id" | "supported_proposition" | "warrant">>
    qualifier: Array<Pick<ManuscriptEvidenceBinding, "evidence_claim_id" | "supported_proposition" | "warrant">>
    counterevidence: Array<Pick<ManuscriptEvidenceBinding, "evidence_claim_id" | "supported_proposition" | "warrant">>
  }
  citations: ManuscriptCitationUse[]
  claim_ids: string[]
  sequence: number
  outline_level: number
  unit_role: ManuscriptUnitRole
  rhetorical_move: ManuscriptRhetoricalMove
  parent_unit_key: string | null
  communicative_job: string | null
  intended_takeaway: string | null
  transition_from_previous: string | null
  quick_reader_role: string | null
  evidence_plan: string[]
  figure_intentions: string[]
  table_intentions: string[]
  citation_intentions: string[]
  blocker: string | null
}

export interface ManuscriptSpine {
  schema_version: "rka-claim-spine/v2"
  authoritative_source: "rka"
  project_id: string
  manuscript_id: string
  manuscript_revision: number
  claims: ManuscriptSpineClaim[]
  units: ManuscriptSpineUnit[]
  reference_manifest: Record<string, unknown>
}

export interface WritingCandidateClaim {
  claim_id: string
  claim_type: string
  status: "candidate"
  text: string
  allowed_wording: string
  prohibited_wording: string[]
  ratified_by: null
  evidence_ids: string[]
  qualifier_ids: string[]
  counterevidence_ids: string[]
  manuscript_units: string[]
  scope_contract_ids: string[]
}

export interface WritingCandidateCluster {
  cluster_id: string
  research_question_id: string | null
  research_question: string | null
  rq_lifecycle: string | null
  label: string
  synthesis: string | null
  confidence: string
  synthesized_by: string
  support_claim_ids: string[]
  representative_claim_ids: string[]
  qualifier_claim_ids: string[]
  counterevidence_claim_ids: string[]
  duplicate_support_groups: string[][]
  disposition: "eligible" | "needs_review"
  blockers: string[]
}

export interface ManuscriptWritingCandidates {
  schema_version: "rka.writing-evidence-candidates/v1"
  project_id: string
  manuscript_id: string
  manuscript_revision: number
  policy: Record<string, unknown>
  clusters: WritingCandidateCluster[]
  excluded_claims: Array<{
    claim_id: string
    cluster_id: string
    reasons: string[]
  }>
  candidate_spine: {
    claims: WritingCandidateClaim[]
    units: unknown[]
  }
  candidate_lineage: Record<
    string,
    {
      cluster_id: string
      research_question_id: string | null
      representative_claim_ids: string[]
      scope_contract_ids: string[]
    }
  >
  summary: {
    clusters_total: number
    clusters_eligible: number
    clusters_needing_review: number
    claims_excluded: number
  }
  required_human_actions: string[]
  mode: "server_attested_read_only_proposal"
}

export interface ManuscriptReadinessFinding {
  verdict: "PASS" | "WARN" | "BLOCK" | "ERROR"
  code: string
  message: string
  claim_id?: string
  unit_id?: string
  citation_key?: string
  literature_id?: string
}

export interface ManuscriptReadiness {
  schema_version?: string
  project_id: string
  manuscript_id: string
  target_phase: string
  ready: boolean
  verdict: "PASS" | "WARN" | "BLOCK" | "ERROR"
  findings: ManuscriptReadinessFinding[]
}

export interface ManuscriptImpact {
  project_id: string
  manuscript_id: string
  requested_since_cursor: number
  next_cursor: number
  has_more: boolean
  relevant_changes: Array<Record<string, unknown>>
  affected_claims: Array<Record<string, unknown>>
  affected_units: Array<Record<string, unknown>>
  changed_sources: Array<Record<string, unknown>>
  [key: string]: unknown
}

export interface ClusterContradiction {
  id: string
  confidence: number
  source_claim_id: string
  source_claim_content: string
  source_entry_id: string | null
  target_claim_id: string | null
  target_claim_content: string | null
  target_source_entry_id: string | null
  created_at: string | null
}

export interface ResearchQuestionReference {
  id: string
  question: string
}

export interface ResearchMapClusterDetail extends EvidenceCluster {
  research_question: ResearchQuestionReference | null
  claims: Claim[]
  contradictions: ClusterContradiction[]
  review_items: ReviewItem[]
}

export interface ReviewItem {
  id: string
  item_type: string
  item_id: string
  flag: string
  context: unknown
  priority: number
  status: string
  raised_by: string
  resolved_by: string | null
  resolution: string | null
  project_id: string
  created_at: string | null
  resolved_at: string | null
}

// ---- Verification & Research Health (eval-v3 themes B/C) ----

export interface StalenessImpactNode {
  id: string
  type: string
  label: string
  status: string | null
  depth: number
  via: { from: string; link_type: string }
}

export interface StalenessImpact {
  root: string
  root_status: string | null
  root_is_stale: boolean
  impacted: StalenessImpactNode[]
  counts: Record<string, number>
  max_depth: number
}

export type MissionGuardKind = "retracted" | "superseded" | "contradicted"

export interface MissionGuardWarning {
  id: string
  kind: MissionGuardKind
  relevance: number
  excerpt: string
  guidance: string
  superseded_by?: string | null
  contradicts?: string | null
}

export interface MissionGuard {
  mission_id: string
  warnings: MissionGuardWarning[]
  checked: { stale_journal: number; contradictions: number }
}

export interface BeliefAsOfDecision {
  id: string
  question: string
  chosen: string
}

export interface BeliefAsOfJournalEntry {
  id: string
  excerpt: string
  confidence_now: string
  approximate?: boolean
}

export interface BeliefChange {
  id: string
  type: string
  was: string
  changed_at: string | null
  superseded_by?: string | null
  change?: string
  approximate?: boolean
}

export interface BeliefAsOf {
  as_of: string
  then_current: {
    decisions: BeliefAsOfDecision[]
    journal_count: number
    journal: BeliefAsOfJournalEntry[]
  }
  changed_since: BeliefChange[]
  note: string
}

export interface CoverageCounts {
  covered: number
  total: number
}

export interface ResearchHealth {
  provenance_coverage: {
    decisions_with_evidence_pct: number
    decisions: CoverageCounts
    missions_with_motivation_pct: number
    missions: CoverageCounts
    claims_with_source_pct: number
    claims: CoverageCounts
    supersede_chain_integrity: {
      superseded_decisions: number
      orphaned_pointers: number
    }
  }
  research_debt_trajectory_weekly: Array<{
    week: string
    created: number
    covered: number
  }>
  mission_cycle: {
    completed: number
    avg_days_to_complete: number | null
    max_days_to_complete: number | null
    checkpoints_total: number
    checkpoints_open: number
  }
  bookkeeping_overhead: {
    recorded_actions: Record<string, number>
    write_share_pct: number
  }
}

export interface ReportContextRequest {
  description: string
  angle_queries?: string[]
  max_depth?: number
  max_nodes?: number
}

export type ReportContextInclusion =
  | { via: "search"; query: string; rank: number }
  | { via: "link"; from: string; link_type: string }

export interface ReportContextNode {
  id: string
  type: string
  label: string
  score: number
  depth: number
  included_via: ReportContextInclusion
  tags: string[]
  status?: string | null
}

export interface ReportContextResult {
  nodes: ReportContextNode[]
  queries: string[]
  seed_count: number
  expanded_count: number
  truncated: boolean
}

export interface StalenessReviewFiling {
  stale_roots: number
  filed: number
  items: Array<{ review_id: string; item_id: string; stale_root: string }>
}

export interface LinkSupportFinding {
  item_type: string
  item_id: string
  label: string
  support: number
  detail: string
}

export interface LinkSupportAudit {
  checked_decisions: number
  checked_clusters: number
  unsupported: LinkSupportFinding[]
  method: string
}

// ---- Provisional manuscript planning ----

export type PlanningBranchState = "active" | "selected" | "archived" | "superseded"
export type PlanningStage =
  | "seed" | "paragraph_spine" | "problem_scope" | "landscape_gap"
  | "response_mechanism" | "challenge_innovation" | "rq_contribution"
  | "evaluation" | "outline" | "review"
export type PlanningLifecycle =
  | "candidate" | "reviewed" | "selected" | "parked" | "superseded" | "archived"

export interface PlanningBranch {
  id: string
  project_id: string
  manuscript_id: string | null
  context_key: string
  name: string
  purpose: string
  parent_branch_id: string | null
  parent_branch_revision: number | null
  base_manuscript_revision: number | null
  state: PlanningBranchState
  revision: number
  created_by: string
  created_at: string
  updated_at: string
}

export interface PlanningEvidenceBinding {
  id: string
  entity_type: string
  entity_id: string
  role: string
  source_version: string | null
  locator_kind: string | null
  locator_value: string | null
  locator_start: number | null
  locator_end: number | null
  content_hash: string | null
  ordinal: number
  note: string | null
}

export interface PlanningArtifactVersion {
  id: string
  artifact_id: string
  branch_id: string
  project_id: string
  version: number
  branch_revision: number
  lifecycle: PlanningLifecycle
  summary: string
  payload: Record<string, unknown>
  origin: string
  provider: string | null
  model: string | null
  context_hash: string | null
  unresolved_items: string[]
  readiness_state: "blocked" | "in_progress" | "ready"
  readiness_missing: string[]
  readiness_notes: string | null
  promotion_target_type: string | null
  promotion_target_id: string | null
  supersedes_version_id: string | null
  derived_from_version_id: string | null
  evidence_bindings: PlanningEvidenceBinding[]
}

export interface PlanningArtifact {
  id: string
  branch_id: string
  project_id: string
  local_key: string
  stage_type: PlanningStage
  current_version: number
  current_version_id: string
  resolved_from_branch_id: string
  is_inherited: boolean
  version: PlanningArtifactVersion
}

export interface PlanningBranchEvent {
  id: string
  branch_id: string
  project_id: string
  branch_revision: number
  action: string
  from_state: PlanningBranchState | null
  to_state: PlanningBranchState
  actor: string
  reason: string
  details: Record<string, unknown>
  created_at: string
}

export interface PlanningContext {
  schema_version: string
  project_id: string
  branch: PlanningBranch
  effective_artifacts: PlanningArtifact[]
  parking_lot: PlanningArtifact[]
  events: PlanningBranchEvent[]
}

export interface PlanningComparisonRef {
  artifact_id: string
  version_id: string
  version: number
  lifecycle: PlanningLifecycle
  summary: string
  resolved_from_branch_id: string
  is_inherited: boolean
}

export interface PlanningComparisonChange {
  stage_type: PlanningStage
  local_key: string
  status: "added" | "removed" | "changed" | "unchanged"
  base: PlanningComparisonRef | null
  other: PlanningComparisonRef | null
}

export interface PlanningBranchComparison {
  schema_version: string
  project_id: string
  context_key: string
  base_branch: PlanningBranch
  other_branch: PlanningBranch
  summary: Record<"added" | "removed" | "changed" | "unchanged", number>
  changes: PlanningComparisonChange[]
}

export interface PlanningBranchCreate {
  manuscript_id?: string
  name: string
  purpose: string
  parent_branch_id?: string
  created_by: "pi" | "brain" | "executor" | "web_ui" | "llm" | "import"
  reason: string
}

export interface PlanningBranchTransition {
  expected_revision: number
  target_state: PlanningBranchState
  actor: "pi" | "brain" | "executor" | "web_ui" | "llm" | "import"
  reason: string
}

export type PlanningWorkflowVerdict = "Ready" | "Needs review" | "Blocked" | "Exploratory"

export interface PlanningWorkflowStage {
  stage_type: PlanningStage
  label: string
  verdict: PlanningWorkflowVerdict
  prerequisites: PlanningStage[]
  dependents: PlanningStage[]
  current_artifact: PlanningArtifact | null
  candidate_artifacts: PlanningArtifact[]
  blockers: string[]
  warnings: string[]
  upstream_conflicts: Array<Record<string, unknown>>
  next_action: string
}

export interface PlanningQuickReaderSlot {
  slot: string
  text: string
  authority: "provisional"
  source: PlanningComparisonRef
}

export interface PlanningArgumentWorkflow {
  schema_version: string
  project_id: string
  branch: PlanningBranch
  stages: PlanningWorkflowStage[]
  next_recommended_stage: PlanningStage | null
  quick_reader: {
    slots: PlanningQuickReaderSlot[]
    canonical_contributions: Array<{
      claim_id: string
      local_key: string
      version: number
      exact_wording: string
      ratified: boolean
    }>
    discrepancies: Array<Record<string, unknown>>
    llm_generated: false
  }
  authority: {
    planning: "provisional"
    canonical_mutation: string
    ratification: string
    llm_at_view_time: false
  }
}

export interface PlanningPromotionEvent {
  id: string
  project_id: string
  branch_id: string
  artifact_id: string
  artifact_version_id: string
  artifact_version: number
  branch_revision: number
  candidate_kind: "research_question" | "contribution"
  candidate_key: string
  action:
    | "rq_promoted"
    | "contribution_proposal_prepared"
    | "contribution_proposal_applied"
    | "contribution_ratified"
  target_type: string
  target_id: string
  target_version: number | null
  proposal_id: string | null
  decision_id: string | null
  proposal_status: SemanticPatchStatus | null
  decision_status: string | null
  actor: string
  reason: string
  details: Record<string, unknown>
  created_at: string
}

export interface PlanningResearchQuestionPromotion {
  expected_branch_revision: number
  artifact_id: string
  expected_artifact_version: number
  candidate_key: string
  phase: string
  reason: string
  confirmed_by?: "pi"
}

export interface PlanningContributionProposalPrepare {
  expected_branch_revision: number
  artifact_id: string
  expected_artifact_version: number
  candidate_key: string
  manuscript_id: string
  expected_manuscript_revision: number
  claim_local_key?: string
  reason: string
  actor?: "pi" | "brain" | "executor" | "web_ui"
}

export interface PlanningContributionRatification {
  expected_branch_revision: number
  artifact_id: string
  expected_artifact_version: number
  candidate_key: string
  manuscript_id: string
  claim_ref: string
  expected_manuscript_revision: number
  proposal_id: string
  decision_id: string
  reason: string
  confirmed_by?: "pi"
}

export interface PlanningEvaluationEvent {
  id: string
  project_id: string
  branch_id: string
  artifact_id: string
  artifact_version_id: string
  artifact_version: number
  branch_revision: number
  commitment_key: string
  requirement_key: string | null
  action:
    | "missing_evidence_mission_created"
    | "result_unit_proposal_prepared"
    | "result_unit_proposal_applied"
  target_type: "mission" | "semantic_patch_proposal" | "manuscript_unit"
  target_id: string
  target_version: number | null
  proposal_id: string | null
  mission_id: string | null
  proposal_status: SemanticPatchStatus | null
  mission_status: string | null
  actor: string
  reason: string
  details: Record<string, unknown>
  created_at: string
}

export interface PlanningEvaluationObservationView {
  binding: {
    observation_id: string
    locator_ids: string[]
    role: string
    outcome: "supports" | "partially_supports" | "fails_to_support" | "inconclusive" | "exploratory"
    claim_effect: "supports_as_worded" | "requires_narrowing" | "negative_result" | "exploratory_only" | "unresolved"
    interpretation: string
  }
  observation: null | {
    id: string
    run_id: string
    experiment_id: string
    plan_version: number
    run_status: string
    name: string
    direction: string
    summary: string
  }
  locators: Array<{
    id: string
    locator_kind: string
    locator_value: string | null
    content_hash: string
  }>
}

export interface PlanningEvaluationRequirementView {
  requirement: {
    local_key: string
    kind: string
    description: string
    required: boolean
    experiment_id: string | null
    plan_version_id: string | null
    plan_version: number | null
    acceptance_criteria: string[]
    failure_criteria: string[]
    missing_evidence: string | null
  }
  plan: null | Record<string, unknown>
  observations: PlanningEvaluationObservationView[]
  conclusive_observation_count: number
  verdict: PlanningWorkflowVerdict
  blockers: string[]
  warnings: string[]
}

export interface PlanningEvaluationCommitmentView {
  legacy: boolean
  commitment: Record<string, unknown> & {
    local_key?: string
    claim_id?: string
    claim_version?: number
    method?: string
    allowed_interpretation?: string
    prohibited_interpretation?: string[]
    disposition?: string
  }
  claim?: null | Record<string, unknown>
  research_questions?: Array<Record<string, unknown>>
  requirements: PlanningEvaluationRequirementView[]
  events?: PlanningEvaluationEvent[]
  verdict: PlanningWorkflowVerdict
  blockers: string[]
  warnings: string[]
  next_action: string
}

export interface PlanningEvaluationWorkflow {
  schema_version: string
  project_id: string
  branch: PlanningBranch
  artifact: PlanningArtifact | null
  candidate_artifacts: PlanningArtifact[]
  verdict: PlanningWorkflowVerdict
  blockers: string[]
  warnings: string[]
  commitments: PlanningEvaluationCommitmentView[]
  events: PlanningEvaluationEvent[]
  next_action: string
  authority: {
    planning: "provisional"
    evidence: "canonical_exact_records"
    outcomes: "explicit_not_inferred_from_direction"
    canonical_mutation: string
    llm_at_view_time: false
  }
}

export interface PlanningEvaluationMissionCreate {
  expected_branch_revision: number
  artifact_id: string
  expected_artifact_version: number
  commitment_key: string
  requirement_key: string
  phase?: string
  motivated_by_decision?: string
  reason: string
  actor?: "pi" | "brain" | "executor" | "web_ui"
}

export interface PlanningEvaluationResultProposalPrepare {
  expected_branch_revision: number
  artifact_id: string
  expected_artifact_version: number
  commitment_key: string
  manuscript_id: string
  expected_manuscript_revision: number
  result_unit_local_key: string
  location: string
  title: string
  artifact_ref: string
  reason: string
  actor?: "pi" | "brain" | "executor" | "web_ui"
}

// ---- Unified semantic edit proposals ----

export type SemanticPatchStatus =
  | "proposed" | "applied" | "rejected" | "conflicted" | "superseded" | "expired"
export type SemanticPatchOrigin = "human" | "host_agent" | "lm_studio"

export interface SemanticPatchDiffChange {
  path: string
  change: "added" | "removed" | "changed"
  before: unknown
  after: unknown
}

export interface SemanticPatchDiff {
  operation_index: number
  operation: string
  target: Record<string, unknown>
  changes: SemanticPatchDiffChange[]
}

export interface SemanticPatchFinding {
  severity: string
  code: string
  message: string
  [key: string]: unknown
}

export interface SemanticPatchProposal {
  schema_version: string
  id: string
  project_id: string
  origin: SemanticPatchOrigin
  status: SemanticPatchStatus
  revision: number
  intent: string
  reason: string
  created_by: string
  operations: Array<Record<string, unknown>>
  target_bases: Array<Record<string, unknown>>
  semantic_diff: SemanticPatchDiff[]
  validation_findings: SemanticPatchFinding[]
  context_manifest_id: string | null
  provider: string | null
  model: string | null
  boundary: "none" | "host_conversation" | "local_loopback"
  created_at: string
  updated_at: string
}

export interface SemanticPatchProposalCreate {
  origin: "human"
  intent: string
  reason: string
  created_by: "web_ui"
  operations: Array<Record<string, unknown>>
}

export interface SemanticPatchTransition {
  expected_revision: number
  actor: "web_ui"
  reason: string
}

export interface LMStudioSemanticPatchRequest {
  instruction: string
  created_by: "web_ui"
  targets: Array<{ target_type: "manuscript" | "planning_branch"; target_id: string }>
  constraints?: string[]
  omissions?: string[]
  model?: string
}
