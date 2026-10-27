import { useState } from "react"
import { NavLink } from "react-router-dom"
import {
  LayoutDashboard,
  BookOpen,
  GitBranch,
  Library,
  Rocket,
  Clock,
  Share2,
  Shield,
  Telescope,
  Map,
  Activity,
  FileSearch,
  Settings,
  Plus,
  Trash2,
  Copy,
  Sun,
  Moon,
  Monitor,
  PanelsTopLeft,
  ListChecks,
  Scale,
} from "lucide-react"
import { cn } from "@/lib/utils"
import { useTheme } from "@/hooks/useTheme"
import { useProjectStatus, useProjects, useCreateProject, useDeleteProject, useProjectEntityCounts } from "@/hooks/useProject"
import { useProjectSelection } from "@/hooks/useProjectSelection"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogFooter,
} from "@/components/ui/dialog"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Textarea } from "@/components/ui/textarea"
import { Label } from "@/components/ui/label"
import { toast } from "sonner"

const navItems = [
  { to: "/", icon: LayoutDashboard, label: "Dashboard" },
  { to: "/journal", icon: BookOpen, label: "Journal" },
  { to: "/decisions", icon: GitBranch, label: "Decisions" },
  { to: "/literature", icon: Library, label: "Literature" },
  { to: "/missions", icon: Rocket, label: "Missions" },
  { to: "/timeline", icon: Clock, label: "Timeline" },
  { to: "/graph", icon: Share2, label: "Knowledge Graph" },
  { to: "/research-map", icon: Map, label: "Research Map" },
  { to: "/interpretations", icon: ListChecks, label: "Interpretation Review" },
  { to: "/claim-scopes", icon: Scale, label: "Claim Scope Review" },
  { to: "/health", icon: Activity, label: "Research Health" },
  { to: "/report-context", icon: FileSearch, label: "Report Context" },
  { to: "/workbench", icon: PanelsTopLeft, label: "Manuscript Workbench" },
  { to: "/audit", icon: Shield, label: "Audit Log" },
  { to: "/context", icon: Telescope, label: "Context" },
  { to: "/settings", icon: Settings, label: "Settings" },
]

