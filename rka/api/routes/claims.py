"""Claims API routes (v2.0)."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Query

from rka.models.claim import (
    Claim,
    ClaimCreate,
    ClaimEdge,
    ClaimEdgeCreate,
    ClaimScopeHistory,
    ClaimScopeWrite,
    ClaimUpdate,
    EvidenceStatus,
)
from rka.services.claims import (
    ClaimNotFoundError,
    ClaimScopeConflictError,
    ClaimService,
)
from rka.api.deps import get_scoped_claim_service

router = APIRouter()


def _raise_claim_scope_error(exc: Exception) -> None:
    if isinstance(exc, ClaimNotFoundError):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(exc, ClaimScopeConflictError):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if isinstance(exc, sqlite3.IntegrityError):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if isinstance(exc, ValueError):
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    raise exc


@router.post("/claims", response_model=Claim, status_code=201)
async def create_claim(
    data: ClaimCreate,
    svc: ClaimService = Depends(get_scoped_claim_service),
):
    return await svc.create(data)


@router.get("/claims", response_model=list[Claim])
async def list_claims(
    source_entry_id: str | None = None,
    cluster_id: str | None = None,
    claim_type: str | None = None,
    verified: bool | None = None,
    evidence_status: EvidenceStatus | None = None,
    stale: bool | None = None,
    limit: int = Query(50, le=200),
    offset: int = 0,
    svc: ClaimService = Depends(get_scoped_claim_service),
):
    return await svc.list(
        source_entry_id=source_entry_id,
        cluster_id=cluster_id,
        claim_type=claim_type,
        verified=verified,
        evidence_status=evidence_status,
        stale=stale,
        limit=limit,
        offset=offset,
    )


@router.get("/claims/{claim_id}", response_model=Claim)
async def get_claim(
    claim_id: str,
    svc: ClaimService = Depends(get_scoped_claim_service),
):
    claim = await svc.get(claim_id)
    if claim is None:
        raise HTTPException(404, f"Claim {claim_id} not found")
    return claim


@router.get("/claims/{claim_id}/scope", response_model=ClaimScopeHistory)
async def get_claim_scope(
    claim_id: str,
    svc: ClaimService = Depends(get_scoped_claim_service),
):
    history = await svc.get_scope_history(claim_id)
    if history is None:
        raise HTTPException(404, f"Claim {claim_id} not found")
    return history


@router.post("/claims/{claim_id}/scope", response_model=ClaimScopeHistory)
async def append_claim_scope(
    claim_id: str,
    data: ClaimScopeWrite,
    svc: ClaimService = Depends(get_scoped_claim_service),
):
    try:
        return await svc.append_scope(claim_id, data)
    except Exception as exc:
        _raise_claim_scope_error(exc)


@router.put("/claims/{claim_id}", response_model=Claim)
async def update_claim(
    claim_id: str,
    data: ClaimUpdate,
    svc: ClaimService = Depends(get_scoped_claim_service),
):
    claim = await svc.get(claim_id)
    if claim is None:
        raise HTTPException(404, f"Claim {claim_id} not found")
    try:
        return await svc.update(claim_id, data)
    except ValueError as exc:
        _raise_claim_scope_error(exc)


@router.post("/claims/edges", response_model=ClaimEdge, status_code=201)
async def create_claim_edge(
    data: ClaimEdgeCreate,
    svc: ClaimService = Depends(get_scoped_claim_service),
):
    return await svc.create_edge(data)
