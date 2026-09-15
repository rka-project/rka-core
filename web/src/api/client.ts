/**
 * RKA API client — thin fetch wrapper with typed endpoints.
 */

const BASE_URL = "/api"
let activeProjectId =
  (typeof window !== "undefined" && window.localStorage.getItem("rka.activeProjectId")) ||
  "proj_default"

export function setApiProjectId(projectId: string | null) {
  activeProjectId = projectId?.trim() || "proj_default"
}

type RequestConfig = {
  includeJsonContentType?: boolean
  includeProjectHeader?: boolean
}

class ApiError extends Error {
  status: number
  detail: string
  constructor(status: number, detail: string) {
    super(`API Error ${status}: ${detail}`)
    this.name = "ApiError"
    this.status = status
    this.detail = detail
  }
}

function buildHeaders(
  headers?: HeadersInit,
  { includeJsonContentType = true, includeProjectHeader = true }: RequestConfig = {},
): Headers {
  const result = new Headers(headers)
  if (includeJsonContentType && !result.has("Content-Type")) {
    result.set("Content-Type", "application/json")
  }
  if (includeProjectHeader) {
    result.set("X-RKA-Project", activeProjectId)
  }
  return result
}

async function parseApiError(res: Response): Promise<never> {
  let detail = res.statusText
  try {
    const body = await res.json()
    detail = formatApiDetail(body.detail ?? body)
  } catch {
    // use statusText
  }
  throw new ApiError(res.status, detail)
}

function formatApiDetail(value: unknown): string {
  if (typeof value === "string") return value
  if (Array.isArray(value)) {
    return value.map((item) => {
      if (item && typeof item === "object" && "msg" in item) {
        const issue = item as { loc?: unknown; msg: unknown }
        const location = Array.isArray(issue.loc) ? issue.loc.join(".") : "request"
        return `${location}: ${String(issue.msg)}`
      }
      return formatApiDetail(item)
    }).join("; ")
  }
  if (value && typeof value === "object") return JSON.stringify(value)
  return String(value)
}

function getFilenameFromDisposition(disposition: string | null, fallback: string): string {
  if (!disposition) return fallback
  const match = disposition.match(/filename="?([^";]+)"?/)
  return match?.[1] ?? fallback
}

async function request<T>(
  path: string,
  options: RequestInit = {},
  config: RequestConfig = {},
): Promise<T> {
  const url = `${BASE_URL}${path}`
  const res = await fetch(url, {
    headers: buildHeaders(options.headers, config),
    ...options,
  })

  if (!res.ok) {
    await parseApiError(res)
  }

  if (res.status === 204) return undefined as T
  return res.json()
}

function get<T>(path: string): Promise<T> {
  return request<T>(path)
}

function post<T>(path: string, body?: unknown, config?: RequestConfig): Promise<T> {
  return request<T>(path, {
    method: "POST",
    body: body ? JSON.stringify(body) : undefined,
  }, config)
}

function put<T>(path: string, body?: unknown): Promise<T> {
  return request<T>(path, {
    method: "PUT",
    body: body ? JSON.stringify(body) : undefined,
  })
}

// ---- Typed API methods ----

