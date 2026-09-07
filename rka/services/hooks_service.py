"""HooksService — CRUD over hooks, hook_executions, brain_notifications.

Distinct from ``HookDispatcher`` (which fires events). This service handles
registration / inspection / cleanup; the dispatcher reads from the same
tables at fire time.
"""

from __future__ import annotations

import json

from rka.infra.ids import generate_id
from rka.models.hooks import (
    BrainNotification,
    Hook,
    HookCreate,
    HookExecution,
)
from rka.services.base import BaseService
from rka.services.hook_policy import require_supported_hook_handler


def _row_to_hook(row: dict) -> Hook:
    return Hook(
        id=row["id"],
        event=row["event"],
        scope=row["scope"],
        project_id=row["project_id"],
        handler_type=row["handler_type"],
        handler_config=json.loads(row["handler_config"]),
        enabled=bool(row["enabled"]),
        name=row["name"],
        created_by=row["created_by"],
        created_at=row["created_at"],
        failure_policy=row["failure_policy"],
    )


def _row_to_execution(row: dict) -> HookExecution:
    return HookExecution(
        id=row["id"],
        hook_id=row["hook_id"],
        project_id=row["project_id"],
        fired_at=row["fired_at"],
        payload=json.loads(row["payload"]) if row["payload"] else None,
        handler_result=json.loads(row["handler_result"]) if row["handler_result"] else None,
        status=row["status"],
        error_message=row["error_message"],
        depth=row["depth"],
    )


def _row_to_notification(row: dict) -> BrainNotification:
    return BrainNotification(
        id=row["id"],
        project_id=row["project_id"],
        hook_id=row["hook_id"],
        created_at=row["created_at"],
        cleared_at=row["cleared_at"],
        content=json.loads(row["content"]),
        severity=row["severity"],
    )


class HooksService(BaseService):
    """CRUD for hook registration + audit + notifications."""

    # ----------------------------------------------------------------- hooks

    async def add(self, data: HookCreate) -> Hook:
        require_supported_hook_handler(data.handler_type)
        hook_id = generate_id("hook")
        await self.db.execute(
            """INSERT INTO hooks
               (id, event, project_id, handler_type, handler_config, enabled, name, created_by)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                hook_id,
                data.event,
                self.project_id,
                data.handler_type,
                json.dumps(data.handler_config),
                1 if data.enabled else 0,
                data.name,
                data.created_by,
            ],
        )
        await self.db.commit()
        row = await self.db.fetchone(
            "SELECT * FROM hooks WHERE id = ?", [hook_id],
        )
        return _row_to_hook(row)

    async def list_hooks(
        self,
        event: str | None = None,
        enabled_only: bool = False,
    ) -> list[Hook]:
        clauses = ["project_id = ?"]
        params: list = [self.project_id]
        if event:
            clauses.append("event = ?")
            params.append(event)
        if enabled_only:
            clauses.append("enabled = 1")
        rows = await self.db.fetchall(
            f"SELECT * FROM hooks WHERE {' AND '.join(clauses)} ORDER BY created_at DESC",
            params,
        )
        return [_row_to_hook(r) for r in rows]

    async def get(self, hook_id: str) -> Hook | None:
        row = await self.db.fetchone(
            "SELECT * FROM hooks WHERE id = ? AND project_id = ?",
            [hook_id, self.project_id],
        )
        return _row_to_hook(row) if row else None

    async def set_enabled(self, hook_id: str, enabled: bool) -> Hook | None:
        async with self.db.transaction():
            hook = await self.get(hook_id)
            if hook is None:
                return None
            if enabled:
                require_supported_hook_handler(hook.handler_type)
            await self.db.execute(
                "UPDATE hooks SET enabled = ? WHERE id = ? AND project_id = ?",
                [1 if enabled else 0, hook_id, self.project_id],
            )
            return await self.get(hook_id)

    async def delete(self, hook_id: str) -> bool:
        cursor = await self.db.execute(
            "DELETE FROM hooks WHERE id = ? AND project_id = ?",
            [hook_id, self.project_id],
        )
        await self.db.commit()
        return cursor.rowcount > 0

    # -------------------------------------------------- hook_executions audit

    async def list_executions(
        self,
        hook_id: str | None = None,
        since: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[HookExecution]:
        clauses = ["project_id = ?"]
        params: list = [self.project_id]
        if hook_id:
            clauses.append("hook_id = ?")
            params.append(hook_id)
        if since:
            clauses.append("fired_at >= ?")
            params.append(since)
        if status:
            clauses.append("status = ?")
            params.append(status)
        rows = await self.db.fetchall(
            f"""SELECT * FROM hook_executions
                WHERE {' AND '.join(clauses)}
                ORDER BY fired_at DESC LIMIT ?""",
            params + [limit],
        )
        return [_row_to_execution(r) for r in rows]

    # -------------------------------------------------- brain_notifications

    async def list_notifications(
        self,
        since: str | None = None,
        include_cleared: bool = False,
        limit: int = 100,
    ) -> list[BrainNotification]:
        clauses = ["project_id = ?"]
        params: list = [self.project_id]
        if not include_cleared:
            clauses.append("cleared_at IS NULL")
        if since:
            clauses.append("created_at >= ?")
            params.append(since)
        rows = await self.db.fetchall(
            f"""SELECT * FROM brain_notifications
                WHERE {' AND '.join(clauses)}
                ORDER BY created_at DESC LIMIT ?""",
            params + [limit],
        )
        return [_row_to_notification(r) for r in rows]

    async def clear_notifications(self, ids: list[str]) -> int:
        if not ids:
            return 0
        placeholders = ",".join("?" for _ in ids)
        cursor = await self.db.execute(
            f"""UPDATE brain_notifications
                SET cleared_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
                WHERE id IN ({placeholders}) AND project_id = ? AND cleared_at IS NULL""",
            ids + [self.project_id],
        )
        await self.db.commit()
        return cursor.rowcount
