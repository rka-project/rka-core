"""Attribution history is canonical knowledge, not optional audit logs."""

import io
import json
import zipfile

import pytest

from rka.models.journal import JournalAttributionCorrection, JournalEntryCreate
from rka.services.knowledge_pack import KnowledgePackIntegrityError, KnowledgePackService
from rka.services.notes import NoteService
from rka.services.project import ProjectService


async def make_pack(db):
    notes = NoteService(db)
    note = await notes.create(JournalEntryCreate(content="Synthetic original"))
    request = JournalAttributionCorrection(
        source="pi", verbatim_input=f"  Keep {note.id} literally.\n",
        capture_mode="raw_capture",
        expected_revision=0, request_id="portable-request", actor="executor",
        reason=f"Checked original {note.id}",
    )
    first = await notes.correct_attribution(note.id, request)
    second = await notes.correct_attribution(note.id, request.model_copy(update={
        "request_id": "portable-request-2", "expected_revision": 1,
        "capture_mode": "agent_restatement",
        "verbatim_input": f"A later exact quotation of {note.id}",
    }))
    path, _ = await KnowledgePackService(db).export_pack()
    return path, note, request, first, second


@pytest.mark.asyncio
async def test_default_pack_preserves_history_exact_originals_retries_and_deletion(db_with_project):
    db = db_with_project
    path, note, request, first, second = await make_pack(db)
    with zipfile.ZipFile(path) as archive:
        manifest = json.loads(archive.read("manifest.json"))
    assert "audit_log" not in manifest["tables"]
    assert len(manifest["tables"]["journal_attribution_revisions"]) == 2
    with open(path, "rb") as source:
        result = await KnowledgePackService(db).import_pack(
            source, project_id="prj_attribution_import", project_name="Imported attribution",
        )
    assert result.imported_counts["journal_attribution_revisions"] == 2
    imported = NoteService(db, project_id="prj_attribution_import")
    imported_note, = await imported.list()
    assert imported_note.id != note.id
    assert imported_note.attribution_revision == 2
    assert imported_note.capture_mode == "agent_restatement"
    assert imported_note.verbatim_input == second.after_verbatim_input
    history = await imported.attribution_history(imported_note.id)
    assert history[0].id != first.id and history[1].id != second.id
    assert history[0].reason == first.reason
    assert history[0].after_verbatim_input == first.after_verbatim_input
    assert history[1].before_verbatim_input == second.before_verbatim_input
    assert history[0].before_capture_mode == "unknown"
    assert history[0].after_capture_mode == history[1].before_capture_mode == "raw_capture"
    assert history[1].after_capture_mode == "agent_restatement"
    assert await imported.correct_attribution(imported_note.id, request) == history[0]
    assert await KnowledgePackService(db).check_integrity("prj_attribution_import") == []
    await ProjectService(db).delete_project("prj_attribution_import", confirm=True)
    assert await db.fetchall("SELECT id FROM journal_attribution_revisions WHERE project_id='prj_attribution_import'") == []
    assert len(await NoteService(db).attribution_history(note.id)) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["missing_history", "broken_chain", "wrong_head",
                                  "capture_chain", "capture_head"])
async def test_import_rejects_incomplete_or_inconsistent_history_atomically(db_with_project, damage):
    db = db_with_project
    path, *_ = await make_pack(db)
    with zipfile.ZipFile(path) as archive:
        manifest = json.loads(archive.read("manifest.json"))
    history = manifest["tables"]["journal_attribution_revisions"]
    if damage == "missing_history":
        history.clear()
    elif damage == "broken_chain":
        history[1]["before_verbatim_input"] = "Tampered predecessor"
    elif damage == "capture_chain":
        history[1]["before_capture_mode"] = "unknown"
    elif damage == "capture_head":
        manifest["tables"]["journal"][0]["capture_mode"] = "unknown"
    else:
        manifest["tables"]["journal"][0]["verbatim_input"] = "Tampered current head"
    manifest["table_counts"]["journal_attribution_revisions"] = len(history)
    blob = io.BytesIO()
    with zipfile.ZipFile(blob, "w") as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
    blob.seek(0)
    with pytest.raises(KnowledgePackIntegrityError, match="journal_attribution_history_invalid"):
        await KnowledgePackService(db).import_pack(
            blob, project_id="prj_rejected", project_name="Must roll back",
        )
    assert await db.fetchone("SELECT id FROM projects WHERE id='prj_rejected'") is None
    assert await db.fetchall("SELECT id FROM journal WHERE project_id='prj_rejected'") == []
    assert await db.fetchall("SELECT id FROM journal_attribution_revisions WHERE project_id='prj_rejected'") == []


@pytest.mark.asyncio
async def test_pre_capture_pack_remains_unknown_with_retryable_history(db_with_project):
    db = db_with_project
    path, _, request, *_ = await make_pack(db)
    with zipfile.ZipFile(path) as archive:
        manifest = json.loads(archive.read("manifest.json"))
    for row in manifest["tables"]["journal"]:
        row.pop("capture_mode")
    for row in manifest["tables"]["journal_attribution_revisions"]:
        row.pop("before_capture_mode")
        row.pop("after_capture_mode")
    blob = io.BytesIO()
    with zipfile.ZipFile(blob, "w") as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
    blob.seek(0)
    await KnowledgePackService(db).import_pack(blob, project_id="prj_old_capture_pack", project_name="Old pack")
    notes = NoteService(db, project_id="prj_old_capture_pack")
    note, = await notes.list()
    assert note.capture_mode == "unknown"
    first, second = await notes.attribution_history(note.id)
    assert first.after_capture_mode == second.before_capture_mode == second.after_capture_mode == "unknown"
    assert await notes.correct_attribution(note.id, request.model_copy(update={"capture_mode": None})) == first
