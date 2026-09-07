"""Attempt-local lease and source-snapshot fences, checked before vector writes."""

from contextvars import ContextVar

from rka.services.jobs import JobLeaseLost


class EmbeddingSourceChanged(RuntimeError):
    pass


execution: ContextVar[dict | None] = ContextVar("rka_job_execution", default=None)


async def assert_job_write(db) -> None:
    current = execution.get()
    if current is None:
        return
    queue, job = current["queue"], current["job"]
    if queue.db is not db:
        raise JobLeaseLost("job attempted a write through a different database connection")
    await queue.assert_owned(job)
    snapshot = current.get("source")
    if snapshot:
        table, entity_id, project_id, before = snapshot
        after = await db.fetchone(
            f"SELECT * FROM {table} WHERE id=? AND project_id=?",
            [entity_id, project_id],
        )
        if before != after:
            raise EmbeddingSourceChanged(
                "embedding source changed during inference; retry required"
            )
