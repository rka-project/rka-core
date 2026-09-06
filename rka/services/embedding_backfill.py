"""Embedding-backfill orchestration.

v2.4 (Mission D T2-gate): synchronous foreground job ("synchronous" =
no Redis/Celery; runs in the API container's process). The PUT
/api/config/embedding handler returns 202 + {job_id, status_url}
immediately and the actual loop runs via FastAPI BackgroundTask.

v2.5.5 (mis_01KS1RFNM2T1HTB077G507T1FR Bug 3): the loop is generalized
over all entity types backed by a vec_* table — claim, journal,
decision, literature, mission, artifact, and figure. The v2.4 implementation hit
only `claims WHERE embedding_pending=1`, leaving the other five vec_*
tables empty after a config change because nothing else ever ran a
backfill cursor against them.

Pending-signal per entity type:
  - `claim` uses the v2.4 `claims.embedding_pending` flag (the cursor
    filters on it; we clear it post-embed).
  - the other six use `embedding_metadata` absence — `reshape_vec_table`
    (T1) DELETEs metadata rows on dim change, and the cursor here picks
    up entities without a metadata row.

Status snapshot shape (unchanged from v2.4 — T3 status endpoint returns
it verbatim):

    {
      "job_id":          str,
      "state":           Literal["pending", "running", "complete", "failed"],
      "processed":       int,
      "total":           int,
      "started_at":      ISO-8601 UTC,
      "elapsed_seconds": float,
      "error":           str | None,
    }
"""

from __future__ import annotations

import logging
import struct
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Literal, Mapping, Sequence

from rka.infra.embedding_resources import EmbeddingInputLimit, EmbeddingResourceLimits

logger = logging.getLogger(__name__)


JobState = Literal["pending", "running", "complete", "failed"]


def _now_iso_z() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class JobStatus:
    """Per-run snapshot read by the GET status endpoint.

    `started_at` is set when the job is registered; `elapsed_seconds` is
    computed from a monotonic clock so it remains correct across
    daylight-savings + system-clock adjustments.
    """

    job_id: str
    state: JobState
    processed: int = 0
    total: int = 0
    started_at: str = ""
    elapsed_seconds: float = 0.0
    error: str | None = None
    # monotonic timestamp at registration; used to compute elapsed_seconds
    _started_perf: float = 0.0

    def snapshot(self) -> dict[str, Any]:
        out = asdict(self)
        out.pop("_started_perf", None)
        if self._started_perf > 0:
            out["elapsed_seconds"] = round(time.monotonic() - self._started_perf, 3)
        return out


# ---------------------------------------------------------------------------
# Job registry (module-level; one backfill at a time)
# ---------------------------------------------------------------------------


_REGISTRY: dict[str, JobStatus] = {}


def register_job(prefix: str = "bf") -> JobStatus:
    """Register a job and return its live status handle.

    `prefix` names the kind of work in the job id. It defaults to `bf` so
    existing backfill callers are unchanged; knowledge-pack import passes
    `imp`. The registry is shared because the status shape and the polling
    contract are the same — only the work differs.
    """
    job_id = f"{prefix}_{uuid.uuid4().hex[:12]}"
    status = JobStatus(
        job_id=job_id,
        state="pending",
        started_at=_now_iso_z(),
        _started_perf=time.monotonic(),
    )
    _REGISTRY[job_id] = status
    logger.info("registered %s job %s", prefix, job_id)
    return status


def get_status(job_id: str) -> JobStatus | None:
    return _REGISTRY.get(job_id)


def latest_status() -> JobStatus | None:
    if not _REGISTRY:
        return None
    return next(reversed(_REGISTRY.values()))


def clear_registry() -> None:
    _REGISTRY.clear()


