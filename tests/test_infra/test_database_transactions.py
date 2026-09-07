"""Managed transaction tests for the SQLite database wrapper."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from rka.infra.database import Database


async def _create_items_table(db: Database) -> None:
    await db.execute(
        "CREATE TABLE transaction_items (id INTEGER PRIMARY KEY, value TEXT NOT NULL)"
    )
    await db.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("migration_lock", [False, True])
async def test_cancellation_during_begin_rolls_back_before_unlock(db, monkeypatch, migration_lock):
    original = db.conn.execute
    begun = asyncio.Event()

    async def paused_begin(sql, *args, **kw):
        cursor = await original(sql, *args, **kw)
        if sql.startswith("BEGIN"):
            begun.set()
            await asyncio.Event().wait()
        return cursor

    monkeypatch.setattr(db.conn, "execute", paused_begin)

    async def work():
        async with db.transaction(migration_lock=migration_lock):
            raise AssertionError("cancelled BEGIN must not enter the transaction body")

    task = asyncio.create_task(work())
    await asyncio.wait_for(begun.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not db.conn.in_transaction
    assert db._transaction_owner is None
    monkeypatch.setattr(db.conn, "execute", original)
    async with db.transaction():
        assert await db.fetchone("SELECT 1 AS healthy") == {"healthy": 1}


@pytest.mark.asyncio
async def test_repeated_cancellation_does_not_release_connection_before_rollback(db, monkeypatch):
    original = db.conn.rollback
    body, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def paused_rollback():
        cleaning.set()
        await release.wait()
        await original()

    monkeypatch.setattr(db.conn, "rollback", paused_rollback)

    async def work():
        async with db.transaction():
            body.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(work())
    try:
        await asyncio.wait_for(body.wait(), 2)
        task.cancel()
        await asyncio.wait_for(cleaning.wait(), 2)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        assert db._transaction_owner is task
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert not db.conn.in_transaction and db._transaction_owner is None


@pytest.mark.asyncio
async def test_outer_transaction_commits(db: Database) -> None:
    await _create_items_table(db)

    async with db.transaction():
        await db.execute(
            "INSERT INTO transaction_items (id, value) VALUES (1, 'committed')"
        )

    row = await db.fetchone("SELECT value FROM transaction_items WHERE id = 1")
    assert row == {"value": "committed"}
    assert db._transaction_depth == 0
    assert db._transaction_owner is None


@pytest.mark.asyncio
async def test_outer_transaction_rolls_back_and_resets_state(db: Database) -> None:
    await _create_items_table(db)

    with pytest.raises(RuntimeError, match="abort outer"):
        async with db.transaction():
            await db.execute(
                "INSERT INTO transaction_items (id, value) VALUES (1, 'rolled back')"
            )
            raise RuntimeError("abort outer")

    assert await db.fetchall("SELECT * FROM transaction_items") == []
    assert not db.conn.in_transaction
    assert db._transaction_depth == 0
    assert db._transaction_owner is None

    # A failed transaction must not poison the next one.
    async with db.transaction():
        await db.execute(
            "INSERT INTO transaction_items (id, value) VALUES (2, 'next works')"
        )
    assert await db.fetchone(
        "SELECT value FROM transaction_items WHERE id = 2"
    ) == {"value": "next works"}


@pytest.mark.asyncio
async def test_nested_transaction_commits_with_outer(db: Database) -> None:
    await _create_items_table(db)

    async with db.transaction():
        await db.execute(
            "INSERT INTO transaction_items (id, value) VALUES (1, 'outer')"
        )
        async with db.transaction():
            await db.execute(
                "INSERT INTO transaction_items (id, value) VALUES (2, 'inner')"
            )

    rows = await db.fetchall("SELECT id, value FROM transaction_items ORDER BY id")
    assert rows == [
        {"id": 1, "value": "outer"},
        {"id": 2, "value": "inner"},
    ]


@pytest.mark.asyncio
async def test_handled_nested_failure_rolls_back_only_savepoint(db: Database) -> None:
    await _create_items_table(db)

    async with db.transaction():
        await db.execute(
            "INSERT INTO transaction_items (id, value) VALUES (1, 'before')"
        )
        with pytest.raises(ValueError, match="abort inner"):
            async with db.transaction():
                await db.execute(
                    "INSERT INTO transaction_items (id, value) VALUES (2, 'inner')"
                )
                raise ValueError("abort inner")
        assert db._transaction_depth == 1
        await db.execute(
            "INSERT INTO transaction_items (id, value) VALUES (3, 'after')"
        )

    rows = await db.fetchall("SELECT id, value FROM transaction_items ORDER BY id")
    assert rows == [
        {"id": 1, "value": "before"},
        {"id": 3, "value": "after"},
    ]


@pytest.mark.asyncio
async def test_outer_failure_rolls_back_released_savepoint(db: Database) -> None:
    await _create_items_table(db)

    with pytest.raises(ValueError, match="abort all"):
        async with db.transaction():
            async with db.transaction():
                await db.execute(
                    "INSERT INTO transaction_items (id, value) VALUES (1, 'inner')"
                )
            raise ValueError("abort all")

    assert await db.fetchall("SELECT * FROM transaction_items") == []


@pytest.mark.asyncio
async def test_helper_commit_is_deferred_inside_transaction(db: Database) -> None:
    await _create_items_table(db)

    with pytest.raises(RuntimeError, match="after helper commit"):
        async with db.transaction():
            await db.execute(
                "INSERT INTO transaction_items (id, value) VALUES (1, 'still atomic')"
            )
            await db.commit()
            raise RuntimeError("after helper commit")

    assert await db.fetchall("SELECT * FROM transaction_items") == []


@pytest.mark.asyncio
async def test_commit_failure_rolls_back_and_resets_state(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _create_items_table(db)
    real_commit = db.conn.commit

    async def failing_commit() -> None:
        raise RuntimeError("simulated commit failure")

    monkeypatch.setattr(db.conn, "commit", failing_commit)
    with pytest.raises(RuntimeError, match="simulated commit failure"):
        async with db.transaction():
            await db.execute(
                "INSERT INTO transaction_items (id, value) VALUES (1, 'not durable')"
            )
    monkeypatch.setattr(db.conn, "commit", real_commit)

    assert not db.conn.in_transaction
    assert db._transaction_depth == 0
    assert db._transaction_owner is None
    assert await db.fetchall("SELECT * FROM transaction_items") == []


@pytest.mark.asyncio
async def test_cancellation_rolls_back_and_resets_state(db: Database) -> None:
    await _create_items_table(db)
    entered = asyncio.Event()
    never_released = asyncio.Event()

    async def worker() -> None:
        async with db.transaction():
            await db.execute(
                "INSERT INTO transaction_items (id, value) VALUES (1, 'cancelled')"
            )
            entered.set()
            await never_released.wait()

    task = asyncio.create_task(worker())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert not db.conn.in_transaction
    assert db._transaction_depth == 0
    assert db._transaction_owner is None
    assert await db.fetchall("SELECT * FROM transaction_items") == []


@pytest.mark.asyncio
async def test_concurrent_transactions_do_not_interleave(db: Database) -> None:
    await _create_items_table(db)
    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    second_attempted = asyncio.Event()
    second_entered = asyncio.Event()

    async def first() -> None:
        async with db.transaction():
            await db.execute(
                "INSERT INTO transaction_items (id, value) VALUES (1, 'first')"
            )
            first_entered.set()
            await release_first.wait()

    async def second() -> None:
        await first_entered.wait()
        second_attempted.set()
        async with db.transaction():
            second_entered.set()
            await db.execute(
                "INSERT INTO transaction_items (id, value) VALUES (2, 'second')"
            )

    first_task = asyncio.create_task(first())
    second_task = asyncio.create_task(second())
    await second_attempted.wait()
    # Give the second task an opportunity to acquire the connection lock.  It
    # must remain outside until the first transaction exits.
    await asyncio.sleep(0)
    assert not second_entered.is_set()

    release_first.set()
    await asyncio.gather(first_task, second_task)
    assert second_entered.is_set()
    assert await db.fetchall(
        "SELECT id, value FROM transaction_items ORDER BY id"
    ) == [
        {"id": 1, "value": "first"},
        {"id": 2, "value": "second"},
    ]


@pytest.mark.asyncio
async def test_standalone_write_autocommits_without_leaking_transaction(
    db: Database,
) -> None:
    await _create_items_table(db)
    await db.execute(
        "INSERT INTO transaction_items (id, value) VALUES (1, 'standalone')"
    )

    assert not db.conn.in_transaction
    async with db.transaction():
        await db.execute(
            "INSERT INTO transaction_items (id, value) VALUES (2, 'managed')"
        )

    assert await db.fetchall(
        "SELECT id FROM transaction_items ORDER BY id"
    ) == [{"id": 1}, {"id": 2}]


@pytest.mark.asyncio
async def test_raw_transaction_control_is_rejected(db: Database) -> None:
    with pytest.raises(RuntimeError, match="Database.transaction"):
        await db.execute("BEGIN")
    with pytest.raises(RuntimeError, match="Database.transaction"):
        await db.execute("ROLLBACK")
    with pytest.raises(RuntimeError, match="Database.transaction"):
        await db.execute("-- caller comment\nBEGIN IMMEDIATE")
    with pytest.raises(RuntimeError, match="Database.transaction"):
        await db.execute("SAVEPOINT caller_owned")
    with pytest.raises(RuntimeError, match="Database.transaction"):
        await db.execute("ROLLBACK TO caller_owned")
    with pytest.raises(RuntimeError, match="Database.transaction"):
        await db.execute("RELEASE caller_owned")
    with pytest.raises(RuntimeError, match="Database.transaction"):
        await db.fetchone("BEGIN")
    with pytest.raises(RuntimeError, match="Database.transaction"):
        await db.fetchall("SAVEPOINT caller_owned")
    with pytest.raises(RuntimeError, match="Database.transaction"):
        await db.executemany("COMMIT", [[]])


@pytest.mark.asyncio
async def test_write_transaction_reserves_lock_before_snapshot(
    tmp_path: Path,
) -> None:
    db_path = str(tmp_path / "write-lock.db")
    first = Database(db_path)
    second = Database(db_path)
    await first.connect()
    await second.connect()
    try:
        await first.execute(
            "CREATE TABLE counters (id INTEGER PRIMARY KEY, value INTEGER NOT NULL)"
        )
        await first.execute("INSERT INTO counters VALUES (1, 0)")
        await first.commit()

        first_has_snapshot = asyncio.Event()
        allow_first_write = asyncio.Event()
        second_entered = asyncio.Event()

        async def first_writer() -> None:
            async with first.transaction():
                assert await first.fetchone(
                    "SELECT value FROM counters WHERE id = 1"
                ) == {"value": 0}
                first_has_snapshot.set()
                await allow_first_write.wait()
                await first.execute(
                    "UPDATE counters SET value = value + 1 WHERE id = 1"
                )

        async def second_writer() -> None:
            await first_has_snapshot.wait()
            async with second.transaction():
                second_entered.set()
                await second.execute(
                    "UPDATE counters SET value = value + 1 WHERE id = 1"
                )

        first_task = asyncio.create_task(first_writer())
        second_task = asyncio.create_task(second_writer())
        await first_has_snapshot.wait()
        await asyncio.sleep(0)
        assert not second_entered.is_set()

        allow_first_write.set()
        await asyncio.gather(first_task, second_task)
        assert await first.fetchone(
            "SELECT value FROM counters WHERE id = 1"
        ) == {"value": 2}
    finally:
        await first.close()
        await second.close()


@pytest.mark.asyncio
async def test_migration_waits_for_runtime_upgrade_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "001_runtime_ledger.sql").write_text(
        "CREATE TABLE runtime_schema_upgrades ("
        "name TEXT PRIMARY KEY, completed_at TEXT, details TEXT);\n",
        encoding="utf-8",
    )
    (migrations / "002_partition_dependent.sql").write_text(
        "-- requires-runtime-upgrade: 053_vec_project_filters_v1\n"
        "CREATE TABLE partition_dependent (id INTEGER PRIMARY KEY);\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        Database, "_migrations_directory", staticmethod(lambda: migrations)
    )

    database = Database(str(tmp_path / "runtime-prerequisite.db"))
    await database.connect()
    try:
        assert await database.run_migrations() == 1
        assert await database.fetchone(
            "SELECT 1 FROM sqlite_master "
            "WHERE type = 'table' AND name = 'partition_dependent'"
        ) is None

        await database.execute(
            "INSERT INTO runtime_schema_upgrades (name) VALUES (?)",
            ["053_vec_project_filters_v1"],
        )
        await database.commit()

        assert await database.run_migrations() == 1
        assert await database.fetchone(
            "SELECT 1 AS present FROM sqlite_master "
            "WHERE type = 'table' AND name = 'partition_dependent'"
        ) == {"present": 1}
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_migration_failure_rolls_back_schema_and_ledger_then_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    migration = migrations / "001_atomic_probe.sql"
    migration.write_text(
        "CREATE TABLE migration_probe (id INTEGER PRIMARY KEY);\n"
        "INSERT INTO migration_probe (id) VALUES (1);\n"
        "INSERT INTO missing_table (id) VALUES (1);\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        Database, "_migrations_directory", staticmethod(lambda: migrations)
    )

    database = Database(str(tmp_path / "atomic-migration.db"))
    await database.connect()
    try:
        with pytest.raises(sqlite3.OperationalError, match="missing_table"):
            await database.run_migrations()

        assert await database.fetchone(
            "SELECT 1 FROM sqlite_master "
            "WHERE type = 'table' AND name = 'migration_probe'"
        ) is None
        assert await database.fetchone(
            "SELECT 1 FROM schema_migrations "
            "WHERE filename = '001_atomic_probe.sql'"
        ) is None
        assert not database.conn.in_transaction

        migration.write_text(
            "CREATE TABLE migration_probe (id INTEGER PRIMARY KEY);\n"
            "INSERT INTO migration_probe (id) VALUES (1);\n",
            encoding="utf-8",
        )
        assert await database.run_migrations() == 1
        assert await database.fetchone(
            "SELECT id FROM migration_probe"
        ) == {"id": 1}
        assert await database.fetchone(
            "SELECT filename FROM schema_migrations "
            "WHERE filename = '001_atomic_probe.sql'"
        ) == {"filename": "001_atomic_probe.sql"}
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_concurrent_migration_runners_apply_file_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "001_once.sql").write_text(
        "CREATE TABLE migration_once (id INTEGER PRIMARY KEY);\n"
        "INSERT INTO migration_once (id) VALUES (1);\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        Database, "_migrations_directory", staticmethod(lambda: migrations)
    )

    db_path = str(tmp_path / "concurrent-migration.db")
    first = Database(db_path)
    second = Database(db_path)
    await first.connect()
    await second.connect()
    try:
        applied = await asyncio.gather(
            first.run_migrations(),
            second.run_migrations(),
        )
        assert sorted(applied) == [0, 1]
        assert await first.fetchall("SELECT id FROM migration_once") == [{"id": 1}]
        assert await first.fetchall(
            "SELECT filename FROM schema_migrations"
        ) == [{"filename": "001_once.sql"}]
    finally:
        await first.close()
        await second.close()


@pytest.mark.asyncio
async def test_migration_retries_past_connection_busy_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "001_wait.sql").write_text(
        "CREATE TABLE migration_waited (id INTEGER PRIMARY KEY);\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        Database, "_migrations_directory", staticmethod(lambda: migrations)
    )
    monkeypatch.setenv("RKA_MIGRATION_LOCK_TIMEOUT_MS", "1000")

    db_path = str(tmp_path / "migration-wait.db")
    lock_holder = Database(db_path)
    migrator = Database(db_path)
    await lock_holder.connect()
    await migrator.connect()
    try:
        # Force each SQLite lock attempt to fail quickly so the migration-level
        # retry loop, rather than the connection-wide timeout, is exercised.
        await migrator.conn.execute("PRAGMA busy_timeout = 20")
        await lock_holder.conn.execute("BEGIN IMMEDIATE")

        task = asyncio.create_task(migrator.run_migrations())
        await asyncio.sleep(0.1)
        assert not task.done()
        await lock_holder.conn.rollback()

        assert await task == 1
        assert await migrator.fetchone(
            "SELECT 1 FROM sqlite_master "
            "WHERE type = 'table' AND name = 'migration_waited'"
        ) == {"1": 1}
    finally:
        if lock_holder.conn.in_transaction:
            await lock_holder.conn.rollback()
        await lock_holder.close()
        await migrator.close()
