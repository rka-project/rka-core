"""Review queue service for Brain-augmented enrichment (v2.0)."""

from __future__ import annotations

import json

from rka.infra.ids import generate_id
from rka.models.review_queue import ReviewItem, ReviewItemCreate, ReviewItemResolve
from rka.services.base import BaseService, VALID_ACTORS, _now


class ReviewQueueService(BaseService):
    """Manages the Brain review queue."""

    async def flag_for_review(self, data: ReviewItemCreate) -> ReviewItem:
        async with self.db.transaction():
            await self._require_review_target(data.item_type, data.item_id)
            return await self._flag_for_review(data)

    async def _require_review_target(self, kind, entity_id):
        if kind in self._LINK_ENTITY_TABLES:
            await self._require_link_entity(kind, entity_id, project_id=self.project_id)
            return
        table = {"topic": "topics", "summary": "exploration_summaries"}.get(kind)
        if table is None or not await self.db.fetchone(f"SELECT id FROM {table} WHERE id = ? AND project_id = ?", [entity_id, self.project_id]):
            raise ValueError("review target not found in project")

    async def _flag_for_review(self, data: ReviewItemCreate) -> ReviewItem:
        review_id = generate_id("review")
        await self.db.execute(
            """INSERT INTO review_queue
               (id, item_type, item_id, flag, context, priority, raised_by, project_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                review_id, data.item_type, data.item_id, data.flag,
                json.dumps(data.context) if data.context else None,
                data.priority, data.raised_by, self.project_id,
            ],
        )
        await self.db.commit()
        return await self.get(review_id)

    async def get(self, review_id: str) -> ReviewItem | None:
        row = await self.db.fetchone(
            "SELECT * FROM review_queue WHERE id = ? AND project_id = ?",
            [review_id, self.project_id],
        )
        if row is None:
            return None
        return self._row_to_model(row)

    async def get_pending(
        self,
        status: str = "pending",
        flag: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> list[ReviewItem]:
        conditions = ["project_id = ?", "status = ?"]
        params: list = [self.project_id, status]

        if flag:
            conditions.append("flag = ?")
            params.append(flag)

        where = " AND ".join(conditions)
        params.extend([limit, offset])

        rows = await self.db.fetchall(
            f"SELECT * FROM review_queue WHERE {where} ORDER BY priority ASC, created_at ASC LIMIT ? OFFSET ?",
            params,
        )
        return [self._row_to_model(r) for r in rows]

    async def resolve(self, review_id: str, data: ReviewItemResolve) -> ReviewItem:
        async with self.db.transaction():
            item = await self.get(review_id)
            if item is None:
                raise ValueError("review not found in project")
            await self._require_review_target(item.item_type, item.item_id)
            if not data.resolved_by.strip() or not data.resolution.strip() or data.status == "pending":
                raise ValueError("review resolution requires rationale and a non-pending status")
            if item.status in {"resolved", "dismissed"}:
                if (item.status, item.resolved_by, item.resolution) == (data.status, data.resolved_by, data.resolution):
                    return item
                raise ValueError("review already closed with different resolution")
            result = await self._resolve_open(review_id, data)
            audit_actor = data.resolved_by if data.resolved_by in VALID_ACTORS else "system"
            await self.audit("update", item.item_type, item.item_id, audit_actor,
                             {"action": "review_resolution", "review_id": review_id, "before_status": item.status,
                              "after_status": data.status, "resolution": data.resolution, "resolved_by": data.resolved_by})
            return result

    async def _resolve_open(self, review_id: str, data: ReviewItemResolve) -> ReviewItem:
        await self.db.execute(
            """UPDATE review_queue
               SET status = ?, resolved_by = ?, resolution = ?, resolved_at = ?
               WHERE id = ? AND project_id = ?""",
            [
                data.status,
                data.resolved_by, data.resolution,
                _now(), review_id, self.project_id,
            ],
        )
        await self.db.commit()
        return await self.get(review_id)

    async def get_stats(self) -> dict:
        rows = await self.db.fetchall(
            """SELECT status, COUNT(*) as cnt FROM review_queue
               WHERE project_id = ? GROUP BY status""",
            [self.project_id],
        )
        stats = {r["status"]: r["cnt"] for r in rows}

        flag_rows = await self.db.fetchall(
            """SELECT flag, COUNT(*) as cnt FROM review_queue
               WHERE project_id = ? AND status = 'pending' GROUP BY flag""",
            [self.project_id],
        )
        by_flag = {r["flag"]: r["cnt"] for r in flag_rows}

        return {
            "total_pending": stats.get("pending", 0),
            "total_resolved": stats.get("resolved", 0),
            "total_dismissed": stats.get("dismissed", 0),
            "by_flag": by_flag,
        }

    @staticmethod
    def _row_to_model(row: dict) -> ReviewItem:
        ctx = row.get("context")
        if ctx and isinstance(ctx, str):
            try:
                ctx = json.loads(ctx)
            except (json.JSONDecodeError, TypeError):
                pass
        return ReviewItem(
            id=row["id"],
            item_type=row["item_type"],
            item_id=row["item_id"],
            flag=row["flag"],
            context=ctx,
            priority=row.get("priority", 100),
            status=row.get("status", "pending"),
            raised_by=row.get("raised_by", "llm"),
            resolved_by=row.get("resolved_by"),
            resolution=row.get("resolution"),
            project_id=row.get("project_id", "proj_default"),
            created_at=row.get("created_at"),
            resolved_at=row.get("resolved_at"),
        )
