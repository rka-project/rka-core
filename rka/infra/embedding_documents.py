"""Canonical source-to-document recipes, without database or provider access.

These preserve existing backfill recipes and the 16-hex SHA256 metadata format.
Field-edge whitespace is stripped for plain-text entities; interior whitespace
and source records are never rewritten. Artifact/figure labels and JSON handling
remain compatible with their existing builders.

Provider document templates are applied later and belong to the embedding space
signature, not this source-content hash. Future recipe changes require explicit
compatibility/reindex decisions, not silent hash upgrades.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping


@dataclass(frozen=True)
class EmbeddingDocumentSpec:
    source_table: str
    vec_table: str
    fields: tuple[str, ...]


# SQL identifiers come only from this fixed registry, never from source data.
DOCUMENT_SPECS = MappingProxyType({
    "claim": EmbeddingDocumentSpec("claims", "vec_claims", ("content",)),
    "journal": EmbeddingDocumentSpec("journal", "vec_journal", ("content", "summary")),
    "decision": EmbeddingDocumentSpec("decisions", "vec_decisions", ("question", "rationale")),
    "literature": EmbeddingDocumentSpec("literature", "vec_literature", ("title", "abstract")),
    "mission": EmbeddingDocumentSpec("missions", "vec_missions", ("objective", "context")),
    "artifact": EmbeddingDocumentSpec(
        "artifacts", "vec_artifacts", ("filename", "filetype", "mime", "metadata"),
    ),
    "figure": EmbeddingDocumentSpec("figures", "vec_artifacts", ("caption", "summary", "claims")),
})


def embedding_content_hash(content: str | bytes) -> str:
    """Hash the untemplated document using the existing metadata format."""
    raw = content if isinstance(content, bytes) else content.encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def _parse_claims(claims: str | list[dict] | None) -> list[dict]:
    """Parse stored figure claims into a list."""
    if claims is None:
        return []
    if isinstance(claims, list):
        return [claim for claim in claims if isinstance(claim, dict)]
    try:
        parsed = json.loads(claims)
    except (json.JSONDecodeError, TypeError):
        return []
    return [claim for claim in parsed if isinstance(claim, dict)] if isinstance(parsed, list) else []


def build_artifact_text(
    filename: str,
    filetype: str | None = None,
    mime: str | None = None,
    metadata: dict | str | None = None,
) -> str:
    """Build a text representation suitable for artifact search embeddings."""
    parts = [filename]
    if filetype:
        parts.append(f"filetype: {filetype}")
    if mime:
        parts.append(f"mime: {mime}")
    if metadata:
        if isinstance(metadata, str):
            try:
                parsed_metadata = json.loads(metadata)
            except (json.JSONDecodeError, TypeError):
                metadata_text = metadata
            else:
                metadata_text = json.dumps(parsed_metadata, sort_keys=True)
        else:
            metadata_text = json.dumps(metadata, sort_keys=True)
        parts.append(f"metadata: {metadata_text}")
    return "\n".join(part for part in parts if part).strip()


def build_figure_text(
    caption: str | None,
    summary: str | None,
    claims: str | list[dict] | None,
) -> str:
    """Build a text representation suitable for figure search embeddings."""
    parts: list[str] = []
    if caption:
        parts.append(f"caption: {caption}")
    if summary:
        parts.append(f"summary: {summary}")
    claim_texts = [
        claim.get("claim", "").strip()
        for claim in _parse_claims(claims)
        if claim.get("claim")
    ]
    if claim_texts:
        parts.append("claims: " + "; ".join(claim_texts[:5]))
    return "\n".join(parts).strip()



def compose_document(entity_type: str, row: Mapping[str, Any]) -> str:
    """Compose exactly one supported entity's untemplated document."""
    spec = DOCUMENT_SPECS[entity_type]
    if entity_type == "artifact":
        return build_artifact_text(
            filename=row.get("filename") or "", filetype=row.get("filetype"),
            mime=row.get("mime"), metadata=row.get("metadata"),
        )
    if entity_type == "figure":
        return build_figure_text(row.get("caption"), row.get("summary"), row.get("claims"))
    return " ".join(
        str(row[field]).strip() for field in spec.fields
        if row.get(field) and str(row[field]).strip()
    ).strip()
