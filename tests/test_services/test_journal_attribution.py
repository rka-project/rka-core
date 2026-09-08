"""I3b: immutable, project-scoped correction and exact retry contracts."""

import asyncio
import sqlite3

import pytest
from pydantic import ValidationError

from rka.infra.database import Database
from rka.models.journal import JournalAttributionCorrection, JournalEntryCreate, JournalEntryUpdate
from rka.services.notes import (
    JournalAttributionConflict, JournalAttributionError, NoteNotFoundError, NoteService,
)


def correction(**overrides):
    return JournalAttributionCorrection.model_validate({
        "expected_revision": 0, "request_id": "test-1", "actor": "executor",
        "reason": "Rechecked original", "source": "pi", "verbatim_input": "Exact quote A",
        **overrides,
    })


@pytest.mark.asyncio
async def test_correction_history_retry_and_independent_content_edits(db):
    svc = NoteService(db)
    note = await svc.create(JournalEntryCreate(content="Synthetic note", tags=["safe"]))
    assert note.attribution_revision == 0
    assert await svc.attribution_history(note.id) == []
    first = await svc.correct_attribution(note.id, correction())
    assert first.revision == 1 and first.before_source == "executor"
    assert first.before_verbatim_input is None
    assert first.after_verbatim_input == "Exact quote A"
    assert first.actor == "executor" and first.actor_basis == "caller_asserted"
    await svc.update(note.id, JournalEntryUpdate(content="Ordinary edit", tags=["later"]))
    assert (await svc.get(note.id)).attribution_revision == 1
    second = await svc.correct_attribution(note.id, correction(
        expected_revision=1, request_id="test-2", verbatim_input="Exact quote B",
    ))
    assert await svc.correct_attribution(note.id, correction()) == first
    assert await svc.attribution_history(note.id) == [first, second]
    assert await svc.attribution_history(note.id, limit=1) == [first]
    assert await svc.attribution_history(note.id, after_revision=1) == [second]
    stored = await svc.get(note.id)
    assert stored.content == "Ordinary edit" and stored.tags == ["later"]
    assert stored.attribution_revision == 2 and stored.verbatim_input == "Exact quote B"


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", [
    {"reason": "Different reason"}, {"actor": "pi"}, {"source": "brain"},
    {"verbatim_input": "Different quote"}, {"expected_revision": 1},
])
async def test_request_id_reuse_rejects_different_intent(db, changed):
    svc = NoteService(db)
    note = await svc.create(JournalEntryCreate(content="Synthetic"))
    first = await svc.correct_attribution(note.id, correction())
    with pytest.raises(JournalAttributionConflict, match="request_id"):
        await svc.correct_attribution(note.id, correction(**changed))
    assert await svc.attribution_history(note.id) == [first]


@pytest.mark.asyncio
async def test_concurrent_corrections_have_one_winner_and_retry_is_unique(db):
    svc = NoteService(db)
    note = await svc.create(JournalEntryCreate(content="Synthetic"))
    results = await asyncio.gather(
        svc.correct_attribution(note.id, correction()),
        svc.correct_attribution(note.id, correction(request_id="other", verbatim_input="Competing quote")),
        return_exceptions=True,
    )
    assert sum(isinstance(result, JournalAttributionConflict) for result in results) == 1
    assert len(await svc.attribution_history(note.id)) == 1
    other = await svc.create(JournalEntryCreate(content="Exact retry"))
    retries = await asyncio.gather(*(svc.correct_attribution(other.id, correction()) for _ in range(4)))
    assert all(result == retries[0] for result in retries)
    assert len(await svc.attribution_history(other.id)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [RuntimeError, asyncio.CancelledError])
async def test_failed_audit_rolls_back_ledger_and_current_attribution(db, monkeypatch, failure):
    svc = NoteService(db)
    note = await svc.create(JournalEntryCreate(content="Synthetic"))
    original_audit = svc.audit

    async def fail(*args, **kwargs):
        await original_audit(*args, **kwargs)
        raise failure("injected after audit")

    monkeypatch.setattr(svc, "audit", fail)
    with pytest.raises(failure, match="injected"):
        await svc.correct_attribution(note.id, correction())
    assert await svc.get(note.id) == note
    assert await svc.attribution_history(note.id) == []
    assert await db.fetchone("SELECT id FROM audit_log WHERE action='update' AND entity_id=?", [note.id]) is None


