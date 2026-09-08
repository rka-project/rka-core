"""Journal (notes) routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from rka.models.journal import (
    JournalAttributionCorrection, JournalAttributionRevision,
    JournalEntry, JournalEntryCreate, JournalEntryUpdate,
)
from rka.services.notes import (
    JournalAttributionConflict, JournalAttributionError, NoteNotFoundError, NoteService,
)
from rka.api.deps import get_scoped_note_service

router = APIRouter()


@router.post("/notes", response_model=JournalEntry, status_code=201)
async def create_note(
    data: JournalEntryCreate,
    svc: NoteService = Depends(get_scoped_note_service),
):
    return await svc.create(data)


@router.get("/notes", response_model=list[JournalEntry])
async def list_notes(
    type: str | None = None,
    phase: str | None = None,
    confidence: str | None = None,
    importance: str | None = None,
    source: str | None = None,
    status: str | None = None,
    since: str | None = None,
    hide_superseded: bool = True,
    limit: int = Query(50, le=200),
    offset: int = 0,
    svc: NoteService = Depends(get_scoped_note_service),
):
    return await svc.list(
        type=type, phase=phase, confidence=confidence,
        importance=importance, source=source, status=status,
        since=since, hide_superseded=hide_superseded,
        limit=limit, offset=offset,
    )


@router.get("/notes/{note_id}", response_model=JournalEntry)
async def get_note(note_id: str, svc: NoteService = Depends(get_scoped_note_service)):
    entry = await svc.get(note_id)
    if entry is None:
        raise HTTPException(404, f"Note {note_id} not found")
    return entry


@router.put("/notes/{note_id}", response_model=JournalEntry)
async def update_note(
    note_id: str,
    data: JournalEntryUpdate,
    svc: NoteService = Depends(get_scoped_note_service),
):
    entry = await svc.get(note_id)
    if entry is None:
        raise HTTPException(404, f"Note {note_id} not found")
    try:
        return await svc.update(note_id, data)
    except JournalAttributionError as exc:
        raise HTTPException(422, str(exc)) from exc
    except NoteNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post("/notes/{note_id}/attribution-corrections", response_model=JournalAttributionRevision)
async def correct_note_attribution(
    note_id: str,
    data: JournalAttributionCorrection,
    svc: NoteService = Depends(get_scoped_note_service),
):
    try:
        return await svc.correct_attribution(note_id, data)
    except NoteNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except JournalAttributionConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except JournalAttributionError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/notes/{note_id}/attribution-history", response_model=list[JournalAttributionRevision])
async def note_attribution_history(
    note_id: str,
    after_revision: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    svc: NoteService = Depends(get_scoped_note_service),
):
    try:
        return await svc.attribution_history(note_id, after_revision=after_revision, limit=limit)
    except NoteNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
