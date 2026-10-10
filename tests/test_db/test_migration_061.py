"""Add the retry ledger without changing or inventing historical records."""

import shutil
from pathlib import Path

import pytest

from rka.infra.database import Database


@pytest.mark.asyncio
async def test_upgrade_from_059_is_additive_and_repeatable(tmp_path, monkeypatch):
    source = Path(__file__).parents[2] / "rka/db/migrations"
    before = tmp_path / "before"
    before.mkdir()
    for migration in source.glob("*.sql"):
        if not migration.name.startswith("._") and int(migration.name.split("_", 1)[0]) <= 59:
            shutil.copy2(migration, before / migration.name)
    monkeypatch.setattr(Database, "_migrations_directory", staticmethod(lambda: before))
    db = Database(str(tmp_path / "legacy.db"))
    await db.connect()
    try:
        await db.initialize_schema()
        await db.initialize_phase2_schema()
        await db.execute("INSERT INTO journal (id, project_id, type, content, source, confidence) "
                         "VALUES ('jrn_old', 'proj_default', 'note', 'Exact legacy text', 'pi', 'tested')")
        await db.commit()
        old = await db.fetchone("SELECT * FROM journal WHERE id='jrn_old'")
        # Upgrade this fixture by this migration alone; future independent
        # migrations must not change what this test is asserting.
        shutil.copy2(source / "061_journal_write_receipts.sql", before / "061_journal_write_receipts.sql")
        assert await db.run_migrations() == 1
        assert await db.fetchone("SELECT * FROM journal WHERE id='jrn_old'") == old
        assert await db.fetchall("SELECT * FROM journal_write_receipts") == []
        assert await db.fetchall("PRAGMA foreign_key_check") == []
        assert await db.run_migrations() == 0
    finally:
        await db.close()
