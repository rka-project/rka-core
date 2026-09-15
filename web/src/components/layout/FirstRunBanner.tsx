import { useState } from "react"
import { NavLink } from "react-router-dom"
import { X, Sparkles } from "lucide-react"
import { Button } from "@/components/ui/button"
import { useEmbeddingStatus } from "@/hooks/useEmbeddingStatus"

// Preserve existing dismissal preferences; the header always shows live status.
const LOCALSTORAGE_KEY = "rka_first_run_banner_dismissed_v2_4"

export function FirstRunBanner() {
  const status = useEmbeddingStatus()
  const [dismissed, setDismissed] = useState<boolean>(() => {
    try {
      return window.localStorage.getItem(LOCALSTORAGE_KEY) === "true"
    } catch {
      // localStorage unavailable (private mode); show banner once per session.
      return false
    }
  })

  const onDismiss = () => {
    setDismissed(true)
    try {
      window.localStorage.setItem(LOCALSTORAGE_KEY, "true")
    } catch {
      // best-effort; banner will reappear next session if storage failed
    }
  }

  if (dismissed) return null

  return (
    <div className="flex items-start gap-2 border-b bg-blue-50/50 px-3 py-2 text-xs sm:items-center sm:gap-3 sm:px-4">
      <Sparkles className="h-4 w-4 text-blue-600 shrink-0" />
      <div className="min-w-0 flex-1">
        <span className="font-medium text-blue-900">
          {status.label}
        </span>
        <span className="text-blue-700 ml-2">
          {status.description} Manage embeddings in{" "}
          <NavLink to="/settings" className="underline font-medium">
            Settings → Embeddings
          </NavLink>
          .
        </span>
      </div>
      <Button
        variant="ghost"
        size="sm"
        onClick={onDismiss}
        aria-label="Dismiss banner"
        className="h-6 w-6 p-0"
      >
        <X className="h-3 w-3" />
      </Button>
    </div>
  )
}
