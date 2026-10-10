"""Database-local creation recovery, including ambiguous and interrupted writes."""

import asyncio
import hashlib
import io
import json
import os
import sqlite3
import sys
import zipfile
from pathlib import Path

import pytest
from pydantic import ValidationError

from rka.infra.database import Database
from rka.models.journal import JournalEntryCreate, JournalEntryUpdate
from rka.models.project import ProjectCreate
from rka.services.knowledge_pack import KnowledgePackService
from rka.services.notes import JournalWriteConflict, NoteService
from rka.services.project import ProjectService


def intent(**changes):
    return JournalEntryCreate.model_validate({
        "request_id": "recovery-1", "content": "整理后的指令\r\n  保留空白。",
        "source": "pi", "type": "directive", "verbatim_input": "  原话\r\n",
        "capture_mode": "agent_restatement", "tags": ["recovery"], **changes,
    })


@pytest.mark.asyncio
async def test_snapshot_replay_hashes_and_no_repeated_side_effects(db, monkeypatch):
    from rka.services.hook_dispatcher import HookDispatcher

    fires = []

    async def fire(*args, **kwargs):
        fires.append(kwargs)

    monkeypatch.setattr(HookDispatcher, "fire", fire)
    svc = NoteService(db, embeddings=object())  # enqueue only; never call a provider
    data = intent()
    first = await svc.create(data)
    receipt = await svc.write_receipt(data.request_id)
    assert receipt.entry == first and receipt.journal_id == first.id
    assert receipt.actor == "pi" and receipt.actor_basis == "caller_asserted"
    assert receipt.content_sha256 == hashlib.sha256(data.content.encode()).hexdigest()
    assert receipt.verbatim_input_sha256 == hashlib.sha256(data.verbatim_input.encode()).hexdigest()
    expected = {"version": "journal-create-v1", "project_id": "proj_default", "actor": "pi",
                "payload": data.model_dump(exclude={"request_id"})}
    assert receipt.request_hash == hashlib.sha256(json.dumps(
        expected, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()
    counts = {table: await db.fetchone(f"SELECT COUNT(*) AS n FROM {table}") for table in
              ("journal", "events", "audit_log", "jobs", "change_events", "tags", "entity_links")}
    assert await svc.create(data) == first
    assert {table: await db.fetchone(f"SELECT COUNT(*) AS n FROM {table}") for table in counts} == counts
    assert len(fires) == 1
    assert len(await db.fetchall("SELECT * FROM jobs WHERE entity_id=?", [first.id])) == 1
    await svc.update(first.id, JournalEntryUpdate(content="Later body", tags=["later"]))
    assert await svc.create(data) == first
    assert await svc.write_receipt(data.request_id) == receipt
    assert (await svc.get(first.id)).content == "Later body"


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", [
    {"content": "changed"}, {"verbatim_input": "changed"}, {"source": "brain"},
    {"capture_mode": "unknown"}, {"type": "note"}, {"summary": "new"},
    {"phase": "new"}, {"tags": []}, {"related_decisions": []},
    {"related_literature": []}, {"related_mission": "mis_other"},
    {"supersedes": "jrn_other"}, {"confidence": "tested"},
    {"importance": "high"}, {"status": "draft"}, {"pinned": True},
])
async def test_same_key_different_validated_intent_conflicts_without_mutation(db, changed):
    svc = NoteService(db)
    first = await svc.create(intent())
    with pytest.raises(JournalWriteConflict, match="request_id"):
        await svc.create(intent(**changed))
    assert await svc.list() == [first]
    assert (await svc.write_receipt("recovery-1")).entry == first


@pytest.mark.asyncio
async def test_actor_is_part_of_intent_and_explicit_defaults_replay(db):
    svc = NoteService(db)
    data = JournalEntryCreate(content="Raw exact text\r\n", capture_mode="raw_capture",
                              type="finding", request_id="defaults")
    first = await svc.create(data)
    normalized = JournalEntryCreate.model_validate(data.model_dump())
    assert normalized.type == "note" and normalized.verbatim_input == normalized.content
    assert await svc.create(normalized, actor="executor") == first
    with pytest.raises(JournalWriteConflict):
        await svc.create(normalized, actor="system")
    no_key = JournalEntryCreate(content="Same body")
    a, b = await svc.create(no_key), await svc.create(no_key)
    assert a.id != b.id
    with_key = await svc.create(no_key.model_copy(update={"request_id": "new-intent"}))
    assert with_key.id not in {a.id, b.id}
    assert (await svc.write_receipt("new-intent")).verbatim_input_sha256 is None


