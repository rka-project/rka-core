import { useState } from "react"
import { api } from "@/api/client"
import type { ProjectInfo } from "@/api/types"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"

type Props = {
  projects: ProjectInfo[]
  loading: boolean
  error: string | null
  onRetry: () => void
  onChoose: (id: string) => void
  onCreated: (project: ProjectInfo) => void
}

export function ProjectChoiceGate({ projects, loading, error, onRetry, onChoose, onCreated }: Props) {
  const [selected, setSelected] = useState("")
  const [name, setName] = useState("")
  const [creating, setCreating] = useState(false)
  const [createError, setCreateError] = useState<string | null>(null)

  async function createProject(event: React.FormEvent) {
    event.preventDefault()
    if (!name.trim() || creating || loading || error) return
    setCreating(true)
    setCreateError(null)
    try {
      const project = await api.createProject({ name: name.trim() })
      onCreated(project)
    } catch (cause) {
      setCreateError(cause instanceof Error ? cause.message : "Could not create project")
    } finally {
      setCreating(false)
    }
  }

  return (
    <main className="flex min-h-screen items-center justify-center bg-background p-6">
      <section className="w-full max-w-lg space-y-6 rounded-xl border bg-card p-6 shadow-sm" aria-labelledby="project-choice-title">
        <div className="space-y-2">
          <h1 id="project-choice-title" className="text-2xl font-semibold">Choose your research project</h1>
          <p className="text-sm text-muted-foreground">
            Select or create a project before opening its records. RKA will not choose a default project or file notes for you.
          </p>
          <p className="text-xs text-muted-foreground">
            Existing projects and their data are unchanged. After an upgrade, confirm your project once.
          </p>
        </div>
        {loading && <p role="status">Loading projects…</p>}
        {error && (
          <div role="alert" className="space-y-2 text-sm text-destructive">
            <p>{error}</p>
            <Button variant="outline" onClick={onRetry}>Retry loading projects</Button>
          </div>
        )}
        {!loading && !error && (
          <>
            {projects.length > 0 ? (
              <form className="space-y-3" onSubmit={(event) => {
                event.preventDefault()
                if (projects.some((project) => project.id === selected)) onChoose(selected)
              }}>
                <Label htmlFor="initial-project">Existing project</Label>
                <select id="initial-project" value={selected} onChange={(event) => setSelected(event.target.value)}
                  className="h-10 w-full rounded-md border bg-background px-3 text-sm">
                  <option value="">Select a project…</option>
                  {projects.map((project) => <option key={project.id} value={project.id}>{project.name} ({project.id})</option>)}
                </select>
                <Button type="submit" disabled={!projects.some((project) => project.id === selected) || creating}>
                  Use selected project
                </Button>
              </form>
            ) : <p className="text-sm">No projects yet. Create one to get started.</p>}
            <form className="space-y-3 border-t pt-5" onSubmit={createProject}>
              <Label htmlFor="initial-project-name">New project name</Label>
              <Input id="initial-project-name" value={name} onChange={(event) => setName(event.target.value)}
                placeholder="e.g. Urban heat study" disabled={creating} />
              {createError && <p role="alert" className="text-sm text-destructive">{createError}</p>}
              <Button type="submit" variant="outline" disabled={!name.trim() || creating}>
                {creating ? "Creating…" : "Create and open project"}
              </Button>
            </form>
          </>
        )}
      </section>
    </main>
  )
}