# ---------------------------------------------------------------------------
# Per-entity-type backfill configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _EntityBackfillConfig:
    """Parameterises the backfill cursor + write path for one entity type.

    Placeholder order is `(model_name, dimensions, last_id, LIMIT)` for
    `pending_cursor_sql` and `(model_name, dimensions)` for
    `pending_count_sql`. The cursor must `SELECT id, project_id,
    ...content_columns...` so the loop can compose text + write metadata
    under the right project.

    The anti-join matches the *current* model and dim, not merely the
    presence of a metadata row. Presence alone meant that swapping the
    model at an unchanged dimension re-embedded nothing: `reshape` no-ops
    when the dim is equal, so every metadata row survived and the cursor
    found no pending work — leaving vectors from the retired model in the
    index while queries were encoded by the new one, which is silent and
    makes the similarity scores meaningless. `EmbeddingService.needs_reembed`
    already gates on the full identity tuple; the cursor is what disagreed
    with it.
    """

    entity_type: str
    source_table: str
    vec_table: str
    pending_cursor_sql: str
    pending_count_sql: str
    compose_text: Callable[[Mapping[str, Any]], str]
    post_embed_sql: str | None = None
    id_column: str = "id"


def _join_parts(*parts: Any) -> str:
    """Join non-empty stripped string parts with single spaces."""
    return " ".join(
        str(p).strip() for p in parts if p and str(p).strip()
    ).strip()


def _build_artifact_text_from_row(r: Mapping[str, Any]) -> str:
    # Imported lazily to avoid an import cycle with rka.services.artifacts.
    from rka.services.artifacts import build_artifact_text

    return build_artifact_text(
        filename=r.get("filename") or "",
        filetype=r.get("filetype"),
        mime=r.get("mime"),
        metadata=r.get("metadata"),
    )


def _build_figure_text_from_row(r: Mapping[str, Any]) -> str:
    # Imported lazily to avoid an import cycle with rka.services.artifacts.
    from rka.services.artifacts import build_figure_text

    return build_figure_text(
        caption=r.get("caption"),
        summary=r.get("summary"),
        claims=r.get("claims"),
    )


# Cursor templates: each non-claims template uses an anti-join against
# embedding_metadata so we only pull entities that lack a matching row.
# Combined with T1's DELETE-on-reshape and T3's 3-tuple needs_reembed
# gate, this is the pending signal for every non-claim type.


