"""Review queue API routes (v2.0)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from rka.models.review_queue import ReviewItem, ReviewItemCreate, ReviewItemResolve
from rka.services.review_queue import ReviewQueueService
from rka.api.deps import get_scoped_review_queue_service

router = APIRouter()


@router.get("/review-queue", response_model=list[ReviewItem])
async def list_review_items(
    status: str = "pending",
    flag: str | None = None,
    limit: int = Query(20, le=100),
    offset: int = 0,
    svc: ReviewQueueService = Depends(get_scoped_review_queue_service),
):
    return await svc.get_pending(status=status, flag=flag, limit=limit, offset=offset)


@router.get("/review-queue/stats")
async def get_review_stats(
    svc: ReviewQueueService = Depends(get_scoped_review_queue_service),
):
    return await svc.get_stats()


@router.post("/review-queue", response_model=ReviewItem, status_code=201)
async def flag_for_review(
    data: ReviewItemCreate,
    svc: ReviewQueueService = Depends(get_scoped_review_queue_service),
):
    try:
        return await svc.flag_for_review(data)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.put("/review-queue/{review_id}", response_model=ReviewItem)
async def resolve_review(
    review_id: str,
    data: ReviewItemResolve,
    svc: ReviewQueueService = Depends(get_scoped_review_queue_service),
):
    item = await svc.get(review_id)
    if item is None:
        raise HTTPException(404, f"Review item {review_id} not found")
    try:
        return await svc.resolve(review_id, data)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