@pytest.mark.parametrize("key", ["", "bad\n", " a", "a/b", "a?b", "a#b", "é", "x" * 129, 123])
def test_request_id_validation(key):
    with pytest.raises(ValidationError):
        intent(request_id=key)


@pytest.mark.asyncio
async def test_project_scope_and_no_disclosure_from_other_project(db):
    svc = NoteService(db)
    first = await svc.create(intent())
    await ProjectService(db).create_project(ProjectCreate(id="prj_other", name="Other"))
    peer = NoteService(db, project_id="prj_other")
    assert await peer.write_receipt("recovery-1") is None
    second = await peer.create(intent(content="Other project"))
    assert second.id != first.id
    assert (await peer.write_receipt("recovery-1")).entry == second
    assert (await svc.write_receipt("recovery-1")).entry == first


@pytest.mark.asyncio
@pytest.mark.parametrize("competing", [False, True])
async def test_independent_connections_serialize_and_restart_recovers(db, competing):
    peer_db = Database(db.db_path)
    await peer_db.connect()
    try:
        svc, peer = NoteService(db), NoteService(peer_db)
        requests = [intent(), intent(content="Competitor") if competing else intent()]
        results = await asyncio.gather(svc.create(requests[0]), peer.create(requests[1]),
                                       return_exceptions=True)
        if competing:
            assert sum(isinstance(result, JournalWriteConflict) for result in results) == 1
        else:
            assert results[0] == results[1]
        winner = next(i for i, result in enumerate(results) if not isinstance(result, Exception))
    finally:
        await peer_db.close()
    await peer_db.connect()
    try:
        reopened = NoteService(peer_db)
        assert await reopened.create(requests[winner]) == results[winner]
        assert len(await reopened.list()) == 1
        assert (await reopened.write_receipt("recovery-1")).entry == results[winner]
    finally:
        await peer_db.close()


@pytest.mark.asyncio
async def test_same_connection_concurrent_retries_and_outer_rollback(db):
    svc = NoteService(db)
    with pytest.raises(RuntimeError, match="outer"):
        async with db.transaction():
            first = await svc.create(intent())
            assert (await svc.write_receipt("recovery-1")).entry == first
            raise RuntimeError("outer rollback")
    assert await svc.write_receipt("recovery-1") is None
    assert await svc.list() == []
    results = await asyncio.gather(*(svc.create(intent()) for _ in range(6)))
    assert all(item == results[0] for item in results)
    assert len(await svc.list()) == 1


@pytest.mark.asyncio
async def test_uncommitted_receipt_is_invisible_until_commit(db):
    peer_db = Database(db.db_path)
    await peer_db.connect()
    try:
        peer, svc = NoteService(peer_db), NoteService(db)
        async with db.transaction():
            note = await svc.create(intent())
            assert await peer.write_receipt("recovery-1") is None
            assert await peer.get(note.id) is None
        assert (await peer.write_receipt("recovery-1")).entry == note
        assert await peer.create(intent()) == note
    finally:
        await peer_db.close()


@pytest.mark.asyncio
async def test_invalid_provenance_does_not_reserve_the_key(db):
    from rka.services.base import EntityLinkValidationError

    svc = NoteService(db)
    with pytest.raises(EntityLinkValidationError):
        await svc.create(intent(related_decisions=["dec_missing"]))
    assert await svc.write_receipt("recovery-1") is None
    assert await svc.list() == []
    first = await svc.create(intent())
    assert (await svc.write_receipt("recovery-1")).entry == first


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [RuntimeError, asyncio.CancelledError])
async def test_failure_after_receipt_insert_rolls_back_entire_aggregate(db, monkeypatch, failure):
    svc = NoteService(db, embeddings=object())
    old = await svc.create(JournalEntryCreate(content="Before replacement"))
    counts = {table: await db.fetchone(f"SELECT COUNT(*) AS n FROM {table}") for table in
              ("journal", "events", "audit_log", "jobs", "change_events", "entity_links", "tags")}
    original = db.execute

    async def injected(sql, parameters=()):
        result = await original(sql, parameters)
        if "INSERT INTO journal_write_receipts" in sql:
            raise failure("after receipt insert")
        return result

    monkeypatch.setattr(db, "execute", injected)
    with pytest.raises(failure):
        await svc.create(intent(supersedes=old.id))
    assert await svc.get(old.id) == old
    assert await svc.write_receipt("recovery-1") is None
    assert {table: await db.fetchone(f"SELECT COUNT(*) AS n FROM {table}") for table in counts} == counts
    assert await db.fetchone("SELECT id FROM fts_journal WHERE content MATCH '整理后的指令'") is None
    monkeypatch.setattr(db, "execute", original)
    created = await svc.create(intent(supersedes=old.id))
    assert (await svc.get(old.id)).superseded_by == created.id
    assert await svc.create(intent(supersedes=old.id)) == created


