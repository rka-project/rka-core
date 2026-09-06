"""Embedding generation — pluggable backends via `embedding_backends/`.

Public surface (`EmbeddingService`) is preserved for every existing
caller. Internally, the work is delegated to a swappable
`EmbeddingBackend` (FastEmbed local, OpenAI-compat HTTP, or Ollama HTTP)
selected at construction time. Mission D (`feat/v2.4-pluggable-embeddings`)
introduces this seam; the factory lives in `embedding_backends/__init__.py`.

Construction modes:

  - `EmbeddingService(model_name="...")` — legacy FastEmbed-only path,
    preserved for tests + boot code that haven't been migrated yet.
  - `EmbeddingService.from_config({"backend": "...", "config": {...}})` —
    new path used by application startup once T2's
    `EmbeddingConfigService` lands.
"""

from __future__ import annotations

import hashlib
import logging
from typing import TYPE_CHECKING, Any

from rka.infra.embedding_backends import EmbeddingBackend, make_backend
from rka.infra.embedding_resources import EmbeddingCallTimeout, EmbeddingResourceError

if TYPE_CHECKING:
    from rka.infra.database import Database

logger = logging.getLogger(__name__)


class EmbeddingService:
    """High-level embedding facade.

    The actual embed calls are dispatched to a backend that satisfies the
    `EmbeddingBackend` Protocol. The legacy positional / keyword arg
    construction defaults to FastEmbed to preserve historical behavior;
    new callers should prefer `EmbeddingService.from_config(...)`.
    """

    def __init__(
        self,
        model_name: str = "nomic-ai/nomic-embed-text-v1.5",
        db: "Database | None" = None,
        *,
        backend: EmbeddingBackend | None = None,
    ) -> None:
        self.db = db
        self.index_generation: int | None = None
        self.runtime_available: bool | None = None
        self.runtime_error_code: str | None = None
        if backend is not None:
            self._backend: EmbeddingBackend = backend
            self.model_name = backend.model_name
        else:
            self._backend = make_backend(
                {"backend": "fastembed", "config": {"model_name": model_name}}
            )
            self.model_name = model_name
        from rka.services.embedding_index import embedding_space_signature

        self.space_signature = embedding_space_signature(
            {
                "backend": "fastembed",
                "config": {"model_name": self.model_name, "dim": self._backend.dim},
            },
            dimensions=self._backend.dim,
        )

    # ------------------------------------------------------------------
    # Construction helpers
    # ------------------------------------------------------------------

    @classmethod
    def from_config(
        cls, config: dict[str, Any], db: "Database | None" = None
    ) -> "EmbeddingService":
        """Build a service from a `/data/embedding_config.json`-shaped dict."""
        backend = make_backend(config)
        service = cls(model_name=backend.model_name, db=db, backend=backend)
        from rka.services.embedding_index import embedding_space_signature

        service.space_signature = embedding_space_signature(
            config,
            dimensions=backend.dim,
        )
        return service

    def bind_index_generation(
        self,
        generation: int,
        *,
        space_signature: str | None = None,
    ) -> None:
        """Bind this process-local backend to a durable index generation."""

        self.index_generation = int(generation)
        if space_signature is not None:
            self.space_signature = space_signature

    # ------------------------------------------------------------------
    # Public surface — preserved from the pre-T1 EmbeddingService
    # ------------------------------------------------------------------

    @property
    def dim(self) -> int:
        return self._backend.dim

    @property
    def backend(self) -> EmbeddingBackend:
        return self._backend

    def validate_document(self, text: str) -> None:
        """Preflight without inference, using the backend's prepared-input policy."""
        validate = getattr(self._backend, "validate_input", None)
        if validate is not None:
            validate(text, is_query=False)

    def _record_resource_error(self, exc: EmbeddingResourceError) -> None:
        # Admission/input rejection says nothing about provider reachability.
        self.runtime_error_code = exc.code
        if isinstance(exc, EmbeddingCallTimeout):
            self.runtime_available = False

    async def embed(self, text: str) -> list[float]:
        """Embed a query string."""
        try:
            result = await self._backend.embed(text, is_query=True)
        except EmbeddingResourceError as exc:
            self._record_resource_error(exc)
            raise
        except Exception:
            self.runtime_available = False
            self.runtime_error_code = "embedding_backend_unavailable"
            raise
        self.runtime_available = True
        self.runtime_error_code = None
        return result

    async def embed_document(self, text: str) -> list[float]:
        """Embed a document for storage."""
        try:
            result = await self._backend.embed(text, is_query=False)
        except EmbeddingResourceError as exc:
            self._record_resource_error(exc)
            raise
        except Exception:
            self.runtime_available = False
            self.runtime_error_code = "embedding_backend_unavailable"
            raise
        self.runtime_available = True
        self.runtime_error_code = None
        return result

    async def embed_batch(
        self, texts: list[str], is_query: bool = False
    ) -> list[list[float]]:
        """Batch embed multiple texts."""
        try:
            result = await self._backend.embed_batch(texts, is_query=is_query)
        except EmbeddingResourceError as exc:
            self._record_resource_error(exc)
            raise
        except Exception:
            self.runtime_available = False
            self.runtime_error_code = "embedding_backend_unavailable"
            raise
        self.runtime_available = True
        self.runtime_error_code = None
        return result

    async def assert_current_generation(self) -> None:
        """Reject writes produced by an outdated API or worker process."""

        if self.db is None:
            return
        from rka.services.embedding_index import assert_embedding_generation

        await assert_embedding_generation(
            self.db,
            generation=self.index_generation,
            space_signature=self.space_signature,
            dim=self._backend.dim,
        )

    async def index_search_ready(self) -> bool:
        """Return whether this backend may query the durable vector index."""

        if self.db is None:
            return True
        from rka.services.embedding_index import embedding_index_search_ready

        return await embedding_index_search_ready(
            self.db,
            generation=self.index_generation,
            space_signature=self.space_signature,
            dim=self._backend.dim,
        )

    @staticmethod
    def content_hash(content: str | bytes) -> str:
        """Hash content to detect changes for re-embedding."""
        if isinstance(content, bytes):
            raw = content
        else:
            raw = content.encode()
        return hashlib.sha256(raw).hexdigest()[:16]

    async def needs_reembed(
        self,
        entity_type: str,
        entity_id: str,
        text: str,
        project_id: str = "proj_default",
    ) -> bool:
        """Check if the stored embedding is stale.

        v2.5.5 (mis_01KS1RFNM2T1HTB077G507T1FR Bug 2): we now check the
        full identity tuple (content_hash, model_name, dimensions). The
        v2.4 implementation only compared content_hash, so a backend
        swap (e.g. nomic-768 → qwen3-4096) left every unchanged entity
        flagged "not stale" even though its stored vector belonged to a
        retired model + dim.
        """
        if self.db is None:
            return True
        # Defensive: backend hasn't reported a dim yet (un-initialized
        # / un-probed). Treat as needs re-embed so the upcoming
        # store_embedding picks a fresh dim from the backend handshake.
        if self._backend.dim == 0:
            return True
        meta = await self.db.fetchone(
            """SELECT content_hash, model_name, dimensions
               FROM embedding_metadata
               WHERE project_id = ? AND entity_type = ? AND entity_id = ?""",
            [project_id, entity_type, entity_id],
        )
        if meta is None:
            return True
        if meta["content_hash"] != self.content_hash(text):
            return True
        if meta["model_name"] != self.model_name:
            return True
        if int(meta["dimensions"]) != int(self._backend.dim):
            return True
        return False

    async def store_embedding(
        self,
        entity_type: str,
        entity_id: str,
        text: str,
        embedding: list[float] | None = None,
        project_id: str = "proj_default",
    ) -> None:
        """Store embedding in sqlite-vec virtual table and update metadata."""
        if self.db is None:
            return

        table_map = {
            "decision": "vec_decisions",
            "literature": "vec_literature",
            "journal": "vec_journal",
            "mission": "vec_missions",
            "claim": "vec_claims",
            "artifact": "vec_artifacts",
            "figure": "vec_artifacts",
        }
        table = table_map.get(entity_type)
        if embedding is None:
            embedding = await self.embed_document(text)

        # v2.7.0.7 — the vec row and its metadata row must be written together
        # or not at all. Previously the metadata INSERT ran unconditionally, so
        # when sqlite-vec was unavailable the metadata row claimed "embedded at
        # <model>/<dim>" with no actual vector — and `needs_reembed` then
        # returned False forever, permanently stranding the entity out of vector
        # search even after vec recovered. Gate both on the same condition.
        if self.db.vec_available and table:
            import struct

            vec_blob = struct.pack(f"{len(embedding)}f", *embedding)
            # sqlite-vec vec0 tables do not honour the REPLACE conflict
            # clause: re-embedding an entity that already has a vector raises
            # "UNIQUE constraint failed on <table> primary key". The caller
            # swallowed that, so an edited entity kept the vector for its
            # withdrawn text — semantically retrievable by wording that is no
            # longer there, and unretrievable by the wording that is. FTS
            # updated correctly, so the entry looked repaired. It never
            # self-healed: every later edit raised and was swallowed again.
            #
            # DELETE-then-INSERT is the form already proven in
            # embedding_backfill.py. The managed transaction also covers the
            # metadata row, so a failed INSERT cannot leave the entity with no
            # vector at all — which would be worse than a stale one.
            async with self.db.transaction():
                await self.assert_current_generation()
                await self.db.execute(
                    f"DELETE FROM {table} WHERE id = ?",
                    [entity_id],
                )
                if table == "vec_artifacts":
                    await self.db.execute(
                        f"INSERT INTO {table} "
                        "(id, project_id, entity_type, embedding) "
                        "VALUES (?, ?, ?, ?)",
                        [entity_id, project_id, entity_type, vec_blob],
                    )
                else:
                    await self.db.execute(
                        f"INSERT INTO {table} "
                        "(id, project_id, embedding) VALUES (?, ?, ?)",
                        [entity_id, project_id, vec_blob],
                    )
                await self.db.execute(
                    """INSERT OR REPLACE INTO embedding_metadata
                       (project_id, entity_type, entity_id, content_hash,
                        model_name, dimensions)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    [
                        project_id,
                        entity_type,
                        entity_id,
                        self.content_hash(text),
                        self.model_name,
                        self._backend.dim,
                    ],
                )

    async def embed_and_store(
        self,
        entity_type: str,
        entity_id: str,
        text: str,
        project_id: str = "proj_default",
    ) -> None:
        """Convenience: embed text and store result if content changed."""
        if not text.strip():
            return
        if not await self.needs_reembed(entity_type, entity_id, text, project_id=project_id):
            return
        await self.store_embedding(entity_type, entity_id, text, project_id=project_id)
        logger.debug("Embedded %s/%s (%d chars)", entity_type, entity_id, len(text))
