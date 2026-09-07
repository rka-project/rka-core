"""Knowledge-pack API models."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class KnowledgePackImportResult(BaseModel):
    """Import result summary."""

    project_id: str
    project_name: str
    source_project_id: str
    imported_counts: dict[str, int] = Field(default_factory=dict)
    artifact_files_restored: int = 0
    integrity_issues: list[dict] = Field(default_factory=list)
    indexing: dict[str, Any] | None = None
