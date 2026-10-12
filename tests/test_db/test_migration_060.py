"""Partial readiness migration preserves the pre-upgrade singleton verbatim."""

import shutil
import sqlite3
import struct
from pathlib import Path

import pytest

from rka.infra.database import Database
from rka.infra.embedding_documents import embedding_content_hash
from rka.models.journal import JournalEntryCreate
from rka.services.embedding_reshape import current_vec_table_dim
from rka.services.notes import NoteService


@pytest.mark.parametrize("status", [None, "ready", "reindexing", "failed"])
def test_partial_status_upgrade_preserves_state_and_constraints(status):
    migrations = Path(__file__).parents[2] / "rka/db/migrations"
    with sqlite3.connect(":memory:") as db:
        db.executescript((migrations / "055_add_embedding_index_state.sql").read_text())
        if status:
            db.execute("INSERT INTO embedding_index_state VALUES (1, 2, 'space', 'model', 1024, ?, 'error retained', '2026-01-01')", [status])
        before = db.execute("SELECT * FROM embedding_index_state").fetchall()
        db.executescript((migrations / "060_partial_embedding_index.sql").read_text())
        assert db.execute("SELECT * FROM embedding_index_state").fetchall() == before
        if status:
            db.execute("UPDATE embedding_index_state SET status='partial'")
            with pytest.raises(sqlite3.IntegrityError):
                db.execute("UPDATE embedding_index_state SET status='unknown'")
            with pytest.raises(sqlite3.IntegrityError):
                db.execute("UPDATE embedding_index_state SET singleton=2")
        assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)


@pytest.mark.asyncio
@pytest.mark.parametrize("partial_already_applied", [False, True])
async def test_060_and_061_coexist_without_rewriting_records_or_receipts(
    tmp_path, monkeypatch, partial_already_applied,
):
    """Cover main's late 060 upgrade and the deployed 060+061 overlay layout."""
    source = Path(__file__).parents[2] / "rka/db/migrations"
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    partial_name = "060_partial_embedding_index.sql"
    for migration in source.glob("*.sql"):
        if migration.name.startswith("._") or int(migration.name.split("_", 1)[0]) > 61:
            continue
        if migration.name != partial_name or partial_already_applied:
            shutil.copy2(migration, migrations / migration.name)
    monkeypatch.setattr(Database, "_migrations_directory", staticmethod(lambda: migrations))
    db = Database(str(tmp_path / "upgrade.db"))
    await db.connect()
    try:
        await db.initialize_schema()
        await db.initialize_phase2_schema()
        assert db.vec_available
        notes = NoteService(db)
        intent = JournalEntryCreate(
            request_id="before-partial-upgrade", type="directive", source="pi",
            content="Exact synthetic 原话\r\n  Keep spacing.\n", capture_mode="raw_capture",
            tags=["rwf-v0", "rwf-stop-pivot"],
        )
        note = await notes.create(intent)
        receipt = await notes.write_receipt(intent.request_id)
        dim = await current_vec_table_dim(db, "vec_journal")
        await db.execute(
            "INSERT INTO embedding_index_state VALUES "
            "(1, 2, 'synthetic-space', 'synthetic-model', ?, ?, 'error retained', '2026-01-01')",
            [dim, "partial" if partial_already_applied else "failed"],
        )
        await db.execute(
            "INSERT INTO embedding_metadata "
            "(entity_type, entity_id, project_id, model_name, dimensions, content_hash) "
            "VALUES ('journal', ?, 'proj_default', 'synthetic-model', ?, ?)",
            [note.id, dim, embedding_content_hash(note.content.strip())],
        )
        await db.execute(
            "INSERT INTO vec_journal (id, project_id, embedding) VALUES (?, 'proj_default', ?)",
            [note.id, struct.pack(f"{dim}f", 1.0, *([0.0] * (dim - 1)))],
        )
        await db.commit()
        queries = {
            "journal": "SELECT * FROM journal ORDER BY id",
            "tags": "SELECT * FROM tags ORDER BY tag",
            "receipts": "SELECT * FROM journal_write_receipts ORDER BY request_id",
            "metadata": "SELECT * FROM embedding_metadata ORDER BY entity_id",
            "vectors": "SELECT id, project_id, embedding FROM vec_journal ORDER BY id",
            "index": "SELECT * FROM embedding_index_state",
        }
        before = {key: await db.fetchall(sql) for key, sql in queries.items()}
        old_ledger = await db.fetchall("SELECT * FROM schema_migrations ORDER BY filename")
        shutil.copy2(source / partial_name, migrations / partial_name)
        assert await db.run_migrations() == (0 if partial_already_applied else 1)
        assert {key: await db.fetchall(sql) for key, sql in queries.items()} == before
        # Reopen the synthetic file and exercise normal startup, not just raw SQL.
        await db.close()
        await db.connect()
        await db.initialize_schema()
        await db.initialize_phase2_schema()
        assert await db.run_migrations() == 0
        assert {key: await db.fetchall(sql) for key, sql in queries.items()} == before
        new_ledger = await db.fetchall("SELECT * FROM schema_migrations ORDER BY filename")
        assert all(row in new_ledger for row in old_ledger)
        assert sum(row["filename"] == partial_name for row in new_ledger) == 1
        assert sum(row["filename"] == "061_journal_write_receipts.sql" for row in new_ledger) == 1
        assert await notes.create(intent) == note  # retry is still exactly-once
        assert await notes.write_receipt(intent.request_id) == receipt
        assert await notes.list(source="pi", tags=["rwf-stop-pivot", "rwf-v0"]) == [note]
        assert await notes.list(source="pi", tags=["rwf-stop-pivot", "missing"]) == []
        assert await notes.list(source="executor", tags=["rwf-v0"]) == []
        assert await db.fetchall("PRAGMA foreign_key_check") == []
        assert (await db.fetchone("PRAGMA integrity_check"))["integrity_check"] == "ok"
    finally:
        await db.close()
