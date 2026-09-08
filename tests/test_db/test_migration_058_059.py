"""Upgrading review/dependency behavior must preserve existing research rows."""

import shutil
from pathlib import Path

from rka.infra.database import Database


async def test_upgrade_057_preserves_dispositions_and_does_not_infer_dependencies(
    tmp_path, monkeypatch
):
    source = Path(__file__).parents[2] / "rka/db/migrations"
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    for path in source.glob("*.sql"):
        if not path.name.startswith("._") and int(path.name.split("_", 1)[0]) <= 57:
            shutil.copy2(path, migrations / path.name)
    monkeypatch.setattr(Database, "_migrations_directory", staticmethod(lambda: migrations))
    db = Database(str(tmp_path / "legacy.db"))
    await db.connect()
    try:
        await db.initialize_schema()
        await db.initialize_phase2_schema()
        await db.execute(
            "INSERT INTO journal (id, project_id, type, content, source) VALUES ('jrn_history', 'proj_default', 'directive', 'original directive', 'pi')"
        )
        await db.execute("""INSERT INTO claims (id, project_id, source_entry_id, content, claim_type,
            stale, staleness, staleness_reviewed_at, staleness_verdict, staleness_resolution, staleness_resolved_by)
            VALUES ('clm_history', 'proj_default', 'jrn_history', 'original claim', 'evidence',
                    1, 'green', '2000-01-01T00:00:00Z', 'historical', 'legacy review', 'pi')""")
        before_note = await db.fetchone("SELECT * FROM journal WHERE id = 'jrn_history'")
        before_claim = await db.fetchone("SELECT * FROM claims WHERE id = 'clm_history'")
        for name in ("058_reopen_staleness_review.sql", "059_directive_dependencies.sql"):
            shutil.copy2(source / name, migrations / name)
        assert await db.run_migrations() == 2
        assert await db.fetchone("SELECT * FROM journal WHERE id = 'jrn_history'") == before_note
        assert await db.fetchone("SELECT * FROM claims WHERE id = 'clm_history'") == before_claim
        assert not await db.fetchall("SELECT * FROM directive_dependencies")
        assert await db.run_migrations() == 0
        assert not await db.fetchall("PRAGMA foreign_key_check")
        await db.execute("UPDATE claims SET stale = 1 WHERE id = 'clm_history'")
        after = await db.fetchone("SELECT * FROM claims WHERE id = 'clm_history'")
        assert after["staleness_reviewed_at"] is None
        assert after["content"] == before_claim["content"] and after["stale"] == 1
    finally:
        await db.close()
