"""Regression tests for consistent SQLite backups."""

from __future__ import annotations

import errno
import sqlite3
import stat
from pathlib import Path

import pytest

from rka.infra import sqlite_backup
from rka.infra.sqlite_backup import backup_sqlite_database, fsync_directory, fsync_file, is_protected_sqlite_path


def test_backup_includes_committed_rows_still_in_wal(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    destination = tmp_path / "backup.db"

    with sqlite3.connect(source) as writer:
        assert writer.execute("PRAGMA journal_mode = WAL").fetchone()[0] == "wal"
        writer.execute("PRAGMA wal_autocheckpoint = 0")
        writer.execute("CREATE TABLE records (id INTEGER PRIMARY KEY, value TEXT)")
        writer.commit()
        writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        writer.execute("INSERT INTO records(value) VALUES ('committed-in-wal')")
        writer.commit()

        wal_path = Path(f"{source}-wal")
        assert wal_path.stat().st_size > 0
        result = backup_sqlite_database(source, destination)

    with sqlite3.connect(destination) as restored:
        assert restored.execute("SELECT value FROM records").fetchall() == [
            ("committed-in-wal",)
        ]
        assert restored.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert restored.execute("PRAGMA journal_mode").fetchone()[0] == "delete"

    assert result.path == destination.resolve()
    assert result.size_bytes == destination.stat().st_size
    assert len(result.sha256) == 64
    assert result.page_count > 0
    assert result.foreign_key_violations == 0
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600


def test_backup_closes_connections_before_atomic_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.db"
    destination = tmp_path / "backup.db"
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE records (id INTEGER PRIMARY KEY)")

    opened_connections: list[sqlite3.Connection] = []
    real_connect = sqlite3.connect

    def tracked_connect(*args, **kwargs):
        connection = real_connect(*args, **kwargs)
        opened_connections.append(connection)
        return connection

    monkeypatch.setattr(sqlite_backup.sqlite3, "connect", tracked_connect)

    backup_sqlite_database(source, destination)

    assert len(opened_connections) == 2
    for connection in opened_connections:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            connection.execute("SELECT 1")


def test_directory_fsync_ignores_unsupported_windows_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unsupported(_descriptor: int) -> None:
        raise OSError(errno.EBADF, "Bad file descriptor")

    monkeypatch.setattr(sqlite_backup.os, "name", "posix")
    monkeypatch.setattr(sqlite_backup.os, "open", lambda *_args: 99)
    monkeypatch.setattr(sqlite_backup.os, "fsync", unsupported)
    monkeypatch.setattr(sqlite_backup.os, "close", unsupported)

    fsync_directory(tmp_path)


def test_directory_fsync_does_not_open_windows_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unexpected_open(*_args):
        raise AssertionError("Windows directory fsync must not open a descriptor")

    monkeypatch.setattr(sqlite_backup.os, "name", "nt")
    monkeypatch.setattr(sqlite_backup.os, "open", unexpected_open)

    fsync_directory(tmp_path)


def test_file_fsync_uses_writable_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "completed.bin"
    target.write_bytes(b"complete")
    real_open = Path.open
    modes: list[str] = []

    def tracked_open(path: Path, mode: str = "r", *args, **kwargs):
        modes.append(mode)
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", tracked_open)

    fsync_file(target)

    assert modes == ["r+b"]
    assert target.read_bytes() == b"complete"


def test_backup_rejects_source_as_destination(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE records (id INTEGER PRIMARY KEY)")

    with pytest.raises(ValueError, match="must not replace"):
        backup_sqlite_database(source, source)


@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal", ".phase2.lock"])
def test_backup_rejects_sqlite_runtime_paths(tmp_path: Path, suffix: str) -> None:
    source = tmp_path / "source.db"
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE records (id INTEGER PRIMARY KEY)")
    protected = Path(f"{source}{suffix}")
    protected.write_bytes(b"runtime-state")

    with pytest.raises(ValueError, match="runtime files"):
        backup_sqlite_database(source, protected)

    assert protected.read_bytes() == b"runtime-state"


@pytest.mark.parametrize("use_alias_path", [False, True])
def test_backup_rejects_runtime_symlink_and_its_target(
    tmp_path: Path,
    use_alias_path: bool,
) -> None:
    source = tmp_path / "source.db"
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE records (id INTEGER PRIMARY KEY)")
    victim = tmp_path / "victim.bin"
    victim.write_bytes(b"do-not-replace")
    sidecar_alias = Path(f"{source}-shm")
    sidecar_alias.symlink_to(victim)
    destination = sidecar_alias if use_alias_path else victim

    with pytest.raises(ValueError):
        backup_sqlite_database(source, destination)

    assert victim.read_bytes() == b"do-not-replace"
    assert sidecar_alias.is_symlink()


def test_failed_backup_preserves_existing_destination(tmp_path: Path) -> None:
    source = tmp_path / "corrupt.db"
    destination = tmp_path / "existing.db"
    source.write_bytes(b"not a sqlite database")
    destination.write_bytes(b"known-good-destination")

    with pytest.raises(sqlite3.DatabaseError):
        backup_sqlite_database(source, destination)

    assert destination.read_bytes() == b"known-good-destination"
    assert list(tmp_path.glob(".existing.db.*.tmp")) == []
def test_runtime_and_recovery_namespace_is_protected(tmp_path):
    source = tmp_path / "db.sqlite"
    assert is_protected_sqlite_path(source, tmp_path / "db.sqlite.runtime-locks" / "gate.lock")
    assert is_protected_sqlite_path(source, tmp_path / "db.sqlite.runtime-locks" / "some-backup" / "original.json")
    assert not is_protected_sqlite_path(source, tmp_path / "report.json")
