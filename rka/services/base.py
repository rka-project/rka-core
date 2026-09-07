"""Base service with shared DB access, audit logging, event emission, and FTS5/embedding sync."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, ClassVar

from rka.constants import DEFAULT_PROJECT_ID
from rka.infra.database import Database
from rka.infra.embedding_documents import DOCUMENT_SPECS, compose_document
from rka.infra.ids import generate_id

if TYPE_CHECKING:
    from rka.infra.embeddings import EmbeddingService
    from rka.infra.llm import LLMClient

logger = logging.getLogger(__name__)
VALID_ACTORS = frozenset({"brain", "executor", "pi", "llm", "web_ui", "system"})


class EntityLinkValidationError(ValueError):
    """Raised when a typed provenance edge names an invalid project endpoint."""


def _now() -> str:
    """ISO 8601 UTC timestamp."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _precise_now() -> str:
    """Microsecond-precise ISO 8601 UTC timestamp."""
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


class BaseService:
    """Base class for all services. Provides DB access, audit logging, event emission,
    and Phase 2 FTS5/embedding sync hooks."""

    def __init__(
        self,
        db: Database,
        llm: "LLMClient | None" = None,
        embeddings: "EmbeddingService | None" = None,
        project_id: str = DEFAULT_PROJECT_ID,
    ):
        self.db = db
        self.llm = llm
        self.embeddings = embeddings
        self.project_id = project_id

    def _resolve_project_id(self, project_id: str | None = None) -> str:
        return (project_id or self.project_id).strip()

    @staticmethod
    def _validate_actor(actor: str | None, *, allow_none: bool = False) -> str | None:
        if actor is None and allow_none:
            return None
        if actor not in VALID_ACTORS:
            allowed = ", ".join(sorted(VALID_ACTORS))
            raise ValueError(f"Invalid actor '{actor}'. Expected one of: {allowed}")
        return actor

    async def emit_event(
        self,
        event_type: str,
        entity_type: str,
        entity_id: str,
        actor: str,
        summary: str,
        caused_by_event: str | None = None,
        caused_by_entity: str | None = None,
        phase: str | None = None,
        details: dict | None = None,
        project_id: str | None = None,
    ) -> str:
        """Record a cross-entity event for the exploration timeline."""
        actor = self._validate_actor(actor)
        event_id = generate_id("event")
        resolved_project_id = self._resolve_project_id(project_id)
        await self.db.execute(
            """INSERT INTO events
               (id, event_type, entity_type, entity_id, actor, summary,
                caused_by_event, caused_by_entity, phase, details, project_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                event_id, event_type, entity_type, entity_id,
                actor, summary, caused_by_event, caused_by_entity,
                phase, json.dumps(details) if details else None, resolved_project_id,
            ],
        )
        await self.db.commit()
        return event_id

    async def audit(
        self,
        action: str,
        entity_type: str,
        entity_id: str | None = None,
        actor: str = "system",
        details: dict | None = None,
        project_id: str | None = None,
    ) -> None:
        """Record an audit log entry."""
        actor = self._validate_actor(actor, allow_none=True)
        resolved_project_id = self._resolve_project_id(project_id)
        await self.db.execute(
            """INSERT INTO audit_log (action, entity_type, entity_id, actor, details, project_id)
               VALUES (?, ?, ?, ?, ?, ?)""",
            [action, entity_type, entity_id, actor, json.dumps(details) if details else None, resolved_project_id],
        )
        await self.db.commit()

    async def _get_tags(
        self,
        entity_type: str,
        entity_id: str,
        project_id: str | None = None,
    ) -> list[str]:
        """Get all tags for an entity."""
        resolved_project_id = self._resolve_project_id(project_id)
        rows = await self.db.fetchall(
            "SELECT tag FROM tags WHERE entity_type = ? AND entity_id = ? AND project_id = ?",
            [entity_type, entity_id, resolved_project_id],
        )
        return [row["tag"] for row in rows]

    async def _set_tags(
        self,
        entity_type: str,
        entity_id: str,
        tags: list[str],
        project_id: str | None = None,
    ) -> None:
        """Replace all tags for an entity."""
        resolved_project_id = self._resolve_project_id(project_id)
        async with self.db.transaction():
            await self.db.execute(
                "DELETE FROM tags WHERE entity_type = ? AND entity_id = ? AND project_id = ?",
                [entity_type, entity_id, resolved_project_id],
            )
            for tag in tags:
                await self.db.execute(
                    "INSERT OR IGNORE INTO tags "
                    "(tag, entity_type, entity_id, project_id) VALUES (?, ?, ?, ?)",
                    [tag.lower().strip(), entity_type, entity_id, resolved_project_id],
                )
            await self.db.commit()

    async def _get_enrichment_status(
        self,
        entity_type: str,
        entity_id: str,
        project_id: str | None = None,
    ) -> str:
        """Return pending/failed/ready based on queued enrichment work."""
        resolved_project_id = self._resolve_project_id(project_id)
        try:
            pending = await self.db.fetchone(
                """SELECT 1
                   FROM jobs
                   WHERE project_id = ? AND entity_type = ? AND entity_id = ?
                     AND status IN ('pending', 'running')
                   LIMIT 1""",
                [resolved_project_id, entity_type, entity_id],
            )
            if pending:
                return "pending"

            failed = await self.db.fetchone(
                """SELECT 1
                   FROM jobs
                   WHERE project_id = ? AND entity_type = ? AND entity_id = ?
                     AND status = 'failed'
                   LIMIT 1""",
                [resolved_project_id, entity_type, entity_id],
            )
            if failed:
                return "failed"
        except Exception:
            return "ready"

        return "ready"

    # ---- FTS5 sync ----

    # Maps entity types to FTS5 table and columns
    _FTS_CONFIG: dict[str, dict] = {
        "journal": {"table": "fts_journal", "columns": ["id", "content", "summary"]},
        "decision": {"table": "fts_decisions", "columns": ["id", "question", "rationale"]},
        "literature": {"table": "fts_literature", "columns": ["id", "title", "abstract", "notes"]},
        "mission": {"table": "fts_missions", "columns": ["id", "objective", "context"]},
        "claim": {"table": "fts_claims", "columns": ["id", "content"]},
        "cluster": {"table": "fts_clusters", "columns": ["id", "label", "synthesis"]},
    }

    async def _sync_fts(self, entity_type: str, entity_id: str, data: dict, *, strict: bool = False) -> None:
        """Insert or update an entity's FTS5 index entry.

        The managed transaction becomes a savepoint when a caller already
        owns the connection, so a failure between the DELETE and INSERT
        restores the previous index row without rolling back the caller's
        aggregate mutation.  When called on its own, it also owns the shared
        connection for the full delete/reinsert window, preventing another
        coroutine from being committed inside a legacy raw savepoint.
        """
        config = self._FTS_CONFIG.get(entity_type)
        if not config:
            return
        try:
            async with self.db.transaction():
                await self.db.execute(
                    f"DELETE FROM {config['table']} WHERE id = ?", [entity_id]
                )
                cols = config["columns"]
                values = [data.get(c, "") or "" for c in cols]
                values[0] = entity_id  # id column
                placeholders = ", ".join("?" for _ in cols)
                col_names = ", ".join(cols)
                await self.db.execute(
                    f"INSERT INTO {config['table']} "
                    f"({col_names}) VALUES ({placeholders})",
                    values,
                )
        except Exception as exc:
            if strict:
                raise
            logger.warning(
                "FTS5 sync failed for %s/%s: %s — search index NOT updated for "
                "this entity; run `rka admin reindex` to repair.",
                entity_type, entity_id, exc,
            )

    # ---- Embedding sync ----

    async def _sync_embedding(self, entity_type: str, entity_id: str, data: dict) -> None:
        """Generate and store embedding for an entity (if embedding service available)."""
        # Cluster vector embeddings remain parked; unsupported types are not
        # accidentally admitted by the shared document codec.
        if not self.embeddings or entity_type not in DOCUMENT_SPECS:
            return
        try:
            text = compose_document(entity_type, data)
            if not text:
                return
            await self.embeddings.embed_and_store(
                entity_type,
                entity_id,
                text,
                project_id=self.project_id,
            )
        except Exception as exc:
            # Was `debug`, i.e. off by default, so a permanently stale vector
            # produced no signal anywhere. Matches the wording _sync_fts has
            # always used for the same class of failure.
            logger.warning(
                "Embedding sync failed for %s/%s: %s — vector index NOT updated "
                "for this entity; it remains searchable by its previous text. "
                "Run an embedding backfill to repair.",
                entity_type, entity_id, exc,
            )

    async def _sync_indexes(self, entity_type: str, entity_id: str, data: dict) -> None:
        """Sync both FTS5 and embedding indexes for an entity."""
        await self._sync_fts(entity_type, entity_id, data)
        await self._sync_embedding(entity_type, entity_id, data)

    # ---- Auto-enrichment ----

    async def _auto_enrich_tags(
        self,
        content: str,
        existing_tags: list[str],
        project_id: str | None = None,
    ) -> list[str] | None:
        """Auto-generate tags via LLM. Returns None if LLM not configured on this service."""
        if not self.llm:
            return None
        resolved_project_id = self._resolve_project_id(project_id)
        # Get existing project tags for reuse hints
        rows = await self.db.fetchall(
            "SELECT DISTINCT tag FROM tags WHERE project_id = ? ORDER BY tag LIMIT 50",
            [resolved_project_id],
        )
        project_tags = [r["tag"] for r in rows]
        return await self.llm.auto_tag(content, project_tags)

    async def add_link(
        self,
        source_type: str,
        source_id: str,
        link_type: str,
        target_type: str,
        target_id: str,
        created_by: str = "system",
        project_id: str | None = None,
    ) -> None:
        """Record a project-local typed edge (idempotent — skips duplicates)."""
        resolved_project_id = self._resolve_project_id(project_id)
        async with self.db.transaction():
            await self._require_link_entity(
                source_type,
                source_id,
                project_id=resolved_project_id,
            )
            await self._require_link_entity(
                target_type,
                target_id,
                project_id=resolved_project_id,
            )
            await self.db.execute(
                """INSERT OR IGNORE INTO entity_links
                   (id, source_type, source_id, link_type, target_type, target_id, created_by, project_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    generate_id("link"),
                    source_type,
                    source_id,
                    link_type,
                    target_type,
                    target_id,
                    created_by,
                    resolved_project_id,
                ],
            )

    _LINK_ENTITY_TABLES: ClassVar[dict[str, str]] = {
        "artifact": "artifacts",
        "checkpoint": "checkpoints",
        "claim": "claims",
        "cluster": "evidence_clusters",
        "decision": "decisions",
        "experiment_observation": "experiment_observations",
        "figure": "figures",
        "interpretation_candidate": "interpretation_candidates",
        "journal": "journal",
        "literature": "literature",
        "mission": "missions",
    }

    async def _require_link_entity(
        self,
        entity_type: str,
        entity_id: str,
        *,
        project_id: str,
    ) -> None:
        """Require a typed edge endpoint to exist in the active project."""
        table = self._LINK_ENTITY_TABLES.get(entity_type)
        if table is None:
            raise EntityLinkValidationError(
                f"unsupported entity link type {entity_type!r}"
            )
        row = await self.db.fetchone(
            f"SELECT id FROM {table} WHERE id = ? AND project_id = ?",
            [entity_id, project_id],
        )
        if row is None:
            raise EntityLinkValidationError(
                f"{entity_type} {entity_id!r} not found in project {project_id}"
            )

    @staticmethod
    def _canonical_link_ids(entity_ids: list[str] | None) -> list[str]:
        """Trim and de-duplicate entity IDs while retaining caller order."""
        canonical: list[str] = []
        seen: set[str] = set()
        for raw_id in entity_ids or []:
            entity_id = raw_id.strip() if raw_id else ""
            if entity_id and entity_id not in seen:
                canonical.append(entity_id)
                seen.add(entity_id)
        return canonical

    @classmethod
    def _normalized_link_ids(cls, entity_ids: list[str] | None) -> set[str]:
        """Return the canonical IDs as a set for edge replacement."""
        return set(cls._canonical_link_ids(entity_ids))

    async def _replace_outgoing_links(
        self,
        *,
        source_type: str,
        source_id: str,
        link_type: str,
        target_type: str,
        target_ids: list[str] | None,
        created_by: str = "system",
        project_id: str | None = None,
    ) -> None:
        """Make one outgoing typed-edge set exactly match ``target_ids``.

        Existing matching edges are retained so their IDs and provenance
        timestamps stay stable. Stale edges are removed and missing edges are
        inserted. The helper is atomic on its own and nests safely inside a
        larger aggregate transaction.
        """
        resolved_project_id = self._resolve_project_id(project_id)
        desired = self._normalized_link_ids(target_ids)
        async with self.db.transaction():
            await self._require_link_entity(
                source_type,
                source_id,
                project_id=resolved_project_id,
            )
            for target_id in sorted(desired):
                await self._require_link_entity(
                    target_type,
                    target_id,
                    project_id=resolved_project_id,
                )
            rows = await self.db.fetchall(
                """SELECT target_id
                   FROM entity_links
                   WHERE project_id = ?
                     AND source_type = ? AND source_id = ?
                     AND link_type = ? AND target_type = ?""",
                [
                    resolved_project_id,
                    source_type,
                    source_id,
                    link_type,
                    target_type,
                ],
            )
            existing = {row["target_id"] for row in rows}
            for target_id in sorted(existing - desired):
                await self.db.execute(
                    """DELETE FROM entity_links
                       WHERE project_id = ?
                         AND source_type = ? AND source_id = ?
                         AND link_type = ? AND target_type = ? AND target_id = ?""",
                    [
                        resolved_project_id,
                        source_type,
                        source_id,
                        link_type,
                        target_type,
                        target_id,
                    ],
                )
            for target_id in sorted(desired - existing):
                await self.add_link(
                    source_type,
                    source_id,
                    link_type,
                    target_type,
                    target_id,
                    created_by=created_by,
                    project_id=resolved_project_id,
                )

    async def _replace_incoming_links(
        self,
        *,
        target_type: str,
        target_id: str,
        link_type: str,
        source_type: str,
        source_ids: list[str] | None,
        created_by: str = "system",
        project_id: str | None = None,
    ) -> None:
        """Make one incoming typed-edge set exactly match ``source_ids``."""
        resolved_project_id = self._resolve_project_id(project_id)
        desired = self._normalized_link_ids(source_ids)
        async with self.db.transaction():
            await self._require_link_entity(
                target_type,
                target_id,
                project_id=resolved_project_id,
            )
            for source_id in sorted(desired):
                await self._require_link_entity(
                    source_type,
                    source_id,
                    project_id=resolved_project_id,
                )
            rows = await self.db.fetchall(
                """SELECT source_id
                   FROM entity_links
                   WHERE project_id = ?
                     AND target_type = ? AND target_id = ?
                     AND link_type = ? AND source_type = ?""",
                [
                    resolved_project_id,
                    target_type,
                    target_id,
                    link_type,
                    source_type,
                ],
            )
            existing = {row["source_id"] for row in rows}
            for source_id in sorted(existing - desired):
                await self.db.execute(
                    """DELETE FROM entity_links
                       WHERE project_id = ?
                         AND target_type = ? AND target_id = ?
                         AND link_type = ? AND source_type = ? AND source_id = ?""",
                    [
                        resolved_project_id,
                        target_type,
                        target_id,
                        link_type,
                        source_type,
                        source_id,
                    ],
                )
            for source_id in sorted(desired - existing):
                await self.add_link(
                    source_type,
                    source_id,
                    link_type,
                    target_type,
                    target_id,
                    created_by=created_by,
                    project_id=resolved_project_id,
                )

    async def _auto_link(
        self,
        content: str,
        current_type: str,
        project_id: str | None = None,
    ):
        """Infer entity links for a new entry using the LLM.

        Fetches recent decisions, literature, and missions as candidates,
        then asks the LLM to identify which are related to this entry.
        Returns a SemanticLinks object or None if LLM not configured.
        """
        if not self.llm:
            return None
        resolved_project_id = self._resolve_project_id(project_id)
        decisions = await self.db.fetchall(
            "SELECT id, question FROM decisions WHERE project_id = ? AND status != 'superseded' ORDER BY created_at DESC LIMIT 30",
            [resolved_project_id],
        )
        literature = await self.db.fetchall(
            "SELECT id, title FROM literature WHERE project_id = ? ORDER BY created_at DESC LIMIT 30",
            [resolved_project_id],
        )
        missions = await self.db.fetchall(
            "SELECT id, objective FROM missions WHERE project_id = ? AND status != 'cancelled' ORDER BY created_at DESC LIMIT 20",
            [resolved_project_id],
        )
        return await self.llm.semantic_link(
            content=content,
            current_type=current_type,
            decisions=[dict(r) for r in decisions],
            literature=[dict(r) for r in literature],
            missions=[dict(r) for r in missions],
        )

    async def _auto_summarize(self, content: str) -> str | None:
        """Generate a one-line summary via LLM. Returns None if LLM not configured."""
        if not self.llm:
            return None
        return await self.llm.summarize_entry(content)

    # ---- JSON helpers ----

    @staticmethod
    def _json_dumps(obj) -> str | None:
        """Serialize to JSON string, or None if obj is None."""
        if obj is None:
            return None
        return json.dumps(obj, default=str)

    @staticmethod
    def _json_loads(s: str | None, default=None):
        """Parse JSON string, or return default if None/empty."""
        if not s:
            return default
        try:
            return json.loads(s)
        except (json.JSONDecodeError, TypeError):
            return default
