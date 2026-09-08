"""Consistent, atomic backups for live SQLite databases."""

from __future__ import annotations

import errno
import hashlib
import os
import sqlite3
import tempfile
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from contextlib import nullcontext
from rka.infra.runtime_lease import RuntimeLease, lock_directory


@dataclass(frozen=True)
class SQLiteBackupResult:
    """Metadata that can be recorded without exposing database contents."""

    path: Path
    sha256: str
    size_bytes: int
    page_count: int
    foreign_key_violations: int


def protected_sqlite_runtime_paths(source: str | Path) -> frozenset[Path]:
    """Paths a backup/report must never replace for this SQLite database."""

    source_path = Path(source).expanduser().resolve()
    candidates = {
        source_path,
        Path(f"{source_path}-wal"),
        Path(f"{source_path}-shm"),
        Path(f"{source_path}-journal"),
        Path(f"{source_path}.phase2.lock"),
    }
    protected = set(candidates)
    protected.update(candidate.resolve() for candidate in candidates)
    return frozenset(protected)


def is_protected_sqlite_path(source: str | Path, target: str | Path) -> bool:
    """Protect both SQLite sidecars and the entire admission/recovery namespace."""
    resolved = Path(target).expanduser().resolve()
    return (resolved in protected_sqlite_runtime_paths(source)
            or resolved.is_relative_to(lock_directory(source)))


def backup_sqlite_database(
    source: str | Path,
    destination: str | Path,
    *,
    maintenance_lease=None,
) -> SQLiteBackupResult:
    source_path = Path(source).expanduser().resolve()
    if not source_path.is_file():
        raise FileNotFoundError(f"SQLite database not found: {source_path}")
    if maintenance_lease is not None:
        maintenance_lease.assert_owner(source_path)
    elif Path(destination).expanduser().resolve().is_relative_to(lock_directory(source_path)):
        raise ValueError("Backup destination must not replace database runtime files")
    with (nullcontext() if maintenance_lease is not None else RuntimeLease(source_path)):
        return _backup_sqlite_database(source, destination)


def _backup_sqlite_database(source, destination) -> SQLiteBackupResult:
    """Create an integrity-checked snapshot with SQLite's online backup API.

    The source connection is query-only, so this operation can safely snapshot
    a live WAL database without checkpointing or otherwise mutating it. The
    destination is written beside its final path and atomically published only
    after ``PRAGMA integrity_check`` succeeds.
    """

    source_path = Path(source).expanduser().resolve()
    destination_input = Path(destination).expanduser()
    if destination_input.is_symlink():
        raise ValueError("Backup destination must not be a symbolic link")
    destination_path = destination_input.resolve()

    if not source_path.is_file():
        raise FileNotFoundError(f"SQLite database not found: {source_path}")
    if destination_path in protected_sqlite_runtime_paths(source_path):
        raise ValueError(
            "Backup destination must not replace the source database or its runtime files"
        )

    destination_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{destination_path.name}.",
        suffix=".tmp",
        dir=destination_path.parent,
    )
    os.close(fd)
    temporary_path = Path(temporary_name)

    try:
        source_uri = f"{source_path.as_uri()}?mode=ro"
        with (
            closing(
                sqlite3.connect(source_uri, uri=True, timeout=30)
            ) as source_connection,
            closing(sqlite3.connect(temporary_path)) as destination_connection,
        ):
            source_connection.execute("PRAGMA query_only = ON")
            source_connection.backup(destination_connection)
            # A backup inherits the source's persistent WAL journal mode. A
            # standalone copy without its own -wal/-shm sidecars may then be
            # impossible to open read-only. No writer can race on this private
            # temporary destination, so normalize it to a portable single-file
            # DELETE-journal database before validation and publication.
            journal_mode = str(
                destination_connection.execute("PRAGMA journal_mode = DELETE").fetchone()[0]
            ).casefold()
            if journal_mode != "delete":
                raise sqlite3.DatabaseError(
                    f"Backup could not enter portable DELETE journal mode: {journal_mode}"
                )
            integrity_rows = [
                str(row[0])
                for row in destination_connection.execute("PRAGMA integrity_check")
            ]
            if integrity_rows != ["ok"]:
                details = "; ".join(integrity_rows[:5])
                raise sqlite3.DatabaseError(
                    f"Backup failed SQLite integrity_check: {details}"
                )
            foreign_key_violations = sum(
                1 for _row in destination_connection.execute("PRAGMA foreign_key_check")
            )
            page_count = int(
                destination_connection.execute("PRAGMA page_count").fetchone()[0]
            )

        os.chmod(temporary_path, 0o600)
        fsync_file(temporary_path)
        digest = _sha256(temporary_path)
        size_bytes = temporary_path.stat().st_size
        os.replace(temporary_path, destination_path)
        fsync_directory(destination_path.parent)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise

    return SQLiteBackupResult(
        path=destination_path,
        sha256=digest,
        size_bytes=size_bytes,
        page_count=page_count,
        foreign_key_violations=foreign_key_violations,
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fsync_file(path: Path) -> None:
    """Persist a completed file using a Windows-compatible descriptor."""

    # Windows' CRT-backed fsync rejects read-only descriptors with EBADF.
    # The file is already complete, so open it read/write without truncation.
    with path.open("r+b") as output:
        os.fsync(output.fileno())


def fsync_directory(directory: Path) -> None:
    """Persist the atomic rename where directory fsync is supported."""

    # Windows does not provide POSIX directory fsync semantics. Opening a
    # directory through the CRT solely to probe fsync can retain a directory
    # handle after EBADF and prevent a disposable directory from being removed.
    if os.name == "nt":
        return

    try:
        descriptor = os.open(directory, os.O_RDONLY)
    except OSError as exc:
        if exc.errno in _UNSUPPORTED_DIRECTORY_FSYNC_ERRNOS:
            return
        raise
    try:
        os.fsync(descriptor)
    except OSError as exc:
        if exc.errno not in _UNSUPPORTED_DIRECTORY_FSYNC_ERRNOS:
            raise
    finally:
        try:
            os.close(descriptor)
        except OSError as exc:
            if exc.errno not in _UNSUPPORTED_DIRECTORY_FSYNC_ERRNOS:
                raise


_UNSUPPORTED_DIRECTORY_FSYNC_ERRNOS = {
    errno.EBADF,
    errno.EINVAL,
    getattr(errno, "ENOTSUP", errno.EINVAL),
    getattr(errno, "EOPNOTSUPP", errno.EINVAL),
}
