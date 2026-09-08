"""Capture classification must not rewrite historical evidence or corrections."""

import shutil
from pathlib import Path

import pytest

from rka.infra.database import Database


@pytest.mark.asyncio
async def test_upgrade_056_preserves_legacy_rows_history_and_is_repeatable(tmp_path, monkeypatch):
    source = Path(__file__).parents[2] / "rka/db/migrations"
    before = tmp_path / "before"
    before.mkdir()
    for migration in source.glob("*.sql"):
        if not migration.name.startswith("._") and int(migration.name.split("_", 1)[0]) <= 56:
            shutil.copy2(migration, before / migration.name)
    monkeypatch.setattr(Database, "_migrations_directory", staticmethod(lambda: before))
    db = Database(str(tmp_path / "legacy.db"))
    await db.connect()
    try:
        await db.initialize_schema()
        await db.initialize_phase2_schema()
        for note_id, source_name, original in (
            ("jrn_unknown", "pi", None), ("jrn_empty", "pi", ""),
            ("jrn_equal", "pi", "legacy"), ("jrn_corrected", "brain", "原文\r\n"),
        ):
            await db.execute(
                "INSERT INTO journal (id, project_id, type, content, source, confidence, verbatim_input) "
                "VALUES (?, 'proj_default', 'note', 'legacy', ?, 'tested', ?)",
                [note_id, source_name, original],
            )
        await db.execute(
            """INSERT INTO journal_attribution_revisions
            (id, project_id, journal_id, revision, expected_revision, request_id, actor,
             actor_basis, reason, before_source, before_verbatim_input, after_source, after_verbatim_input)
            VALUES ('jar_old', 'proj_default', 'jrn_corrected', 1, 0, 'old-request', 'executor',
                    'caller_asserted', 'Historical reason', 'brain', ?, 'pi', ?)""",
            ["原文\r\n", "Corrected\r\n"],
        )
        await db.execute(
            "UPDATE journal SET source='pi', verbatim_input=?, attribution_revision=1 "
            "WHERE id='jrn_corrected'", ["Corrected\r\n"],
        )
        rows_before = await db.fetchall("SELECT * FROM journal ORDER BY id")
        events_before = await db.fetchall("SELECT * FROM journal_attribution_revisions")
        # This test isolates 056 -> 057; later migrations have their own gates.
        shutil.copy2(source / "057_journal_capture_mode.sql", before / "057_journal_capture_mode.sql")
        assert await db.run_migrations() == 1
        assert await db.fetchall("SELECT * FROM journal ORDER BY id") == [
            {**row, "capture_mode": "unknown"} for row in rows_before
        ]
        assert await db.fetchall("SELECT * FROM journal_attribution_revisions") == [
            {**row, "before_capture_mode": "unknown", "after_capture_mode": "unknown"}
            for row in events_before
        ]
        assert await db.run_migrations() == 0
        assert await db.fetchall("PRAGMA foreign_key_check") == []
    finally:
        await db.close()
