"""Search routes — hybrid FTS5 + vector search (Phase 2) with LIKE fallback."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from rka.api.deps import get_scoped_search_service
from rka.services.search import SearchService

router = APIRouter()


class SearchRequest(BaseModel):
    query: str
    entity_types: list[str] | None = None
    limit: int = 20
    keyword_weight: float = 0.3
    semantic_weight: float = 0.7


class SearchResult(BaseModel):
    entity_type: str
    entity_id: str
    title: str
    snippet: str
    score: float = 0.0
    # Currency signals — absent means the source table carries none.
    status: str | None = None
    superseded_by: str | None = None
    stale: bool | None = None
    staleness: str | None = None
    staleness_verdict: str | None = None
    currentness: dict | None = None


@router.post("/search", response_model=list[SearchResult])
async def search(
    data: SearchRequest,
    svc: SearchService = Depends(get_scoped_search_service),
):
    """Hybrid search across all entity types (FTS5 + vector + LIKE fallback)."""
    hits = await svc.search(
        query=data.query,
        entity_types=data.entity_types,
        limit=data.limit,
        keyword_weight=data.keyword_weight,
        semantic_weight=data.semantic_weight,
    )

    return [
        SearchResult(
            entity_type=h.entity_type,
            entity_id=h.entity_id,
            title=h.title,
            snippet=h.snippet,
            score=h.score,
            status=h.status,
            superseded_by=h.superseded_by,
            stale=h.stale,
            staleness=h.staleness,
            staleness_verdict=h.staleness_verdict,
            currentness=h.currentness,
        )
        for h in hits
    ]
