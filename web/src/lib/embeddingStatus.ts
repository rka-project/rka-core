import type { CoreCapabilities } from "../api/types"

/** Never infer semantic readiness from sqlite-vec availability or configuration. */
export function embeddingStatus(data: CoreCapabilities | undefined, isError: boolean) {
  if (isError || data?.schema_version !== "rka.core-capabilities/v1") {
    return {
      mode: "unknown",
      label: "Search status unknown",
      description: "Runtime search capabilities could not be confirmed.",
    } as const
  }
  if (data.embedding?.available === true && data.embedding.search_mode === "hybrid") {
    return {
      mode: "hybrid",
      label: "Semantic + keyword search",
      description: "The runtime reports semantic retrieval is available.",
    } as const
  }
  if (data.embedding?.available === false && data.embedding.search_mode === "lexical") {
    return {
      mode: "lexical",
      label: "Keyword search only",
      description: "Semantic retrieval is disabled, unavailable, or still preparing. Keyword search remains available.",
    } as const
  }
  return {
    mode: "unknown",
    label: "Search status unknown",
    description: "Runtime search capabilities could not be confirmed.",
  } as const
}
