import { useCallback, useEffect, useState, useSyncExternalStore, type ReactNode } from "react"
import { useQueryClient } from "@tanstack/react-query"
import { getApiProjectId, setApiProjectId, subscribeApiProjectId } from "@/api/client"
import type { ProjectInfo } from "@/api/types"
import { ProjectSelectionContext } from "@/contexts/projectSelection"
import { useProjects } from "@/hooks/useProject"
import { normalizeProjectId, readConfirmedProject, saveProjectChoice } from "@/lib/projectSelection"
import { ProjectChoiceGate } from "./ProjectChoiceGate"

export function ProjectSelectionProvider({ children }: { children: ReactNode }) {
  const qc = useQueryClient()
  const projects = useProjects()
  const [candidateId, setCandidateId] = useState(readConfirmedProject)
  const publishedId = useSyncExternalStore(subscribeApiProjectId, getApiProjectId, () => null)
  const selectedExists = !!candidateId && !!projects.data?.some((project) => project.id === candidateId)
  const selectionChecked = projects.isSuccess && !projects.isFetching

  // A settled list can invalidate the current choice, but a list that is
  // still refreshing after an import/create cannot establish absence yet.
  if (candidateId && selectionChecked && !selectedExists) {
    setCandidateId(null)
  }

  const setProjectId = useCallback((nextProjectId: string | null) => {
    const next = normalizeProjectId(nextProjectId)
    // Revoke the previous request scope before unmounting its pages.
    setApiProjectId(null)
    saveProjectChoice(next)
    setCandidateId(next)
  }, [])

  useEffect(() => {
    const next = selectedExists && !projects.isError ? candidateId : null
    // Publish HTTP scope before mounting children and their scoped query keys.
    setApiProjectId(next)
    if (selectionChecked && !selectedExists && readConfirmedProject()) {
      saveProjectChoice(null)
    }
  }, [candidateId, selectedExists, projects.isError, selectionChecked])

  useEffect(() => () => setApiProjectId(null), [])

  const ready = !!publishedId && publishedId === candidateId && selectedExists && !projects.isError
  return (
    <ProjectSelectionContext.Provider value={{ projectId: ready ? publishedId : "", setProjectId }}>
      {ready ? children : (
        <ProjectChoiceGate
          projects={projects.data ?? []}
          loading={projects.isPending || (!!candidateId && projects.isFetching) || (selectedExists && !projects.isError)}
          error={projects.isError ? (projects.error instanceof Error ? projects.error.message : "Could not load projects") : null}
          onRetry={() => { void projects.refetch() }}
          onChoose={setProjectId}
          onCreated={(project) => {
            qc.setQueryData<ProjectInfo[]>(["projects"], (previous = []) => [
              ...previous.filter((item) => item.id !== project.id), project,
            ])
            setProjectId(project.id)
            void qc.invalidateQueries({ queryKey: ["projects"] })
          }}
        />
      )}
    </ProjectSelectionContext.Provider>
  )
}
