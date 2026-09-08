"""Bounded read transactions without normal RKA startup or migrations.

mode=ro preserves WAL visibility and SQLite locking. Do not use immutable=1 or
nolock=1 for an inspection that may overlap a live writer. SQLite may create or
update WAL shared-memory sidecars; no canonical rows/schema/permissions change.
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite
import sqlite3
from rka.infra.runtime_lease import RuntimeLease, finish_before_cancellation


class ReadOnlySQLite:
    def __init__(self, connection, *, vec_available: bool):
        self._connection = connection
        self.vec_available = vec_available

    async def fetchall(self, sql, params=()):
        async with self._connection.execute(sql, params) as cursor:
            return [dict(row) for row in await cursor.fetchall()]

    async def fetchone(self, sql, params=()):
        async with self._connection.execute(sql, params) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row is not None else None


_CORE_IMAGE_VEC = Path("/usr/local/lib/vec0.so")


def _load_vec(connection) -> bool:
    """Load installed sqlite-vec or Core's fixed image artifact, never data paths."""
    try:
        connection.enable_load_extension(True)
        # The Core image's pinned artifact takes precedence over a possibly
        # incompatible Python wheel (notably Linux ARM64). No env/data path.
        if _CORE_IMAGE_VEC.is_file() and not _CORE_IMAGE_VEC.is_symlink():
            try:
                connection.load_extension(str(_CORE_IMAGE_VEC))
                return True
            except sqlite3.Error:
                pass
        try:
            import sqlite_vec
        except ImportError:
            return False
        else:
            sqlite_vec.load(connection)
        return True
    except (AttributeError, sqlite3.Error):
        return False
    finally:
        if hasattr(connection, "enable_load_extension"):
            connection.enable_load_extension(False)


def _read_authorizer(action, arg1, arg2, database, trigger):
    # query_only and mode=ro protect rows, but ATTACH would unnecessarily open
    # another filesystem target. No inspection query needs that capability.
    if action in {sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH}:
        return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_FUNCTION and (arg2 or "").lower() == "load_extension":
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


@asynccontextmanager
async def readonly_sqlite(path: Path, *, timeout_seconds: float = 30, maintenance_lease=None):
    """Yield a read snapshot with a query deadline and 2 MiB row-value ceiling."""
    source = path.expanduser().resolve(strict=True)
    if not source.is_file():
        raise ValueError("inspection requires an existing database file")
    # as_uri quotes ?, # and Windows drive paths rather than treating them as
    # SQLite URI switches. No directory creation/chmod/normal Database.connect.
    lease = None
    connection = None
    if maintenance_lease is not None:
        maintenance_lease.assert_owner(source)
    else:
        lease = RuntimeLease(source).acquire()
    async def open_connection():
        nonlocal connection
        connection = await aiosqlite.connect(
            source.as_uri() + "?mode=ro", uri=True, isolation_level=None, timeout=2,
        )
    try:
        await finish_before_cancellation(open_connection())
        connection.row_factory = aiosqlite.Row
        await connection.execute("PRAGMA query_only = ON")
        await connection.execute("PRAGMA trusted_schema = OFF")
        await connection._execute(connection._conn.setlimit, sqlite3.SQLITE_LIMIT_LENGTH, 2 * 1024 * 1024)
        deadline = time.monotonic() + timeout_seconds
        await connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        available = await connection._execute(_load_vec, connection._conn)
        await connection._execute(connection._conn.set_authorizer, _read_authorizer)
        await connection.execute("BEGIN")
        # Pin a snapshot now, not on some later lazily executed source scan.
        async with connection.execute("SELECT count(*) FROM sqlite_schema") as cursor:
            await cursor.fetchone()
        yield ReadOnlySQLite(connection, vec_available=available)
    finally:
        # Closing releases the read transaction even after cancellation or a
        # malformed schema. No COMMIT, checkpoint, schema initialization or DML.
        async def close_connection():
            if connection is not None:
                await connection.close()
            if lease is not None:
                lease.close()
        await finish_before_cancellation(close_connection())
