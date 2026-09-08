"""API routes for researcher experience tools (v2.1)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from rka.models.freshness import ResolveStaleRequest

from rka.services.researcher_tools import ResearcherToolsService
from rka.services.knowledge_pack import KnowledgePackService
from rka.api.deps import get_scoped_researcher_tools_service, get_scoped_knowledge_pack_service

router = APIRouter()


# ------------------------------------------------------------------
# Request models
# ------------------------------------------------------------------

class SplitClusterSpec(BaseModel):
    label: str
    claim_ids: list[str] = Field(default_factory=list)
    research_question_id: str | None = None


class SplitClusterRequest(BaseModel):
    source_id: str
    new_clusters: list[SplitClusterSpec]


class MergeClustersRequest(BaseModel):
    source_ids: list[str]
    target_label: str
    target_synthesis: str | None = None
    research_question_id: str | None = None


class PaperAnnotation(BaseModel):
    passage: str
    note: str | None = None
    claim_type: str
    confidence: float = 0.5
    cluster_id: str | None = None


class ProcessPaperRequest(BaseModel):
    lit_id: str
    annotations: list[PaperAnnotation]
    summary: str | None = None


class AdvanceRQRequest(BaseModel):
    rq_id: str
    status: str
    conclusion: str | None = None
    evidence_cluster_ids: list[str] | None = None


# ------------------------------------------------------------------
# Routes
# ------------------------------------------------------------------

@router.get("/changelog")
async def get_changelog(
    since: str = Query(..., description="ISO date/datetime"),
    limit: int = Query(50, le=200),
    svc: ResearcherToolsService = Depends(get_scoped_researcher_tools_service),
):
    return await svc.get_changelog(since, limit)


@router.get("/assemble-evidence")
async def assemble_evidence(
    research_question_id: str = Query(...),
    format: str = Query("progress_report"),
    svc: ResearcherToolsService = Depends(get_scoped_researcher_tools_service),
):
    try:
        markdown = await svc.assemble_evidence(research_question_id, format)
    except ValueError as e:
        raise HTTPException(404, str(e))
    return {"format": format, "research_question_id": research_question_id, "content": markdown}


@router.post("/clusters/split")
async def split_cluster(
    data: SplitClusterRequest,
    svc: ResearcherToolsService = Depends(get_scoped_researcher_tools_service),
):
    try:
        return await svc.split_cluster(data.source_id, [s.model_dump() for s in data.new_clusters])
    except ValueError as e:
        raise HTTPException(404, str(e))


@router.post("/clusters/merge")
async def merge_clusters(
    data: MergeClustersRequest,
    svc: ResearcherToolsService = Depends(get_scoped_researcher_tools_service),
):
    return await svc.merge_clusters(
        data.source_ids, data.target_label, data.target_synthesis, data.research_question_id,
    )


@router.post("/literature/process-paper")
async def process_paper(
    data: ProcessPaperRequest,
    svc: ResearcherToolsService = Depends(get_scoped_researcher_tools_service),
):
    try:
        return await svc.process_paper(
            data.lit_id,
            [a.model_dump() for a in data.annotations],
            data.summary,
        )
    except ValueError as e:
        raise HTTPException(404, str(e))


@router.post("/research-questions/advance")
async def advance_rq(
    data: AdvanceRQRequest,
    svc: ResearcherToolsService = Depends(get_scoped_researcher_tools_service),
):
    try:
        return await svc.advance_rq(
            data.rq_id, data.status, data.conclusion, data.evidence_cluster_ids,
        )
    except ValueError as e:
        raise HTTPException(422, str(e))


# ------------------------------------------------------------------
# Phase 2: Knowledge Freshness
# ------------------------------------------------------------------

class FlagStaleRequest(BaseModel):
    entity_id: str
    reason: str
    staleness: str = "yellow"
    propagate: bool = True


class DetectContradictionsRequest(BaseModel):
    entity_id: str
    similarity_threshold: float = 0.7
    max_results: int = 5


@router.post("/freshness/flag-stale")
async def flag_stale(
    data: FlagStaleRequest,
    svc: ResearcherToolsService = Depends(get_scoped_researcher_tools_service),
):
    try:
        return await svc.flag_stale(data.entity_id, data.reason, data.staleness, data.propagate)
    except ValueError as e:
        raise HTTPException(422, str(e))


@router.get("/freshness/check")
async def check_freshness(
    days_threshold: int = Query(30, ge=1),
    svc: ResearcherToolsService = Depends(get_scoped_researcher_tools_service),
):
    return await svc.check_freshness(days_threshold)


@router.post("/freshness/resolve-stale")
async def resolve_stale(data: ResolveStaleRequest,
                        svc: ResearcherToolsService = Depends(get_scoped_researcher_tools_service)):
    try:
        return await svc.resolve_stale(**data.model_dump())
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/freshness/detect-contradictions")
async def detect_contradictions(
    data: DetectContradictionsRequest,
    svc: ResearcherToolsService = Depends(get_scoped_researcher_tools_service),
):
    try:
        return await svc.detect_contradictions(data.entity_id, data.similarity_threshold, data.max_results)
    except ValueError as e:
        raise HTTPException(404, str(e))


# ------------------------------------------------------------------
# Integrity Check
# ------------------------------------------------------------------

@router.get("/integrity")
async def check_integrity(
    svc: KnowledgePackService = Depends(get_scoped_knowledge_pack_service),
):
    issues = await svc.check_integrity()
    return {"total_issues": sum(i["count"] for i in issues), "issues": issues}
