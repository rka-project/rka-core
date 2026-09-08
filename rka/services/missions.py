"""Mission lifecycle service."""

from __future__ import annotations

import json

from rka.infra.ids import generate_id
from rka.models.mission import (
    Mission, MissionCreate, MissionUpdate, MissionTask,
    MissionReport, MissionReportCreate,
)
from rka.models.journal import JournalEntryCreate
from rka.services.base import BaseService, _now
from rka.services.jobs import JobQueue


class MissionNotFoundError(ValueError):
    """Raised when a mission is absent from the active project scope."""


class MissionService(BaseService):
    """Manages mission lifecycle."""

    async def _validate_mission_parent(self, mis_id, target_id, column):
        if column not in {"depends_on", "parent_mission_id"}:
            raise ValueError("unsupported mission relationship")
        visited = {mis_id}
        while target_id:
            if target_id in visited:
                raise ValueError(f"{column} would form a mission cycle")
            visited.add(target_id)
            row = await self.db.fetchone(f"SELECT {column} FROM missions WHERE id = ? AND project_id = ?", [target_id, self.project_id])
            if row is None:
                raise ValueError(f"{column} target not found in project")
            target_id = row[column]

    def _job_dedupe_key(self, mis_id: str, operation: str) -> str:
        return f"{self.project_id}:mission:{mis_id}:{operation}"

    async def _enqueue_enrichment_jobs(
        self,
        mis_id: str,
        *,
        include_embedding: bool,
    ) -> None:
        if not include_embedding:
            return
        queue = JobQueue(self.db)
        await queue.enqueue(
            "mission_embed",
            project_id=self.project_id,
            entity_type="mission",
            entity_id=mis_id,
            dedupe_key=self._job_dedupe_key(mis_id, "embed"),
        )

    async def create(self, data: MissionCreate, actor: str = "brain") -> Mission:
        """Create a new mission.

        Raises ValueError (mapped to HTTP 400 by the route) when
        ``motivated_by_decision`` references a decision that does not exist in
        this project. Validating up front turns what was an opaque
        ``sqlite3.IntegrityError`` -> 500 (the FK constraint firing inside the
        INSERT) into a clear client error. An empty/whitespace reference is
        normalized to NULL ("no motivation") rather than attempting a doomed FK.
        """
        mis_id = generate_id("mission")

        # Normalize optional FK references: SQLite enforces a FK on any non-NULL
        # value, so an empty string would trip the constraint. Treat blank as
        # "unset".
        motivated_by = (data.motivated_by_decision or "").strip() or None
        depends_on = (data.depends_on or "").strip() or None

        tasks_json = None
        if data.tasks:
            tasks_json = json.dumps([t.model_dump() for t in data.tasks])

        async with self.db.transaction():
            await self._validate_mission_parent(mis_id, depends_on, "depends_on")
            if motivated_by is not None:
                dec = await self.db.fetchone(
                    "SELECT id FROM decisions WHERE id = ? AND project_id = ?",
                    [motivated_by, self.project_id],
                )
                if not dec:
                    raise ValueError(
                        f"motivated_by_decision {motivated_by!r} not found in "
                        f"project {self.project_id}"
                    )

            await self.db.execute(
                """INSERT INTO missions
                   (id, phase, objective, tasks, context, acceptance_criteria,
                    scope_boundaries, checkpoint_triggers, depends_on,
                    motivated_by_decision, project_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    mis_id,
                    data.phase,
                    data.objective,
                    tasks_json,
                    data.context,
                    data.acceptance_criteria,
                    data.scope_boundaries,
                    data.checkpoint_triggers,
                    depends_on,
                    motivated_by,
                    self.project_id,
                ],
            )

            await self._replace_incoming_links(
                target_type="mission",
                target_id=mis_id,
                link_type="motivated",
                source_type="decision",
                source_ids=[motivated_by] if motivated_by else [],
                created_by=actor,
            )
            await self._replace_incoming_links(
                target_type="mission",
                target_id=mis_id,
                link_type="triggered",
                source_type="mission",
                source_ids=[depends_on] if depends_on else [],
                created_by=actor,
            )

            if data.tags:
                await self._set_tags("mission", mis_id, data.tags)

            await self._sync_fts(
                "mission",
                mis_id,
                {"objective": data.objective, "context": data.context},
            )
            await self._enqueue_enrichment_jobs(
                mis_id,
                include_embedding=bool(self.embeddings),
            )
            await self.emit_event(
                event_type="mission_created",
                entity_type="mission",
                entity_id=mis_id,
                actor=actor,
                summary=f"Mission: {data.objective[:100]}",
                phase=data.phase,
            )
            await self.audit("create", "mission", mis_id, actor)
        return await self.get(mis_id)

    async def get(self, mis_id: str | None = None) -> Mission | None:
        """Get a mission. If no ID, return the currently active mission."""
        if mis_id:
            row = await self.db.fetchone(
                "SELECT * FROM missions WHERE id = ? AND project_id = ?",
                [mis_id, self.project_id],
            )
        else:
            row = await self.db.fetchone(
                "SELECT * FROM missions WHERE project_id = ? AND status = 'active' ORDER BY created_at DESC LIMIT 1",
                [self.project_id],
            )
        if row is None:
            return None
        return await self._row_to_model(row)

    async def list(
        self,
        phase: str | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Mission]:
        """List missions with filters."""
        conditions = []
        params = [self.project_id]

        conditions.append("project_id = ?")

        if phase:
            conditions.append("phase = ?")
            params.append(phase)
        if status:
            conditions.append("status = ?")
            params.append(status)

        where = " AND ".join(conditions) if conditions else "1=1"
        params.extend([limit, offset])

        rows = await self.db.fetchall(
            f"SELECT * FROM missions WHERE {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            params,
        )
        return [await self._row_to_model(row) for row in rows]

    async def update(self, mis_id: str, data: MissionUpdate, actor: str = "executor") -> Mission:
        """Update a mission. Generic dump-iterating (Pattern A) — every field
        declared in MissionUpdate is persisted; undeclared fields are rejected
        by Pydantic at request time (extra="forbid")."""
        self._validate_actor(actor)
        dump = data.model_dump(exclude_none=True)
        tags = dump.pop("tags", None)
        replace_motivated_by = "motivated_by_decision" in dump
        replace_depends_on = "depends_on" in dump

        updates = {}
        for field, value in dump.items():
            if field == "tasks":
                updates[field] = json.dumps(value)
            elif field in ("motivated_by_decision", "depends_on", "parent_mission_id"):
                updates[field] = (value or "").strip() or None
            else:
                updates[field] = value

        # Side effect: terminal-status transitions advance completed_at.
        if data.status in ("complete", "partial"):
            updates["completed_at"] = _now()

        async with self.db.transaction():
            owned = await self.db.fetchone(
                "SELECT id, status, report FROM missions WHERE id = ? AND project_id = ?",
                [mis_id, self.project_id],
            )
            if owned is None:
                raise MissionNotFoundError(
                    f"mission {mis_id!r} not found in project "
                    f"{self.project_id}"
                )

            if tags is not None:
                await self._set_tags("mission", mis_id, tags)

            for column in ("depends_on", "parent_mission_id"):
                if column in updates:
                    await self._validate_mission_parent(mis_id, updates[column], column)
            if owned["report"] and updates.get("status", owned["status"]) != owned["status"]:
                raise ValueError("accepted report is immutable; create a follow-up mission instead of reopening")

            if not updates:
                current = await self.get(mis_id)
                if current is None:  # pragma: no cover - protected by write lock
                    raise RuntimeError("mission update target disappeared")
                return current

            if replace_motivated_by and updates["motivated_by_decision"] is not None:
                dec = await self.db.fetchone(
                    "SELECT id FROM decisions WHERE id = ? AND project_id = ?",
                    [updates["motivated_by_decision"], self.project_id],
                )
                if not dec:
                    raise ValueError(
                        "motivated_by_decision "
                        f"{updates['motivated_by_decision']!r} not found in "
                        f"project {self.project_id}"
                    )

            set_clause = ", ".join(f"{k} = ?" for k in updates)
            values = list(updates.values()) + [mis_id]
            cursor = await self.db.execute(
                f"UPDATE missions SET {set_clause} WHERE id = ? AND project_id = ?",
                values + [self.project_id],
            )
            if cursor.rowcount != 1:  # pragma: no cover - protected by write lock
                raise MissionNotFoundError(
                    f"mission {mis_id!r} not found in project "
                    f"{self.project_id}"
                )

            if replace_motivated_by:
                motivated_by = updates["motivated_by_decision"]
                await self._replace_incoming_links(
                    target_type="mission",
                    target_id=mis_id,
                    link_type="motivated",
                    source_type="decision",
                    source_ids=[motivated_by] if motivated_by else [],
                    created_by=actor,
                )
            if replace_depends_on:
                depends_on = updates["depends_on"]
                await self._replace_incoming_links(
                    target_type="mission",
                    target_id=mis_id,
                    link_type="triggered",
                    source_type="mission",
                    source_ids=[depends_on] if depends_on else [],
                    created_by=actor,
                )

            if data.status:
                event_type_map = {
                    "complete": "mission_completed",
                    "blocked": "mission_blocked",
                }
                event_type = event_type_map.get(data.status, "status_updated")
                await self.emit_event(
                    event_type=event_type,
                    entity_type="mission",
                    entity_id=mis_id,
                    actor=actor,
                    summary=f"Mission status → {data.status}",
                )

            if "objective" in updates or "context" in updates:
                row = await self.db.fetchone(
                    "SELECT objective, context FROM missions "
                    "WHERE id = ? AND project_id = ?",
                    [mis_id, self.project_id],
                )
                if row:
                    await self._sync_fts("mission", mis_id, dict(row))
                    await self._enqueue_enrichment_jobs(
                        mis_id,
                        include_embedding=bool(self.embeddings),
                    )

            await self.audit(
                "update",
                "mission",
                mis_id,
                actor,
                {"fields": list(updates.keys())},
            )
        updated = await self.get(mis_id)
        if updated is None:  # pragma: no cover - guards impossible DB drift
            raise RuntimeError("mission update was not readable")
        return updated

    async def submit_report(
        self, mis_id: str, data: MissionReportCreate, actor: str = "executor"
    ) -> Mission:
        """First report wins; identical retries are side-effect free."""
        self._validate_actor(actor)
        async with self.db.transaction():
            mission = await self.get(mis_id)
            if mission is None:
                raise MissionNotFoundError("mission not found in project")
            if mission.report:
                old = mission.report.model_dump(exclude={"mission_id", "submitted_at"})
                if old != data.model_dump():
                    raise ValueError("accepted report cannot be overwritten; create a follow-up mission")
                recorded = await self.db.fetchone("SELECT actor FROM audit_log WHERE project_id = ? AND entity_type = 'mission' AND entity_id = ? AND json_extract(details, '$.action') = 'submit_report' ORDER BY id DESC LIMIT 1", [self.project_id, mis_id])
                if recorded and recorded["actor"] != actor:
                    raise ValueError("report retry actor differs from original")
                return mission
            if mission.status not in {"pending", "active", "partial", "complete"}:
                raise ValueError("blocked or cancelled mission cannot accept a completion report")
            return await self._submit_first_report(mis_id, data, actor)

    async def _submit_first_report(
        self, mis_id: str, data: MissionReportCreate, actor: str = "executor"
    ) -> Mission:
        """Submit an execution report for a mission."""
        report = MissionReport(
            mission_id=mis_id,
            # v2.6.1 — `summary` is now a first-class field; see
            # MissionReportCreate docstring for the migration note.
            summary=data.summary,
            tasks_completed=data.tasks_completed,
            findings=data.findings,
            anomalies=data.anomalies,
            questions=data.questions,
            codebase_state=data.codebase_state,
            recommended_next=data.recommended_next,
            submitted_at=_now(),
        )

        async with self.db.transaction():
            cursor = await self.db.execute(
                "UPDATE missions SET report = ?, status = 'complete', completed_at = ? "
                "WHERE id = ? AND project_id = ?",
                [report.model_dump_json(), _now(), mis_id, self.project_id],
            )
            if cursor.rowcount != 1:
                raise MissionNotFoundError(
                    f"mission {mis_id!r} not found in project "
                    f"{self.project_id}"
                )
            await self.emit_event(
                event_type="mission_completed",
                entity_type="mission",
                entity_id=mis_id,
                actor=actor,
                summary=f"Report submitted with {len(data.findings or [])} findings",
            )
            await self.audit(
                "update",
                "mission",
                mis_id,
                actor,
                {"action": "submit_report"},
            )

            # Materialized journal entries are part of the report aggregate:
            # any failure rolls back status, report, event, audit, and notes.
            mission = await self.get(mis_id)
            phase = mission.phase if mission else None
            await self._materialize_report(mis_id, phase, data, actor)

        return await self.get(mis_id)

    async def _materialize_report(
        self,
        mis_id: str,
        phase: str | None,
        data: MissionReportCreate,
        actor: str,
    ) -> None:
        """Create journal entries from each section of a mission report.

        Uses NoteService so entries get auto-tags, auto-links, FTS indexing,
        and events — exactly as if the Executor had called rka_add_note manually.
        Deduplication: skips creation if an identical content+mission entry exists.
        """
        # Lazy import to avoid circular dependency
        from rka.services.notes import NoteService

        note_svc = NoteService(
            self.db,
            llm=self.llm,
            embeddings=self.embeddings,
            project_id=self.project_id,
        )

        async def _create(note_type: str, text: str, confidence: str = "hypothesis") -> None:
            text = text.strip()
            if not text:
                return
            # Deduplicate: skip if identical content already linked to this mission
            existing = await self.db.fetchone(
                "SELECT id FROM journal WHERE related_mission = ? AND content = ? AND project_id = ?",
                [mis_id, text, self.project_id],
            )
            if existing:
                return
            note_in = JournalEntryCreate(
                type=note_type,
                content=text,
                source=actor,
                phase=phase,
                related_mission=mis_id,
                confidence=confidence,
            )
            await note_svc.create(note_in, actor=actor)

        async with self.db.transaction():
            for finding in data.findings or []:
                await _create("note", finding, confidence="tested")

            for anomaly in data.anomalies or []:
                await _create("note", anomaly, confidence="hypothesis")

            for question in data.questions or []:
                await _create("directive", question, confidence="hypothesis")

            if data.recommended_next:
                await _create("note", data.recommended_next, confidence="hypothesis")

    async def get_report(self, mis_id: str | None = None) -> MissionReport | None:
        """Get report for a mission. Defaults to latest complete mission."""
        if mis_id:
            row = await self.db.fetchone(
                "SELECT report FROM missions WHERE id = ? AND project_id = ?",
                [mis_id, self.project_id],
            )
        else:
            row = await self.db.fetchone(
                "SELECT report FROM missions WHERE project_id = ? AND status = 'complete' ORDER BY completed_at DESC LIMIT 1",
                [self.project_id],
            )
        if not row or not row.get("report"):
            return None
        return MissionReport.model_validate_json(row["report"])

    async def _row_to_model(self, row: dict) -> Mission:
        tags = await self._get_tags("mission", row["id"])
        enrichment_status = await self._get_enrichment_status("mission", row["id"])

        tasks = None
        if row.get("tasks"):
            raw = self._json_loads(row["tasks"], [])
            tasks = [MissionTask(**t) for t in raw]

        report = None
        if row.get("report"):
            try:
                report = MissionReport.model_validate_json(row["report"])
            except Exception:
                pass

        consistency_warnings: list[str] = []
        if row["status"] == "complete" and tasks:
            open_statuses = {"pending", "in_progress", "blocked"}
            open_tasks = [task for task in tasks if task.status in open_statuses]
            if open_tasks:
                counts = {
                    status: sum(task.status == status for task in open_tasks)
                    for status in sorted(open_statuses)
                    if any(task.status == status for task in open_tasks)
                }
                rendered_counts = ", ".join(
                    f"{status}={count}" for status, count in counts.items()
                )
                consistency_warnings.append(
                    "Mission is complete but has non-terminal task rows "
                    f"({rendered_counts}); reconcile task status before treating "
                    "the report as a fully closed execution record."
                )

        return Mission(
            id=row["id"],
            project_id=row["project_id"],
            phase=row["phase"],
            objective=row["objective"],
            tasks=tasks,
            context=row.get("context"),
            acceptance_criteria=row.get("acceptance_criteria"),
            scope_boundaries=row.get("scope_boundaries"),
            checkpoint_triggers=row.get("checkpoint_triggers"),
            status=row["status"],
            depends_on=row.get("depends_on"),
            report=report,
            iteration=row.get("iteration", 1),
            parent_mission_id=row.get("parent_mission_id"),
            motivated_by_decision=row.get("motivated_by_decision"),
            tags=tags,
            consistency_warnings=consistency_warnings,
            enrichment_status=enrichment_status,
            created_at=row.get("created_at"),
            completed_at=row.get("completed_at"),
        )

    async def process_auto_tag_job(self, mis_id: str) -> dict[str, str | int]:
        """Generate tags for an existing mission when none are present."""
        row = await self.db.fetchone(
            "SELECT objective FROM missions WHERE id = ? AND project_id = ?",
            [mis_id, self.project_id],
        )
        if row is None:
            return {"outcome": "missing"}
        if not self.llm:
            return {"outcome": "skipped", "reason": "llm_disabled"}

        existing_tags = await self._get_tags("mission", mis_id)
        if existing_tags:
            return {"outcome": "skipped", "reason": "tags_present"}

        auto_tags = await self._auto_enrich_tags(row["objective"], existing_tags)
        if not auto_tags:
            return {"outcome": "noop"}

        await self._set_tags("mission", mis_id, auto_tags)
        return {"outcome": "updated", "tag_count": len(auto_tags)}

    async def process_embedding_job(self, mis_id: str) -> dict[str, str | int]:
        """Generate or refresh the mission embedding."""
        row = await self.db.fetchone(
            "SELECT objective, context FROM missions WHERE id = ? AND project_id = ?",
            [mis_id, self.project_id],
        )
        if row is None:
            return {"outcome": "missing"}
        if not self.embeddings:
            return {"outcome": "skipped", "reason": "embeddings_disabled"}

        from rka.infra.embedding_documents import compose_document

        text = compose_document("mission", row)
        if not text:
            return {"outcome": "skipped", "reason": "empty"}

        await self.embeddings.embed_and_store("mission", mis_id, text, project_id=self.project_id)
        return {"outcome": "updated", "char_count": len(text)}
