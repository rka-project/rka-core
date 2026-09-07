"""Durable DB-backed job queue for asynchronous enrichment work."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from rka.infra.database import Database
from rka.infra.ids import generate_id
from rka.services.base import DEFAULT_PROJECT_ID, _now

logger = logging.getLogger(__name__)


class JobLeaseLost(RuntimeError):
    """The worker no longer owns the claimed attempt it tried to finish."""


def _after_seconds(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


class JobQueue:
    """Simple durable queue stored in SQLite."""

    def __init__(
        self,
        db: Database,
        *,
        lease_seconds: int = 300,
        default_max_attempts: int = 5,
    ):
        self.db = db
        self.lease_seconds = lease_seconds
        self.default_max_attempts = default_max_attempts

    async def enqueue(
        self,
        job_type: str,
        *,
        project_id: str = DEFAULT_PROJECT_ID,
        entity_type: str | None = None,
        entity_id: str | None = None,
        payload: dict[str, Any] | None = None,
        dedupe_key: str | None = None,
        priority: int = 100,
        max_attempts: int | None = None,
        run_after: str | None = None,
    ) -> str:
        """Enqueue a job and coalesce with an existing active job when dedupe_key matches."""
        job_id = generate_id("job")
        now = _now()
        async with self.db.transaction():
            cursor = await self.db.execute(
                """INSERT OR IGNORE INTO jobs
                   (id, job_type, project_id, entity_type, entity_id, payload, status,
                    attempts, max_attempts, priority, run_after, dedupe_key,
                    created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?, ?, ?, ?)""",
                [
                    job_id,
                    job_type,
                    project_id,
                    entity_type,
                    entity_id,
                    json.dumps(payload) if payload is not None else None,
                    max_attempts or self.default_max_attempts,
                    priority,
                    run_after or now,
                    dedupe_key,
                    now,
                    now,
                ],
            )
            if cursor.rowcount == 1:
                return job_id
            if not dedupe_key:
                raise RuntimeError("Generated job id collided with an existing row")
            row = await self.db.fetchone(
                """SELECT id
                   FROM jobs
                   WHERE dedupe_key = ?
                     AND job_type = ?
                     AND project_id = ?
                     AND status IN ('pending', 'running')
                   ORDER BY created_at DESC
                   LIMIT 1""",
                [dedupe_key, job_type, project_id],
            )
            if row is None:
                raise RuntimeError(
                    "Active dedupe conflict disappeared before it could be read"
                )
            return row["id"]

    async def claim_next(self, worker_id: str) -> dict[str, Any] | None:
        """Claim the next runnable job."""
        if not isinstance(worker_id, str) or not worker_id or len(worker_id) > 128:
            raise ValueError(
                "worker_id must be a non-empty identifier up to 128 characters"
            )
        now = _now()
        lease_until = _after_seconds(self.lease_seconds)
        async with self.db.transaction():
            # A worker can disappear after consuming its final allowed
            # attempt. Terminalize that expired lease before selecting work so
            # it can never be reclaimed for attempt max_attempts + 1.
            await self.db.execute(
                """UPDATE jobs
                   SET status = 'failed',
                       lease_until = NULL,
                       lease_token = NULL,
                       worker_id = NULL,
                       last_error = 'lease expired after maximum attempts',
                       updated_at = ?,
                       completed_at = COALESCE(completed_at, ?)
                   WHERE status = 'running'
                     AND lease_until IS NOT NULL
                     AND lease_until <= ?
                     AND attempts >= max_attempts""",
                [now, now, now],
            )
            candidates = await self.db.fetchall(
                """SELECT id
                   FROM jobs
                   WHERE attempts < max_attempts
                     AND (
                         (status = 'pending' AND run_after <= ?)
                         OR (
                             status = 'running'
                             AND lease_until IS NOT NULL
                             AND lease_until <= ?
                         )
                     )
                   ORDER BY priority ASC, created_at ASC
                   LIMIT 10""",
                [now, now],
            )
            for candidate in candidates:
                lease_token = generate_id("lease")
                cursor = await self.db.execute(
                    """UPDATE jobs
                       SET status = 'running',
                           attempts = attempts + 1,
                           worker_id = ?,
                           lease_until = ?,
                           lease_token = ?,
                           updated_at = ?,
                           last_error = NULL
                       WHERE id = ?
                         AND attempts < max_attempts
                         AND (
                            (status = 'pending' AND run_after <= ?)
                            OR (
                                status = 'running'
                                AND lease_until IS NOT NULL
                                AND lease_until <= ?
                            )
                         )""",
                    [
                        worker_id,
                        lease_until,
                        lease_token,
                        now,
                        candidate["id"],
                        now,
                        now,
                    ],
                )
                if cursor.rowcount != 1:
                    continue
                row = await self.db.fetchone(
                    "SELECT * FROM jobs WHERE id = ?",
                    [candidate["id"]],
                )
                if row:
                    return self._decode_row(row)
        return None

    async def get(
        self,
        job_id: str,
        *,
        project_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Read one job without exposing another project's queue state."""
        if project_id is None:
            row = await self.db.fetchone("SELECT * FROM jobs WHERE id = ?", [job_id])
        else:
            row = await self.db.fetchone(
                "SELECT * FROM jobs WHERE id = ? AND project_id = ?",
                [job_id, project_id],
            )
        return self._decode_row(row) if row else None

    async def complete(
        self,
        job: dict[str, Any],
        result: dict[str, Any] | None = None,
    ) -> None:
        """Complete only the exact claimed attempt represented by ``job``."""
        lease_token = job.get("lease_token")
        worker_id = job.get("worker_id")
        if not lease_token or not worker_id:
            raise JobLeaseLost(f"Job {job.get('id')} has no active lease proof")
        now = _now()
        async with self.db.transaction():
            cursor = await self.db.execute(
                """UPDATE jobs
                   SET status = 'completed',
                       lease_until = NULL,
                       lease_token = NULL,
                       worker_id = NULL,
                       result = ?,
                       updated_at = ?,
                       completed_at = ?
                   WHERE id = ?
                     AND status = 'running'
                     AND worker_id = ?
                     AND lease_token = ?
                     AND lease_until IS NOT NULL
                     AND lease_until > ?""",
                [
                    json.dumps(result) if result is not None else None,
                    now,
                    now,
                    job["id"],
                    worker_id,
                    lease_token,
                    now,
                ],
            )
            if cursor.rowcount != 1:
                raise JobLeaseLost(f"Job {job['id']} lease was superseded")

    async def fail(self, job: dict[str, Any], error: str, *, retry: bool = True) -> None:
        """Requeue with backoff, or mark failed after max_attempts."""
        lease_token = job.get("lease_token")
        worker_id = job.get("worker_id")
        if not lease_token or not worker_id:
            raise JobLeaseLost(f"Job {job.get('id')} has no active lease proof")
        attempts = int(job.get("attempts") or 0)
        max_attempts = int(job.get("max_attempts") or self.default_max_attempts)
        now = _now()
        terminal = not retry or attempts >= max_attempts
        status = "failed" if terminal else "pending"
        run_after = _after_seconds(self._backoff_seconds(attempts)) if not terminal else now
        async with self.db.transaction():
            cursor = await self.db.execute(
                """UPDATE jobs
                   SET status = ?,
                       run_after = ?,
                       lease_until = NULL,
                       lease_token = NULL,
                       worker_id = NULL,
                       last_error = ?,
                       updated_at = ?,
                       completed_at = CASE
                           WHEN ? = 'failed' THEN ?
                           ELSE completed_at
                       END
                   WHERE id = ?
                     AND status = 'running'
                     AND worker_id = ?
                     AND lease_token = ?
                     AND lease_until IS NOT NULL
                     AND lease_until > ?""",
                [
                    status,
                    run_after,
                    error[:1000],
                    now,
                    status,
                    now,
                    job["id"],
                    worker_id,
                    lease_token,
                    now,
                ],
            )
            if cursor.rowcount != 1:
                raise JobLeaseLost(f"Job {job['id']} lease was superseded")

    async def assert_owned(self, job: dict[str, Any]) -> None:
        """Call inside the write transaction to fence stale inference results."""
        row = await self.db.fetchone(
            """SELECT 1 AS owned FROM jobs WHERE id=? AND status='running'
               AND worker_id=? AND lease_token=? AND lease_until > ?""",
            [job["id"], job.get("worker_id"), job.get("lease_token"), _now()],
        )
        if not row:
            raise JobLeaseLost(f"Job {job['id']} lease was superseded or expired")

    async def renew(self, job: dict[str, Any]) -> None:
        async with self.db.transaction():
            await self.assert_owned(job)
            await self.db.execute(
                "UPDATE jobs SET lease_until=?, updated_at=? WHERE id=?",
                [_after_seconds(self.lease_seconds), _now(), job["id"]],
            )

    async def progress(self, job: dict[str, Any], result: dict[str, Any]) -> None:
        async with self.db.transaction():
            await self.assert_owned(job)
            await self.db.execute(
                "UPDATE jobs SET result=?, updated_at=? WHERE id=?",
                [json.dumps(result), _now(), job["id"]],
            )

    @staticmethod
    def _backoff_seconds(attempt: int) -> int:
        return min(300, 15 * (2 ** max(0, attempt - 1)))

    @staticmethod
    def _decode_row(row: dict[str, Any]) -> dict[str, Any]:
        payload = row.get("payload")
        result = row.get("result")
        row["payload"] = json.loads(payload) if payload else None
        row["result"] = json.loads(result) if result else None
        return row
