import { createContext } from "react"

export { PROJECT_SELECTION_STORAGE_KEY } from "@/lib/projectSelection"

export type ProjectSelectionValue = {
  projectId: string
  setProjectId: (projectId: string | null) => void
}

export const ProjectSelectionContext = createContext<ProjectSelectionValue | null>(null)
