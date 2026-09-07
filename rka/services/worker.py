"""Background worker for asynchronous embedding jobs.

Processes local embedding jobs. LLM-dependent enrichment (auto-tag, auto-link,
auto-summarize,
claim extraction, claim verification, theme synthesis, contradiction checks)
has been removed — those tasks are handled by the Brain during maintenance
sessions.

Worker boot reads the persisted embedding config from
`/data/embedding_config.json` via `EmbeddingConfigService.load_config()`
(see `EnrichmentWorker.boot()`). This matches the api-server boot path
established in v2.4.0 (rka/api/app.py) and was added in v2.5.8 per
dec_01KS3E1FGSK530N8HM04BNMCEW (Bug 2 fix; pre-existing bug surfaced by
corpus refresh mis_01KS0QEW21N2NG4EJTKJ3JTWTE).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import socket
from pathlib import Path
from typing import Any

from rka.infra.database import Database
from rka.services.jobs import JobLeaseLost, JobQueue

logger = logging.getLogger(__name__)

_EMBEDDING_JOB_TYPES = {
    "mission_embed",
    "note_embed",
    "claim_embed",
    "decision_embed",
    "literature_embed",
}


class EnrichmentWorker:
    """Poll the durable queue for embedding and migration-drain jobs."""

    def __init__(
        self,
        *,
        db: Database,
        embeddings=None,
        poll_interval: float = 1.0,
        lease_seconds: int = 300,
        max_attempts: int = 5,
        worker_id: str | None = None,
        data_dir: Path | str | None = None,
        embeddings_enabled: bool = True,
        env_fallback_model: str = "nomic-ai/nomic-embed-text-v1.5",
    ):
        self.db = db
        self.embeddings = embeddings
        self.poll_interval = poll_interval
        self.queue = JobQueue(db, lease_seconds=lease_seconds, default_max_attempts=max_attempts)
        self.worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}"
        self._embedding_data_dir = Path(data_dir) if data_dir is not None else None
        self._embeddings_enabled = embeddings_enabled
        self._env_fallback_model = env_fallback_model
        self._embedding_config_fingerprint: str | None = None
        self._embedding_generation: int | None = None

    # ------------------------------------------------------------------
    # v2.5.8 boot path (Bug 2 fix per dec_01KS3E1FGSK530N8HM04BNMCEW)
    # ------------------------------------------------------------------

    @classmethod
    def boot(
        cls,
        *,
        db: Database,
        data_dir: Path | str = Path("/data"),
        embeddings_enabled: bool = True,
        env_fallback_model: str = "nomic-ai/nomic-embed-text-v1.5",
        poll_interval: float = 1.0,
        lease_seconds: int = 300,
        max_attempts: int = 5,
        worker_id: str | None = None,
    ) -> "EnrichmentWorker":
        """Construct a worker with embeddings loaded from persisted config.

        Resolution order matches the api-server boot path (rka/api/app.py
        v2.4.0+):

        1. If embeddings_enabled is False: worker has embeddings=None.
        2. Otherwise, attempt to read `<data_dir>/embedding_config.json`
           via `EmbeddingConfigService.load_config()` and construct an
           `EmbeddingService.from_config(...)`.
        3. A missing file retains the legacy first-run default. An existing
           invalid file fails closed instead of selecting another model.

        Log lines explicitly indicate which path was taken so operators
        can correlate worker boot logs with config-changed-via-webui events.
        """
        embeddings = cls._resolve_embeddings(
            db=db,
            data_dir=data_dir,
            embeddings_enabled=embeddings_enabled,
            env_fallback_model=env_fallback_model,
        )
        return cls(
            db=db,
            embeddings=embeddings,
            poll_interval=poll_interval,
            lease_seconds=lease_seconds,
            max_attempts=max_attempts,
            worker_id=worker_id,
            data_dir=data_dir,
            embeddings_enabled=embeddings_enabled,
            env_fallback_model=env_fallback_model,
        )

    @staticmethod
    def _resolve_embeddings(
        *,
        db: Database,
        data_dir: Path | str,
        embeddings_enabled: bool,
        env_fallback_model: str,
    ):
        """Load EmbeddingService, failing closed on an invalid saved config."""
        if not embeddings_enabled:
            logger.info(
                "worker boot: embeddings_enabled=False; running without EmbeddingService"
            )
            return None

        # Lazy imports keep the worker.py top-level import surface small.
        from rka.infra.embeddings import EmbeddingService
        from rka.services.embedding_config import EmbeddingConfigService

        cfg_svc = EmbeddingConfigService(config_dir=data_dir)
        if not cfg_svc.config_path.exists():
            logger.info(
                "worker boot: falling back to env defaults; persisted config "
                "not found at %s",
                cfg_svc.config_path,
            )
            return EmbeddingService(model_name=env_fallback_model, db=db)

        embedding_cfg = cfg_svc.load_config()
        if not (embedding_cfg.config or {}).get("dim"):
            raise ValueError(
                "persisted embedding config has no detected dimension; save it through the API"
            )
        embeddings = EmbeddingService.from_config(embedding_cfg.model_dump(), db=db)
        logger.info(
            "worker boot: reading config from %s (backend=%s, dim=%d)",
            cfg_svc.config_path,
            embedding_cfg.backend,
            embeddings.dim,
        )
        return embeddings

    def _config_fingerprint(self) -> str:
        if self._embedding_data_dir is None:
            return "unmanaged"
        path = self._embedding_data_dir / "embedding_config.json"
        if not path.exists():
            return "missing"
        return hashlib.sha256(path.read_bytes()).hexdigest()

    async def _refresh_embeddings_before_job(self) -> None:
        """Reload config/generation before an embedding job when needed."""

        if not self._embeddings_enabled or self._embedding_data_dir is None:
            return

        from rka.infra.embeddings import EmbeddingService
        from rka.services.embedding_config import EmbeddingConfigService
        from rka.services.embedding_index import (
            EmbeddingGenerationMismatch,
            embedding_space_signature,
            get_embedding_index_state,
        )

        fingerprint = self._config_fingerprint()
        state = await get_embedding_index_state(self.db)
        generation = state.generation if state is not None else None
        if (
            self.embeddings is not None
            and fingerprint == self._embedding_config_fingerprint
            and generation == self._embedding_generation
        ):
            return

        cfg_svc = EmbeddingConfigService(config_dir=self._embedding_data_dir)
        if not cfg_svc.config_path.exists():
            if state is not None:
                raise EmbeddingGenerationMismatch(
                    "persisted embedding config is missing for the active generation"
                )
            embeddings = EmbeddingService(
                model_name=self._env_fallback_model,
                db=self.db,
            )
        else:
            embedding_cfg = cfg_svc.load_config()
            if not (embedding_cfg.config or {}).get("dim"):
                raise ValueError(
                    "persisted embedding config has no detected dimension; "
                    "save it through the API"
                )
            embeddings = EmbeddingService.from_config(
                embedding_cfg.model_dump(),
                db=self.db,
            )
            if state is not None:
                signature = embedding_space_signature(
                    embedding_cfg,
                    dimensions=embeddings.dim,
                )
                if (
                    signature != state.space_signature
                    or embeddings.model_name != state.model_name
                    or embeddings.dim != state.dimensions
                ):
                    raise EmbeddingGenerationMismatch(
                        "embedding config and active index generation do not match"
                    )
                embeddings.bind_index_generation(
                    state.generation,
                    space_signature=state.space_signature,
                )

        self.embeddings = embeddings
        self._embedding_config_fingerprint = fingerprint
        self._embedding_generation = generation
        logger.info(
            "worker refreshed embedding configuration (generation=%s)",
            generation if generation is not None else "legacy",
        )

    async def run_once(self) -> bool:
        """Process one job if available."""
        job = await self.queue.claim_next(self.worker_id)
        if job is None:
            from rka.services.embedding_jobs import EmbeddingJobs
            await EmbeddingJobs(self.db).reconcile_failed()
            return False

        try:
            result, scope = await self._with_heartbeat(job)
            async with self.db.transaction():
                # An edit can land after vector commit but before queue
                # completion. Keep its coalesced job retryable in that case.
                from rka.services.job_execution import execution, assert_job_write
                token = execution.set(scope)
                try:
                    await assert_job_write(self.db)
                finally:
                    execution.reset(token)
                if job["job_type"] == "embedding_backfill":
                    from rka.services.embedding_jobs import EmbeddingJobs
                    await EmbeddingJobs(self.db).finish(job, success=True, queue=self.queue)
                await self.queue.complete(job, result=result)
        except asyncio.CancelledError:
            try:
                await self.queue.fail(job, "embedding_worker_interrupted")
            except JobLeaseLost:
                pass
            raise
        except Exception as exc:  # pragma: no cover - failure path tested via queue state
            logger.exception("Worker job %s failed", job["id"])
            durable_error = str(exc)
            try:
                from rka.services.embedding_index import EmbeddingGenerationMismatch
                await self.queue.fail(
                    job, durable_error,
                    retry=not (job["job_type"] == "embedding_backfill" and isinstance(exc, EmbeddingGenerationMismatch)),
                )
            except JobLeaseLost:
                logger.info(
                    "Worker job %s failure ignored after lease supersession",
                    job["id"],
                )
            if job["job_type"] == "embedding_backfill":
                from rka.services.embedding_jobs import EmbeddingJobs
                await EmbeddingJobs(self.db).reconcile_failed()
        return True

    async def _with_heartbeat(self, job):
        from rka.services.job_execution import execution
        scope = {"queue": self.queue, "job": job}
        stop_heartbeat = asyncio.Event()

        async def process():
            token = execution.set(scope)
            try:
                return await self._process_job_with_generation_retry(job)
            finally:
                execution.reset(token)

        async def heartbeat():
            while not stop_heartbeat.is_set():
                await self.queue.renew(job)
                try:
                    await asyncio.wait_for(
                        stop_heartbeat.wait(), min(30.0, max(0.05, self.queue.lease_seconds / 3)),
                    )
                except TimeoutError:
                    pass

        work, pulse = asyncio.create_task(process()), asyncio.create_task(heartbeat())
        try:
            done, _ = await asyncio.wait((work, pulse), return_when=asyncio.FIRST_COMPLETED)
            if pulse in done:
                await pulse  # heartbeat loss must abort, not leave an unowned writer
            return await work, scope
        finally:
            work.cancel()
            stop_heartbeat.set()
            await asyncio.gather(work, pulse, return_exceptions=True)

    async def _process_job_with_generation_retry(
        self,
        job: dict[str, Any],
    ) -> dict[str, Any]:
        """Retry one managed embedding job after losing a generation race.

        The queue attempt already owns a live lease.  Reloading the durable
        config and retrying once in that lease avoids terminalizing a
        max-attempts=1 edit merely because its first vector write crossed a
        configuration transition.  Persistent config/backend failures still
        flow through the normal durable queue failure policy.
        """
        try:
            return await self._process_job(job)
        except Exception as exc:
            from rka.services.embedding_index import EmbeddingGenerationMismatch

            if (
                not isinstance(exc, EmbeddingGenerationMismatch)
                or job["job_type"] not in _EMBEDDING_JOB_TYPES
                or self._embedding_data_dir is None
            ):
                raise
            logger.info(
                "Worker job %s lost embedding generation; reloading config and retrying",
                job["id"],
            )
            self._embedding_config_fingerprint = None
            self._embedding_generation = None
            return await self._process_job(job)

    async def run_forever(self, stop_event: asyncio.Event | None = None) -> None:
        """Process jobs until cancelled or stop_event is set."""
        while True:
            if stop_event is not None and stop_event.is_set():
                return
            handled = await self.run_once()
            if handled:
                continue
            await asyncio.sleep(self.poll_interval)

    async def _process_job(self, job: dict[str, Any]) -> dict[str, Any]:
        job_type = job["job_type"]
        project_id = job["project_id"]
        entity_id = job.get("entity_id")

        if job_type in _EMBEDDING_JOB_TYPES or job_type == "embedding_backfill":
            await self._refresh_embeddings_before_job()

        if job_type == "embedding_backfill":
            from rka.services.embedding_jobs import EmbeddingJobs
            return await EmbeddingJobs(self.db).run(job, self.embeddings, self.queue)

        if job_type in _EMBEDDING_JOB_TYPES:
            from rka.services.job_execution import execution
            table = {
                "mission_embed": "missions", "note_embed": "journal", "claim_embed": "claims",
                "decision_embed": "decisions", "literature_embed": "literature",
            }[job_type]
            current = execution.get()
            if current is not None:
                row = await self.db.fetchone(
                    f"SELECT * FROM {table} WHERE id=? AND project_id=?", [entity_id, project_id],
                )
                current["source"] = (table, entity_id, project_id, row)

        # ── Embedding-only jobs ──────────────────────────────────

        if job_type == "mission_embed":
            from rka.services.missions import MissionService
            svc = MissionService(self.db, embeddings=self.embeddings, project_id=project_id)
            return await svc.process_embedding_job(entity_id)

        if job_type == "note_embed":
            from rka.services.notes import NoteService
            svc = NoteService(self.db, embeddings=self.embeddings, project_id=project_id)
            return await svc.process_embedding_job(entity_id)

        if job_type == "claim_embed":
            from rka.services.claims import ClaimService
            svc = ClaimService(self.db, embeddings=self.embeddings, project_id=project_id)
            return await svc.process_embedding_job(entity_id)

        if job_type == "decision_embed":
            from rka.services.decisions import DecisionService
            svc = DecisionService(self.db, embeddings=self.embeddings, project_id=project_id)
            return await svc.process_embedding_job(entity_id)

        if job_type == "literature_embed":
            from rka.services.literature import LiteratureService
            svc = LiteratureService(self.db, embeddings=self.embeddings, project_id=project_id)
            return await svc.process_embedding_job(entity_id)

        # ── Pre-split Writer jobs ──────────────────────────────────

        if job_type == "reference_validate":
            logger.info(
                "Skipping pre-split Writer reference-validation job %s",
                job["id"],
            )
            return {"outcome": "skipped", "reason": "writer_runtime_moved"}

        # ── Legacy LLM jobs — skip gracefully ────────────────────
        # These job types may still exist in the queue from before the migration.
        # Complete them as no-ops instead of failing.

        _LEGACY_LLM_JOBS = {
            "note_auto_tag", "note_auto_link", "note_auto_summarize", "note_extract_claims",
            "claim_verify", "cluster_update", "theme_synthesize", "contradiction_check",
            "decision_auto_tag", "literature_auto_tag", "mission_auto_tag", "re_distill",
        }
        if job_type in _LEGACY_LLM_JOBS:
            logger.info("Skipping legacy LLM job %s (%s) — no longer processed by worker", job_type, job["id"])
            return {"outcome": "skipped", "reason": "legacy_llm_job"}

        raise ValueError(f"Unsupported job_type '{job_type}'")
