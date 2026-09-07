"""Core security regression tests using only synthetic, temporary records."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rka.models.hooks import HookCreate
from rka.models.journal import JournalEntryCreate
from rka.services.hook_dispatcher import HookDispatcher
from rka.services.hooks_service import HooksService
from rka.services.knowledge_pack import KnowledgePackService
from rka.services.notes import NoteService


async def seed_legacy_hook(db, handler_type, *, event="periodic", config=None, enabled=True):
    hook_id = f"hk_legacy_{handler_type}"
    await db.execute(
        """INSERT INTO hooks
           (id, event, project_id, handler_type, handler_config, enabled, name, created_by)
           VALUES (?, ?, 'proj_default', ?, ?, ?, 'legacy fixture', 'pi')""",
        [hook_id, event, handler_type, json.dumps(config or {}), int(enabled)],
    )
    await db.commit()
    return hook_id


@pytest.mark.asyncio
@pytest.mark.parametrize("handler_type", ["sql", "mcp_tool"])
@pytest.mark.parametrize("enabled", [True, False])
async def test_service_rejects_unsupported_registration_without_writing(db, handler_type, enabled):
    with pytest.raises(ValueError, match="unsupported"):
        await HooksService(db).add(
            HookCreate(
                event="periodic",
                handler_type=handler_type,
                handler_config={},
                name="blocked",
                enabled=enabled,
            )
        )
    assert (await db.fetchone("SELECT count(*) AS n FROM hooks"))["n"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("handler_type", ["sql", "mcp_tool"])
async def test_legacy_hook_read_disable_allowed_but_reenable_rejected(db, handler_type):
    hook_id = await seed_legacy_hook(db, handler_type)
    service = HooksService(db)
    assert (await service.get(hook_id)).handler_type == handler_type
    assert (await service.set_enabled(hook_id, False)).enabled is False
    with pytest.raises(ValueError, match="unsupported"):
        await service.set_enabled(hook_id, True)
    assert (await service.get(hook_id)).enabled is False


@pytest.mark.asyncio
async def test_legacy_sql_cannot_write_another_projects_record(db):
    await db.execute("INSERT INTO projects (id, name) VALUES ('prj_other', 'other')")
    await db.commit()
    note = await NoteService(db, project_id="prj_other").create(
        JournalEntryCreate(content="original other-project note")
    )
    hook_id = await seed_legacy_hook(
        db,
        "sql",
        config={
            "statement": "UPDATE journal SET content=? WHERE id=?",
            "params": ["tampered", note.id],
        },
    )
    ids = await HookDispatcher(db).fire("periodic", {}, "proj_default")
    assert len(ids) == 1
    assert (await db.fetchone("SELECT content FROM journal WHERE id=?", [note.id]))[
        "content"
    ] == note.content
    execution = await HooksService(db).list_executions(hook_id=hook_id)
    assert execution[0].status == "error"
    assert "unsupported" in execution[0].error_message
    assert execution[0].handler_result is None


@pytest.mark.asyncio
@pytest.mark.parametrize("handler_type", ["sql", "mcp_tool"])
async def test_legacy_handler_block_does_not_break_journal_create(db, handler_type):
    hook_id = await seed_legacy_hook(
        db,
        handler_type,
        event="post_journal_create",
        config={
            "statement": "DELETE FROM journal",
            "tool": "rka_add_note",
            "args": {},
        },
    )
    note = await NoteService(db).create(JournalEntryCreate(content="must survive"))
    assert await NoteService(db).get(note.id) is not None
    executions = await HooksService(db).list_executions(hook_id=hook_id)
    assert len(executions) == 1
    assert executions[0].status == "error"
    assert executions[0].handler_result is None


@pytest.mark.asyncio
async def test_blocked_legacy_hook_log_obeys_enclosing_transaction_rollback(db):
    await seed_legacy_hook(
        db,
        "sql",
        config={
            "statement": "INSERT INTO audit_log(action, entity_type, actor) VALUES ('injected', 'journal', 'system')",
        },
    )
    with pytest.raises(RuntimeError, match="roll back"):
        async with db.transaction():
            await HookDispatcher(db).fire("periodic", {}, "proj_default")
            raise RuntimeError("roll back")
    assert (await db.fetchone("SELECT count(*) AS n FROM hook_executions"))["n"] == 0
    assert (await db.fetchone("SELECT count(*) AS n FROM audit_log WHERE action='injected'"))[
        "n"
    ] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("handler_type", ["sql", "mcp_tool"])
async def test_pack_round_trip_preserves_legacy_hook_without_granting_execution(db, handler_type):
    await NoteService(db).create(JournalEntryCreate(content="portable original"))
    original_id = await seed_legacy_hook(
        db,
        handler_type,
        config={
            "statement": "UPDATE journal SET content='tampered'",
            "tool": "rka_add_note",
        },
    )
    pack_path, _ = await KnowledgePackService(db).export_pack()
    try:
        with Path(pack_path).open("rb") as packed:
            await KnowledgePackService(db).import_pack(
                packed,
                project_id="prj_copy",
                project_name="Isolated copy",
            )
    finally:
        Path(pack_path).unlink()

    service = HooksService(db, project_id="prj_copy")
    (imported,) = await service.list_hooks()
    assert imported.id != original_id
    assert imported.handler_type == handler_type
    assert imported.enabled is True  # Import preserves data, not permission to run.
    await HookDispatcher(db).fire("periodic", {}, "prj_copy")
    executions = await service.list_executions(hook_id=imported.id)
    assert len(executions) == 1
    assert executions[0].status == "error"
    assert executions[0].handler_result is None
    assert "unsupported" in executions[0].error_message
    notes = await db.fetchall("SELECT content FROM journal")
    assert len(notes) == 2
    assert all(note["content"] == "portable original" for note in notes)
    assert (await service.set_enabled(imported.id, False)).enabled is False