_ENTITY_BACKFILL_CONFIGS: dict[str, _EntityBackfillConfig] = {
    "claim": _EntityBackfillConfig(
        entity_type="claim",
        source_table="claims",
        vec_table="vec_claims",
        compose_text=lambda r: (r.get("content") or "").strip(),
        # Pending is decided by the absence of an embedding_metadata row, the
        # same test every other entity type uses -- NOT by the
        # `claims.embedding_pending` flag. That flag drifts: in a real store
        # all 976 claims carried embedding_pending = 0 while 341 of them had
        # no vector, so a flag-gated backfill skipped every claim that needed
        # one. The flag is still cleared after a successful embed so external
        # readers of it stay consistent.
        pending_cursor_sql=(
            "SELECT c.id, c.content, c.project_id FROM claims c "
            "WHERE NOT EXISTS ("
            "  SELECT 1 FROM embedding_metadata m "
            "  WHERE m.entity_type = 'claim' AND m.entity_id = c.id"
            "    AND m.project_id = c.project_id"
            "    AND m.model_name = ? AND m.dimensions = ?"
            ") AND c.id > ? "
            "ORDER BY c.id LIMIT ?"
        ),
        pending_count_sql=(
            "SELECT COUNT(*) AS n FROM claims c "
            "WHERE NOT EXISTS ("
            "  SELECT 1 FROM embedding_metadata m "
            "  WHERE m.entity_type = 'claim' AND m.entity_id = c.id"
            "    AND m.project_id = c.project_id"
            "    AND m.model_name = ? AND m.dimensions = ?"
            ")"
        ),
        post_embed_sql="UPDATE claims SET embedding_pending = 0 WHERE id = ?",
    ),
    "journal": _EntityBackfillConfig(
        entity_type="journal",
        source_table="journal",
        vec_table="vec_journal",
        compose_text=lambda r: _join_parts(r.get("content"), r.get("summary")),
        pending_cursor_sql=(
            "SELECT j.id, j.content, j.summary, j.project_id FROM journal j "
            "WHERE NOT EXISTS ("
            "  SELECT 1 FROM embedding_metadata m "
            "  WHERE m.entity_type = 'journal' AND m.entity_id = j.id"
            "    AND m.project_id = j.project_id"
            "    AND m.model_name = ? AND m.dimensions = ?"
            ") AND j.id > ? "
            "ORDER BY j.id LIMIT ?"
        ),
        pending_count_sql=(
            "SELECT COUNT(*) AS n FROM journal j "
            "WHERE NOT EXISTS ("
            "  SELECT 1 FROM embedding_metadata m "
            "  WHERE m.entity_type = 'journal' AND m.entity_id = j.id"
            "    AND m.project_id = j.project_id"
            "    AND m.model_name = ? AND m.dimensions = ?"
            ")"
        ),
    ),
    "decision": _EntityBackfillConfig(
        entity_type="decision",
        source_table="decisions",
        vec_table="vec_decisions",
        compose_text=lambda r: _join_parts(r.get("question"), r.get("rationale")),
        pending_cursor_sql=(
            "SELECT d.id, d.question, d.rationale, d.project_id FROM decisions d "
            "WHERE NOT EXISTS ("
            "  SELECT 1 FROM embedding_metadata m "
            "  WHERE m.entity_type = 'decision' AND m.entity_id = d.id"
            "    AND m.project_id = d.project_id"
            "    AND m.model_name = ? AND m.dimensions = ?"
            ") AND d.id > ? "
            "ORDER BY d.id LIMIT ?"
        ),
        pending_count_sql=(
            "SELECT COUNT(*) AS n FROM decisions d "
            "WHERE NOT EXISTS ("
            "  SELECT 1 FROM embedding_metadata m "
            "  WHERE m.entity_type = 'decision' AND m.entity_id = d.id"
            "    AND m.project_id = d.project_id"
            "    AND m.model_name = ? AND m.dimensions = ?"
            ")"
        ),
    ),
    "literature": _EntityBackfillConfig(
        entity_type="literature",
        source_table="literature",
        vec_table="vec_literature",
        compose_text=lambda r: _join_parts(r.get("title"), r.get("abstract")),
        pending_cursor_sql=(
            "SELECT l.id, l.title, l.abstract, l.project_id FROM literature l "
            "WHERE NOT EXISTS ("
            "  SELECT 1 FROM embedding_metadata m "
            "  WHERE m.entity_type = 'literature' AND m.entity_id = l.id"
            "    AND m.project_id = l.project_id"
            "    AND m.model_name = ? AND m.dimensions = ?"
            ") AND l.id > ? "
            "ORDER BY l.id LIMIT ?"
        ),
        pending_count_sql=(
            "SELECT COUNT(*) AS n FROM literature l "
            "WHERE NOT EXISTS ("
            "  SELECT 1 FROM embedding_metadata m "
            "  WHERE m.entity_type = 'literature' AND m.entity_id = l.id"
            "    AND m.project_id = l.project_id"
            "    AND m.model_name = ? AND m.dimensions = ?"
            ")"
        ),
    ),
    "mission": _EntityBackfillConfig(
        entity_type="mission",
        source_table="missions",
        vec_table="vec_missions",
        compose_text=lambda r: _join_parts(r.get("objective"), r.get("context")),
        pending_cursor_sql=(
            "SELECT mi.id, mi.objective, mi.context, mi.project_id "
            "FROM missions mi "
            "WHERE NOT EXISTS ("
            "  SELECT 1 FROM embedding_metadata m "
            "  WHERE m.entity_type = 'mission' AND m.entity_id = mi.id"
            "    AND m.project_id = mi.project_id"
            "    AND m.model_name = ? AND m.dimensions = ?"
            ") AND mi.id > ? "
            "ORDER BY mi.id LIMIT ?"
        ),
        pending_count_sql=(
            "SELECT COUNT(*) AS n FROM missions mi "
            "WHERE NOT EXISTS ("
            "  SELECT 1 FROM embedding_metadata m "
            "  WHERE m.entity_type = 'mission' AND m.entity_id = mi.id"
            "    AND m.project_id = mi.project_id"
            "    AND m.model_name = ? AND m.dimensions = ?"
            ")"
        ),
    ),
    "artifact": _EntityBackfillConfig(
        entity_type="artifact",
        source_table="artifacts",
        vec_table="vec_artifacts",
        compose_text=_build_artifact_text_from_row,
        pending_cursor_sql=(
            "SELECT a.id, a.filename, a.filetype, a.mime, a.metadata, a.project_id "
            "FROM artifacts a "
            "WHERE NOT EXISTS ("
            "  SELECT 1 FROM embedding_metadata m "
            "  WHERE m.entity_type = 'artifact' AND m.entity_id = a.id"
            "    AND m.project_id = a.project_id"
            "    AND m.model_name = ? AND m.dimensions = ?"
            ") AND a.id > ? "
            "ORDER BY a.id LIMIT ?"
        ),
        pending_count_sql=(
            "SELECT COUNT(*) AS n FROM artifacts a "
            "WHERE NOT EXISTS ("
            "  SELECT 1 FROM embedding_metadata m "
            "  WHERE m.entity_type = 'artifact' AND m.entity_id = a.id"
            "    AND m.project_id = a.project_id"
            "    AND m.model_name = ? AND m.dimensions = ?"
            ")"
        ),
    ),
    "figure": _EntityBackfillConfig(
        entity_type="figure",
        source_table="figures",
        vec_table="vec_artifacts",
        compose_text=_build_figure_text_from_row,
        pending_cursor_sql=(
            "SELECT f.id, f.caption, f.summary, f.claims, f.project_id "
            "FROM figures f "
            "WHERE NOT EXISTS ("
            "  SELECT 1 FROM embedding_metadata m "
            "  WHERE m.entity_type = 'figure' AND m.entity_id = f.id"
            "    AND m.project_id = f.project_id"
            "    AND m.model_name = ? AND m.dimensions = ?"
            ") AND f.id > ? "
            "ORDER BY f.id LIMIT ?"
        ),
        pending_count_sql=(
            "SELECT COUNT(*) AS n FROM figures f "
            "WHERE NOT EXISTS ("
            "  SELECT 1 FROM embedding_metadata m "
            "  WHERE m.entity_type = 'figure' AND m.entity_id = f.id"
            "    AND m.project_id = f.project_id"
            "    AND m.model_name = ? AND m.dimensions = ?"
            ")"
        ),
    ),
}


