export const PROJECT_SELECTION_STORAGE_KEY = "rka.activeProjectId"
export const CONFIRMED_PROJECT_SELECTION_KEY = "rka.projectSelection.v1"

type SelectionStorage = Pick<Storage, "getItem" | "setItem" | "removeItem">

export function projectStorage(): SelectionStorage | undefined {
  try {
    return typeof window === "undefined" ? undefined : window.localStorage
  } catch {
    return undefined
  }
}

export function normalizeProjectId(value: string | null | undefined): string | null {
  return value?.trim() || null
}

type ProjectChoiceActions = {
  candidateId: string | null
  publishedId: string | null
  revokeScope: () => void
  persistChoice: (id: string | null) => void
  setCandidate: (id: string | null) => void
}

export function applyProjectChoice(value: string | null, actions: ProjectChoiceActions): void {
  const next = normalizeProjectId(value)
  if (next !== null && next === actions.publishedId && next === actions.candidateId) {
    // Candidate state would not change, so the publication effect would not
    // run again. Keep the active scope (and unsaved page state) intact.
    actions.persistChoice(next)
    return
  }
  actions.revokeScope()
  actions.persistChoice(next)
  actions.setCandidate(next)
}

// Old activeProjectId values may have been written automatically. They are not
// evidence of an explicit choice. Existing users confirm once after upgrading.
export function readConfirmedProject(storage = projectStorage()): string | null {
  try {
    const raw = storage?.getItem(CONFIRMED_PROJECT_SELECTION_KEY)
    if (!raw) return null
    const saved: unknown = JSON.parse(raw)
    if (!saved || typeof saved !== "object" || !("version" in saved) ||
        saved.version !== 1 || !("projectId" in saved) || typeof saved.projectId !== "string") {
      return null
    }
    return normalizeProjectId(saved.projectId)
  } catch {
    return null
  }
}

export function saveProjectChoice(projectId: string | null, storage = projectStorage()): void {
  try {
    const selected = normalizeProjectId(projectId)
    if (selected) {
      storage?.setItem(CONFIRMED_PROJECT_SELECTION_KEY, JSON.stringify({ version: 1, projectId: selected }))
      storage?.setItem(PROJECT_SELECTION_STORAGE_KEY, selected)
    } else {
      storage?.removeItem(CONFIRMED_PROJECT_SELECTION_KEY)
      storage?.removeItem(PROJECT_SELECTION_STORAGE_KEY)
    }
  } catch {
    // A blocked browser storage area must not prevent a current-tab choice.
    // Reloading such a tab safely asks for a choice again.
  }
}