@pytest.mark.asyncio
async def test_independent_connections_serialize_corrections_and_reopen_keeps_retry(db):
    svc = NoteService(db)
    note = await svc.create(JournalEntryCreate(content="Synthetic shared database"))
    peer_db = Database(db.db_path)
    await peer_db.connect()
    try:
        peer = NoteService(peer_db)
        requests = [correction(), correction(request_id="peer", verbatim_input="Peer quote")]
        results = await asyncio.gather(
            svc.correct_attribution(note.id, requests[0]),
            peer.correct_attribution(note.id, requests[1]),
            return_exceptions=True,
        )
        assert sum(isinstance(item, JournalAttributionConflict) for item in results) == 1
        winner = next(i for i, result in enumerate(results) if not isinstance(result, Exception))
    finally:
        await peer_db.close()
    await peer_db.connect()
    try:
        reopened = NoteService(peer_db)
        assert await reopened.correct_attribution(note.id, requests[winner]) == results[winner]
        assert len(await reopened.attribution_history(note.id)) == 1
    finally:
        await peer_db.close()


@pytest.mark.asyncio
async def test_wrong_project_cannot_read_history_replay_or_correct(db):
    svc = NoteService(db)
    note = await svc.create(JournalEntryCreate(content="Private synthetic"))
    first = await svc.correct_attribution(note.id, correction())
    outsider = NoteService(db, project_id="prj_outsider")
    with pytest.raises(NoteNotFoundError):
        await outsider.correct_attribution(note.id, correction())
    with pytest.raises(NoteNotFoundError):
        await outsider.attribution_history(note.id)
    assert await svc.attribution_history(note.id) == [first]


@pytest.mark.asyncio
async def test_ordinary_update_and_direct_sql_cannot_bypass_correction(db):
    svc = NoteService(db)
    note = await svc.create(JournalEntryCreate(content="Synthetic"))
    for field, value in (("source", "pi"), ("verbatim_input", "forged")):
        with pytest.raises(ValidationError, match="correct_note_attribution"):
            JournalEntryUpdate(**{field: value})
        with pytest.raises(JournalAttributionError, match="correct_note_attribution"):
            await svc.update(note.id, JournalEntryUpdate.model_construct(**{field: value}, content="not written"))
        with pytest.raises(sqlite3.IntegrityError, match="revision-guarded"):
            await db.execute(f"UPDATE journal SET {field}=? WHERE id=?", [value, note.id])
    assert await svc.get(note.id) == note
    assert await svc.attribution_history(note.id) == []


@pytest.mark.asyncio
async def test_history_is_immutable_and_nullable_original_can_be_corrected(db):
    svc = NoteService(db)
    note = await svc.create(JournalEntryCreate(content="Synthetic", source="pi"))
    first = await svc.correct_attribution(note.id, correction(source="brain", verbatim_input=None))
    assert first.before_source == "pi" and first.before_verbatim_input is None
    assert first.after_verbatim_input is None
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        await db.execute("UPDATE journal_attribution_revisions SET reason='rewritten' WHERE id=?", [first.id])
    with pytest.raises(sqlite3.IntegrityError, match="project-authorized"):
        await db.execute("DELETE FROM journal_attribution_revisions WHERE id=?", [first.id])
    with pytest.raises(JournalAttributionError, match="must change"):
        await svc.correct_attribution(note.id, correction(
            expected_revision=1, request_id="noop", source="brain", verbatim_input=None,
        ))


@pytest.mark.parametrize("changed", [
    {"reason": " \n"}, {"reason": "x" * 4001}, {"expected_revision": -1},
    {"expected_revision": True}, {"expected_revision": "0"}, {"request_id": ""},
    {"request_id": "bad\n"}, {"request_id": "x" * 129}, {"actor": "admin"},
    {"source": "pi", "verbatim_input": None}, {"source": "pi", "verbatim_input": " \n"},
])
def test_correction_validation(changed):
    with pytest.raises(ValidationError):
        correction(**changed)