_DEFAULT_ENTITY_TYPES: tuple[str, ...] = tuple(_ENTITY_BACKFILL_CONFIGS.keys())


async def stored_metadata_hashes_match(
    db: Any,
    *,
    model_name: str,
    dimensions: int,
) -> bool:
    """Verify legacy metadata hashes against the current canonical text.

    This keyset-paged scan is used only while adopting a pre-generation
    index. It avoids either trusting stale vectors or needlessly discarding a
    healthy existing installation.
    """

    from rka.infra.embeddings import EmbeddingService

    for entity_type, cfg in _ENTITY_BACKFILL_CONFIGS.items():
        last_id = ""
        while True:
            rows = await db.fetchall(
                f"""SELECT s.*, m.content_hash AS _embedding_content_hash
                    FROM {cfg.source_table} s
                    JOIN embedding_metadata m
                      ON m.project_id = s.project_id
                     AND m.entity_type = ?
                     AND m.entity_id = s.{cfg.id_column}
                    WHERE m.model_name = ? AND m.dimensions = ?
                      AND s.{cfg.id_column} > ?
                    ORDER BY s.{cfg.id_column} LIMIT ?""",
                [entity_type, model_name, dimensions, last_id, 8],
            )
            if not rows:
                break
            for row in rows:
                try:
                    text = cfg.compose_text(row)
                except Exception:  # noqa: BLE001
                    return False
                if row["_embedding_content_hash"] != EmbeddingService.content_hash(text):
                    return False
            last_id = rows[-1][cfg.id_column]
    return True


# ---------------------------------------------------------------------------
# BackfillService
# ---------------------------------------------------------------------------


