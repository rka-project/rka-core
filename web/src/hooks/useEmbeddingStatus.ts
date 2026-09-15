import { useQuery } from "@tanstack/react-query"
import { api } from "@/api/client"
import { embeddingStatus } from "@/lib/embeddingStatus"

export function useEmbeddingStatus() {
  const { data, isError } = useQuery({
    queryKey: ["capabilities"],
    queryFn: api.capabilities,
    refetchInterval: 30_000,
    retry: false,
  })
  // React Query retains the last data after refetch failure. Do not advertise
  // stale semantic availability while the current capability check is failing.
  return embeddingStatus(data, isError)
}
