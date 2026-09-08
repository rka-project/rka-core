"""I3a: execution attribution and transactional before/after update evidence."""

import asyncio
import json

import pytest

from rka.models.journal import JournalAttributionCorrection, JournalEntryCreate, JournalEntryUpdate
from rka.services.notes import NoteNotFoundError, NoteService


async def update_audits(db, entry_id):
    return await db.fetchall(
        "SELECT actor, details FROM audit_log "
        "WHERE entity_type='journal' AND entity_id=? AND action='update' ORDER BY id",
        [entry_id],
    )


@pytest.mark.asyncio
async def test_update_preserves_before_after_without_equating_actor_and_source(db):
    service = NoteService(db)
    entry = await service.create(JournalEntryCreate(
        content="Synthetic original", source="pi", verbatim_input="Synthetic quote A",
    ))
    updated = await service.update(
        entry.id, JournalEntryUpdate(content="Synthetic revision"),
        actor="executor",
    )
    assert updated.source == "pi"
    rows = await update_audits(db, entry.id)
    assert len(rows) == 1 and rows[0]["actor"] == "executor"
    details = json.loads(rows[0]["details"])
    assert updated.verbatim_input == "Synthetic quote A"
    assert details["before"] == {"content": "Synthetic original"}
    assert details["after"] == {"content": "Synthetic revision"}
    assert details["actor_basis"] == "caller_asserted"


@pytest.mark.asyncio
async def test_source_correction_keeps_old_assertion_in_audit(db):
    service = NoteService(db)
    entry = await service.create(JournalEntryCreate(content="Synthetic note", source="executor"))
    await service.correct_attribution(entry.id, JournalAttributionCorrection(
        source="brain", verbatim_input=None, actor="pi", expected_revision=0,
        request_id="audit-test", reason="Correct original author",
    ))
    row, = await update_audits(db, entry.id)
    details = json.loads(row["details"])
    assert row["actor"] == "pi"
    assert details["before"] == {"source": "executor", "verbatim_input": None, "capture_mode": "unknown"}
    assert details["after"] == {"source": "brain", "verbatim_input": None, "capture_mode": "unknown"}
    assert details["actor_basis"] == "caller_asserted"


@pytest.mark.asyncio
async def test_legacy_default_is_recorded_as_unknown_execution_attribution(db):
    service = NoteService(db)
    entry = await service.create(JournalEntryCreate(content="Synthetic note", source="pi"))
    await service.update(entry.id, JournalEntryUpdate(status="retracted"))
    row, = await update_audits(db, entry.id)
    assert row["actor"] == "system"
    assert json.loads(row["details"])["actor_basis"] == "legacy_default"


@pytest.mark.asyncio
async def test_tag_only_update_is_audited_with_normalized_stored_values(db):
    service = NoteService(db)
    entry = await service.create(JournalEntryCreate(content="Synthetic note", tags=["before"]))
    updated = await service.update(entry.id, JournalEntryUpdate(tags=[" AFTER ", "after"]), actor="web_ui")
    assert updated.tags == ["after"]
    row, = await update_audits(db, entry.id)
    details = json.loads(row["details"])
    assert row["actor"] == "web_ui"
    assert details["before"] == {"tags": ["before"]}
    assert details["after"] == {"tags": ["after"]}
    assert "tags" in details["fields"]


@pytest.mark.asyncio
async def test_invalid_actor_cannot_partially_change_note_or_tags(db):
    service = NoteService(db)
    entry = await service.create(JournalEntryCreate(content="Synthetic original", tags=["before"]))
    with pytest.raises(ValueError, match="Invalid actor"):
        await service.update(entry.id, JournalEntryUpdate(content="changed", tags=["after"]), actor="admin")
    assert (await service.get(entry.id)).content == "Synthetic original"
    assert (await service.get(entry.id)).tags == ["before"]
    assert await update_audits(db, entry.id) == []


@pytest.mark.asyncio
async def test_audit_failure_rolls_back_note_tags_and_fts(db, monkeypatch):
    service = NoteService(db)
    entry = await service.create(JournalEntryCreate(content="Synthetic original", tags=["before"]))
    original_audit = service.audit

    async def failing_audit(*args, **kwargs):
        await original_audit(*args, **kwargs)
        raise RuntimeError("synthetic audit failure after insert")

    monkeypatch.setattr(service, "audit", failing_audit)
    with pytest.raises(RuntimeError, match="synthetic audit failure"):
        await service.update(entry.id, JournalEntryUpdate(content="replacement", tags=["after"]), actor="executor")
    assert (await service.get(entry.id)).content == "Synthetic original"
    assert (await service.get(entry.id)).tags == ["before"]
    assert (await db.fetchone("SELECT content FROM fts_journal WHERE id=?", [entry.id]))["content"] == "Synthetic original"
    assert await update_audits(db, entry.id) == []


@pytest.mark.asyncio
async def test_concurrent_updates_keep_contiguous_history_and_own_readback(db):
    service = NoteService(db)
    entry = await service.create(JournalEntryCreate(content="Synthetic original"))
    results = await asyncio.gather(*(
        service.update(entry.id, JournalEntryUpdate(content=value), actor="executor")
        for value in ("Synthetic A", "Synthetic B")
    ))
    assert [result.content for result in results] == ["Synthetic A", "Synthetic B"]
    details = [json.loads(row["details"]) for row in await update_audits(db, entry.id)]
    assert len(details) == 2
    assert details[0]["before"]["content"] == "Synthetic original"
    assert details[0]["after"] == details[1]["before"]
    assert details[1]["after"]["content"] == (await service.get(entry.id)).content


@pytest.mark.asyncio
async def test_wrong_project_cannot_update_or_read_previous_values(db):
    service = NoteService(db)
    entry = await service.create(JournalEntryCreate(content="Synthetic private note"))
    outsider = NoteService(db, project_id="prj_unrelated")
    with pytest.raises(NoteNotFoundError):
        await outsider.update(entry.id, JournalEntryUpdate(content="wrong project"), actor="executor")
    assert (await service.get(entry.id)).content == "Synthetic private note"
    assert await update_audits(db, entry.id) == []


@pytest.mark.asyncio
async def test_empty_update_retains_existing_noop_behavior(db):
    service = NoteService(db)
    entry = await service.create(JournalEntryCreate(content="Synthetic original"))
    result = await service.update(entry.id, JournalEntryUpdate(), actor="executor")
    assert result.model_dump() == entry.model_dump()
    assert await update_audits(db, entry.id) == []


@pytest.mark.asyncio
async def test_explicit_execution_actor_also_labels_new_graph_links(db):
    from rka.models.decision import DecisionCreate
    from rka.services.decisions import DecisionService

    decision = await DecisionService(db).create(DecisionCreate(
        question="Synthetic decision?", phase="design", decided_by="brain",
    ))
    service = NoteService(db)
    entry = await service.create(JournalEntryCreate(content="Synthetic note", source="pi"))
    await service.update(entry.id, JournalEntryUpdate(related_decisions=[decision.id]), actor="executor")
    link = await db.fetchone(
        "SELECT created_by FROM entity_links WHERE source_id=? AND target_id=? AND project_id=?",
        [entry.id, decision.id, "proj_default"],
    )
    assert link["created_by"] == "executor"
    assert (await service.get(entry.id)).source == "pi"
