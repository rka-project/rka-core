"""Durable lifecycle guard for RKA's rebuildable embedding index.

The canonical research records never depend on this state.  The singleton
coordinates API and worker processes while a document embedding space is
being replaced, preventing an old process from writing vectors after a newer
configuration has become active.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from rka.services.embedding_reshape import (
    current_vec_table_dim,
    reshape_all_vec_tables_if_needed,
)


_VEC_TABLE_NAMES = (
    "vec_claims",
    "vec_journal",
    "vec_decisions",
    "vec_literature",
    "vec_missions",
    "vec_artifacts",
)


class EmbeddingGenerationMismatch(RuntimeError):
    """Raised when a stale process attempts to write an embedding."""


class EmbeddingDimensionTransitionRequired(RuntimeError):
    """Raised when a vec0 dimension change needs supervised maintenance."""


@dataclass(frozen=True)
class EmbeddingIndexState:
    generation: int
    space_signature: str
    model_name: str
    dimensions: int
    status: str
    last_error: str | None = None


@dataclass(frozen=True)
class EmbeddingIndexReconciliation:
    state: EmbeddingIndexState
    transitioned: bool
    resumed: bool
    reshape: dict[str, tuple[bool, int]]


def _config_payload(config: Any) -> tuple[str, Mapping[str, Any]]:
    if hasattr(config, "model_dump"):
        payload = config.model_dump()
    else:
        payload = dict(config)
    return str(payload.get("backend") or ""), payload.get("config") or {}


def embedding_space_model_name(config: Any) -> str:
    """Return the identity stored in legacy ``model_name`` metadata.

    App-managed runtimes provide a complete ``embedding_space_id``.  Existing
    configurations retain their model identifier for backward compatibility.
    """

    _backend, sub = _config_payload(config)
    model = str(sub.get("model") or sub.get("model_name") or "").strip()
    explicit = str(sub.get("embedding_space_id") or "").strip()
    return explicit or model


def embedding_space_signature(config: Any, *, dimensions: int | None = None) -> str:
    """Return a stable fingerprint of the stored *document* vector space.

    Query-only formatting is intentionally excluded because it does not alter
    stored vectors.  App profiles should place tokenizer, artifact hash,
    pooling, normalization, truncation, and runtime revision in the explicit
    ``embedding_space_id``.
    """

    backend, sub = _config_payload(config)
    dim = int(dimensions or sub.get("dim") or 0)
    payload = {
        "version": 1,
        "backend": backend,
        "space": embedding_space_model_name(config),
        "dimensions": dim,
        "document_template": str(sub.get("document_template") or "{text}"),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def legacy_index_adoption_safe(config: Any) -> bool:
    """Return whether pre-generation metadata fully describes this space.

    Legacy metadata recorded only model name and dimension. It cannot prove a
    custom document template or an App-managed artifact/runtime identity, so
    those configurations require one clean transition when migration 055 is
    first encountered.
    """

    _backend, sub = _config_payload(config)
    return bool(
        not str(sub.get("embedding_space_id") or "").strip()
        and str(sub.get("document_template") or "{text}") == "{text}"
    )


def _state_from_row(row: Mapping[str, Any]) -> EmbeddingIndexState:
    return EmbeddingIndexState(
        generation=int(row["generation"]),
        space_signature=str(row["space_signature"]),
        model_name=str(row["model_name"]),
        dimensions=int(row["dimensions"]),
        status=str(row["status"]),
        last_error=row.get("last_error"),
    )


async def get_embedding_index_state(db: Any) -> EmbeddingIndexState | None:
    row = await db.fetchone(
        """SELECT generation, space_signature, model_name, dimensions,
                  status, last_error
           FROM embedding_index_state WHERE singleton = 1"""
    )
    return _state_from_row(row) if row else None


async def _stored_index_adoption_status(
    db: Any,
    *,
    model_name: str,
    dim: int,
) -> str | None:
    if not getattr(db, "vec_available", False):
        return None
    for table_name in _VEC_TABLE_NAMES:
        if await current_vec_table_dim(db, table_name) != dim:
            return None
    state = EmbeddingIndexState(
        generation=1,
        space_signature="legacy-adoption-check",
        model_name=model_name,
        dimensions=dim,
        status="ready",
    )
    if not await _active_index_rows_are_coherent(db, state):
        return None
    # Only reached for eligible legacy identity + coherent rows. Keep each
    # hash-verified pair; invalidate only unprovable derived pairs under the
    # caller's migration/write transaction. State adoption and these deletions
    # roll back together. Source content is never modified.
    from rka.infra.embedding_documents import DOCUMENT_SPECS
    from rka.services.embedding_verification import iter_stored_document_checks

    async for check in iter_stored_document_checks(db, model_name=model_name, dimensions=dim):
        if check.status == "verified":
            continue
        spec = DOCUMENT_SPECS[check.entity_type]
        vector_filter = " AND entity_type = ?" if spec.vec_table == "vec_artifacts" else ""
        params = [check.entity_id, check.project_id]
        if vector_filter:
            params.append(check.entity_type)
        await db.execute(
            f"DELETE FROM {spec.vec_table} WHERE id = ? AND project_id = ?{vector_filter}",
            params,
        )
        await db.execute(
            """DELETE FROM embedding_metadata
               WHERE entity_id = ? AND project_id = ? AND entity_type = ?""",
            [check.entity_id, check.project_id, check.entity_type],
        )
        if check.entity_type == "claim":
            await db.execute(
                "UPDATE claims SET embedding_pending = 1 WHERE id = ? AND project_id = ?",
                [check.entity_id, check.project_id],
            )
    return "ready" if await _active_index_has_full_coverage(db, state) else "reindexing"


async def assert_online_dimension_compatible(db: Any, *, dim: int) -> None:
    """Reject online vec0 schema changes while peer processes may be loaded.

    Same-dimension space changes are safe because they clear rows without
    replacing the virtual tables.  A dimension change requires the future App
    supervisor (or an offline maintenance command) to stop API and worker
    peers before rebuilding sqlite-vec schemas.
    """

    if not getattr(db, "vec_available", False):
        return
    observed = {
        table_name: await current_vec_table_dim(db, table_name)
        for table_name in _VEC_TABLE_NAMES
    }
    mismatched = {
        table_name: table_dim
        for table_name, table_dim in observed.items()
        if table_dim is not None and table_dim != dim
    }
    if mismatched:
        has_metadata = await db.fetchone(
            "SELECT 1 AS present FROM embedding_metadata LIMIT 1"
        )
        has_vectors = False
        for table_name in _VEC_TABLE_NAMES:
            row = await db.fetchone(f"SELECT 1 AS present FROM {table_name} LIMIT 1")
            if row:
                has_vectors = True
                break
        if not has_metadata and not has_vectors:
            # First-time setup has no index to protect.  Reshaping the empty
            # schema is safe and avoids requiring maintenance before a user
            # has stored any research records.
            return
        details = ", ".join(
            f"{table}={table_dim}" for table, table_dim in sorted(mismatched.items())
        )
        raise EmbeddingDimensionTransitionRequired(
            "embedding dimension change requires controlled offline reindex "
            f"(target={dim}; current {details})"
        )


async def reconcile_embedding_index(
    db: Any,
    *,
    space_signature: str,
    model_name: str,
    dim: int,
    allow_legacy_adoption: bool = True,
) -> EmbeddingIndexReconciliation:
    """Make ``space_signature`` the one active, restart-safe generation.

    A matching interrupted/failed generation is resumed without clearing
    already rebuilt rows.  A genuinely different space increments the
    generation and clears all derived vectors plus metadata atomically.
    """

    if dim <= 0:
        raise ValueError(f"embedding index dimension must be positive (got {dim})")
    if not space_signature or not model_name:
        raise ValueError("embedding index identity must not be empty")

    reshape: dict[str, tuple[bool, int]] = {}
    transitioned = False
    resumed = False

    async with db.transaction(migration_lock=True):
        # The preflight check used by the API is only advisory. Recheck after
        # BEGIN IMMEDIATE so another process cannot populate an old-dimension
        # table between validation and DROP/CREATE.
        await assert_online_dimension_compatible(db, dim=dim)
        current = await get_embedding_index_state(db)
        matches = bool(
            current
            and current.space_signature == space_signature
            and current.model_name == model_name
            and current.dimensions == dim
        )

        if matches:
            # The durable generation can be created while sqlite-vec is
            # unavailable.  When the extension later returns, the physical
            # tables may still have their bootstrap dimension even though the
            # singleton already names the target space.  Repair empty schemas
            # before returning from the matching-generation fast path.
            if getattr(db, "vec_available", False):
                reshape = await reshape_all_vec_tables_if_needed(
                    db,
                    dim=dim,
                    force=False,
                )
            schema_repaired = any(did for did, _pending in reshape.values())
            if current.status in {"reindexing", "failed"} or schema_repaired:
                resumed = True
                await db.execute(
                    """UPDATE embedding_index_state
                       SET status = 'reindexing', last_error = NULL,
                           updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                       WHERE singleton = 1"""
                )
                current = EmbeddingIndexState(
                    generation=current.generation,
                    space_signature=current.space_signature,
                    model_name=current.model_name,
                    dimensions=current.dimensions,
                    status="reindexing",
                )
            return EmbeddingIndexReconciliation(
                state=current,
                transitioned=False,
                resumed=resumed,
                reshape=reshape,
            )

        legacy_status = None
        if current is None and allow_legacy_adoption:
            legacy_status = await _stored_index_adoption_status(
                db,
                model_name=model_name,
                dim=dim,
            )
        if legacy_status is not None:
            await db.execute(
                """INSERT INTO embedding_index_state
                   (singleton, generation, space_signature, model_name,
                    dimensions, status)
                   VALUES (1, 1, ?, ?, ?, ?)""",
                [space_signature, model_name, dim, legacy_status],
            )
            state = EmbeddingIndexState(
                generation=1,
                space_signature=space_signature,
                model_name=model_name,
                dimensions=dim,
                status=legacy_status,
            )
            return EmbeddingIndexReconciliation(
                state=state,
                transitioned=False,
                resumed=legacy_status == "reindexing",
                reshape=reshape,
            )

        generation = (current.generation + 1) if current else 1
        await db.execute(
            """INSERT INTO embedding_index_state
               (singleton, generation, space_signature, model_name,
                dimensions, status, last_error, updated_at)
               VALUES (1, ?, ?, ?, ?, 'reindexing', NULL,
                       strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
               ON CONFLICT(singleton) DO UPDATE SET
                   generation = excluded.generation,
                   space_signature = excluded.space_signature,
                   model_name = excluded.model_name,
                   dimensions = excluded.dimensions,
                   status = 'reindexing',
                   last_error = NULL,
                   updated_at = excluded.updated_at""",
            [generation, space_signature, model_name, dim],
        )
        reshape = await reshape_all_vec_tables_if_needed(db, dim=dim, force=True)
        # This is a global new-space transition, not selective legacy reuse.
        # Per-table reshape removes known types; unknown legacy metadata must
        # not survive as purported members of the new generation either.
        await db.execute("DELETE FROM embedding_metadata")
        transitioned = True
        state = EmbeddingIndexState(
            generation=generation,
            space_signature=space_signature,
            model_name=model_name,
            dimensions=dim,
            status="reindexing",
        )

    return EmbeddingIndexReconciliation(
        state=state,
        transitioned=transitioned,
        resumed=False,
        reshape=reshape,
    )


async def resume_embedding_transition(
    db: Any,
    *,
    generation: int | None,
    space_signature: str,
    model_name: str,
    dim: int,
) -> EmbeddingIndexState:
    """Resume only the generation already bound to this process.

    Unlike full reconciliation, this operation can never move the durable
    index to the caller's identity. A stale API process therefore receives a
    generation mismatch instead of clearing vectors created by a newer one.
    """

    async with db.transaction():
        current = await get_embedding_index_state(db)
        if (
            current is None
            or generation is None
            or current.generation != generation
            or current.space_signature != space_signature
            or current.model_name != model_name
            or current.dimensions != dim
        ):
            raise EmbeddingGenerationMismatch(
                "embedding generation changed; reload the configured backend and retry"
            )
        if current.status == "failed":
            await db.execute(
                """UPDATE embedding_index_state
                   SET status = 'reindexing', last_error = NULL,
                       updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                   WHERE singleton = 1 AND generation = ? AND status = 'failed'""",
                [generation],
            )
            current = EmbeddingIndexState(
                generation=current.generation,
                space_signature=current.space_signature,
                model_name=current.model_name,
                dimensions=current.dimensions,
                status="reindexing",
            )
        return current


async def assert_embedding_generation(
    db: Any,
    *,
    generation: int | None,
    space_signature: str,
    dim: int,
) -> None:
    """Fail closed when a process is not bound to the active generation."""

    current = await get_embedding_index_state(db)
    if current is None:
        # Backward-compatible for tests and databases not yet migrated through
        # normal startup.  Once the singleton exists, every writer is guarded.
        return
    if (
        generation is None
        or current.generation != generation
        or current.space_signature != space_signature
        or current.dimensions != dim
        or current.status not in {"ready", "reindexing"}
    ):
        raise EmbeddingGenerationMismatch(
            "embedding generation changed; reload the configured backend and retry"
        )


async def embedding_index_search_ready(
    db: Any,
    *,
    generation: int | None,
    space_signature: str,
    dim: int,
) -> bool:
    current = await get_embedding_index_state(db)
    if current is None:
        return True
    return bool(
        generation is not None
        and current.generation == generation
        and current.space_signature == space_signature
        and current.dimensions == dim
        and current.status == "ready"
    )


async def finish_embedding_transition(
    db: Any,
    *,
    generation: int,
    success: bool,
    error: str | None = None,
) -> bool:
    """Finish only the still-current generation; stale jobs are ignored."""

    status = "ready" if success else "failed"
    async with db.transaction():
        current = await get_embedding_index_state(db)
        if (
            current is None
            or current.generation != generation
            or current.status != "reindexing"
        ):
            return False
        if success and not await _active_index_rows_are_consistent(db, current):
            success = False
            status = "failed"
            error = "embedding backfill left inconsistent vector metadata"
        await db.execute(
            """UPDATE embedding_index_state
               SET status = ?, last_error = ?,
                   updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
               WHERE singleton = 1 AND generation = ?""",
            [status, None if success else (error or "embedding backfill failed"), generation],
        )
    return True


async def _active_index_rows_are_consistent(
    db: Any,
    state: EmbeddingIndexState,
) -> bool:
    """Verify that every stored vector and metadata row agree on the space.

    Entities with genuinely empty text may have neither row and do not block a
    usable index.  The transition invariant is that no stored row is stale or
    orphaned, not that every canonical record must be embeddable.
    """

    return bool(
        await _active_index_rows_are_coherent(db, state)
        and await _active_index_has_full_coverage(db, state)
    )


async def _active_index_rows_are_coherent(
    db: Any,
    state: EmbeddingIndexState,
) -> bool:
    """Return whether every stored row belongs to one usable vector space."""

    from rka.infra.embedding_documents import DOCUMENT_SPECS

    mismatch = await db.fetchone(
        f"""SELECT 1 AS mismatch FROM embedding_metadata
            WHERE model_name <> ? OR dimensions <> ?
               OR entity_type NOT IN ({", ".join("?" for _ in DOCUMENT_SPECS)}) LIMIT 1""",
        [state.model_name, state.dimensions, *DOCUMENT_SPECS],
    )
    if mismatch:
        return False
    if await db.fetchone(
        """SELECT 1 AS mismatch FROM vec_artifacts
           WHERE entity_type NOT IN ('artifact', 'figure') OR entity_type IS NULL LIMIT 1"""
    ):
        return False

    table_entities = {
        "vec_claims": (("claim", "claims"),),
        "vec_journal": (("journal", "journal"),),
        "vec_decisions": (("decision", "decisions"),),
        "vec_literature": (("literature", "literature"),),
        "vec_missions": (("mission", "missions"),),
        "vec_artifacts": (("artifact", "artifacts"), ("figure", "figures")),
    }
    for table_name, entities in table_entities.items():
        for entity_type, source_table in entities:
            missing_source = await db.fetchone(
                f"""SELECT 1 AS mismatch FROM embedding_metadata m
                    WHERE m.entity_type = ?
                      AND NOT EXISTS (
                          SELECT 1 FROM {source_table} s
                          WHERE s.id = m.entity_id
                            AND s.project_id = m.project_id
                      )
                    LIMIT 1""",
                [entity_type],
            )
            if missing_source:
                return False

            vector_filter = (
                " AND v.entity_type = m.entity_type"
                if table_name == "vec_artifacts"
                else ""
            )
            missing_vector = await db.fetchone(
                f"""SELECT 1 AS mismatch FROM embedding_metadata m
                    WHERE m.entity_type = ?
                      AND NOT EXISTS (
                          SELECT 1 FROM {table_name} v
                          WHERE v.id = m.entity_id
                            AND v.project_id = m.project_id{vector_filter}
                      )
                    LIMIT 1""",
                [entity_type],
            )
            if missing_vector:
                return False

            metadata_filter = (
                " AND m.entity_type = v.entity_type"
                if table_name == "vec_artifacts"
                else ""
            )
            orphan_vector = await db.fetchone(
                f"""SELECT 1 AS mismatch FROM {table_name} v
                    WHERE {"v.entity_type = ? AND " if table_name == "vec_artifacts" else ""}
                      NOT EXISTS (
                          SELECT 1 FROM embedding_metadata m
                          WHERE m.entity_id = v.id
                            AND m.project_id = v.project_id{metadata_filter}
                            AND m.model_name = ? AND m.dimensions = ?
                      )
                    LIMIT 1""",
                (
                    [entity_type, state.model_name, state.dimensions]
                    if table_name == "vec_artifacts"
                    else [state.model_name, state.dimensions]
                ),
            )
            if orphan_vector:
                return False
    return True


async def _active_index_has_full_coverage(
    db: Any,
    state: EmbeddingIndexState,
    *,
    project_id: str | None = None,
    entity_types: tuple[str, ...] | None = None,
) -> bool:
    """Return whether every eligible canonical record has current metadata."""

    from rka.infra.embedding_documents import DOCUMENT_SPECS, compose_document

    for entity_type, spec in DOCUMENT_SPECS.items():
        if entity_types is not None and entity_type not in entity_types:
            continue
        last_id = None
        # Only fetch records without current metadata. Python's shared recipe
        # is the eligibility authority, including Unicode whitespace and JSON
        # figure claims; SQLite trim/raw JSON cannot express those semantics.
        while True:
            pending = await db.fetchall(
                f"""SELECT s.id, {", ".join("s." + field for field in spec.fields)}
                    FROM {spec.source_table} s
                    WHERE (? IS NULL OR s.id > ?)
                      AND (? IS NULL OR s.project_id = ?)
                      AND NOT EXISTS (
                          SELECT 1 FROM embedding_metadata m
                          WHERE m.project_id = s.project_id
                            AND m.entity_type = ? AND m.entity_id = s.id
                            AND m.model_name = ? AND m.dimensions = ?
                      )
                    ORDER BY s.id LIMIT ?""",
                [last_id, last_id, project_id, project_id, entity_type,
                 state.model_name, state.dimensions, 8],
            )
            if not pending:
                break
            for row in pending:
                if row["id"] is None:
                    # Corrupt/unaddressable sources cannot prove readiness;
                    # also avoid an endlessly repeated NULL cursor page.
                    return False
                try:
                    if compose_document(entity_type, row):
                        return False
                except Exception:  # noqa: BLE001
                    # Malformed source is unverified, not proof of completion.
                    return False
            last_id = pending[-1]["id"]
    return True