ProgressCallback = Callable[[JobStatus], Awaitable[None] | None]


class _BackfillBatchError(Exception):
    """Per-type embed_batch failure; carries the formatted detail."""

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(detail)


@dataclass
class _RowErrors:
    count: int = 0
    samples: list[str] = field(default_factory=list)

    def record(self, entity_id: str, exc: Exception) -> None:
        self.count += 1
        if len(self.samples) < 3:
            # Resource errors contain only limit names/counts. Arbitrary backend
            # or compose exceptions may contain input text or credentials.
            detail = str(exc) if isinstance(exc, EmbeddingInputLimit) else type(exc).__name__
            sample = f"{entity_id}: {detail[:256]}"
            self.samples.append(sample)
            logger.warning("backfill row left pending: %s", sample)

    def summary(self) -> str:
        return f"{self.count} row(s) failed; " + "; ".join(self.samples)


class BackfillService:
    """Iterates one or more entity types and (re-)embeds them.

      - `run_backfill(status, progress_callback=None, entity_types=None)`
        is async. With `entity_types=None` it processes all seven known
        types in stable order (claim → journal → decision → literature
        → mission → artifact → figure).
      - Default `batch_size = 8` (v2.4.1: lowered from 32 so local
        8B-class embedding backends don't time out on a single batch).
      - Per-row write failures get logged + the row is left "pending"
        for a later run.
      - Per-type embed_batch failures are recorded as the type's error
        but the loop continues to the next type (v2.5.5: isolated
        failure). The final state is "failed" if any type errored,
        "complete" otherwise.
    """

    def __init__(self, *, db: Any, embeddings: Any, batch_size: int = 8) -> None:
        if isinstance(batch_size, bool) or not isinstance(batch_size, int) or not 1 <= batch_size <= 128:
            raise ValueError("batch_size must be an integer between 1 and 128")
        self._db = db
        self._embeddings = embeddings
        self._batch_size = batch_size
        limits = getattr(getattr(embeddings, "backend", None), "resource_limits", None)
        if isinstance(limits, EmbeddingResourceLimits):
            # Conservative fetch cap ensures even maximum-sized accepted inputs
            # fit the logical call when an operator tightens resource budgets.
            self._batch_size = min(
                batch_size, limits.max_batch_inputs, limits.max_call_inputs,
                limits.max_batch_bytes // limits.max_input_bytes,
                limits.max_padding_bytes // limits.max_input_bytes,
                limits.max_call_bytes // limits.max_input_bytes,
            )

    async def run_backfill(
        self,
        status: JobStatus,
        progress_callback: ProgressCallback | None = None,
        entity_types: Sequence[str] | None = None,
    ) -> JobStatus:
        """Embed every pending entity, batch by batch.

        Cursor pattern: rows are iterated in `id`-ascending order with an
        `id > last_id` filter so persistent per-row failures don't
        trigger an infinite re-fetch loop.

        Wall-clock target per the v2.4 mission spec: ~0.5–1s per entity
        with LM Studio qwen3-8b; the v2.5.5 surface includes the other
        five entity types so the upper-bound batch is somewhat larger.
        """
        status.state = "running"

        if not bool(getattr(self._db, "vec_available", False)):
            status.state = "failed"
            status.error = "sqlite-vec is unavailable; embedding backfill was not run"
            await _emit_progress(progress_callback, status)
            return status

        if entity_types is None:
            types = _DEFAULT_ENTITY_TYPES
        else:
            types = tuple(entity_types)
            for et in types:
                if et not in _ENTITY_BACKFILL_CONFIGS:
                    raise ValueError(
                        f"run_backfill: unknown entity_type {et!r}; "
                        f"expected one of {_DEFAULT_ENTITY_TYPES}"
                    )

        # Step 1: snapshot per-type pending counts; total is the sum.
        # Counted against the current model + dim, matching what the cursor
        # will actually select — a count taken on metadata presence alone
        # would report zero work for a model swap that has a full re-embed
        # ahead of it.
        count_model: str = getattr(self._embeddings, "model_name", "unknown")
        count_dim: int = int(getattr(self._embeddings, "dim", 0) or 0)
        per_type_totals: dict[str, int] = {}
        for et in types:
            cfg = _ENTITY_BACKFILL_CONFIGS[et]
            row = await self._db.fetchone(
                cfg.pending_count_sql, [count_model, count_dim]
            )
            per_type_totals[et] = int((row or {}).get("n") or 0)
        status.total = sum(per_type_totals.values())
        await _emit_progress(progress_callback, status)

        if status.total == 0:
            status.state = "complete"
            await _emit_progress(progress_callback, status)
            return status

        # Step 2: iterate entity types, isolating per-type batch failures.
        type_errors: list[str] = []
        for et in types:
            if per_type_totals[et] == 0:
                continue
            cfg = _ENTITY_BACKFILL_CONFIGS[et]
            try:
                await self._backfill_one_type(cfg, status, progress_callback)
            except _BackfillBatchError as exc:
                type_errors.append(f"{et}: {exc.detail}")
                logger.warning(
                    "backfill %s batch failed: %s", et, exc.detail
                )

        # Step 3: terminal state.
        if type_errors:
            status.state = "failed"
            if len(type_errors) == 1:
                # v2.4 single-type error format: "batch embed failed (...)".
                status.error = f"batch embed failed: {type_errors[0]}"
            else:
                status.error = (
                    "batch embed failed across multiple types: "
                    + "; ".join(type_errors)
                )
        else:
            status.state = "complete"

        await _emit_progress(progress_callback, status)
        return status

    async def _backfill_one_type(
        self,
        cfg: _EntityBackfillConfig,
        status: JobStatus,
        progress_callback: ProgressCallback | None,
    ) -> None:
        """Cursor-paginate through one entity type's pending rows."""
        # Lazily import to avoid a hot-path import on every method call.
        from rka.infra.embeddings import EmbeddingService as _ES

        vec_available = bool(getattr(self._db, "vec_available", False))
        model_name: str = getattr(self._embeddings, "model_name", "unknown")
        embed_dim: int = int(getattr(self._embeddings, "dim", 0) or 0)

        last_id = ""
        row_errors = _RowErrors()
        while True:
            rows = await self._db.fetchall(
                cfg.pending_cursor_sql,
                [model_name, embed_dim, last_id, self._batch_size],
            )
            if not rows:
                break

            # Compose text per row; drop rows whose composed text is empty.
            work: list[tuple[Mapping[str, Any], str]] = []
            for r in rows:
                try:
                    text = cfg.compose_text(r)
                except Exception as exc:  # noqa: BLE001
                    row_errors.record(r.get(cfg.id_column), exc)
                    continue
                if text:
                    validate = getattr(self._embeddings, "validate_document", None)
                    if validate is not None:
                        try:
                            validate(text)
                        except EmbeddingInputLimit as exc:
                            row_errors.record(r.get(cfg.id_column), exc)
                            continue
                    work.append((r, text))

            last_id = rows[-1][cfg.id_column]

            if not work:
                # Whole batch was empty-text; advance cursor and continue.
                continue

            # Batch-embed; a failure aborts THIS type only (the outer
            # loop catches and continues to the next type).
            try:
                vectors = await self._embeddings.embed_batch(
                    [t for _, t in work], is_query=False
                )
            except Exception as exc:  # noqa: BLE001
                raise _BackfillBatchError(
                    f"(cursor at {last_id}): "
                    f"{type(exc).__name__}: {exc!s}"
                ) from exc
            if len(vectors) != len(work):
                raise _BackfillBatchError(
                    f"(cursor at {last_id}): backend returned "
                    f"{len(vectors)} vectors for {len(work)} inputs"
                )

            # Per-row: write vec_* + embedding_metadata + post_embed.
            #
            # Invariant (per dec_01KS3E1FGSK530N8HM04BNMCEW, surfaced empirically
            # by corpus refresh mis_01KS0QEW21N2NG4EJTKJ3JTWTE):
            #   metadata write is COUPLED to vec_* write; both gate on
            #   vec_available to prevent silent under-embedding.
            #
            # Pre-fix behavior (v2.5.5 latent edge case): when vec_available=False,
            # the vec_* INSERT was skipped but the metadata INSERT ran unconditionally,
            # leaving the row claiming "embedded at <model>/<dim>" with no actual
            # vec_* row. The v2.5.5 3-tuple needs_reembed gate then returned False
            # permanently for those rows even after vec_available flipped True;
            # the entity was never re-embedded.
            #
            # Note: the pre-fix "always update metadata" rationale (preserving
            # model_name across config switches) is preserved by Bug 1 fix
            # because the vec_available=True path still updates metadata on
            # every backfill pass. The False path now leaves metadata in its
            # prior state, which means stale model_name is possible during
            # vec_available=False windows; that surfaces correctly as
            # needs_reembed=True once vec_available recovers (the entity's
            # stale row gets re-examined and re-embedded).
            for (row, text), vec in zip(work, vectors):
                entity_id = row[cfg.id_column]
                try:
                    project_id = row.get("project_id") or "proj_default"
                    if vec_available:
                        vec_blob = struct.pack(f"{len(vec)}f", *vec)
                        async with self._db.transaction():
                            generation_guard = getattr(
                                self._embeddings,
                                "assert_current_generation",
                                None,
                            )
                            if generation_guard is not None:
                                await generation_guard()
                            # sqlite-vec's vec0 tables do not honour the
                            # REPLACE conflict clause — re-embedding an entity
                            # that already has a vector raises "UNIQUE
                            # constraint failed on <table> primary key".
                            # Until now nothing re-embedded in place: the only
                            # path here was a dimension change, and reshape
                            # DROPs the table first, so no row ever pre-existed.
                            # A same-dim model swap does re-embed in place.
                            await self._db.execute(
                                f"DELETE FROM {cfg.vec_table} WHERE id = ?",
                                [entity_id],
                            )
                            if cfg.vec_table == "vec_artifacts":
                                await self._db.execute(
                                    f"INSERT INTO {cfg.vec_table} "
                                    "(id, project_id, entity_type, embedding) "
                                    "VALUES (?, ?, ?, ?)",
                                    [
                                        entity_id,
                                        project_id,
                                        cfg.entity_type,
                                        vec_blob,
                                    ],
                                )
                            else:
                                await self._db.execute(
                                    f"INSERT INTO {cfg.vec_table} "
                                    "(id, project_id, embedding) VALUES (?, ?, ?)",
                                    [entity_id, project_id, vec_blob],
                                )
                            await self._db.execute(
                                """INSERT OR REPLACE INTO embedding_metadata
                                   (project_id, entity_type, entity_id,
                                    content_hash, model_name, dimensions)
                                   VALUES (?, ?, ?, ?, ?, ?)""",
                                [
                                    project_id,
                                    cfg.entity_type,
                                    entity_id,
                                    _ES.content_hash(text),
                                    model_name,
                                    embed_dim,
                                ],
                            )
                            # v2.7.0.7 — `post_embed_sql` clears the
                            # entity's pending flag (e.g. `UPDATE claims SET
                            # embedding_pending = 0`). It MUST only run when
                            # a vector was actually stored.
                            if cfg.post_embed_sql:
                                await self._db.execute(
                                    cfg.post_embed_sql, [entity_id]
                                )
                        status.processed += 1
                    # else: no vector written (sqlite-vec unavailable). Leave the
                    # entity's pending signal intact and do NOT count it as
                    # processed so backfill retries once vec is available again.
                except Exception as exc:  # noqa: BLE001
                    # Per-row failure: leave any pending signal intact,
                    # log, continue.
                    row_errors.record(entity_id, exc)

            await _emit_progress(progress_callback, status)

        if row_errors.count:
            raise _BackfillBatchError(f"vector writes failed: {row_errors.summary()}")


async def _emit_progress(
    cb: ProgressCallback | None, status: JobStatus
) -> None:
    if cb is None:
        return
    await _maybe_await(cb(status))


async def _maybe_await(value: Any) -> None:
    if hasattr(value, "__await__"):
        await value
