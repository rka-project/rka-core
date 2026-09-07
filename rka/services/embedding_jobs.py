"""Durable generation-bound backfill intents, consumed only by Core workers."""

from datetime import datetime, timezone

from rka.services.embedding_backfill import BackfillService, JobStatus, _DEFAULT_ENTITY_TYPES
from rka.services.embedding_index import (
    EmbeddingGenerationMismatch,
    finish_embedding_transition,
    get_embedding_index_state,
    resume_embedding_transition,
)
from rka.services.jobs import JobQueue

JOB_TYPE = "embedding_backfill"


class BackfillScopeBusy(RuntimeError):
    pass


def validate_types(entity_types=None):
    if entity_types is None:
        return _DEFAULT_ENTITY_TYPES
    if not isinstance(entity_types, (tuple, list)) or not entity_types or len(entity_types) > 7:
        raise ValueError("entity_types must be a non-empty subset of the seven embedding types")
    if any(t not in _DEFAULT_ENTITY_TYPES for t in entity_types):
        raise ValueError("unknown embedding entity type")
    return tuple(t for t in _DEFAULT_ENTITY_TYPES if t in entity_types)


class EmbeddingJobs:
    def __init__(self, db):
        self.db = db
        self.queue = JobQueue(db)

    async def request(self, embeddings, entity_types=None, *, automatic=False):
        types = validate_types(entity_types)
        async with self.db.transaction():
            state = await get_embedding_index_state(self.db)
            if (
                state is None
                or state.generation != embeddings.index_generation
                or state.space_signature != embeddings.space_signature
                or state.model_name != embeddings.model_name
                or state.dimensions != embeddings.dim
            ):
                raise EmbeddingGenerationMismatch(
                    "embedding generation changed; reload configuration"
                )
            key = f"embedding_backfill:{state.generation}"
            latest = await self.db.fetchone(
                """SELECT * FROM jobs WHERE job_type=? AND project_id='proj_default'
                   AND dedupe_key=? ORDER BY rowid DESC LIMIT 1""",
                [JOB_TYPE, key],
            )
            latest = self.queue._decode_row(latest) if latest else None
            if latest and latest["status"] in {"pending", "running"}:
                if not set(types).issubset(latest["payload"]["entity_types"]):
                    raise BackfillScopeBusy(
                        "an active backfill has a different scope; wait or cancel it first"
                    )
                return latest
            if automatic and latest and latest["status"] == "failed":
                await finish_embedding_transition(
                    self.db,
                    generation=state.generation,
                    success=False,
                    error=latest["last_error"],
                )
                return latest
            if automatic:
                from rka.services.embedding_backfill import _ENTITY_BACKFILL_CONFIGS

                pending = 0
                for cfg in _ENTITY_BACKFILL_CONFIGS.values():
                    row = await self.db.fetchone(
                        cfg.pending_count_sql, [state.model_name, state.dimensions]
                    )
                    pending += row["n"]
                if not pending and state.status == "ready":
                    return None
            # Invalidate old generations' queued/running authority, without
            # copying provider config or secrets into the durable intent.
            await self.db.execute(
                """UPDATE jobs SET status='failed', lease_until=NULL, worker_id=NULL,
                   lease_token=NULL, last_error='embedding_generation_superseded',
                   completed_at=strftime('%Y-%m-%dT%H:%M:%SZ','now')
                   WHERE job_type=? AND project_id='proj_default' AND dedupe_key<>?
                   AND status IN ('pending','running')""",
                [JOB_TYPE, key],
            )
            await resume_embedding_transition(
                self.db,
                generation=state.generation,
                space_signature=state.space_signature,
                model_name=state.model_name,
                dim=state.dimensions,
            )
            await self.db.execute(
                "UPDATE embedding_index_state SET status='reindexing', last_error=NULL WHERE singleton=1",
            )
            job_id = await self.queue.enqueue(
                JOB_TYPE,
                dedupe_key=key,
                priority=120,
                payload={
                    "version": 1,
                    "generation": state.generation,
                    "space_signature": state.space_signature,
                    "model_name": state.model_name,
                    "dimensions": state.dimensions,
                    "entity_types": list(types),
                },
            )
            return await self.queue.get(job_id)

    async def status(self, job_id=None):
        if job_id:
            row = await self.db.fetchone(
                "SELECT * FROM jobs WHERE id=? AND job_type=?", [job_id, JOB_TYPE]
            )
        else:
            row = await self.db.fetchone(
                "SELECT * FROM jobs WHERE job_type=? ORDER BY rowid DESC LIMIT 1",
                [JOB_TYPE],
            )
        if row is None:
            return None
        job = self.queue._decode_row(row)
        progress = job["result"] or {}
        error = job["last_error"]
        start = datetime.fromisoformat(job["created_at"].replace("Z", "+00:00"))
        end = (
            datetime.fromisoformat(job["completed_at"].replace("Z", "+00:00"))
            if job["completed_at"]
            else datetime.now(timezone.utc)
        )
        return {
            "job_id": job["id"],
            "state": "complete" if job["status"] == "completed" else job["status"],
            "processed": progress.get("processed", 0),
            "total": progress.get("total", 0),
            "started_at": job["created_at"],
            "elapsed_seconds": max(0, (end - start).total_seconds()),
            "error": error,
            "generation": job["payload"]["generation"],
            "attempts": job["attempts"],
            "max_attempts": job["max_attempts"],
            "run_after": job["run_after"],
            "lease_until": job["lease_until"],
            "error_code": error.split(":", 1)[0]
            if error and error.startswith("embedding_")
            else None,
        }

    async def cancel(self, job_id):
        async with self.db.transaction():
            job = await self.queue.get(job_id)
            if not job or job["job_type"] != JOB_TYPE:
                raise LookupError("unknown embedding backfill job")
            if job["status"] in {"pending", "running"}:
                await self.db.execute(
                    """UPDATE jobs SET status='failed', lease_until=NULL, worker_id=NULL,
                       lease_token=NULL, last_error='embedding_backfill_cancelled',
                       completed_at=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE id=?""",
                    [job_id],
                )
                await finish_embedding_transition(
                    self.db,
                    generation=job["payload"]["generation"],
                    success=False,
                    error="embedding_backfill_cancelled",
                )
        return await self.status(job_id)

    async def run(self, job, embeddings, queue):
        payload = job["payload"]
        if embeddings is None:
            raise RuntimeError("embedding_unavailable: worker has no configured backend")
        if any(
            (
                payload["generation"] != embeddings.index_generation,
                payload["space_signature"] != embeddings.space_signature,
                payload["model_name"] != embeddings.model_name,
                payload["dimensions"] != embeddings.dim,
            )
        ):
            raise EmbeddingGenerationMismatch("embedding_generation_superseded")
        await queue.assert_owned(job)
        await resume_embedding_transition(
            self.db,
            generation=payload["generation"],
            space_signature=embeddings.space_signature,
            model_name=embeddings.model_name,
            dim=embeddings.dim,
        )
        status = JobStatus(job_id=job["id"], state="running")

        async def progress(value):
            await queue.progress(job, {"processed": value.processed, "total": value.total})

        await BackfillService(db=self.db, embeddings=embeddings).run_backfill(
            status,
            progress_callback=progress,
            entity_types=validate_types(payload["entity_types"]),
        )
        if status.state != "complete":
            raise RuntimeError(status.error or "embedding_backfill_failed")
        return {"processed": status.processed, "total": status.total}

    async def finish(self, job, *, success, queue):
        await queue.assert_owned(job)
        await finish_embedding_transition(
            self.db,
            generation=job["payload"]["generation"],
            success=success,
        )
        state = await get_embedding_index_state(self.db)
        if success and (
            state is None
            or state.generation != job["payload"]["generation"]
            or state.status != "ready"
        ):
            raise RuntimeError("embedding_index_incomplete: coverage/consistency gate failed")

    async def reconcile_failed(self):
        """Queue exhaustion may be detected during claim after process loss."""
        async with self.db.transaction():
            state = await get_embedding_index_state(self.db)
            if state is None or state.status != "reindexing":
                return
            row = await self.db.fetchone(
                """SELECT status,last_error FROM jobs WHERE job_type=? AND dedupe_key=?
                   ORDER BY rowid DESC LIMIT 1""",
                [JOB_TYPE, f"embedding_backfill:{state.generation}"],
            )
            if row and row["status"] == "failed":
                await finish_embedding_transition(
                    self.db,
                    generation=state.generation,
                    success=False,
                    error=row["last_error"],
                )
