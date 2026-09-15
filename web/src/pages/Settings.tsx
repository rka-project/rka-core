import { useEffect, useMemo, useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Separator } from "@/components/ui/separator"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { useProjectStatus } from "@/hooks/useProject"
import { useHealth } from "@/hooks/useProject"
import { useNotes } from "@/hooks/useNotes"
import { useDecisions } from "@/hooks/useDecisions"
import { useLiterature } from "@/hooks/useLiterature"
import { useMissions } from "@/hooks/useMissions"
import { useCheckpoints } from "@/hooks/useCheckpoints"
import { useTags } from "@/hooks/useSearch"
import { api } from "@/api/client"
import type {
  BackfillStatus,
  ConnectionTestResult,
  EmbeddingConfig as EmbeddingConfigT,
  EmbeddingBackendKind,
} from "@/api/types"
import { toast } from "sonner"
import {
  Settings as SettingsIcon,
  Database,
  Activity,
  Server,
  Tag,
  BookOpen,
  CheckCircle2,
  XCircle,
  Cpu,
  Loader2,
  AlertTriangle,
} from "lucide-react"

export default function Settings() {
  const { data: project, isLoading: projectLoading } = useProjectStatus()
  const { data: health } = useHealth()
  const { data: notes } = useNotes()
  const { data: decisions } = useDecisions()
  const { data: literature } = useLiterature()
  const { data: missions } = useMissions()
  const { data: checkpoints } = useCheckpoints()
  const { data: tags } = useTags()

  const counts = [
    { label: "Journal Entries", count: notes?.length ?? 0, color: "text-blue-600" },
    { label: "Decisions", count: decisions?.length ?? 0, color: "text-purple-600" },
    { label: "Literature", count: literature?.length ?? 0, color: "text-green-600" },
    { label: "Missions", count: missions?.length ?? 0, color: "text-orange-600" },
    { label: "Checkpoints", count: checkpoints?.length ?? 0, color: "text-red-600" },
    { label: "Tags", count: tags?.length ?? 0, color: "text-cyan-600" },
  ]

  if (projectLoading) {
    return (
      <div className="space-y-6">
        <div>
          <div className="h-8 w-40 bg-muted rounded animate-pulse" />
          <div className="h-4 w-60 bg-muted rounded animate-pulse mt-2" />
        </div>
        <div className="grid gap-4 md:grid-cols-2">
          {[1, 2, 3, 4].map((i) => (
            <Card key={i}>
              <CardContent className="pt-6">
                <div className="h-24 bg-muted rounded animate-pulse" />
              </CardContent>
            </Card>
          ))}
        </div>
      </div>
    )
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Settings</h1>
        <p className="text-muted-foreground text-sm">
          About, system configuration, health, and database statistics
        </p>
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="text-sm font-medium flex items-center gap-2">
            <BookOpen className="h-4 w-4" />
            About RKA
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-4 text-sm text-muted-foreground">
          <p>
            RKA is a persistent research memory for long-running, AI-assisted investigations.
            It organizes literature, hypotheses, findings, decisions, missions, checkpoints,
            artifacts, and events so that research context survives across sessions, phases,
            and projects.
          </p>
          <p>
            Its design separates strategic interpretation from implementation. The Brain
            supports framing, synthesis, and research direction. The Executor carries out
            coding, experiments, extraction, and reporting. The PI remains the supervising
            researcher. All three collaborate against the same structured knowledge base.
          </p>
          <div className="grid gap-3 md:grid-cols-2">
            <div className="rounded-md border p-3">
              <p className="text-xs font-semibold uppercase tracking-wide text-foreground">
                Research Memory
              </p>
              <p className="mt-1 text-xs">
                Preserve hypotheses, negative results, methodological choices, and evolving
                project state instead of losing them between chat sessions.
              </p>
            </div>
            <div className="rounded-md border p-3">
              <p className="text-xs font-semibold uppercase tracking-wide text-foreground">
                Provenance
              </p>
              <p className="mt-1 text-xs">
                Link papers, findings, decisions, experiments, and outcomes through an
                event-sourced audit trail and explicit cross-references.
              </p>
            </div>
            <div className="rounded-md border p-3">
              <p className="text-xs font-semibold uppercase tracking-wide text-foreground">
                Brain / Executor Workflow
              </p>
              <p className="mt-1 text-xs">
                Coordinate strategic planning, implementation, reporting, and escalation
                through missions and checkpoints rather than informal chat memory.
              </p>
            </div>
            <div className="rounded-md border p-3">
              <p className="text-xs font-semibold uppercase tracking-wide text-foreground">
                Project Scope
              </p>
              <p className="mt-1 text-xs">
                Keep work isolated per project while supporting export and import through
                portable knowledge packs.
              </p>
            </div>
          </div>
        </CardContent>
      </Card>

      {/* Embeddings Configuration — full width, prominent (Mission D / v2.4.0) */}
      <EmbeddingsConfigCard />

      <div className="grid gap-4 md:grid-cols-2">
        {/* API Health */}
        <Card>
          <CardHeader>
            <CardTitle className="text-sm font-medium flex items-center gap-2">
              <Activity className="h-4 w-4" />
              API Health
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            <div className="flex items-center justify-between">
              <span className="text-sm">Status</span>
              <div className="flex items-center gap-2">
                {health?.status === "ok" ? (
                  <CheckCircle2 className="h-4 w-4 text-green-500" />
                ) : (
                  <XCircle className="h-4 w-4 text-red-500" />
                )}
                <Badge
                  variant="outline"
                  className={
                    health?.status === "ok"
                      ? "bg-green-100 text-green-800 border-green-200"
                      : "bg-red-100 text-red-800 border-red-200"
                  }
                >
                  {health?.status ?? "unknown"}
                </Badge>
              </div>
            </div>
            <Separator />
            <div className="flex items-center justify-between">
              <span className="text-sm">Version</span>
              <Badge variant="secondary">{health?.version ?? "—"}</Badge>
            </div>
            <Separator />
            <div className="flex items-center justify-between">
              <span className="text-sm">Vector Search</span>
              <Badge
                variant="outline"
                className={
                  health?.vec_available
                    ? "bg-green-100 text-green-800 border-green-200"
                    : "bg-yellow-100 text-yellow-800 border-yellow-200"
                }
              >
                {health?.vec_available ? "available" : "unavailable"}
              </Badge>
            </div>
          </CardContent>
        </Card>

        {/* Project Configuration */}
        <Card>
          <CardHeader>
            <CardTitle className="text-sm font-medium flex items-center gap-2">
              <SettingsIcon className="h-4 w-4" />
              Project Configuration
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            <div className="flex items-center justify-between">
              <span className="text-sm">Project Name</span>
              <span className="text-sm font-medium">{project?.project_name ?? "—"}</span>
            </div>
            <Separator />
            <div className="flex items-center justify-between">
              <span className="text-sm">Current Phase</span>
              <Badge variant="outline">{project?.current_phase ?? "—"}</Badge>
            </div>
            <Separator />
            <div>
              <span className="text-sm">Phases</span>
              <div className="flex flex-wrap gap-1 mt-1">
                {project?.phases_config?.map((p) => (
                  <Badge
                    key={p}
                    variant={p === project.current_phase ? "default" : "secondary"}
                    className="text-[10px]"
                  >
                    {p}
                  </Badge>
                )) ?? <span className="text-xs text-muted-foreground">—</span>}
              </div>
            </div>
            {project?.project_description && (
              <>
                <Separator />
                <div>
                  <span className="text-sm">Description</span>
                  <p className="text-xs text-muted-foreground mt-1">
                    {project.project_description}
                  </p>
                </div>
              </>
            )}
          </CardContent>
        </Card>

        {/* Database Statistics */}
        <Card>
          <CardHeader>
            <CardTitle className="text-sm font-medium flex items-center gap-2">
              <Database className="h-4 w-4" />
              Database Statistics
            </CardTitle>
          </CardHeader>
          <CardContent>
            <div className="grid grid-cols-2 gap-3">
              {counts.map(({ label, count, color }) => (
                <div key={label} className="flex items-center justify-between p-2 rounded border">
                  <span className="text-xs">{label}</span>
                  <span className={`text-sm font-bold ${color}`}>{count}</span>
                </div>
              ))}
            </div>
          </CardContent>
        </Card>

        {/* Server Info */}
        <Card>
          <CardHeader>
            <CardTitle className="text-sm font-medium flex items-center gap-2">
              <Server className="h-4 w-4" />
              Server Info
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            <div className="flex items-center justify-between">
              <span className="text-sm">API Base URL</span>
              <code className="text-xs bg-muted px-2 py-0.5 rounded">
                http://localhost:9712/api
              </code>
            </div>
            <Separator />
            <div className="flex items-center justify-between">
              <span className="text-sm">Backend</span>
              <span className="text-xs text-muted-foreground">
                FastAPI + SQLite + FTS5
              </span>
            </div>
            <Separator />
            <div>
              <span className="text-sm">Quick Links</span>
              <div className="flex gap-2 mt-1">
                <a
                  href="/api/health"
                  target="_blank"
                  rel="noopener noreferrer"
                  className="text-xs text-blue-600 hover:underline"
                >
                  /api/health
                </a>
                <a
                  href="/docs"
                  target="_blank"
                  rel="noopener noreferrer"
                  className="text-xs text-blue-600 hover:underline"
                >
                  /docs (OpenAPI)
                </a>
              </div>
            </div>
          </CardContent>
        </Card>
      </div>

      {/* Top Tags */}
      {tags && tags.length > 0 && (
        <Card>
          <CardHeader>
            <CardTitle className="text-sm font-medium flex items-center gap-2">
              <Tag className="h-4 w-4" />
              Top Tags
            </CardTitle>
          </CardHeader>
          <CardContent>
            <div className="flex flex-wrap gap-2">
              {tags.slice(0, 30).map((t) => (
                <Badge key={t.tag} variant="secondary" className="text-xs gap-1">
                  {t.tag}
                  <span className="text-muted-foreground">({t.count})</span>
                </Badge>
              ))}
            </div>
          </CardContent>
        </Card>
      )}

      {/* Blockers */}
      {project?.blockers && (
        <Card className="border-orange-200">
          <CardHeader>
            <CardTitle className="text-sm font-medium text-orange-700">
              Current Blockers
            </CardTitle>
          </CardHeader>
          <CardContent>
            <p className="text-sm">{project.blockers}</p>
          </CardContent>
        </Card>
      )}
    </div>
  )
}

