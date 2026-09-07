"""Durable import receipts, distinct from their worker-owned vector job."""

import json

from rka.services.base import _now
from rka.services.embedding_jobs import EmbeddingJobs
from rka.services.embedding_index import get_embedding_index_state
from rka.services.jobs import JobQueue


class ImportIndexing:
    def __init__(self, db):
        self.db = db

    async def record(self, project_id, embeddings, *, lexical_count):
        """Called in the graph/FTS transaction; failure rolls back the import."""
        async with self.db.transaction():
            embedding_job = (
                await EmbeddingJobs(self.db).request(embeddings, project_id=project_id)
                if embeddings is not None
                else None
            )
            queue = JobQueue(self.db)
            receipt_id = await queue.enqueue(
                "pack_import",
                project_id=project_id,
                payload={
                    "version": 1,
                    "embedding_job_id": embedding_job["id"] if embedding_job else None,
                },
            )
            # This records work already completed by the import transaction,
            # not an attempt claimed from the worker queue. It is never visible
            # as pending: both INSERT and terminal receipt commit atomically.
            await self.db.execute(
                """UPDATE jobs SET status='completed', completed_at=?, result=? WHERE id=?""",
                [
                    _now(),
                    json.dumps({"processed": lexical_count, "total": lexical_count}),
                    receipt_id,
                ],
            )
        return {
            "job_id": receipt_id,
            "status_url": f"/api/projects/import/status?job_id={receipt_id}",
            "note": "Rows and lexical search are durable. Check semantic_state separately; semantic recall requires a worker and a ready embedding generation.",
        }

    async def status(self, job_id=None):
        params = [job_id] if job_id else []
        row = await self.db.fetchone(
            "SELECT * FROM jobs WHERE job_type='pack_import'"
            + (" AND id=?" if job_id else " ORDER BY rowid DESC LIMIT 1"),
            params,
        )
        if row is None:
            return None
        receipt = JobQueue._decode_row(row)
        progress = receipt["result"] or {}
        embedding_id = receipt["payload"].get("embedding_job_id")
        semantic = await EmbeddingJobs(self.db).status(embedding_id) if embedding_id else None
        state = await get_embedding_index_state(self.db)
        missing = embedding_id is not None and semantic is None
        return {
            "job_id": receipt["id"],
            "project_id": receipt["project_id"],
            "state": "failed" if missing else semantic["state"] if semantic else "complete",
            "lexical_state": "complete",
            "lexical_processed": progress.get("processed", 0),
            "semantic_state": "failed"
            if missing
            else semantic["state"]
            if semantic
            else "disabled",
            "semantic_ready": bool(
                semantic
                and semantic["state"] == "complete"
                and state
                and state.status == "ready"
                and state.generation == semantic["generation"]
            ),
            "embedding_job_id": embedding_id,
            "generation": semantic["generation"] if semantic else None,
            "index_state": state.status if state else "unavailable",
            "processed": semantic["processed"] if semantic else progress.get("processed", 0),
            "total": semantic["total"] if semantic else progress.get("total", 0),
            "started_at": receipt["created_at"],
            "elapsed_seconds": semantic["elapsed_seconds"] if semantic else 0,
            "error": "embedding_job_missing"
            if missing
            else semantic["error"]
            if semantic
            else None,
        }
