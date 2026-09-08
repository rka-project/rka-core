"""No startup mutation, correct live WAL reads, bounded/closed read handles."""

import sqlite3
from pathlib import Path

import pytest

from rka.infra.readonly_sqlite import readonly_sqlite


@pytest.mark.asyncio
async def test_read_snapshot_sees_committed_wal_but_not_later_commits(tmp_path):
    source = tmp_path / "snapshot.db"
    writer = sqlite3.connect(source, isolation_level=None)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("CREATE TABLE samples (value INTEGER)")
        writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        writer.execute("INSERT INTO samples VALUES (1)")
        assert Path(str(source) + "-wal").stat().st_size > 0
        async with readonly_sqlite(source) as reader:
            assert await reader.fetchall("SELECT value FROM samples") == [{"value": 1}]
            writer.execute("INSERT INTO samples VALUES (2)")
            assert await reader.fetchall("SELECT value FROM samples") == [{"value": 1}]
        async with readonly_sqlite(source) as reader:
            assert await reader.fetchall("SELECT value FROM samples ORDER BY value") == [{"value": 1}, {"value": 2}]
    finally:
        writer.close()


@pytest.mark.asyncio
async def test_readonly_connection_rejects_writes_and_attach(tmp_path):
    source = tmp_path / "read only # 汉.db"
    with sqlite3.connect(source) as writer:
        writer.execute("CREATE TABLE samples (value INTEGER)")
    async with readonly_sqlite(source) as reader:
        for sql in ("INSERT INTO samples VALUES (1)", "CREATE TABLE forbidden(value INTEGER)"):
            with pytest.raises(sqlite3.OperationalError):
                await reader.fetchall(sql)
        target = tmp_path / "never-created.db"
        with pytest.raises(sqlite3.DatabaseError):
            await reader.fetchall("ATTACH DATABASE ? AS forbidden", [str(target)])
        assert not target.exists()
        assert await reader.fetchall("SELECT value FROM samples") == []
    with pytest.raises(ValueError, match="closed|active"):
        await reader.fetchall("SELECT 1")


@pytest.mark.asyncio
async def test_missing_database_is_not_created(tmp_path):
    source = tmp_path / "missing" / "database.db"
    with pytest.raises(FileNotFoundError):
        async with readonly_sqlite(source):
            pytest.fail("must not open a missing database")
    assert not source.parent.exists()


@pytest.mark.asyncio
async def test_reader_closes_on_failure(tmp_path):
    source = tmp_path / "close.db"
    with sqlite3.connect(source) as writer:
        writer.execute("CREATE TABLE samples(value)")
    with pytest.raises(RuntimeError, match="injected"):
        async with readonly_sqlite(source) as reader:
            raise RuntimeError("injected")
    with pytest.raises(ValueError, match="closed|active"):
        await reader.fetchall("SELECT 1")


@pytest.mark.asyncio
async def test_inspection_reads_are_row_size_bounded(tmp_path):
    source = tmp_path / "oversize.db"
    with sqlite3.connect(source) as writer:
        writer.execute("CREATE TABLE samples(value)")
        writer.execute("INSERT INTO samples VALUES (?)", ["x" * (2 * 1024 * 1024 + 1)])
    async with readonly_sqlite(source) as reader:
        with pytest.raises(sqlite3.DataError):
            await reader.fetchall("SELECT value FROM samples")


def test_fixed_image_artifact_precedes_incompatible_wrapper(tmp_path, monkeypatch):
    from rka.infra import readonly_sqlite as module
    artifact = tmp_path / "vec0.so"
    artifact.touch()
    monkeypatch.setattr(module, "_CORE_IMAGE_VEC", artifact)
    calls = []
    class Connection:
        def enable_load_extension(self, value):
            calls.append(value)
        def load_extension(self, path):
            calls.append(path)
    assert module._load_vec(Connection())
    assert calls == [True, str(artifact), False]
