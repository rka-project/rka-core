"""Project state service."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
from pathlib import Path, PurePosixPath

from rka.constants import DEFAULT_PROJECT_ID, SENTINEL_PROJECT_ID
from rka.infra.ids import generate_id
from rka.models.project import ProjectCreate, ProjectInfo, ProjectState, ProjectStateUpdate
from rka.services.base import BaseService, _now


class ProjectService(BaseService):
    """Manages projects and per-project state."""

    DEFAULT_PROJECT_ID = DEFAULT_PROJECT_ID
    _SAFE_STORAGE_PROJECT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")

    async def list_projects(self) -> list[ProjectInfo]:
        rows = await self.db.fetchall(
            "SELECT id, name, description, created_by, created_at, updated_at "
            "FROM projects ORDER BY created_at"
        )
        return [ProjectInfo(**dict(row)) for row in rows]

    async def create_project(self, data: ProjectCreate, actor: str = "system") -> ProjectInfo:
        """Create project metadata, state, and audit record atomically."""
        async with self.db.transaction():
            return await self._create_project(data, actor)

    async def _create_project(
        self,
        data: ProjectCreate,
        actor: str,
    ) -> ProjectInfo:
        project_id = (data.id or generate_id("project")).strip()
        existing_id = await self.db.fetchone("SELECT id FROM projects WHERE id = ?", [project_id])
        if existing_id:
            raise ValueError(f"Project '{project_id}' already exists")

        existing_name = await self.db.fetchone(
            "SELECT id FROM projects WHERE name = ?", [data.name]
        )
        if existing_name:
            raise ValueError(f"Project name '{data.name}' already exists")

        now = _now()
        await self.db.execute(
            """INSERT INTO projects (id, name, description, created_by, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            [project_id, data.name, data.description, actor, now, now],
        )

        phases = data.phases_config or [
            "literature",
            "planning",
            "data_collection",
            "implementation",
            "evaluation",
            "paper_writing",
        ]
        await self.db.execute(
            """INSERT OR IGNORE INTO project_states
               (project_id, project_name, project_description, current_phase, phases_config, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            [project_id, data.name, data.description, phases[0], json.dumps(phases), now, now],
        )
        await self.db.commit()
        await self.audit(
            "create", "project", project_id, actor, {"name": data.name}, project_id=project_id
        )
        row = await self.db.fetchone("SELECT * FROM projects WHERE id = ?", [project_id])
        return ProjectInfo(**dict(row))

    async def get(self, project_id: str = DEFAULT_PROJECT_ID) -> ProjectState | None:
        """Get state for a project."""
        row = await self.db.fetchone(
            "SELECT * FROM project_states WHERE project_id = ?",
            [project_id],
        )
        if row is None and project_id == SENTINEL_PROJECT_ID:
            # Legacy fallback for pre-migration DBs (the legacy project_state
            # table only ever held the truly-default project's state).
            row = await self.db.fetchone("SELECT * FROM project_state WHERE id = 1")
        if row is None:
            return None
        return self._row_to_model(row)

    async def initialize(
        self,
        name: str,
        description: str | None = None,
        phases: list[str] | None = None,
        project_id: str = DEFAULT_PROJECT_ID,
    ) -> ProjectState:
        """Initialize project state (called by `rka init`)."""
        async with self.db.transaction():
            return await self._initialize(
                name,
                description,
                phases,
                project_id,
            )

    async def _initialize(
        self,
        name: str,
        description: str | None,
        phases: list[str] | None,
        project_id: str,
    ) -> ProjectState:
        """Implementation for :meth:`initialize` inside its transaction."""
        default_phases = phases or [
            "literature",
            "planning",
            "data_collection",
            "implementation",
            "evaluation",
            "paper_writing",
        ]
        await self.db.execute(
            """INSERT OR IGNORE INTO projects (id, name, description, created_by)
               VALUES (?, ?, ?, 'system')""",
            [project_id, name, description],
        )
        await self.db.execute(
            """INSERT OR REPLACE INTO project_states
               (project_id, project_name, project_description, current_phase, phases_config, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, COALESCE((SELECT created_at FROM project_states WHERE project_id = ?), ?), ?)""",
            [
                project_id,
                name,
                description,
                default_phases[0],
                json.dumps(default_phases),
                project_id,
                _now(),
                _now(),
            ],
        )
        await self.db.commit()
        await self.audit(
            "create", "project", project_id, "system", {"name": name}, project_id=project_id
        )
        return await self.get(project_id=project_id)

    async def update(
        self,
        data: ProjectStateUpdate,
        actor: str = "system",
        project_id: str = DEFAULT_PROJECT_ID,
    ) -> ProjectState:
        """Update project state with partial data."""
        async with self.db.transaction():
            return await self._update(data, actor, project_id)

    async def _update(
        self,
        data: ProjectStateUpdate,
        actor: str,
        project_id: str,
    ) -> ProjectState:
        """Implementation for :meth:`update` inside its transaction."""
        current = await self.get(project_id=project_id)
        if current is None:
            raise ValueError("Project not initialized. Run `rka init` first.")

        updates = {}
        for field, value in data.model_dump(exclude_none=True).items():
            if field == "phases_config":
                updates[field] = json.dumps(value)
            elif field == "metrics":
                updates[field] = json.dumps(value)
            else:
                updates[field] = value

        if not updates:
            return current

        updates["updated_at"] = _now()
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values())

        await self.db.execute(
            f"UPDATE project_states SET {set_clause} WHERE project_id = ?",
            values + [project_id],
        )
        await self.db.commit()

        # Emit phase change event if phase changed
        if data.current_phase and data.current_phase != current.current_phase:
            await self.emit_event(
                event_type="phase_changed",
                entity_type="project",
                entity_id=project_id,
                actor=actor,
                summary=f"Phase changed: {current.current_phase} → {data.current_phase}",
                phase=data.current_phase,
                project_id=project_id,
            )

        await self.audit(
            "update",
            "project",
            project_id,
            actor,
            {"fields": list(updates.keys())},
            project_id=project_id,
        )
        return await self.get(project_id=project_id)

    # All project-scoped tables that must be cascade-deleted.
    # Order: dependents first (reverse of insert order).
    # source table -> the index tables keyed by its ids.
    #
    # Legacy `vec_*` and all `fts_*` tables lacked a project_id, so they could
    # not be deleted by the project-scoped loop below and were simply left behind:
    # 808 orphaned vector rows and 2913 orphaned FTS rows on this instance,
    # from projects deleted months ago. They kept competing for slots in
    # every live search — 26% of the vec_claims KNN window on average, 92%
    # at worst — and the only reason they never appeared in results is that
    # hydration filtered them out after they had already taken the slot.
    #
    # Deleted through a subquery against the source table, which means this
    # must run BEFORE the source rows go.
    _INDEX_TABLES: tuple[tuple[str, tuple[str, ...]], ...] = (
        ("journal", ("vec_journal", "fts_journal")),
        ("decisions", ("vec_decisions", "fts_decisions")),
        ("literature", ("vec_literature", "fts_literature")),
        ("missions", ("vec_missions", "fts_missions")),
        ("claims", ("vec_claims", "fts_claims")),
        ("evidence_clusters", ("fts_clusters",)),
        ("artifacts", ("vec_artifacts",)),
        ("figures", ("vec_artifacts",)),
    )

    _DELETE_TABLES = (
        # Native manuscript and immutable validation histories.
        "manuscript_source_events",
        "manuscript_source_proposals",
        "semantic_patch_provider_events",
        "manuscript_evaluation_events",
        "manuscript_planning_promotion_events",
        "semantic_patch_proposal_events",
        "semantic_patch_proposals",
        "semantic_patch_context_manifests",
        "manuscript_planning_evidence_bindings",
        "manuscript_planning_artifact_versions",
        "manuscript_planning_artifacts",
        "manuscript_planning_branch_events",
        "manuscript_planning_branches",
        "manuscript_claim_verification_attestations",
        "manuscript_claim_ratifications",
        "manuscript_claim_evidence",
        "manuscript_unit_citations",
        "manuscript_unit_evidence",
        "manuscript_claim_units",
        "manuscript_checkpoints",
        "manuscript_unit_outline_profiles",
        "manuscript_reference_members",
        "reference_validation_attestations",
        "manuscript_claim_versions",
        "manuscript_units",
        "manuscript_claims",
        "manuscripts",
        "manuscript_migration_issues",
        "reference_validation_migration_issues",
        # Research graph and operational records.
        "review_queue",
        "claim_evidence_relations",
        "claim_scope_versions",
        "journal_attribution_revisions",
        "directive_dependencies",
        "source_admissions",
        "interpretation_review_events",
        "interpretation_candidate_hints",
        "interpretation_promotions",
        "interpretation_candidates",
        "registered_sources",
        "evidence_locators",
        "experiment_observations",
        "experiment_run_events",
        "experiment_runs",
        "experiment_plan_versions",
        "experiments",
        "claim_edges",
        "claims",
        "evidence_clusters",
        "calibration_outcomes",
        "decision_options",
        "hook_executions",
        "brain_notifications",
        "hooks",
        "topics",
        "context_snapshots",
        "checkpoints",
        "bootstrap_log",
        "graph_views",
        "keynodes",
        "entity_links",
        "tags",
        "audit_log",
        "events",
        "qa_sessions",
        "exploration_summaries",
        "figures",
        "artifacts",
        "jobs",
        "embedding_metadata",
        "journal",
        "missions",
        "decisions",
        "literature",
        # Delete the semantic cursor after all delete triggers have fired.
        "change_events",
        "project_states",
    )

    _INDIRECT_DELETE_COUNT_QUERIES = {
        "qa_logs": """
            SELECT COUNT(*) AS cnt
            FROM qa_logs
            WHERE session_id IN (
                SELECT id FROM qa_sessions WHERE project_id = ?
            )
        """,
        "entity_topics": """
            SELECT COUNT(*) AS cnt
            FROM entity_topics
            WHERE topic_id IN (
                SELECT id FROM topics WHERE project_id = ?
            )
        """,
    }

    async def get_project_entity_counts(self, project_id: str) -> dict[str, int]:
        """Return row counts per table for a project, for confirmation UI."""
        counts: dict[str, int] = {}
        for table, query in self._INDIRECT_DELETE_COUNT_QUERIES.items():
            try:
                row = await self.db.fetchone(query, [project_id])
            except sqlite3.OperationalError as exc:
                if "no such table" not in str(exc).lower():
                    raise
                continue
            count = row["cnt"] if row else 0
            if count > 0:
                counts[table] = count

        for table in self._DELETE_TABLES:
            try:
                row = await self.db.fetchone(
                    f"SELECT COUNT(*) as cnt FROM {table} WHERE project_id = ?",
                    [project_id],
                )
            except sqlite3.OperationalError as exc:
                if "no such table" not in str(exc).lower():
                    raise
                continue
            count = row["cnt"] if row else 0
            if count > 0:
                counts[table] = count
        return counts

    async def delete_project(self, project_id: str, confirm: bool = False) -> dict:
        """Delete a project and all its scoped data. Requires confirm=True."""
        knowledge_pack_dir: Path | None = None
        source_recovery_dir: Path | None = None
        retained_source_artifacts: list[dict[str, str]] = []
        async with self.db.transaction():
            retained_source_artifacts = await self._retained_source_artifact_candidates(
                project_id
            )
            if confirm:
                # Preflight the only filesystem path this service owns before
                # mutating the database. Filesystem removal itself happens
                # after commit because it cannot be rolled back with SQLite.
                knowledge_pack_dir = self._knowledge_pack_project_dir(project_id)
                source_recovery_dir = self._manuscript_source_recovery_project_dir(
                    project_id
                )
            result = await self._delete_project(project_id, confirm)

        result["retained_workspace_artifacts"] = {
            "status": (
                "manual_review_required"
                if retained_source_artifacts
                else "none_recorded"
            ),
            "auto_deleted": False,
            "items": retained_source_artifacts,
            "message": (
                "Source-adjacent hidden recovery artifacts are never auto-deleted; "
                "the listed deterministic paths may exist and must be inspected "
                "after all editor descriptors are closed."
                if retained_source_artifacts
                else "No source-adjacent recovery artifact candidates were recorded."
            ),
        }
        if confirm and retained_source_artifacts:
            result["message"] = (
                f"Project '{result['project_name']}' was permanently deleted from "
                "RKA-managed storage; source-adjacent recovery artifacts were "
                "intentionally retained and are listed in retained_workspace_artifacts."
            )

        if knowledge_pack_dir is not None:
            try:
                removed = self._remove_knowledge_pack_project_dir(
                    project_id,
                    expected_path=knowledge_pack_dir,
                )
            except (OSError, RuntimeError, ValueError) as exc:
                # The SQLite deletion is already committed. Report that truth
                # and leave an actionable cleanup warning instead of raising a
                # misleading, non-retryable "delete failed" response.
                result["managed_storage_cleanup"] = {
                    "status": "failed",
                    "path": str(knowledge_pack_dir),
                    "error": str(exc),
                }
                result["message"] = (
                    f"Project '{result['project_name']}' was permanently "
                    "deleted from RKA, but its managed KnowledgePack files "
                    "could not be removed; see managed_storage_cleanup."
                )
            else:
                result["managed_storage_cleanup"] = {
                    "status": "deleted" if removed else "not_present",
                    "path": str(knowledge_pack_dir),
                }
        if source_recovery_dir is not None:
            try:
                removed = self._remove_manuscript_source_recovery_project_dir(
                    project_id,
                    expected_path=source_recovery_dir,
                )
            except (OSError, RuntimeError, ValueError) as exc:
                result["manuscript_source_recovery_cleanup"] = {
                    "status": "failed",
                    "path": str(source_recovery_dir),
                    "error": str(exc),
                }
                result["message"] = (
                    f"Project '{result['project_name']}' was permanently deleted "
                    "from RKA, but manuscript source recovery files require manual cleanup."
                )
            else:
                result["manuscript_source_recovery_cleanup"] = {
                    "status": "deleted" if removed else "not_present",
                    "path": str(source_recovery_dir),
                }
        return result

    async def _retained_source_artifact_candidates(
        self, project_id: str
    ) -> list[dict[str, str]]:
        """Enumerate deterministic source-side names before deleting their ledger."""
        try:
            rows = await self.db.fetchall(
                """SELECT p.id AS proposal_id, p.manuscript_id, p.relative_path,
                          p.status, m.workspace_ref
                   FROM manuscript_source_proposals AS p
                   JOIN manuscripts AS m
                     ON m.id = p.manuscript_id AND m.project_id = p.project_id
                   WHERE p.project_id = ?
                   ORDER BY p.created_at, p.id""",
                [project_id],
            )
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc).lower() or "no such column" in str(exc).lower():
                return []
            raise

        result: list[dict[str, str]] = []
        for raw in rows:
            row = dict(raw)
            source = PurePosixPath(str(row["relative_path"]))
            swap_name = (
                ".rka-source-"
                f"{hashlib.sha256(str(row['proposal_id']).encode()).hexdigest()[:32]}"
                ".recovery"
            )
            retained_relative = (source.parent / swap_name).as_posix()
            result.append(
                {
                    "proposal_id": str(row["proposal_id"]),
                    "manuscript_id": str(row["manuscript_id"]),
                    "proposal_status": str(row["status"]),
                    "workspace_ref": str(row["workspace_ref"] or ""),
                    "source_relative_path": str(row["relative_path"]),
                    "retained_relative_path": retained_relative,
                }
            )
        return result

    def _manuscript_source_recovery_project_dir(self, project_id: str) -> Path:
        """Return one validated service-owned source-recovery directory."""
        if project_id in {".", ".."} or not self._SAFE_STORAGE_PROJECT_ID.fullmatch(project_id):
            raise ValueError("Project ID is not safe for source-recovery cleanup")
        storage_root = Path(self.db.db_path).resolve().parent / "manuscript-source-recovery"
        if storage_root.is_symlink():
            raise ValueError("Manuscript source recovery root must not be a symbolic link")
        resolved_root = storage_root.resolve()
        project_dir = storage_root / project_id
        if project_dir.is_symlink():
            raise ValueError("Manuscript source recovery project directory must not be a symbolic link")
        resolved_project_dir = project_dir.resolve()
        if (
            not resolved_project_dir.is_relative_to(resolved_root)
            or resolved_project_dir.parent != resolved_root
        ):
            raise ValueError("Project ID escapes manuscript source recovery root")
        if project_dir.exists() and not project_dir.is_dir():
            raise ValueError("Manuscript source recovery path is not a directory")
        return project_dir

    def _remove_manuscript_source_recovery_project_dir(
        self,
        project_id: str,
        *,
        expected_path: Path,
    ) -> bool:
        project_dir = self._manuscript_source_recovery_project_dir(project_id)
        if project_dir != expected_path:
            raise RuntimeError("Manuscript source recovery path changed during deletion")
        if project_dir.exists():
            shutil.rmtree(project_dir)
            return True
        return False

    def _knowledge_pack_project_dir(self, project_id: str) -> Path:
        """Return the service-owned project directory after strict validation."""
        if project_id in {".", ".."} or not self._SAFE_STORAGE_PROJECT_ID.fullmatch(project_id):
            raise ValueError("Project ID is not safe for knowledge-pack storage cleanup")

        db_dir = Path(self.db.db_path).resolve().parent
        storage_root = db_dir / "knowledge-packs"
        if storage_root.is_symlink():
            raise ValueError("Knowledge-pack storage root must not be a symbolic link")

        resolved_root = storage_root.resolve()
        project_dir = storage_root / project_id
        if project_dir.is_symlink():
            raise ValueError("Knowledge-pack project directory must not be a symbolic link")

        resolved_project_dir = project_dir.resolve()
        if (
            not resolved_project_dir.is_relative_to(resolved_root)
            or resolved_project_dir.parent != resolved_root
        ):
            raise ValueError("Project ID escapes knowledge-pack storage cleanup root")
        if project_dir.exists() and not project_dir.is_dir():
            raise ValueError("Knowledge-pack project storage path is not a directory")
        return project_dir

    def _remove_knowledge_pack_project_dir(
        self,
        project_id: str,
        *,
        expected_path: Path,
    ) -> bool:
        """Remove only the validated service-owned directory after DB commit."""
        project_dir = self._knowledge_pack_project_dir(project_id)
        if project_dir != expected_path:
            raise RuntimeError("Knowledge-pack project storage path changed during deletion")
        if project_dir.exists():
            shutil.rmtree(project_dir)
            return True
        return False

    async def _delete_planning_versions_dependency_ordered(self, project_id: str) -> None:
        """Delete immutable planning versions from leaves back to their roots.

        SQLite enforces ``ON DELETE RESTRICT`` self-references row by row, so a
        single project-scoped DELETE cannot remove a supersession/derivation
        chain even when every row in that chain is in scope.  Project deletion
        is already explicitly authorized; deleting only unreferenced leaves
        retains the immutable-row contract without weakening its triggers.
        """
        while True:
            remaining = await self.db.fetchone(
                """SELECT COUNT(*) AS cnt
                   FROM manuscript_planning_artifact_versions
                   WHERE project_id = ?""",
                [project_id],
            )
            if not remaining or remaining["cnt"] == 0:
                return
            cursor = await self.db.execute(
                """DELETE FROM manuscript_planning_artifact_versions AS version
                   WHERE version.project_id = ?
                     AND NOT EXISTS (
                         SELECT 1
                         FROM manuscript_planning_artifact_versions AS dependent
                         WHERE dependent.project_id = version.project_id
                           AND (
                               dependent.supersedes_version_id = version.id
                               OR dependent.derived_from_version_id = version.id
                           )
                     )""",
                [project_id],
            )
            if cursor.rowcount == 0:
                raise sqlite3.IntegrityError(
                    "Planning artifact version dependency cycle blocks project deletion"
                )

    async def _delete_planning_branches_dependency_ordered(self, project_id: str) -> None:
        """Delete planning branches from child leaves back to their roots."""
        while True:
            remaining = await self.db.fetchone(
                """SELECT COUNT(*) AS cnt
                   FROM manuscript_planning_branches
                   WHERE project_id = ?""",
                [project_id],
            )
            if not remaining or remaining["cnt"] == 0:
                return
            cursor = await self.db.execute(
                """DELETE FROM manuscript_planning_branches AS branch
                   WHERE branch.project_id = ?
                     AND NOT EXISTS (
                         SELECT 1
                         FROM manuscript_planning_branches AS child
                         WHERE child.project_id = branch.project_id
                           AND child.parent_branch_id = branch.id
                     )""",
                [project_id],
            )
            if cursor.rowcount == 0:
                raise sqlite3.IntegrityError(
                    "Planning branch dependency cycle blocks project deletion"
                )

    async def _delete_project(self, project_id: str, confirm: bool) -> dict:
        """Implementation for :meth:`delete_project` inside its transaction."""
        if project_id == SENTINEL_PROJECT_ID:
            raise ValueError("Cannot delete the default project (proj_default)")

        # Verify project exists
        row = await self.db.fetchone("SELECT id, name FROM projects WHERE id = ?", [project_id])
        if not row:
            raise ValueError(f"Project '{project_id}' not found")

        project_name = row["name"]
        counts = await self.get_project_entity_counts(project_id)

        if not confirm:
            return {
                "project_id": project_id,
                "project_name": project_name,
                "entity_counts": counts,
                "total_rows": sum(counts.values()),
                "confirmed": False,
                "message": (
                    "Set confirm=true to permanently delete this project and its "
                    "RKA-managed data. Source-adjacent recovery candidates, when "
                    "listed, are intentionally retained."
                ),
            }

        await self.db.execute(
            """INSERT INTO project_deletion_authorizations (project_id)
               VALUES (?)""",
            [project_id],
        )

        # Child tables without their own project_id need scoped joins. Break
        # the checkpoint self-reference before its ON DELETE RESTRICT guard.
        await self.db.execute(
            """DELETE FROM qa_logs
               WHERE session_id IN (
                   SELECT id FROM qa_sessions WHERE project_id = ?
               )""",
            [project_id],
        )
        await self.db.execute(
            """DELETE FROM entity_topics
               WHERE topic_id IN (
                   SELECT id FROM topics WHERE project_id = ?
               )""",
            [project_id],
        )
        await self.db.execute(
            """UPDATE manuscript_checkpoints
               SET supersedes_id = NULL
               WHERE project_id = ?""",
            [project_id],
        )
        await self.db.execute(
            """UPDATE decisions
               SET recommended_option_id = NULL,
                   pi_selected_option_id = NULL
               WHERE project_id = ?""",
            [project_id],
        )

        # Index rows first: they are keyed by the source ids and have no
        # project_id of their own, so once the source rows are gone there is
        # nothing left to identify them by.
        for source, index_tables in self._INDEX_TABLES:
            for index_table in index_tables:
                try:
                    await self.db.execute(
                        f"DELETE FROM {index_table} WHERE id IN "
                        f"(SELECT id FROM {source} WHERE project_id = ?)",
                        [project_id],
                    )
                except sqlite3.OperationalError as exc:
                    # sqlite-vec may be unavailable, and pre-migration
                    # databases do not have every index table.
                    if "no such table" not in str(exc).lower() and \
                       "no such module" not in str(exc).lower():
                        raise

        # Cascade delete in reverse dependency order
        for table in self._DELETE_TABLES:
            try:
                if table == "manuscript_planning_artifact_versions":
                    await self._delete_planning_versions_dependency_ordered(project_id)
                elif table == "manuscript_planning_branches":
                    await self._delete_planning_branches_dependency_ordered(project_id)
                else:
                    await self.db.execute(
                        f"DELETE FROM {table} WHERE project_id = ?",
                        [project_id],
                    )
            except sqlite3.OperationalError as exc:
                if "no such table" not in str(exc).lower():
                    raise
                # Some pre-migration databases do not have every scoped table.
            except sqlite3.IntegrityError as exc:
                raise sqlite3.IntegrityError(
                    f"Failed to delete project rows from {table}: {exc}"
                ) from exc

        # Delete the project row itself
        await self.db.execute("DELETE FROM projects WHERE id = ?", [project_id])
        await self.db.execute(
            """DELETE FROM project_deletion_authorizations
               WHERE project_id = ?""",
            [project_id],
        )
        await self.db.commit()

        return {
            "project_id": project_id,
            "project_name": project_name,
            "entity_counts": counts,
            "total_rows": sum(counts.values()),
            "confirmed": True,
            "message": (
                f"Project '{project_name}' and all RKA-managed database records "
                "have been permanently deleted."
            ),
        }

    def _row_to_model(self, row: dict) -> ProjectState:
        return ProjectState(
            project_name=row["project_name"],
            project_description=row.get("project_description"),
            current_phase=row.get("current_phase"),
            phases_config=self._json_loads(row.get("phases_config"), []),
            summary=row.get("summary"),
            blockers=row.get("blockers"),
            metrics=self._json_loads(row.get("metrics"), {}),
            created_at=row.get("created_at"),
            updated_at=row.get("updated_at"),
        )