export function Sidebar({
  className,
  onNavigate,
}: {
  className?: string
  onNavigate?: () => void
}) {
  const { projectId, setProjectId } = useProjectSelection()
  const { data: project } = useProjectStatus()
  const { data: projects } = useProjects()
  const createProject = useCreateProject()
  const deleteProject = useDeleteProject()
  const [dialogOpen, setDialogOpen] = useState(false)
  const [deleteDialogOpen, setDeleteDialogOpen] = useState(false)
  const [deleteConfirmName, setDeleteConfirmName] = useState("")
  const [name, setName] = useState("")
  const [description, setDescription] = useState("")

  const currentProject = projects?.find((p) => p.id === projectId)
  const canDelete = projectId !== "proj_default" && !!currentProject
  const { data: entityCounts } = useProjectEntityCounts(deleteDialogOpen ? projectId : null)
  const displayProjectId = currentProject?.id ?? projectId

  const handleCreate = () => {
    if (!name.trim()) return
    createProject.mutate(
      { name: name.trim(), description: description.trim() || undefined },
      {
        onSuccess: (newProject) => {
          setProjectId(newProject.id)
          setDialogOpen(false)
          setName("")
          setDescription("")
        },
      },
    )
  }

  const handleCopyProjectId = async () => {
    try {
      await navigator.clipboard.writeText(displayProjectId)
      toast.success(`Copied ${displayProjectId}`)
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "Copy failed")
    }
  }

  return (
    <aside className={cn("flex h-screen w-56 flex-col border-r bg-sidebar", className)}>
      {/* Logo / Project Name */}
      <div className="border-b px-4 py-3">
        <div className="flex items-center gap-2">
          <img
            src="/brand/rka-project-plugin-app-icon.svg"
            alt=""
            className="h-7 w-7 shrink-0 rounded-md"
          />
          <div className="flex min-w-0 flex-col">
            <span className="max-w-[140px] truncate text-sm font-semibold">
              {project?.project_name ?? "RKA"}
            </span>
            {project?.current_phase && (
              <span className="text-[10px] text-muted-foreground truncate">
                {project.current_phase}
              </span>
            )}
          </div>
        </div>
        <div className="mt-2 flex items-center gap-1">
          <code className="min-w-0 flex-1 truncate rounded bg-muted px-1.5 py-1 text-[10px] text-muted-foreground">
            {displayProjectId}
          </code>
          <Button
            type="button"
            variant="ghost"
            size="icon-xs"
            className="shrink-0"
            onClick={handleCopyProjectId}
            title="Copy project ID"
          >
            <Copy className="h-3 w-3" />
          </Button>
        </div>
        <div className="mt-3 flex gap-1">
          {/* min-w-0 lets the flex-1 trigger shrink below its intrinsic
              content width. Without it, the SelectTrigger's default
              min-width:auto keeps it at full content size, overflowing the
              224px sidebar and pushing the shrink-0 New/Delete buttons
              off-screen (unclickable) once the Delete button appears on a
              non-default project. */}
          <Select
            value={projectId}
            onValueChange={(value) => {
              setProjectId(value)
              onNavigate?.()
            }}
          >
            <SelectTrigger className="h-9 min-w-0 flex-1 text-xs">
              <SelectValue placeholder="Select project" />
            </SelectTrigger>
            <SelectContent>
              {projects?.map((item) => (
                <SelectItem key={item.id} value={item.id}>
                  {item.name}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <Button
            variant="outline"
            size="icon"
            className="h-9 w-9 shrink-0"
            onClick={() => setDialogOpen(true)}
            title="New project"
          >
            <Plus className="h-4 w-4" />
          </Button>
          {canDelete && (
            <Button
              variant="outline"
              size="icon"
              className="h-9 w-9 shrink-0 text-destructive hover:bg-destructive/10"
              onClick={() => { setDeleteDialogOpen(true); setDeleteConfirmName("") }}
              title="Delete project"
            >
              <Trash2 className="h-4 w-4" />
            </Button>
          )}
        </div>
      </div>

      {/* Nav Links */}
      <nav className="flex-1 space-y-1 p-2">
        {navItems.map(({ to, icon: Icon, label }) => (
          <NavLink
            key={to}
            to={to}
            end={to === "/"}
            onClick={onNavigate}
            className={({ isActive }) =>
              cn(
                "flex items-center gap-3 rounded-md px-3 py-2 text-sm font-medium transition-colors",
                isActive
                  ? "bg-sidebar-accent text-sidebar-accent-foreground"
                  : "text-sidebar-foreground hover:bg-sidebar-accent/50",
              )
            }
          >
            <Icon className="h-4 w-4" />
            {label}
          </NavLink>
        ))}
      </nav>

      {/* Footer */}
      <div className="border-t p-3 space-y-2">
        <ThemeToggle />
        <p className="text-[10px] text-muted-foreground text-center">
          Research Knowledge Agent
        </p>
      </div>

      {/* New Project Dialog */}
      <Dialog open={dialogOpen} onOpenChange={setDialogOpen}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>New Project</DialogTitle>
          </DialogHeader>
          <div className="space-y-4 py-2">
            <div className="space-y-2">
              <Label htmlFor="project-name">Project name</Label>
              <Input
                id="project-name"
                placeholder="e.g. Climate Policy Analysis"
                value={name}
                onChange={(e) => setName(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && handleCreate()}
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="project-description">Description (optional)</Label>
              <Textarea
                id="project-description"
                placeholder="Brief description of the research project"
                rows={3}
                value={description}
                onChange={(e) => setDescription(e.target.value)}
              />
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setDialogOpen(false)}>
              Cancel
            </Button>
            <Button
              onClick={handleCreate}
              disabled={!name.trim() || createProject.isPending}
            >
              {createProject.isPending ? "Creating..." : "Create"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Delete Project Dialog */}
      <Dialog open={deleteDialogOpen} onOpenChange={setDeleteDialogOpen}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>Delete Project</DialogTitle>
            <DialogDescription>
              This action is irreversible. All project data will be permanently deleted.
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-4 py-2">
            {entityCounts && (
              <div className="rounded-md border p-3 text-sm space-y-1">
                <p className="font-medium">This will delete {entityCounts.total_rows.toLocaleString()} rows:</p>
                <div className="grid grid-cols-2 gap-x-4 gap-y-0.5 text-xs text-muted-foreground mt-2">
                  {Object.entries(entityCounts.entity_counts)
                    .sort(([, a], [, b]) => (b as number) - (a as number))
                    .map(([table, count]) => (
                      <div key={table} className="flex justify-between">
                        <span>{table}</span>
                        <span className="font-mono">{(count as number).toLocaleString()}</span>
                      </div>
                    ))}
                </div>
              </div>
            )}
            <div className="space-y-2">
              <Label htmlFor="delete-confirm">
                Type <span className="font-mono font-bold">{currentProject?.name}</span> to confirm
              </Label>
              <Input
                id="delete-confirm"
                placeholder="Project name"
                value={deleteConfirmName}
                onChange={(e) => setDeleteConfirmName(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && deleteConfirmName === currentProject?.name) {
                    deleteProject.mutate(projectId, {
                      onSuccess: () => {
                        setDeleteDialogOpen(false)
                        setDeleteConfirmName("")
                        setProjectId(null)
                      },
                    })
                  }
                }}
              />
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setDeleteDialogOpen(false)}>
              Cancel
            </Button>
            <Button
              variant="destructive"
              disabled={deleteConfirmName !== currentProject?.name || deleteProject.isPending}
              onClick={() => {
                deleteProject.mutate(projectId, {
                  onSuccess: () => {
                    setDeleteDialogOpen(false)
                    setDeleteConfirmName("")
                    setProjectId(null)
                  },
                })
              }}
            >
              {deleteProject.isPending ? "Deleting..." : "Delete Forever"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </aside>
  )
}

function ThemeToggle() {
  const { theme, setTheme } = useTheme()
  const options = [
    { value: "light" as const, icon: Sun, label: "Light" },
    { value: "dark" as const, icon: Moon, label: "Dark" },
    { value: "system" as const, icon: Monitor, label: "System" },
  ]

  return (
    <div className="flex items-center justify-center gap-1 rounded-md bg-muted p-0.5">
      {options.map(({ value, icon: Icon, label }) => (
        <button
          key={value}
          onClick={() => setTheme(value)}
          title={label}
          className={cn(
            "flex-1 flex items-center justify-center gap-1 rounded px-2 py-1 text-[10px] transition-colors",
            theme === value
              ? "bg-background text-foreground shadow-sm"
              : "text-muted-foreground hover:text-foreground",
          )}
        >
          <Icon className="h-3 w-3" />
          {label}
        </button>
      ))}
    </div>
  )
}
