"""Upgrade unknown legacy attribution without fabricating history."""

import shutil
from pathlib import Path

import pytest

from rka.infra.database import Database


@pytest.mark.asyncio
async def test_upgrade_055_preserves_unknown_original_and_is_repeatable(tmp_path, monkeypatch):
    source = Path(__file__).parents[2] / "rka/db/migrations"
    before = tmp_path / "before"
    before.mkdir()
    for migration in source.glob("*.sql"):
        if not migration.name.startswith("._") and int(migration.name.split("_", 1)[0]) <= 55:
            shutil.copy2(migration, before / migration.name)
    monkeypatch.setattr(Database, "_migrations_directory", staticmethod(lambda: before))
    db = Database(str(tmp_path / "legacy.db"))
    await db.connect()
    try:
        await db.initialize_schema()
        await db.initialize_phase2_schema()
        await db.execute(
            "INSERT INTO journal (id, project_id, type, content, source, confidence) "
            "VALUES ('jrn_legacy', 'proj_default', 'note', 'Exact legacy bytes', 'pi', 'tested')"
        )
        previous = await db.fetchone("SELECT * FROM journal WHERE id='jrn_legacy'")
        after = tmp_path / "after"
        after.mkdir()
        for migration in source.glob("*.sql"):
            if not migration.name.startswith("._") and int(migration.name.split("_", 1)[0]) <= 56:
                shutil.copy2(migration, after / migration.name)
        monkeypatch.setattr(Database, "_migrations_directory", staticmethod(lambda: after))
        assert await db.run_migrations() == 1
        current = await db.fetchone("SELECT * FROM journal WHERE id='jrn_legacy'")
        assert current == {**previous, "attribution_revision": 0}
        assert current["verbatim_input"] is None
        assert await db.fetchall("SELECT * FROM journal_attribution_revisions") == []
        assert await db.fetchall("PRAGMA foreign_key_check") == []
        assert await db.run_migrations() == 0
    finally:
        await db.close()