import type {
  JournalEntry,
  JournalEntryCreate,
  JournalEntryUpdate,
  Decision,
  DecisionCreate,
  DecisionUpdate,
  DecisionTreeNode,
  Literature,
  LiteratureCreate,
  LiteratureUpdate,
  Mission,
  MissionCreate,
  MissionUpdate,
  MissionReportCreate,
  MissionReport,
  Checkpoint,
  CheckpointResolve,
  Event,
  ProjectCreate,
  ProjectInfo,
  ProjectState,
  ProjectStateUpdate,
  ContextRequest,
  ContextPackage,
  SearchResult,
  TagCount,
  HealthStatus,
  CoreCapabilities,
  AuditEntry,
  BibtexImportResult,
  MermaidExport,
  GraphData,
  GraphStats,
  SummaryResult,
  ExplorationSummary,
  QAResult,
  QASession,
  EmbeddingConfig,
  ConnectionTestResult,
  BackfillStatus,
  KnowledgePackDownload,
  KnowledgePackImportResult,
  EvidenceClusterUpdateRequest,
  StalenessImpact,
  MissionGuard,
  BeliefAsOf,
  ResearchHealth,
  ReportContextRequest,
  ReportContextResult,
  StalenessReviewFiling,
  LinkSupportAudit,
  ManuscriptContext,
  ManuscriptImpact,
  ManuscriptReadiness,
  ManuscriptSpine,
  ManuscriptOutline,
  ManuscriptSourceFile,
  ManuscriptSourceOverview,
  ManuscriptSourceProposal,
  ManuscriptSourceProposalCreate,
  OutlineProposalRequest,
  OutlineProposalResult,
  ManuscriptWritingCandidates,
  InterpretationCandidate,
  InterpretationCandidateDetail,
  InterpretationReviewStatus,
  InterpretationTriageRequest,
  ClaimScopeHistory,
  ClaimScopeReadiness,
  ClaimScopeWrite,
  PlanningBranch,
  PlanningBranchComparison,
  PlanningBranchCreate,
  PlanningBranchTransition,
  PlanningContext,
  PlanningArgumentWorkflow,
  PlanningPromotionEvent,
  PlanningResearchQuestionPromotion,
  PlanningContributionProposalPrepare,
  PlanningContributionRatification,
  PlanningEvaluationWorkflow,
  PlanningEvaluationEvent,
  PlanningEvaluationMissionCreate,
  PlanningEvaluationResultProposalPrepare,
  SemanticPatchProposal,
  SemanticPatchProposalCreate,
  SemanticPatchTransition,
  LMStudioSemanticPatchRequest,
} from "./types"

