"""Checkpoint service."""

from __future__ import annotations

import json

from rka.infra.ids import generate_id
from rka.models.checkpoint import (
    Checkpoint, CheckpointCreate, CheckpointResolve, CheckpointOption,
)
from rka.services.base import BaseService, _now


class CheckpointNotFoundError(ValueError):
    """Raised when a checkpoint is absent from the active project scope."""


class CheckpointService(BaseService):
    """Manages checkpoints (Executor submits, Brain/PI resolves)."""

    async def create(self, data: CheckpointCreate, actor: str = "executor") -> Checkpoint:
        """Create a new checkpoint."""
        chk_id = generate_id("checkpoint")

        options_json = None
        if data.options:
            options_json = json.dumps([o.model_dump() for o in data.options])

        async with self.db.transaction():
            mission = await self.db.fetchone(
                "SELECT id FROM missions WHERE id = ? AND project_id = ?",
                [data.mission_id, self.project_id],
            )
            if mission is None:
                raise ValueError(
                    f"mission {data.mission_id!r} not found in project "
                    f"{self.project_id}"
                )

            await self.db.execute(
                """INSERT INTO checkpoints
                   (id, mission_id, task_reference, type, description, context,
                    options, recommendation, blocking, project_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    chk_id, data.mission_id, data.task_reference,
                    data.type, data.description, data.context,
                    options_json, data.recommendation,
                    1 if data.blocking else 0, self.project_id,
                ],
            )
            await self.db.commit()

            await self.emit_event(
                event_type="checkpoint_created",
                entity_type="checkpoint",
                entity_id=chk_id,
                actor=actor,
                summary=f"Checkpoint ({data.type}): {data.description[:100]}",
            )
            await self.audit("create", "checkpoint", chk_id, actor)
        return await self.get(chk_id)

    async def get(self, chk_id: str) -> Checkpoint | None:
        """Get a single checkpoint."""
        row = await self.db.fetchone(
            "SELECT * FROM checkpoints WHERE id = ? AND project_id = ?",
            [chk_id, self.project_id],
        )
        if row is None:
            return None
        return self._row_to_model(row)

    async def list(
        self,
        status: str | None = None,
        mission_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Checkpoint]:
        """List checkpoints with filters."""
        conditions = []
        params = [self.project_id]

        conditions.append("project_id = ?")

        if status:
            conditions.append("status = ?")
            params.append(status)
        if mission_id:
            conditions.append("mission_id = ?")
            params.append(mission_id)

        where = " AND ".join(conditions) if conditions else "1=1"
        params.extend([limit, offset])

        rows = await self.db.fetchall(
            f"SELECT * FROM checkpoints WHERE {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            params,
        )
        return [self._row_to_model(row) for row in rows]

    async def resolve(
        self,
        chk_id: str,
        data: CheckpointResolve,
        decision_service=None,
    ) -> Checkpoint:
        """Resolve a checkpoint."""
        now = _now()
        if not data.resolution.strip():
            raise ValueError("resolution must not be blank")
        linked_decision_id = None

        async with self.db.transaction():
            checkpoint = await self.db.fetchone(
                """SELECT *
                   FROM checkpoints
                   WHERE id = ? AND project_id = ?""",
                [chk_id, self.project_id],
            )
            if checkpoint is None:
                raise CheckpointNotFoundError(
                    f"checkpoint {chk_id!r} not found in project "
                    f"{self.project_id}"
                )

            if checkpoint["status"] != "open":
                if (checkpoint["status"] == "resolved" and checkpoint["resolution"] == data.resolution
                        and checkpoint["resolved_by"] == data.resolved_by
                        and checkpoint.get("resolution_rationale") == data.rationale
                        and bool(checkpoint.get("linked_decision_id")) == data.create_decision):
                    return self._row_to_model(checkpoint)
                raise ValueError("checkpoint already closed; conflicting resolution rejected")
            await self._require_link_entity("mission", checkpoint["mission_id"], project_id=self.project_id)
            if data.create_decision and decision_service is None:
                raise ValueError("create_decision requires a scoped decision service")

            # Optionally create a linked decision. DecisionService uses the
            # same managed Database, so its nested transaction becomes a
            # savepoint owned by this aggregate resolution.
            if data.create_decision and decision_service:
                if (
                    decision_service.db is not self.db
                    or decision_service.project_id != self.project_id
                ):
                    raise ValueError(
                        "decision_service must share the checkpoint database "
                        "and project scope"
                    )
                from rka.models.decision import DecisionCreate
                project_row = await self.db.fetchone(
                    "SELECT current_phase FROM project_states WHERE project_id = ?",
                    [self.project_id],
                )
                dec_data = DecisionCreate(
                    question=checkpoint["description"],
                    chosen=data.resolution,
                    rationale=data.rationale or "",
                    decided_by=data.resolved_by,
                    phase=(project_row or {}).get("current_phase") or "",
                )
                dec = await decision_service.create(
                    dec_data,
                    actor=data.resolved_by,
                )
                linked_decision_id = dec.id

            cursor = await self.db.execute(
                """UPDATE checkpoints
                   SET resolution = ?, resolved_by = ?, resolution_rationale = ?,
                       linked_decision_id = ?, status = 'resolved', resolved_at = ?
                   WHERE id = ? AND project_id = ?""",
                [data.resolution, data.resolved_by, data.rationale,
                 linked_decision_id, now, chk_id, self.project_id],
            )
            if cursor.rowcount != 1:  # pragma: no cover - protected by write lock
                raise CheckpointNotFoundError(
                    f"checkpoint {chk_id!r} not found in project "
                    f"{self.project_id}"
                )
            await self.db.commit()

            resolve_evt = await self.emit_event(
                event_type="checkpoint_resolved",
                entity_type="checkpoint",
                entity_id=chk_id,
                actor=data.resolved_by,
                summary=f"Resolved: {data.resolution[:100]}",
            )

            # If a decision was created, link the causal chain.
            if linked_decision_id:
                await self.emit_event(
                    event_type="decision_created",
                    entity_type="decision",
                    entity_id=linked_decision_id,
                    actor=data.resolved_by,
                    summary="Decision created from checkpoint resolution",
                    caused_by_event=resolve_evt,
                    caused_by_entity=chk_id,
                )

            await self.audit(
                "update",
                "checkpoint",
                chk_id,
                data.resolved_by,
                {"action": "resolve"},
            )
        return await self.get(chk_id)

    def _row_to_model(self, row: dict) -> Checkpoint:
        options = None
        if row.get("options"):
            raw = self._json_loads(row["options"], [])
            options = [CheckpointOption(**o) for o in raw]

        return Checkpoint(
            id=row["id"],
            mission_id=row.get("mission_id"),
            task_reference=row.get("task_reference"),
            type=row["type"],
            description=row["description"],
            context=row.get("context"),
            options=options,
            recommendation=row.get("recommendation"),
            blocking=bool(row.get("blocking", 1)),
            resolution=row.get("resolution"),
            resolved_by=row.get("resolved_by"),
            resolution_rationale=row.get("resolution_rationale"),
            linked_decision_id=row.get("linked_decision_id"),
            status=row["status"],
            created_at=row.get("created_at"),
            resolved_at=row.get("resolved_at"),
        )
