"""Bounded, read-only document-hash checks for index maintenance.

This checks known source/metadata pairs for a requested model and dimension.
It is NOT a complete index-health or space-identity proof: callers must also
check physical schema, vector/source coherence and generation/config identity.
No provider is constructed, and results never contain source text or arbitrary
exception messages. A coherent snapshot requires a caller-owned transaction.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, AsyncIterator, Literal

from rka.infra.embedding_documents import (
    DOCUMENT_SPECS,
    compose_document,
    embedding_content_hash,
)


@dataclass(frozen=True)
class StoredDocumentCheck:
    entity_type: str
    entity_id: str
    project_id: str
    status: Literal["verified", "hash_mismatch", "empty_document", "invalid_document"]


async def iter_stored_document_checks(
    db: Any, *, model_name: str, dimensions: int,
) -> AsyncIterator[StoredDocumentCheck]:
    """Yield individual proofs/failures, using keyset pages of at most 8 rows."""
    for entity_type, spec in DOCUMENT_SPECS.items():
        last_id = None
        while True:
            rows = await db.fetchall(
                f"""SELECT s.id, s.project_id,
                           {", ".join("s." + field for field in spec.fields)},
                           m.content_hash AS _embedding_content_hash
                    FROM {spec.source_table} s
                    JOIN embedding_metadata m
                      ON m.project_id = s.project_id
                     AND m.entity_type = ? AND m.entity_id = s.id
                    WHERE m.model_name = ? AND m.dimensions = ?
                      AND (? IS NULL OR s.id > ?)
                    ORDER BY s.id LIMIT ?""",
                [entity_type, model_name, dimensions, last_id, last_id, 8],
            )
            if not rows:
                break
            for row in rows:
                try:
                    text = compose_document(entity_type, row)
                    if not text:
                        status = "empty_document"
                    elif embedding_content_hash(text) != row["_embedding_content_hash"]:
                        status = "hash_mismatch"
                    else:
                        status = "verified"
                except Exception:  # noqa: BLE001
                    status = "invalid_document"
                yield StoredDocumentCheck(entity_type, row["id"], row["project_id"], status)
            last_id = rows[-1]["id"]
