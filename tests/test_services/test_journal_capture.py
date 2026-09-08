"""I3c: preserve evidence separately from the mutable journal body."""

import sqlite3

import pytest
from pydantic import ValidationError

from rka.models.journal import JournalAttributionCorrection, JournalEntryCreate, JournalEntryUpdate
from rka.services.academic import AcademicImportService
from rka.services.literature import LiteratureService
from rka.services.notes import JournalAttributionConflict, JournalAttributionError, NoteService


def correction(**overrides):
    return JournalAttributionCorrection(
        **{"source": "brain", "verbatim_input": "  Original\r\n原文\n",
           "expected_revision": 0, "request_id": "capture-1", "actor": "executor",
           "reason": "Reviewed capture method", **overrides},
    )


@pytest.mark.asyncio
async def test_raw_capture_preserves_original_across_body_edits_and_reopen(db):
    notes = NoteService(db)
    raw = "  Original\r\n原文\n"
    note = await notes.create(JournalEntryCreate(content=raw, source="pi", capture_mode="raw_capture"))
    assert note.verbatim_input == raw and note.capture_mode == "raw_capture"
    updated = await notes.update(note.id, JournalEntryUpdate(content="Agent's edited body"))
    assert updated.verbatim_input == raw and updated.attribution_revision == 0
    await db.close()
    await db.connect()
    assert (await notes.get(note.id)).verbatim_input == raw


@pytest.mark.parametrize("mode", ["raw_capture", "agent_restatement"])
def test_explicit_modes_validate_pi_originals_without_stripping(mode):
    original = "  exact quote\r\n"
    data = JournalEntryCreate(content="Derived display", source="pi", capture_mode=mode,
                              verbatim_input=original)
    assert data.verbatim_input == original
    with pytest.raises(ValidationError):
        JournalEntryCreate(content="  ", source="pi", capture_mode=mode, verbatim_input=" \n")
    with pytest.raises(ValidationError):
        JournalAttributionCorrection(**correction(capture_mode=mode).model_dump(), unexpected=True)


@pytest.mark.asyncio
async def test_unknown_is_not_inferred_from_source_quote_or_equal_text(db):
    for source, original in (("pi", None), ("pi", "body"), ("executor", "body")):
        note = await NoteService(db).create(JournalEntryCreate(
            content="body", source=source, verbatim_input=original,
        ))
        assert note.capture_mode == "unknown"
        assert note.verbatim_input == original
    with pytest.raises(ValidationError):
        await NoteService(db).create(JournalEntryCreate.model_construct(
            content="restatement", source="pi", capture_mode="agent_restatement",
        ))


@pytest.mark.asyncio
async def test_mode_only_correction_is_audited_and_retry_preserves_original_intent(db):
    notes = NoteService(db)
    request = correction(capture_mode="raw_capture")
    note = await notes.create(JournalEntryCreate(content="body", source="brain",
                                                verbatim_input=request.verbatim_input))
    first = await notes.correct_attribution(note.id, request)
    assert first.before_capture_mode == "unknown" and first.after_capture_mode == "raw_capture"
    assert (await notes.get(note.id)).capture_mode == "raw_capture"
    with pytest.raises(JournalAttributionConflict, match="request_id"):
        await notes.correct_attribution(note.id, request.model_copy(update={"capture_mode": "unknown"}))
    second_request = correction(expected_revision=1, request_id="capture-2", source="executor")
    second = await notes.correct_attribution(note.id, second_request)
    assert second.after_capture_mode == "raw_capture"  # omission preserves
    await notes.correct_attribution(note.id, correction(
        expected_revision=2, request_id="capture-3", source="executor", capture_mode="unknown",
    ))
    assert await notes.correct_attribution(note.id, second_request) == second
    assert await notes.correct_attribution(note.id, request) == first
    with pytest.raises(ValidationError):
        JournalEntryUpdate(capture_mode="raw_capture")
    with pytest.raises(sqlite3.IntegrityError, match="revision-guarded"):
        await db.execute("UPDATE journal SET capture_mode='agent_restatement' WHERE id=?", [note.id])


@pytest.mark.asyncio
async def test_correction_cannot_invent_raw_original_from_edited_body(db):
    notes = NoteService(db)
    note = await notes.create(JournalEntryCreate(content="Not an original", capture_mode="raw_capture"))
    with pytest.raises(JournalAttributionError, match="raw_capture"):
        await notes.correct_attribution(note.id, correction(verbatim_input=None))
    assert await notes.attribution_history(note.id) == []
    assert await notes.get(note.id) == note
    with pytest.raises(ValidationError, match="raw_capture"):
        correction(capture_mode="raw_capture", verbatim_input=None)


@pytest.mark.asyncio
@pytest.mark.parametrize("split", [True, False])
async def test_document_ingest_stores_exact_input_not_formatted_body(db, split):
    notes = NoteService(db)
    svc = AcademicImportService(LiteratureService(db), note_service=notes)
    raw = " \r\n## Findings\r\n  原文 first  \r\n\r\n### Next\n  second  \n"
    result = await svc.ingest_document(raw, source="pi", split_by_headings=split)
    assert result["errors"] == []
    entries = [await notes.get(item["id"]) for item in result["created"]]
    assert len(entries) == (2 if split else 1)
    assert "".join(entry.verbatim_input for entry in entries) == raw
    assert all(entry.capture_mode == "raw_capture" and entry.source == "pi" for entry in entries)


@pytest.mark.asyncio
async def test_invalid_direct_insert_cannot_claim_capture_without_original(db):
    with pytest.raises(sqlite3.IntegrityError, match="original text"):
        await db.execute(
            "INSERT INTO journal (id, project_id, type, content, source, confidence, capture_mode) "
            "VALUES ('jrn_bad_capture', 'proj_default', 'note', 'body', 'pi', 'tested', 'raw_capture')"
        )
