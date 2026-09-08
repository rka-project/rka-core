"""Checkpoint routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from rka.models.checkpoint import Checkpoint, CheckpointCreate, CheckpointResolve
from rka.services.checkpoints import CheckpointService
from rka.services.decisions import DecisionService
from rka.api.deps import get_scoped_checkpoint_service, get_scoped_decision_service

router = APIRouter()


@router.post("/checkpoints", response_model=Checkpoint, status_code=201)
async def create_checkpoint(
    data: CheckpointCreate,
    actor: str = "executor",
    svc: CheckpointService = Depends(get_scoped_checkpoint_service),
):
    try:
        return await svc.create(data, actor=actor)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/checkpoints", response_model=list[Checkpoint])
async def list_checkpoints(
    status: str | None = None,
    mission_id: str | None = None,
    limit: int = Query(50, le=200),
    offset: int = 0,
    svc: CheckpointService = Depends(get_scoped_checkpoint_service),
):
    return await svc.list(status=status, mission_id=mission_id, limit=limit, offset=offset)


@router.get("/checkpoints/{chk_id}", response_model=Checkpoint)
async def get_checkpoint(chk_id: str, svc: CheckpointService = Depends(get_scoped_checkpoint_service)):
    chk = await svc.get(chk_id)
    if chk is None:
        raise HTTPException(404, f"Checkpoint {chk_id} not found")
    return chk


@router.put("/checkpoints/{chk_id}/resolve", response_model=Checkpoint)
async def resolve_checkpoint(
    chk_id: str,
    data: CheckpointResolve,
    svc: CheckpointService = Depends(get_scoped_checkpoint_service),
    dec_svc: DecisionService = Depends(get_scoped_decision_service),
):
    chk = await svc.get(chk_id)
    if chk is None:
        raise HTTPException(404, f"Checkpoint {chk_id} not found")
    try:
        return await svc.resolve(chk_id, data, decision_service=dec_svc)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
