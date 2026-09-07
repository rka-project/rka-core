"""SQLite database connection and initialization."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

import aiosqlite

from rka.infra.file_lock import release_exclusive, try_acquire_exclusive

logger = logging.getLogger(__name__)


class Database:
    """Async SQLite database wrapper."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._conn: aiosqlite.Connection | None = None
        self._vec_loaded = False
        # aiosqlite serializes individual calls on its worker thread, but that
        # alone does not keep another coroutine from interleaving statements in
        # a multi-statement transaction.  Hold this lock for the lifetime of a
        # managed transaction and for each standalone wrapper operation.
        self._access_lock = asyncio.Lock()
        self._transaction_owner: asyncio.Task[object] | None = None
        self._transaction_depth = 0
        self._savepoint_counter = 0
        self._phase2_memory_lock = asyncio.Lock()

    async def connect(self) -> None:
        """Open database connection and apply PRAGMAs."""
        db_file: Path | None = None
        if self.db_path != ":memory:" and not self.db_path.startswith("file:"):
            db_file = Path(self.db_path).expanduser()
            try:
                db_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                fd = os.open(db_file, os.O_CREAT | os.O_RDWR, 0o600)
                os.close(fd)
                os.chmod(db_file, 0o600)
            except OSError as exc:
                raise sqlite3.OperationalError(
                    f"cannot prepare private RKA database at {db_file}: {exc}"
                ) from exc

        # Standalone statements run in SQLite autocommit mode. Multi-statement
        # mutations must opt into ``transaction()`` explicitly; otherwise an
        # implicit transaction could outlive the calling request and an
        # unrelated coroutine's ``commit()`` could make its partial work
        # durable.
        self._conn = await aiosqlite.connect(
            self.db_path,
            isolation_level=None,
        )
        self._conn.row_factory = aiosqlite.Row
        # Enable loading extensions (needed for sqlite-vec)
        await self._conn.execute("PRAGMA journal_mode = WAL")
        await self._conn.execute("PRAGMA foreign_keys = ON")
        await self._conn.execute("PRAGMA busy_timeout = 5000")
        if db_file is not None:
            for candidate in (db_file, Path(f"{db_file}-wal"), Path(f"{db_file}-shm")):
                try:
                    os.chmod(candidate, 0o600)
                except FileNotFoundError:
                    pass
        # Allow extension loading for sqlite-vec (must run on aiosqlite's thread)
        try:
            await self._conn._execute(self._conn._conn.enable_load_extension, True)
        except (AttributeError, Exception):
            pass  # Some Python builds don't support this

    async def close(self) -> None:
        """Close database connection."""
        if self._conn:
            await self._conn.close()
            self._conn = None

    async def initialize_schema(self) -> None:
        """Create tables from schema.sql if they don't exist, then run migrations."""
        schema_path = Path(__file__).parent.parent / "db" / "schema.sql"
        schema_sql = schema_path.read_text()
        await self._conn.executescript(schema_sql)
        await self._conn.commit()
        await self.run_migrations()

    async def run_migrations(self) -> int:
        """Run pending SQL migrations from rka/db/migrations/.

        Each migration and its tracking-row insert are committed as one SQLite
        transaction.  The connection-wide access lock serializes migration
        runners in this process; ``BEGIN IMMEDIATE`` also serializes writers
        across processes before any schema change is attempted.

        Returns the number of newly applied migrations.
        """
        migrations_dir = self._migrations_directory()
        if not migrations_dir.exists():
            return 0

        # Gather .sql files sorted by name. Ignore macOS AppleDouble/resource-fork
        # sidecars that can appear on external or synced volumes.
        sql_files = sorted(
            f
            for f in migrations_dir.iterdir()
            if f.suffix == ".sql" and not f.name.startswith("._")
        )
        if not sql_files:
            return 0

        task = asyncio.current_task()
        if self._transaction_owner is task:
            raise RuntimeError(
                "Cannot run migrations inside an application-managed transaction"
            )

        count = 0
        async with self._access_lock:
            conn = self.conn
            if conn.in_transaction:
                raise RuntimeError(
                    "Cannot run migrations while an unmanaged transaction is active"
                )

            # Ensure the tracking table exists.  Each migration re-reads its
            # ledger row after BEGIN IMMEDIATE because another process may have
            # applied the file while this process was waiting for the lock.
            try:
                await self._begin_migration_transaction(conn)
                await conn.execute(
                    "CREATE TABLE IF NOT EXISTS schema_migrations ("
                    "  filename TEXT PRIMARY KEY,"
                    "  applied_at TEXT NOT NULL DEFAULT "
                    "(strftime('%Y-%m-%dT%H:%M:%SZ','now'))"
                    ")"
                )
                await conn.commit()
            except BaseException:
                if conn.in_transaction:
                    await conn.rollback()
                raise

            for sql_file in sql_files:
                sql = sql_file.read_text()
                required_tables = self._migration_required_tables(sql)
                if required_tables and not await self._tables_exist_on_connection(
                    conn, required_tables
                ):
                    logger.info(
                        "Skipping migration %s (waiting for tables: %s)",
                        sql_file.name,
                        ", ".join(required_tables),
                    )
                    continue

                required_upgrades = self._migration_required_runtime_upgrades(sql)
                if required_upgrades and not await self._runtime_upgrades_exist_on_connection(
                    conn, required_upgrades
                ):
                    logger.info(
                        "Skipping migration %s (waiting for runtime upgrades: %s)",
                        sql_file.name,
                        ", ".join(required_upgrades),
                    )
                    continue

                # Skip vec0 virtual tables if sqlite-vec is not loaded.
                if "USING vec0(" in sql and not self._vec_loaded:
                    logger.info(
                        "Skipping migration %s (sqlite-vec not available)",
                        sql_file.name,
                    )
                    continue

                # Some legacy table-rebuild migrations explicitly disable FK
                # enforcement.  SQLite ignores that PRAGMA after BEGIN, so set
                # it before the atomic script and restore the prior value after
                # commit or rollback.
                disables_foreign_keys = bool(
                    re.search(
                        r"(?im)^\s*PRAGMA\s+foreign_keys\s*=\s*OFF\s*;",
                        sql,
                    )
                )
                fk_cursor = await conn.execute("PRAGMA foreign_keys")
                fk_row = await fk_cursor.fetchone()
                foreign_keys_were_enabled = bool(fk_row and fk_row[0])
                if disables_foreign_keys and foreign_keys_were_enabled:
                    await conn.execute("PRAGMA foreign_keys = OFF")

                logger.info("Applying migration: %s", sql_file.name)
                try:
                    await self._begin_migration_transaction(conn)
                    cursor = await conn.execute(
                        "SELECT 1 FROM schema_migrations WHERE filename = ?",
                        [sql_file.name],
                    )
                    if await cursor.fetchone() is not None:
                        await conn.rollback()
                        continue

                    for statement in self._migration_statements(sql):
                        await conn.execute(statement)
                    await conn.execute(
                        "INSERT INTO schema_migrations (filename) VALUES (?)",
                        [sql_file.name],
                    )
                    await conn.commit()
                except BaseException:
                    if conn.in_transaction:
                        await conn.rollback()
                    raise
                finally:
                    if disables_foreign_keys and foreign_keys_were_enabled:
                        await conn.execute("PRAGMA foreign_keys = ON")

                count += 1

        if count:
            logger.info("Applied %d migration(s)", count)
        return count

    @staticmethod
    def _migration_lock_timeout_ms() -> int:
        """Return the bounded cross-process migration lock wait budget."""
        raw = os.environ.get("RKA_MIGRATION_LOCK_TIMEOUT_MS", "60000")
        try:
            timeout_ms = int(raw)
        except ValueError as exc:
            raise ValueError(
                "RKA_MIGRATION_LOCK_TIMEOUT_MS must be an integer"
            ) from exc
        if not 1 <= timeout_ms <= 600_000:
            raise ValueError(
                "RKA_MIGRATION_LOCK_TIMEOUT_MS must be between 1 and 600000"
            )
        return timeout_ms

    async def _begin_migration_transaction(
        self,
        conn: aiosqlite.Connection,
    ) -> None:
        """Acquire the migration write lock with a bounded retry budget.

        The connection's ordinary 5-second busy timeout remains appropriate
        for request traffic. Startup migrations receive a longer, configurable
        aggregate budget because another healthy RKA process may briefly hold
        the database writer lock during a rolling restart.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._migration_lock_timeout_ms() / 1000
        delay = 0.05
        while True:
            try:
                await conn.execute("BEGIN IMMEDIATE")
                return
            except sqlite3.OperationalError as exc:
                message = str(exc).casefold()
                if "locked" not in message and "busy" not in message:
                    raise
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise
                await asyncio.sleep(min(delay, remaining))
                delay = min(delay * 2, 0.5)

    @asynccontextmanager
    async def _phase2_schema_lock(self) -> AsyncIterator[None]:
        """Serialize sqlite-vec initialization before loading the extension.

        A SQLite write transaction is too late for this boundary: a second
        process that has already loaded sqlite-vec can retain an obsolete
        virtual-table schema while the first process rebuilds vec0 shadow
        tables, which can corrupt the database or crash the waiting process in
        native code. The sidecar file lock is shared by server/worker containers
        and covers extension loading, vec table creation, runtime upgrades,
        and vec-dependent migrations as one startup unit.
        """
        configured_lock_path = os.environ.get("RKA_PHASE2_LOCK_PATH")
        loop = asyncio.get_running_loop()
        timeout_seconds = self._migration_lock_timeout_ms() / 1000
        deadline = loop.time() + timeout_seconds
        if self.db_path == ":memory:" and not configured_lock_path:
            try:
                await asyncio.wait_for(
                    self._phase2_memory_lock.acquire(), timeout=timeout_seconds
                )
            except TimeoutError as exc:
                raise TimeoutError(
                    "Timed out waiting for in-memory sqlite-vec Phase 2 "
                    "startup lock"
                ) from exc
            try:
                yield
            finally:
                self._phase2_memory_lock.release()
            return

        if configured_lock_path:
            lock_path = Path(configured_lock_path).expanduser().resolve()
        else:
            resolved = Path(self.db_path).expanduser().resolve()
            lock_path = resolved.with_name(f"{resolved.name}.phase2.lock")

        flags = os.O_CREAT | os.O_RDWR
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        lock_fd = os.open(lock_path, flags, 0o600)
        acquired = False
        try:
            while True:
                if try_acquire_exclusive(lock_fd):
                    acquired = True
                    break
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise TimeoutError(
                        "Timed out waiting for sqlite-vec Phase 2 "
                        f"startup lock: {lock_path}"
                    )
                await asyncio.sleep(min(0.05, remaining))
            yield
        finally:
            try:
                if acquired:
                    release_exclusive(lock_fd)
            finally:
                os.close(lock_fd)

    @staticmethod
    def _migrations_directory() -> Path:
        """Return the migration directory (overridable by isolated tests)."""
        return Path(__file__).parent.parent / "db" / "migrations"

    @staticmethod
    async def _tables_exist_on_connection(
        conn: aiosqlite.Connection, table_names: list[str]
    ) -> bool:
        """Check migration prerequisites without re-entering the wrapper lock."""
        for table_name in table_names:
            cursor = await conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                [table_name],
            )
            if await cursor.fetchone() is None:
                return False
        return True

    @staticmethod
    async def _runtime_upgrades_exist_on_connection(
        conn: aiosqlite.Connection, upgrade_names: list[str]
    ) -> bool:
        """Check data-preserving runtime migration prerequisites."""
        cursor = await conn.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type = 'table' AND name = 'runtime_schema_upgrades'"
        )
        if await cursor.fetchone() is None:
            return False
        for upgrade_name in upgrade_names:
            cursor = await conn.execute(
                "SELECT 1 FROM runtime_schema_upgrades WHERE name = ?",
                [upgrade_name],
            )
            if await cursor.fetchone() is None:
                return False
        return True

    @staticmethod
    def _migration_statements(sql: str) -> list[str]:
        """Split a migration script without breaking trigger bodies.

        ``sqlite3.complete_statement`` understands quoted strings, comments,
        and ``CREATE TRIGGER ... BEGIN ... END`` blocks.  Unlike
        ``Connection.executescript``, executing the returned statements does
        not issue an implicit commit before the script.
        """
        statements: list[str] = []
        start = 0
        for index, char in enumerate(sql):
            if char != ";":
                continue
            candidate = sql[start : index + 1]
            if sqlite3.complete_statement(candidate):
                if candidate.strip():
                    statements.append(candidate)
                start = index + 1
        trailing = sql[start:]
        if trailing.strip():
            # SQL migration files normally terminate statements with ';'.
            # Permit a final complete statement but reject truncated SQL before
            # entering the transaction so retry cannot inherit partial DDL.
            if not sqlite3.complete_statement(trailing):
                if all(
                    not line.strip() or line.lstrip().startswith("--")
                    for line in trailing.splitlines()
                ):
                    return statements
                raise ValueError("Migration SQL ends with an incomplete statement")
            statements.append(trailing)
        return statements

    async def initialize_phase2_schema(self) -> None:
        """Load sqlite-vec extension and create Phase 2 tables (FTS5 + vec)."""
        async with self._phase2_schema_lock():
            await self._initialize_phase2_schema_locked()

    async def _initialize_phase2_schema_locked(self) -> None:
        """Run Phase 2 initialization while holding the process-wide lock."""
        # Try to load sqlite-vec extension
        await self._load_sqlite_vec()
        if self._vec_loaded:
            # Existing databases already have every vec table at this point.
            # Enforce the runtime structural upgrade before newer SQL
            # migrations can depend on the project metadata filters.
            from rka.services.embedding_reshape import ensure_vec_table_partitions

            await ensure_vec_table_partitions(self)
        # Re-run migrations now that vec may be available. This lets skipped
        # vec-specific migrations apply on a later startup once the extension loads.
        await self.run_migrations()

        schema_path = Path(__file__).parent.parent / "db" / "schema_phase2.sql"
        if not schema_path.exists():
            logger.warning("Phase 2 schema not found at %s", schema_path)
            return

        schema_sql = schema_path.read_text()

        if not self._vec_loaded:
            # Strip sqlite-vec virtual table CREATE statements if extension not loaded
            lines = schema_sql.split("\n")
            filtered = []
            skip = False
            for line in lines:
                if "USING vec0(" in line:
                    skip = True
                    continue
                if skip and ");" in line:
                    skip = False
                    continue
                if skip:
                    continue
                filtered.append(line)
            schema_sql = "\n".join(filtered)
            logger.info("sqlite-vec not available; skipping vector tables. FTS5 still active.")

        await self._conn.executescript(schema_sql)
        await self._conn.commit()
        if self._vec_loaded:
            # Fresh databases gain the remaining Phase 2 vec tables above;
            # this second idempotent pass covers them and records completion.
            await ensure_vec_table_partitions(self)
        # Re-run migrations after Phase 2 schema exists so migrations that depend
        # on embedding_metadata or other Phase 2 tables can apply on fresh DBs.
        await self.run_migrations()
        logger.info("Phase 2 schema initialized (vec=%s)", self._vec_loaded)

    async def _load_sqlite_vec(self) -> None:
        """Try to load the sqlite-vec extension (runs on aiosqlite's thread)."""
        load_errors: list[str] = []

        for candidate in self._sqlite_vec_candidates():
            try:
                await self._conn._execute(self._conn._conn.load_extension, str(candidate))
                self._vec_loaded = True
                logger.info("sqlite-vec extension loaded from %s", candidate)
                return
            except Exception as exc:
                load_errors.append(f"{candidate}: {exc}")

        try:
            import sqlite_vec
            await self._conn._execute(sqlite_vec.load, self._conn._conn)
            self._vec_loaded = True
            logger.info("sqlite-vec extension loaded successfully via sqlite_vec package")
        except ImportError:
            logger.info("sqlite-vec not installed; vector search disabled")
            self._vec_loaded = False
        except Exception as exc:
            if load_errors:
                logger.warning(
                    "Failed to load sqlite-vec. Tried explicit paths [%s] and package loader error: %s",
                    "; ".join(load_errors),
                    exc,
                )
            else:
                logger.warning("Failed to load sqlite-vec: %s", exc)
            self._vec_loaded = False

    def _sqlite_vec_candidates(self) -> list[Path]:
        """Return possible loadable extension paths for sqlite-vec."""
        seen: set[str] = set()
        candidates: list[Path] = []

        def _add(path_str: str | None) -> None:
            if not path_str:
                return
            path = Path(path_str)
            key = str(path)
            if key in seen:
                return
            seen.add(key)
            candidates.append(path)

        _add(os.getenv("RKA_SQLITE_VEC_PATH"))
        for path_str in (
            "/usr/local/lib/vec0",
            "/usr/local/lib/vec0.so",
            "/usr/local/lib/vec0.dylib",
            "/usr/local/lib/vec0.dll",
        ):
            _add(path_str)

        try:
            import sqlite_vec

            package_dir = Path(sqlite_vec.__file__).resolve().parent
            for name in ("vec0", "vec0.so", "vec0.dylib", "vec0.dll"):
                _add(str(package_dir / name))
        except ImportError:
            pass

        return [path for path in candidates if path.exists()]

    @property
    def vec_available(self) -> bool:
        """Whether sqlite-vec extension is loaded."""
        return self._vec_loaded

    @staticmethod
    def _reject_raw_transaction_control(sql: str) -> None:
        """Require every wrapper entry point to use managed transactions."""
        normalized = re.sub(
            r"\A(?:\s+|--[^\n]*(?:\n|\Z)|/\*.*?\*/)*",
            "",
            sql,
            flags=re.DOTALL,
        ).upper()
        keyword_match = re.match(r"[A-Z]+", normalized)
        keyword = keyword_match.group(0) if keyword_match else ""
        if keyword in {
            "BEGIN",
            "COMMIT",
            "END",
            "ROLLBACK",
            "SAVEPOINT",
            "RELEASE",
        }:
            raise RuntimeError(
                "raw transaction control is not allowed; use Database.transaction()"
            )

    async def execute(self, sql: str, params: list | tuple | None = None) -> aiosqlite.Cursor:
        """Execute one statement without permitting raw transaction control.

        Transaction ownership must go through :meth:`transaction`; otherwise
        another coroutine can interleave and commit or roll back unrelated
        service work on the shared connection.  Nested managed transactions
        create their savepoints directly on the owned connection; application
        callers may not open raw savepoints either.
        """
        self._reject_raw_transaction_control(sql)
        async with self._connection_access() as conn:
            if params:
                return await conn.execute(sql, params)
            return await conn.execute(sql)

    async def executemany(self, sql: str, params_list: list) -> aiosqlite.Cursor:
        """Execute SQL with multiple parameter sets."""
        self._reject_raw_transaction_control(sql)
        async with self._connection_access() as conn:
            return await conn.executemany(sql, params_list)

    async def fetchone(self, sql: str, params: list | tuple | None = None) -> dict | None:
        """Fetch a single row as a dict."""
        self._reject_raw_transaction_control(sql)
        async with self._connection_access() as conn:
            if params:
                cursor = await conn.execute(sql, params)
            else:
                cursor = await conn.execute(sql)
            row = await cursor.fetchone()
        if row is None:
            return None
        return dict(row)

    async def fetchall(self, sql: str, params: list | tuple | None = None) -> list[dict]:
        """Fetch all rows as dicts."""
        self._reject_raw_transaction_control(sql)
        async with self._connection_access() as conn:
            if params:
                cursor = await conn.execute(sql, params)
            else:
                cursor = await conn.execute(sql)
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def commit(self) -> None:
        """Commit pending work unless a managed transaction owns the call.

        Service methods historically commit their own work.  Treat those
        commits as deferred while the same task is inside ``transaction()`` so
        a caller can compose existing service methods atomically.
        """
        if self._transaction_owner is asyncio.current_task():
            return
        async with self._connection_access() as conn:
            await conn.commit()

    @asynccontextmanager
    async def transaction(
        self,
        *,
        write: bool = True,
        migration_lock: bool = False,
    ) -> AsyncIterator[Database]:
        """Run statements atomically, using savepoints for same-task nesting.

        The outermost context owns the connection until it commits or rolls
        back.  Nested contexts in that task use uniquely named savepoints, so a
        handled inner failure rolls back only the inner unit.  Calls to
        :meth:`commit` made by composed service helpers are deferred until the
        outermost context exits.  Write transactions acquire SQLite's reserved
        write lock before reading so optimistic revision checks cannot later
        fail with ``SQLITE_BUSY_SNAPSHOT`` after a competing process commits.
        Read-only snapshot callers should pass ``write=False`` to avoid
        unnecessarily serializing writers. Startup-time structural upgrades
        should pass ``migration_lock=True`` to use the longer bounded retry
        budget shared with SQL migrations.

        Managed transactions are task-affine.  Do not spawn a child task that
        uses this ``Database`` and await it from inside the transaction; the
        child correctly waits for connection ownership and the parent would
        therefore deadlock waiting for it.
        """
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("Managed transactions require a running asyncio task")
        if migration_lock and not write:
            raise ValueError("migration_lock requires a write transaction")

        if self._transaction_owner is task:
            async with self._nested_transaction():
                yield self
            return

        async with self._access_lock:
            conn = self.conn
            if conn.in_transaction:
                raise RuntimeError(
                    "Cannot start a managed transaction while an unmanaged "
                    "transaction is active; commit or roll it back first"
                )

            self._transaction_owner = task
            self._transaction_depth = 1
            try:
                try:
                    if migration_lock:
                        await self._begin_migration_transaction(conn)
                    else:
                        await conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
                    yield self
                except BaseException:
                    await self._rollback_before_unlock(conn)
                    raise
                else:
                    try:
                        await conn.commit()
                    except BaseException:
                        await self._rollback_before_unlock(conn)
                        raise
            finally:
                self._transaction_depth = 0
                self._transaction_owner = None

    @staticmethod
    async def _rollback_before_unlock(conn) -> None:
        # Cancelling aiosqlite's awaiter does not remove an already queued BEGIN
        # from its thread. Queue rollback after it and retain connection ownership
        # until cleanup completes, including under repeated cancellation.
        cleanup = asyncio.create_task(conn.rollback())
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                continue
        cleanup.result()

    @asynccontextmanager
    async def _nested_transaction(self) -> AsyncIterator[None]:
        """Create a savepoint inside the transaction owned by this task."""
        conn = self.conn
        self._savepoint_counter += 1
        savepoint = f"rka_sp_{self._savepoint_counter}"
        await conn.execute(f"SAVEPOINT {savepoint}")
        self._transaction_depth += 1
        try:
            try:
                yield
            except BaseException:
                await conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                await conn.execute(f"RELEASE SAVEPOINT {savepoint}")
                raise
            else:
                try:
                    await conn.execute(f"RELEASE SAVEPOINT {savepoint}")
                except BaseException:
                    # A failed release must not leave the outer transaction
                    # carrying a partially applied inner unit.
                    await conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                    await conn.execute(f"RELEASE SAVEPOINT {savepoint}")
                    raise
        finally:
            self._transaction_depth -= 1

    @asynccontextmanager
    async def _connection_access(self) -> AsyncIterator[aiosqlite.Connection]:
        """Serialize wrapper calls without re-locking the owning task."""
        if self._transaction_owner is asyncio.current_task():
            yield self.conn
            return
        async with self._access_lock:
            yield self.conn

    async def _tables_exist(self, table_names: list[str]) -> bool:
        """Return True when all named tables exist."""
        for table_name in table_names:
            row = await self.fetchone(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                [table_name],
            )
            if row is None:
                return False
        return True

    @staticmethod
    def _migration_required_tables(sql: str) -> list[str]:
        """Parse migration prerequisites from leading SQL comments.

        Supports comment lines like:
          -- requires-table: embedding_metadata
          -- requires-table: foo, bar
        """
        tables: list[str] = []
        for raw_line in sql.splitlines():
            line = raw_line.strip()
            if not line.startswith("-- requires-table:"):
                continue
            _, _, raw_tables = line.partition(":")
            tables.extend(
                table.strip()
                for table in raw_tables.split(",")
                if table.strip()
            )
        return tables

    @staticmethod
    def _migration_required_runtime_upgrades(sql: str) -> list[str]:
        """Parse ``-- requires-runtime-upgrade: name`` prerequisites."""
        upgrades: list[str] = []
        for raw_line in sql.splitlines():
            line = raw_line.strip()
            if not line.startswith("-- requires-runtime-upgrade:"):
                continue
            _, _, raw_upgrades = line.partition(":")
            upgrades.extend(
                upgrade.strip()
                for upgrade in raw_upgrades.split(",")
                if upgrade.strip()
            )
        return upgrades

    @property
    def conn(self) -> aiosqlite.Connection:
        """Get the raw connection (for transactions)."""
        if self._conn is None:
            raise RuntimeError("Database not connected. Call connect() first.")
        return self._conn