@pytest.mark.asyncio
@pytest.mark.parametrize("crash_before_commit", [False, True])
async def test_hard_process_exit_and_lost_response(tmp_path, crash_before_commit):
    db_path = tmp_path / "crash.db"
    # Exit without clean connection shutdown or returning a receipt to the
    # caller. Uses only synthetic data in the test directory, no live services.
    script = """
import asyncio, os, sys
from rka.infra.database import Database
from rka.models.journal import JournalEntryCreate
from rka.services.notes import NoteService
async def main():
    db = Database(sys.argv[1])
    await db.connect()
    await db.initialize_schema()
    await db.initialize_phase2_schema()
    original = db.execute
    async def execute(sql, params=()):
        result = await original(sql, params)
        if sys.argv[2] == 'before' and 'INSERT INTO journal_write_receipts' in sql:
            os._exit(23)
        return result
    db.execute = execute
    await NoteService(db).create(JournalEntryCreate(content='Crash recovery', request_id='crash-1'))
    os._exit(24)
asyncio.run(main())
"""
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-c", script, str(db_path), "before" if crash_before_commit else "after",
        env={**os.environ, "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"},
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(process.communicate(), timeout=45)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    assert process.returncode == (23 if crash_before_commit else 24), stderr.decode()
    db = Database(str(db_path))
    await db.connect()
    try:
        svc = NoteService(db)
        before = await svc.write_receipt("crash-1")
        assert (before is None) == crash_before_commit
        result = await svc.create(JournalEntryCreate(content="Crash recovery", request_id="crash-1"))
        assert (await svc.write_receipt("crash-1")).entry == result
        assert len(await svc.list()) == 1
        if before is not None:
            assert before.entry == result
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_receipt_is_immutable_survives_note_deletion_and_project_cleanup_is_scoped(db):
    projects = ProjectService(db)
    await projects.create_project(ProjectCreate(id="prj_receipts", name="Receipts"))
    svc = NoteService(db, project_id="prj_receipts")
    first = await svc.create(intent())
    default = await NoteService(db).create(intent())
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        await db.execute("UPDATE journal_write_receipts SET actor='brain'")
    with pytest.raises(sqlite3.IntegrityError, match="project-authorized"):
        await db.execute("DELETE FROM journal_write_receipts")
    await db.execute("DELETE FROM journal WHERE id=?", [first.id])
    await db.commit()
    assert await svc.create(intent()) == first
    assert await svc.get(first.id) is None  # replay must not recreate deleted data
    await projects.delete_project("prj_receipts", confirm=True)
    assert await svc.write_receipt("recovery-1") is None
    assert (await NoteService(db).write_receipt("recovery-1")).entry == default
    assert await db.fetchall("PRAGMA foreign_key_check") == []


@pytest.mark.asyncio
async def test_knowledge_pack_does_not_clone_operational_receipts(db_with_project):
    db = db_with_project
    svc = NoteService(db)
    await svc.create(intent())
    path, _ = await KnowledgePackService(db).export_pack(include_logs=True)
    with zipfile.ZipFile(path) as archive:
        manifest = json.loads(archive.read("manifest.json"))
    assert "journal_write_receipts" not in manifest["tables"]
    pack_bytes = await asyncio.to_thread(Path(path).read_bytes)
    await KnowledgePackService(db).import_pack(io.BytesIO(pack_bytes), project_id="prj_copy", project_name="Copy")
    imported = NoteService(db, project_id="prj_copy")
    assert len(await imported.list()) == 1
    assert await imported.write_receipt("recovery-1") is None
    assert await svc.write_receipt("recovery-1") is not None