export const api = {
  // Health
  health: () => get<HealthStatus>("/health"),
  capabilities: () => get<CoreCapabilities>("/capabilities"),

  // Project
  listProjects: () => get<ProjectInfo[]>("/projects"),
  createProject: (data: ProjectCreate) =>
    post<ProjectInfo>("/projects", data, { includeProjectHeader: false }),
  deleteProject: (projectId: string, confirm = false) =>
    request<{ project_id: string; project_name: string; entity_counts: Record<string, number>; total_rows: number; confirmed: boolean; message: string }>(
      `/projects/${projectId}?confirm=${confirm}`,
      { method: "DELETE" },
    ),
  getProjectEntityCounts: (projectId: string) =>
    get<{ project_id: string; entity_counts: Record<string, number>; total_rows: number }>(
      `/projects/${projectId}/entity-counts`,
    ),
  getStatus: () => get<ProjectState>("/status"),
  updateStatus: (data: ProjectStateUpdate) => put<ProjectState>("/status", data),
  exportKnowledgePack: async (): Promise<KnowledgePackDownload> => {
    const res = await fetch(`${BASE_URL}/projects/export`, {
      headers: buildHeaders(undefined, { includeJsonContentType: false }),
    })
    if (!res.ok) {
      await parseApiError(res)
    }
    return {
      blob: await res.blob(),
      filename: getFilenameFromDisposition(
        res.headers.get("Content-Disposition"),
        "knowledge-pack.rka-pack.zip",
      ),
    }
  },
  importKnowledgePack: async (
    file: File,
    options?: { project_id?: string; project_name?: string },
  ) => {
    const form = new FormData()
    form.append("file", file)
    if (options?.project_id) form.append("project_id", options.project_id)
    if (options?.project_name) form.append("project_name", options.project_name)

    const res = await fetch(`${BASE_URL}/projects/import`, {
      method: "POST",
      headers: buildHeaders(undefined, { includeJsonContentType: false }),
      body: form,
    })
    if (!res.ok) {
      await parseApiError(res)
    }
    return res.json() as Promise<KnowledgePackImportResult>
  },

  // Notes / Journal
  listNotes: (params?: { phase?: string; type?: string; since?: string; limit?: number }) => {
    const search = new URLSearchParams()
    if (params?.phase) search.set("phase", params.phase)
    if (params?.type) search.set("type", params.type)
    if (params?.since) search.set("since", params.since)
    if (params?.limit) search.set("limit", String(params.limit))
    const qs = search.toString()
    return get<JournalEntry[]>(`/notes${qs ? `?${qs}` : ""}`)
  },
  getNote: (id: string) => get<JournalEntry>(`/notes/${id}`),
  createNote: (data: JournalEntryCreate) => post<JournalEntry>("/notes", data),
  updateNote: (id: string, data: JournalEntryUpdate) => put<JournalEntry>(`/notes/${id}`, data),

  // Decisions
  listDecisions: (params?: { phase?: string; status?: string }) => {
    const search = new URLSearchParams()
    if (params?.phase) search.set("phase", params.phase)
    if (params?.status) search.set("status", params.status)
    const qs = search.toString()
    return get<Decision[]>(`/decisions${qs ? `?${qs}` : ""}`)
  },
  getDecision: (id: string) => get<Decision>(`/decisions/${id}`),
  createDecision: (data: DecisionCreate) => post<Decision>("/decisions", data),
  updateDecision: (id: string, data: DecisionUpdate) => put<Decision>(`/decisions/${id}`, data),
  getDecisionTree: (phase?: string) => {
    const qs = phase ? `?phase=${phase}` : ""
    return get<DecisionTreeNode[]>(`/decisions/tree${qs}`)
  },

  // Literature
  listLiterature: (params?: { status?: string }) => {
    const search = new URLSearchParams()
    if (params?.status) search.set("status", params.status)
    const qs = search.toString()
    return get<Literature[]>(`/literature${qs ? `?${qs}` : ""}`)
  },
  getLiterature: (id: string) => get<Literature>(`/literature/${id}`),
  createLiterature: (data: LiteratureCreate) => post<Literature>("/literature", data),
  updateLiterature: (id: string, data: LiteratureUpdate) =>
    put<Literature>(`/literature/${id}`, data),

  // Missions
  listMissions: (params?: { status?: string }) => {
    const search = new URLSearchParams()
    if (params?.status) search.set("status", params.status)
    const qs = search.toString()
    return get<Mission[]>(`/missions${qs ? `?${qs}` : ""}`)
  },
  getMission: (id: string) => get<Mission>(`/missions/${id}`),
  createMission: (data: MissionCreate) => post<Mission>("/missions", data),
  updateMission: (id: string, data: MissionUpdate) => put<Mission>(`/missions/${id}`, data),
  submitReport: (id: string, data: MissionReportCreate) =>
    post<Mission>(`/missions/${id}/report`, data),
  getReport: (id: string) => get<MissionReport | null>(`/missions/${id}/report`),

  // Checkpoints
  listCheckpoints: (params?: { status?: string; mission_id?: string }) => {
    const search = new URLSearchParams()
    if (params?.status) search.set("status", params.status)
    if (params?.mission_id) search.set("mission_id", params.mission_id)
    const qs = search.toString()
    return get<Checkpoint[]>(`/checkpoints${qs ? `?${qs}` : ""}`)
  },
  getCheckpoint: (id: string) => get<Checkpoint>(`/checkpoints/${id}`),
  resolveCheckpoint: (id: string, data: CheckpointResolve) =>
    put<Checkpoint>(`/checkpoints/${id}/resolve`, data),

  // Events
  listEvents: (params?: { entity_type?: string; entity_id?: string; limit?: number }) => {
    const search = new URLSearchParams()
    if (params?.entity_type) search.set("entity_type", params.entity_type)
    if (params?.entity_id) search.set("entity_id", params.entity_id)
    if (params?.limit) search.set("limit", String(params.limit))
    const qs = search.toString()
    return get<Event[]>(`/events${qs ? `?${qs}` : ""}`)
  },

  // Search
  search: (query: string, entityTypes?: string[], limit?: number) =>
    post<SearchResult[]>("/search", { query, entity_types: entityTypes, limit }),

  // Tags
  listTags: () => get<TagCount[]>("/tags"),

  // Context
  getContext: (data: ContextRequest) => post<ContextPackage>("/context", data),

  // Summarize
  summarize: (data: { topic?: string; phase?: string; entity_ids?: string[] }) =>
    post<{ summary_id: string | null; summary: string; source_count: number }>("/summarize", data),

  // Audit
  listAudit: (params?: {
    action?: string; entity_type?: string; actor?: string; since?: string; limit?: number
  }) => {
    const search = new URLSearchParams()
    if (params?.action) search.set("action", params.action)
    if (params?.entity_type) search.set("entity_type", params.entity_type)
    if (params?.actor) search.set("actor", params.actor)
    if (params?.since) search.set("since", params.since)
    if (params?.limit) search.set("limit", String(params.limit))
    const qs = search.toString()
    return get<AuditEntry[]>(`/audit${qs ? `?${qs}` : ""}`)
  },
  auditCounts: () => get<Record<string, number>>("/audit/counts"),

  // Academic Import
  importBibtex: (bibtex: string, skipDuplicates?: boolean) =>
    post<BibtexImportResult>("/import/bibtex", {
      bibtex,
      skip_duplicates: skipDuplicates ?? true,
    }),
  enrichDoi: (litId: string) =>
    post<{ status: string; fields_updated?: string[] }>(`/literature/${litId}/enrich-doi`),
  getMermaid: (phase?: string) => {
    const qs = phase ? `?phase=${phase}` : ""
    return get<MermaidExport>(`/decisions/mermaid${qs}`)
  },

  // Graph
  getGraph: (params?: { include_types?: string; phase?: string; limit?: number }) => {
    const search = new URLSearchParams()
    if (params?.include_types) search.set("include_types", params.include_types)
    if (params?.phase) search.set("phase", params.phase)
    if (params?.limit) search.set("limit", String(params.limit))
    const qs = search.toString()
    return get<GraphData>(`/graph${qs ? `?${qs}` : ""}`)
  },
  getEgoGraph: (entityId: string, depth?: number) => {
    const qs = depth ? `?depth=${depth}` : ""
    return get<GraphData>(`/graph/ego/${entityId}${qs}`)
  },
  getGraphStats: () => get<GraphStats>("/graph/stats"),

  // Summaries
  generateSummary: (data: { scope_type: string; scope_id?: string; granularity?: string }) =>
    post<SummaryResult>("/summaries/generate", data),
  listSummaries: (params?: { scope_type?: string; blessed_only?: boolean }) => {
    const search = new URLSearchParams()
    if (params?.scope_type) search.set("scope_type", params.scope_type)
    if (params?.blessed_only) search.set("blessed_only", "true")
    const qs = search.toString()
    return get<ExplorationSummary[]>(`/summaries${qs ? `?${qs}` : ""}`)
  },
  blessSummary: (id: string) => post<ExplorationSummary>(`/summaries/${id}/bless`, { actor: "pi" }),

  // QA
  askQuestion: (data: { question: string; session_id?: string; scope_type?: string; scope_id?: string }) =>
    post<QAResult>("/qa/ask", data),
  listQASessions: () => get<QASession[]>("/qa/sessions"),
  getQASession: (id: string) => get<QASession>(`/qa/sessions/${id}`),

  // Embedding configuration (v2.4.0, Mission D)
  // The LLM client methods that lived here pre-v2.4.0 (getLLMStatus,
  // updateLLMConfig, checkLLM, getLLMModels) were removed per the
  // LLM-capability-removal directive (jrn_01KRNZBS50K250HHHHEC58E4GC).
  // Server-side /api/llm/* routes are PRESERVED for future re-wiring.
  getEmbeddingConfig: () => get<EmbeddingConfig>("/config/embedding"),
  updateEmbeddingConfig: (data: EmbeddingConfig) =>
    put<EmbeddingConfig & { job_id?: string; status_url?: string }>(
      "/config/embedding",
      data,
    ),
  testEmbeddingConfig: (data: EmbeddingConfig) =>
    post<ConnectionTestResult>("/config/embedding/test", data),
  getEmbeddingBackfillStatus: (jobId?: string) =>
    get<BackfillStatus>(
      jobId
        ? `/config/embedding/backfill/status?job_id=${encodeURIComponent(jobId)}`
        : "/config/embedding/backfill/status",
    ),

  // v2.0: Research Map
  getResearchMap: () => {
    return get<ResearchMapData>("/research-map")
  },
  getRQClusters: (rqId: string) => get<EvidenceClusterData[]>(`/research-map/rq/${rqId}`),
  getClusterDetail: (clusterId: string) =>
    get<ResearchMapClusterDetailData>(`/research-map/cluster/${clusterId}`),
  getClusterClaims: (clusterId: string) => get<ClaimData[]>(`/research-map/cluster/${clusterId}/claims`),
  updateCluster: (clusterId: string, data: EvidenceClusterUpdateRequest) =>
    put<EvidenceClusterData>(`/clusters/${clusterId}`, data),

  // v2.0: Claims
  listClaims: (params?: {
    source_entry_id?: string
    cluster_id?: string
    claim_type?: string
    limit?: number
    scope_readiness?: ClaimScopeReadiness
  }) => {
    const search = new URLSearchParams()
    if (params?.source_entry_id) search.set("source_entry_id", params.source_entry_id)
    if (params?.cluster_id) search.set("cluster_id", params.cluster_id)
    if (params?.claim_type) search.set("claim_type", params.claim_type)
    if (params?.limit) search.set("limit", String(params.limit))
    const qs = search.toString()
    return get<ClaimData[]>(`/claims${qs ? `?${qs}` : ""}`).then((claims) => (
      params?.scope_readiness
        ? claims.filter((claim) => claim.scope_readiness === params.scope_readiness)
        : claims
    ))
  },
  getClaimScope: (claimId: string) =>
    get<ClaimScopeHistory>(`/claims/${encodeURIComponent(claimId)}/scope`),
  appendClaimScope: (claimId: string, data: ClaimScopeWrite) =>
    post<ClaimScopeHistory>(
      `/claims/${encodeURIComponent(claimId)}/scope`,
      data,
    ),

  // M1: reviewable source interpretations. A candidate is not a claim until
  // the explicit, revision-guarded promote action succeeds.
  listInterpretationCandidates: (params?: {
    review_status?: InterpretationReviewStatus
    disposition?: string
    epistemic_kind?: string
    source_type?: string
    source_id?: string
    limit?: number
  }) => {
    const search = new URLSearchParams()
    if (params?.review_status) search.set("review_status", params.review_status)
    if (params?.disposition) search.set("disposition", params.disposition)
    if (params?.epistemic_kind) search.set("epistemic_kind", params.epistemic_kind)
    if (params?.source_type) search.set("source_type", params.source_type)
    if (params?.source_id) search.set("source_id", params.source_id)
    search.set("limit", String(params?.limit ?? 200))
    return get<InterpretationCandidate[]>(`/interpretations?${search.toString()}`)
  },
  getInterpretationCandidate: (candidateId: string) =>
    get<InterpretationCandidateDetail>(
      `/interpretations/${encodeURIComponent(candidateId)}`,
    ),
  triageInterpretationCandidate: (
    candidateId: string,
    data: InterpretationTriageRequest,
  ) =>
    post<InterpretationCandidateDetail>(
      `/interpretations/${encodeURIComponent(candidateId)}/triage`,
      data,
    ),

  // v2.0: Review Queue
  getReviewQueue: (params?: { status?: string; limit?: number }) => {
    const search = new URLSearchParams()
    search.set("status", params?.status ?? "pending")
    if (params?.limit) search.set("limit", String(params.limit))
    const qs = search.toString()
    return get<ReviewItemData[]>(`/review-queue${qs ? `?${qs}` : ""}`)
  },
  getReviewStats: () => get<Record<string, unknown>>("/review-queue/stats"),

  // Verification & research health (eval-v3 themes B/C)
  getStalenessImpact: (entityId: string, maxDepth?: number) =>
    get<StalenessImpact>(
      `/graph/staleness-impact/${entityId}${maxDepth ? `?max_depth=${maxDepth}` : ""}`,
    ),
  getMissionGuard: (missionId: string) =>
    get<MissionGuard>(`/missions/${missionId}/guard`),
  getBeliefAsOf: (date: string) =>
    get<BeliefAsOf>(`/graph/as-of?date=${encodeURIComponent(date)}`),
  getResearchHealth: () => get<ResearchHealth>("/maintenance/research-health"),
  buildReportContext: (data: ReportContextRequest) =>
    post<ReportContextResult>("/graph/report-context", data),
  fileStalenessReviews: () =>
    post<StalenessReviewFiling>("/verification/file-staleness-reviews"),
  auditLinkSupport: (limit = 200) =>
    get<LinkSupportAudit>(`/verification/link-support?limit=${limit}`),

  // Native manuscript workbench. Reads are canonical projections; outline
  // edits prepare semantic proposals and never bypass explicit apply.
  getManuscriptContext: (manuscriptId: string) =>
    get<ManuscriptContext>(`/manuscripts/${encodeURIComponent(manuscriptId)}/context`),
  getManuscriptSpine: (manuscriptId: string) =>
    get<ManuscriptSpine>(`/manuscripts/${encodeURIComponent(manuscriptId)}/spine`),
  getManuscriptOutline: (manuscriptId: string) =>
    get<ManuscriptOutline>(`/manuscripts/${encodeURIComponent(manuscriptId)}/outline`),
  prepareManuscriptOutlineProposal: (
    manuscriptId: string,
    data: OutlineProposalRequest,
  ) => post<OutlineProposalResult>(
    `/manuscripts/${encodeURIComponent(manuscriptId)}/outline/proposals`, data,
  ),
  createManuscriptCheckpoint: (
    manuscriptId: string,
    data: {
      expected_revision: number
      kind: string
      unit_id?: string
      supersedes_id?: string
    },
  ) => post<Record<string, unknown>>(
    `/manuscripts/${encodeURIComponent(manuscriptId)}/checkpoints`, data,
  ),
  getManuscriptWritingCandidates: (manuscriptId: string) =>
    get<ManuscriptWritingCandidates>(
      `/manuscripts/${encodeURIComponent(manuscriptId)}/writing-candidates`,
    ),
  getManuscriptReadiness: (manuscriptId: string, targetPhase = "drafting") =>
    get<ManuscriptReadiness>(
      `/manuscripts/${encodeURIComponent(manuscriptId)}/readiness?target_phase=${encodeURIComponent(targetPhase)}`,
    ),
  getManuscriptImpact: (manuscriptId: string, sinceCursor = 0, limit = 100) =>
    get<ManuscriptImpact>(
      `/manuscripts/${encodeURIComponent(manuscriptId)}/impact?since_cursor=${sinceCursor}&limit=${limit}`,
    ),
  getManuscriptSourceOverview: (manuscriptId: string) =>
    get<ManuscriptSourceOverview>(
      `/manuscripts/${encodeURIComponent(manuscriptId)}/source`,
    ),
  readManuscriptSource: (manuscriptId: string, relativePath: string) =>
    post<ManuscriptSourceFile>(
      `/manuscripts/${encodeURIComponent(manuscriptId)}/source/read`,
      { relative_path: relativePath },
    ),
  listManuscriptSourceProposals: (manuscriptId: string) =>
    get<ManuscriptSourceProposal[]>(
      `/manuscripts/${encodeURIComponent(manuscriptId)}/source/proposals`,
    ),
  getManuscriptSourceProposal: (proposalId: string) =>
    get<ManuscriptSourceProposal>(
      `/manuscript-source-proposals/${encodeURIComponent(proposalId)}`,
    ),
  createManuscriptSourceProposal: (
    manuscriptId: string,
    data: ManuscriptSourceProposalCreate,
  ) => post<ManuscriptSourceProposal>(
    `/manuscripts/${encodeURIComponent(manuscriptId)}/source/proposals`, data,
  ),
  applyManuscriptSourceProposal: (proposalId: string, expectedRevision: number, reason: string) =>
    post<ManuscriptSourceProposal>(
      `/manuscript-source-proposals/${encodeURIComponent(proposalId)}/apply`,
      { expected_revision: expectedRevision, actor: "web_ui", reason },
    ),
  rejectManuscriptSourceProposal: (proposalId: string, expectedRevision: number, reason: string) =>
    post<ManuscriptSourceProposal>(
      `/manuscript-source-proposals/${encodeURIComponent(proposalId)}/reject`,
      { expected_revision: expectedRevision, actor: "web_ui", reason },
    ),

  // Versioned, provisional workbench deliberation. These writes never mutate
  // canonical manuscript claims or authoring files.
  listPlanningBranches: (manuscriptId: string | null, includeArchived = true) => {
    const search = new URLSearchParams()
    if (manuscriptId) search.set("manuscript_id", manuscriptId)
    search.set("include_archived", String(includeArchived))
    return get<PlanningBranch[]>(`/planning/branches?${search.toString()}`)
  },
  resumePlanningBranch: (manuscriptId: string | null) => {
    const query = manuscriptId ? `?manuscript_id=${encodeURIComponent(manuscriptId)}` : ""
    return get<PlanningContext | null>(`/planning/resume${query}`)
  },
  createPlanningBranch: (data: PlanningBranchCreate) =>
    post<PlanningContext>("/planning/branches", data),
  transitionPlanningBranch: (branchId: string, data: PlanningBranchTransition) =>
    post<PlanningContext>(
      `/planning/branches/${encodeURIComponent(branchId)}/transition`,
      data,
    ),
  comparePlanningBranches: (baseBranchId: string, otherBranchId: string) => {
    const search = new URLSearchParams({
      base_branch_id: baseBranchId,
      other_branch_id: otherBranchId,
    })
    return get<PlanningBranchComparison>(`/planning/branches/compare?${search.toString()}`)
  },
  getPlanningArgumentWorkflow: (branchId: string) =>
    get<PlanningArgumentWorkflow>(
      `/planning/branches/${encodeURIComponent(branchId)}/argument-workflow`,
    ),
  getPlanningEvaluationWorkflow: (branchId: string) =>
    get<PlanningEvaluationWorkflow>(
      `/planning/branches/${encodeURIComponent(branchId)}/evaluation-workflow`,
    ),
  listPlanningEvaluationEvents: (branchId: string) =>
    get<PlanningEvaluationEvent[]>(
      `/planning/branches/${encodeURIComponent(branchId)}/evaluation-events`,
    ),
  listPlanningPromotions: (branchId: string) =>
    get<PlanningPromotionEvent[]>(
      `/planning/branches/${encodeURIComponent(branchId)}/promotions`,
    ),
  promotePlanningResearchQuestion: (
    branchId: string,
    data: PlanningResearchQuestionPromotion,
  ) => post<Record<string, unknown>>(
    `/planning/branches/${encodeURIComponent(branchId)}/promote-rq`, data,
  ),
  preparePlanningContribution: (
    branchId: string,
    data: PlanningContributionProposalPrepare,
  ) => post<Record<string, unknown>>(
    `/planning/branches/${encodeURIComponent(branchId)}/prepare-contribution`, data,
  ),
  ratifyPlanningContribution: (
    branchId: string,
    data: PlanningContributionRatification,
  ) => post<Record<string, unknown>>(
    `/planning/branches/${encodeURIComponent(branchId)}/ratify-contribution`, data,
  ),
  createPlanningEvaluationMission: (
    branchId: string,
    data: PlanningEvaluationMissionCreate,
  ) => post<Record<string, unknown>>(
    `/planning/branches/${encodeURIComponent(branchId)}/evaluation-missions`, data,
  ),
  preparePlanningEvaluationResult: (
    branchId: string,
    data: PlanningEvaluationResultProposalPrepare,
  ) => post<Record<string, unknown>>(
    `/planning/branches/${encodeURIComponent(branchId)}/evaluation-result-proposals`, data,
  ),

  // Human, host-agent, and local-model edits converge on this proposal ledger.
  listSemanticPatchProposals: (status?: string) => {
    const query = status ? `?status=${encodeURIComponent(status)}` : ""
    return get<SemanticPatchProposal[]>(`/semantic-patches/proposals${query}`)
  },
  createSemanticPatchProposal: (data: SemanticPatchProposalCreate) =>
    post<SemanticPatchProposal>("/semantic-patches/proposals", data),
  applySemanticPatchProposal: (proposalId: string, data: SemanticPatchTransition) =>
    post<SemanticPatchProposal>(
      `/semantic-patches/proposals/${encodeURIComponent(proposalId)}/apply`, data,
    ),
  rejectSemanticPatchProposal: (proposalId: string, data: SemanticPatchTransition) =>
    post<SemanticPatchProposal>(
      `/semantic-patches/proposals/${encodeURIComponent(proposalId)}/reject`, data,
    ),
  generateLMStudioSemanticPatch: (data: LMStudioSemanticPatchRequest) =>
    post<SemanticPatchProposal>("/semantic-patches/providers/lm-studio/proposals", data),
}

export { ApiError }

// v2.0 type aliases for API responses
type ResearchMapData = import("./types").ResearchMapData
type EvidenceClusterData = import("./types").EvidenceCluster
type ResearchMapClusterDetailData = import("./types").ResearchMapClusterDetail
type ClaimData = import("./types").Claim
type ReviewItemData = import("./types").ReviewItem