// ── Embeddings Configuration Card (Mission D / v2.4.0) ─────────────────────

const POLL_INTERVAL_MS = 1500

function EmbeddingsConfigCard() {
  const queryClient = useQueryClient()

  // Load the current config. 422 surfaces as a thrown error carrying
  // {error, detail, hint} per the Affordance-G mapping.
  const { data: config, isLoading, error: loadError } = useQuery({
    queryKey: ["embedding-config"],
    queryFn: () => api.getEmbeddingConfig(),
    retry: false,
  })

  const testMutation = useMutation({
    mutationFn: (payload: EmbeddingConfigT) => api.testEmbeddingConfig(payload),
  })

  const saveMutation = useMutation({
    mutationFn: (payload: EmbeddingConfigT) => api.updateEmbeddingConfig(payload),
    onSuccess: (data) => {
      queryClient.invalidateQueries({ queryKey: ["embedding-config"] })
      queryClient.invalidateQueries({ queryKey: ["capabilities"] })
      const jobId = (data as { job_id?: string }).job_id
      if (jobId) {
        toast.success(`Re-embed started (job ${jobId})`)
        setActiveJobId(jobId)
      } else {
        toast.success("Config saved (document index unchanged)")
      }
      setConfirmOpen(false)
    },
    onError: (err: Error) => {
      toast.error(err.message || "save failed")
    },
  })

  // Local form state — initialized from server config on first load.
  const [backend, setBackend] = useState<EmbeddingBackendKind>("fastembed")
  const [baseUrl, setBaseUrl] = useState("")
  const [model, setModel] = useState("")
  const [modelName, setModelName] = useState("nomic-ai/nomic-embed-text-v1.5")
  const [apiKey, setApiKey] = useState("")
  const [dim, setDim] = useState("768")
  const [confirmOpen, setConfirmOpen] = useState(false)
  const [activeJobId, setActiveJobId] = useState<string | null>(null)

  useEffect(() => {
    if (!config) return
    setBackend(config.backend)
    const sub = config.config || {}
    setBaseUrl(sub.base_url ?? "")
    setModel(sub.model ?? "")
    setModelName(sub.model_name ?? "nomic-ai/nomic-embed-text-v1.5")
    // api_key returns as "***" on GET — start blank so the user types a new
    // value only when they intend to change it.
    setApiKey("")
    setDim(String(sub.dim ?? 768))
  }, [config])

  // Read the durable latest job after a page refresh, then poll active work.
  const { data: backfill } = useQuery({
    queryKey: ["embedding-backfill", activeJobId],
    queryFn: () => api.getEmbeddingBackfillStatus(activeJobId ?? undefined),
    refetchInterval: (q) => {
      const last = q.state.data as BackfillStatus | undefined
      if (!last) return POLL_INTERVAL_MS
      if (last.state === "idle" || last.state === "complete" || last.state === "failed") return false
      return POLL_INTERVAL_MS
    },
  })

  // Stop polling once the job reaches a terminal state.
  useEffect(() => {
    if (backfill && (backfill.state === "complete" || backfill.state === "failed")) {
      queryClient.invalidateQueries({ queryKey: ["capabilities"] })
      if (backfill.state === "complete") toast.success("Re-embed complete")
      else toast.error(`Re-embed failed: ${backfill.error ?? "unknown"}`)
    }
  }, [backfill?.state])

  const buildPayload = (): EmbeddingConfigT => {
    const preserved = config?.backend === backend ? { ...config.config } : {}
    // The GET response contains only the redaction marker, never the saved
    // secret. Omission means "preserve" on PUT; a newly typed value replaces it.
    delete preserved.api_key
    if (backend === "fastembed") {
      return {
        backend: "fastembed",
        config: {
          ...preserved,
          model_name: modelName,
          dim: Number(dim) || 768,
        },
      }
    }
    if (backend === "ollama") {
      return {
        backend: "ollama",
        config: {
          ...preserved,
          base_url: baseUrl,
          model,
          dim: Number(dim) || 0,
        },
      }
    }
    return {
      backend: "openai_compat",
      config: {
        ...preserved,
        base_url: baseUrl,
        model,
        ...(apiKey ? { api_key: apiKey } : {}),
        dim: Number(dim) || 0,
      },
    }
  }

  const onTest = () => {
    testMutation.mutate(buildPayload())
  }

  const onSave = () => {
    // Open confirmation modal first; the modal's primary button kicks
    // the mutation.
    setConfirmOpen(true)
  }

  const onConfirmSave = () => {
    saveMutation.mutate(buildPayload())
  }

  const testResult = testMutation.data as ConnectionTestResult | undefined

  // 422 hint surfacing for corrupt config — render the server-provided hint
  // verbatim per the Brain T2-gate refinement.
  const loadErrorDetail = useMemo(() => {
    if (!loadError) return null
    try {
      const parsed = JSON.parse((loadError as Error).message)
      if (parsed && parsed.error === "embedding_config_invalid") {
        return { detail: parsed.detail as string, hint: parsed.hint as string }
      }
    } catch {
      // not JSON; fall through
    }
    return { detail: (loadError as Error).message, hint: "" }
  }, [loadError])

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-sm font-medium flex items-center gap-2">
          <Cpu className="h-4 w-4" />
          Embeddings
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        {isLoading && (
          <div className="text-xs text-muted-foreground flex items-center gap-2">
            <Loader2 className="h-3 w-3 animate-spin" />
            Loading current config…
          </div>
        )}

        {loadErrorDetail && (
          <div className="rounded-md border border-amber-200 bg-amber-50/50 p-3 flex items-start gap-3">
            <AlertTriangle className="h-5 w-5 text-amber-500 mt-0.5 shrink-0" />
            <div className="space-y-1">
              <p className="text-sm font-medium text-amber-800">
                Embedding config could not be loaded
              </p>
              <p className="text-xs text-amber-700">{loadErrorDetail.detail}</p>
              {loadErrorDetail.hint && (
                <p className="text-xs text-amber-600 italic">{loadErrorDetail.hint}</p>
              )}
            </div>
          </div>
        )}

        {/* Backend dropdown */}
        <div className="space-y-2">
          <label className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
            Backend
          </label>
          <Select
            value={backend}
            onValueChange={(v) => setBackend(v as EmbeddingBackendKind)}
          >
            <SelectTrigger className="w-full">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="fastembed">FastEmbed (local, default)</SelectItem>
              <SelectItem value="openai_compat">OpenAI-compat HTTP (LM Studio / vLLM / OpenAI)</SelectItem>
              <SelectItem value="ollama">Ollama (local HTTP)</SelectItem>
            </SelectContent>
          </Select>
        </div>

        {/* Conditional fields per backend */}
        {backend === "fastembed" && (
          <div className="grid grid-cols-2 gap-3">
            <div className="space-y-2">
              <label className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
                Model name
              </label>
              <Input
                value={modelName}
                onChange={(e) => setModelName(e.target.value)}
                placeholder="nomic-ai/nomic-embed-text-v1.5"
              />
            </div>
            <div className="space-y-2">
              <label className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
                Dim
              </label>
              <Input
                value={dim}
                onChange={(e) => setDim(e.target.value)}
                placeholder="768"
              />
            </div>
          </div>
        )}

        {(backend === "openai_compat" || backend === "ollama") && (
          <div className="space-y-3">
            <div className="space-y-2">
              <label className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
                Base URL
              </label>
              <Input
                value={baseUrl}
                onChange={(e) => setBaseUrl(e.target.value)}
                placeholder={
                  backend === "ollama"
                    ? "http://host.docker.internal:11434"
                    : "http://host.docker.internal:1234"
                }
              />
            </div>
            <div className="grid grid-cols-2 gap-3">
              <div className="space-y-2">
                <label className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
                  Model
                </label>
                <Input
                  value={model}
                  onChange={(e) => setModel(e.target.value)}
                  placeholder={
                    backend === "openai_compat"
                      ? "text-embedding-qwen3-embedding-8b"
                      : "nomic-embed-text"
                  }
                />
              </div>
              <div className="space-y-2">
                <label className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
                  Dim
                </label>
                <Input
                  value={dim}
                  onChange={(e) => setDim(e.target.value)}
                  placeholder="4096"
                />
              </div>
            </div>
            {backend === "openai_compat" && (
              <div className="space-y-2">
                <label className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
                  API key (optional — LM Studio doesn't require)
                </label>
                <Input
                  type="password"
                  value={apiKey}
                  onChange={(e) => setApiKey(e.target.value)}
                  placeholder={config?.config.api_key ? "•••• (already saved; type to replace)" : "(leave blank for unauthenticated)"}
                />
              </div>
            )}
          </div>
        )}

        {/* Test + Save buttons */}
        <div className="flex items-center gap-2">
          <Button
            variant="outline"
            onClick={onTest}
            disabled={testMutation.isPending}
            className="gap-2"
          >
            {testMutation.isPending && <Loader2 className="h-3 w-3 animate-spin" />}
            Test connection
          </Button>
          <Button
            onClick={onSave}
            disabled={saveMutation.isPending}
            className="gap-2"
          >
            {saveMutation.isPending && <Loader2 className="h-3 w-3 animate-spin" />}
            Save & re-embed
          </Button>
        </div>

        {/* Test result */}
        {testResult && (
          <div
            className={`rounded-md border p-3 text-xs ${
              testResult.ok
                ? "border-green-200 bg-green-50/50 text-green-800"
                : "border-red-200 bg-red-50/50 text-red-800"
            }`}
          >
            <div className="flex items-center gap-2 font-medium">
              {testResult.ok ? (
                <CheckCircle2 className="h-3 w-3" />
              ) : (
                <XCircle className="h-3 w-3" />
              )}
              {testResult.detail}
            </div>
            {testResult.ok && testResult.detected_dim !== null && (
              <p className="mt-1 text-[11px]">
                detected dim: {testResult.detected_dim}
                {testResult.latency_ms !== null &&
                  ` • latency: ${testResult.latency_ms.toFixed(0)} ms`}
              </p>
            )}
          </div>
        )}

        {/* Backfill progress */}
        {backfill?.job_id && (
          <div className="rounded-md border p-3 space-y-2">
            <div className="flex items-center justify-between text-xs">
              <span className="font-semibold">Re-embedding research records</span>
              <Badge variant="outline">{backfill.state}</Badge>
            </div>
            <div className="w-full bg-muted rounded h-2 overflow-hidden">
              <div
                className="bg-primary h-2 transition-all"
                style={{
                  width:
                    backfill.total && backfill.total > 0
                      ? `${Math.min(100, ((backfill.processed ?? 0) / backfill.total) * 100)}%`
                      : "0%",
                }}
              />
            </div>
            <p className="text-[11px] text-muted-foreground">
              {backfill.processed ?? 0} / {backfill.total ?? 0} records •{" "}
              {Math.round(backfill.elapsed_seconds ?? 0)}s elapsed
            </p>
            {backfill.state === "failed" && backfill.error && (
              <p className="text-[11px] text-red-700">{backfill.error}</p>
            )}
          </div>
        )}

        {/* Confirmation modal */}
        <Dialog open={confirmOpen} onOpenChange={setConfirmOpen}>
          <DialogContent>
            <DialogHeader>
              <DialogTitle>Save embedding configuration?</DialogTitle>
              <DialogDescription>
                RKA will test the configuration before saving it. A compatible
                change repairs only missing records; a same-dimension embedding
                space change rebuilds derived vectors while retrieval stays
                lexical. A populated cross-dimension change is rejected until
                supervised offline maintenance is available.
              </DialogDescription>
            </DialogHeader>
            <DialogFooter>
              <Button variant="outline" onClick={() => setConfirmOpen(false)}>
                Cancel
              </Button>
              <Button
                onClick={onConfirmSave}
                disabled={saveMutation.isPending}
                className="gap-2"
              >
                {saveMutation.isPending && <Loader2 className="h-3 w-3 animate-spin" />}
                Save configuration
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      </CardContent>
    </Card>
  )
}
