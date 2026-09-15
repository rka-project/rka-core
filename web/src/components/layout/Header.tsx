import { useState } from "react"
import { useNavigate } from "react-router-dom"
import { Search, Circle, Menu } from "lucide-react"
import { Input } from "@/components/ui/input"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Sheet,
  SheetContent,
  SheetTitle,
  SheetTrigger,
} from "@/components/ui/sheet"
import { Sidebar } from "./Sidebar"
import { useHealth } from "@/hooks/useProject"
import { useEmbeddingStatus } from "@/hooks/useEmbeddingStatus"

export function Header() {
  const [query, setQuery] = useState("")
  const [navigationOpen, setNavigationOpen] = useState(false)
  const navigate = useNavigate()
  const { data: health } = useHealth()
  const searchStatus = useEmbeddingStatus()

  const handleSearch = (e: React.FormEvent) => {
    e.preventDefault()
    if (query.trim()) {
      navigate(`/journal?search=${encodeURIComponent(query.trim())}`)
      setQuery("")
    }
  }

  return (
    <header className="flex h-14 shrink-0 items-center gap-2 border-b bg-background px-3 sm:px-6">
      <Sheet open={navigationOpen} onOpenChange={setNavigationOpen}>
        <SheetTrigger
          render={
            <Button
              type="button"
              variant="outline"
              size="icon"
              className="shrink-0 md:hidden"
            />
          }
        >
          <Menu className="h-4 w-4" />
          <span className="sr-only">Open navigation</span>
        </SheetTrigger>
        <SheetContent side="left" className="w-72 gap-0 p-0">
          <SheetTitle className="sr-only">RKA navigation</SheetTitle>
          <Sidebar
            className="h-full w-full border-r-0"
            onNavigate={() => setNavigationOpen(false)}
          />
        </SheetContent>
      </Sheet>

      {/* Search */}
      <form onSubmit={handleSearch} className="flex min-w-0 flex-1 items-center gap-2 sm:max-w-md">
        <div className="relative flex-1">
          <Search className="absolute left-2.5 top-2.5 h-4 w-4 text-muted-foreground" />
          <Input
            type="search"
            placeholder="Search entries..."
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            className="pl-9 h-9"
          />
        </div>
      </form>

      {/* Status Indicators */}
      <div className="hidden shrink-0 items-center gap-3 sm:flex">
        {health && (
          <>
            <Badge variant="outline" className="gap-1.5 text-xs">
              <Circle
                className={`h-2 w-2 fill-current ${
                  health.status === "ok" ? "text-green-500" : "text-red-500"
                }`}
              />
              {health.status === "ok" ? "Online" : "Error"}
            </Badge>
            <span className="text-xs text-muted-foreground">v{health.version}</span>
          </>
        )}
        <Badge variant="secondary" className="text-xs" title={searchStatus.description}>
          {searchStatus.label}
        </Badge>
      </div>
    </header>
  )
}
