"""Mission routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from rka.models.mission import Mission, MissionCreate, MissionUpdate, MissionReport, MissionReportCreate
from rka.services.missions import MissionService
from rka.api.deps import get_scoped_mission_service

router = APIRouter()


@router.post("/missions", response_model=Mission, status_code=201)
async def create_mission(
    data: MissionCreate,
    actor: str = "brain",
    svc: MissionService = Depends(get_scoped_mission_service),
):
    try:
        return await svc.create(data, actor=actor)
    except ValueError as e:
        # e.g. motivated_by_decision references a non-existent decision —
        # a client error, not a 500. Without this, the service's FK-validation
        # ValueError would propagate as an unhandled 500.
        raise HTTPException(400, str(e))


@router.get("/missions", response_model=list[Mission])
async def list_missions(
    phase: str | None = None,
    status: str | None = None,
    limit: int = Query(50, le=200),
    offset: int = 0,
    svc: MissionService = Depends(get_scoped_mission_service),
):
    return await svc.list(phase=phase, status=status, limit=limit, offset=offset)


@router.get("/missions/{mis_id}", response_model=Mission)
async def get_mission(mis_id: str, svc: MissionService = Depends(get_scoped_mission_service)):
    mission = await svc.get(mis_id)
    if mission is None:
        raise HTTPException(404, f"Mission {mis_id} not found")
    return mission


@router.put("/missions/{mis_id}", response_model=Mission)
async def update_mission(
    mis_id: str,
    data: MissionUpdate,
    actor: str = "executor",
    svc: MissionService = Depends(get_scoped_mission_service),
):
    mission = await svc.get(mis_id)
    if mission is None:
        raise HTTPException(404, f"Mission {mis_id} not found")
    try:
        return await svc.update(mis_id, data, actor=actor)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/missions/{mis_id}/report", response_model=Mission)
async def submit_report(
    mis_id: str,
    data: MissionReportCreate,
    actor: str = "executor",
    svc: MissionService = Depends(get_scoped_mission_service),
):
    mission = await svc.get(mis_id)
    if mission is None:
        raise HTTPException(404, f"Mission {mis_id} not found")
    try:
        return await svc.submit_report(mis_id, data, actor=actor)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get("/missions/{mis_id}/report", response_model=MissionReport | None)
async def get_report(
    mis_id: str,
    svc: MissionService = Depends(get_scoped_mission_service),
):
    return await svc.get_report(mis_id)
