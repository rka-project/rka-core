"""Portable lifetime admission for cooperating SQLite/config clients.

The gate and slots are permanent kernel-lock files, never PID evidence. All
paths must be on a shared, lock-capable, operator-controlled filesystem. Old
binaries and unrelated SQLite tools require an explicit operator stop.
"""

from __future__ import annotations

import asyncio
import os
import stat
import time
from pathlib import Path

from rka.infra.file_lock import release_exclusive, try_acquire_exclusive


class MaintenanceBusy(RuntimeError):
    pass


def canonical_path(path: str | Path) -> Path:
    value = str(path)
    if value.startswith("file:"):
        raise ValueError("SQLite URI paths are unsupported by lifetime admission")
    resolved = Path(path).expanduser().resolve()
    if resolved.exists() and (not resolved.is_file() or resolved.stat().st_nlink != 1):
        raise ValueError("lifetime admission requires a regular, non-hardlinked file")
    return resolved


def lock_directory(path: str | Path) -> Path:
    source = canonical_path(path)
    return source.with_name(source.name + ".runtime-locks")


def _open_lock(path: Path) -> int:
    if path.is_symlink():
        raise ValueError("symlinked lock files are unsupported")
    fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not os.path.samestat(info, path.stat()):
            raise ValueError("unsafe lock file identity")
        os.set_inheritable(fd, False)
        return fd
    except BaseException:
        os.close(fd)
        raise


class RuntimeLease:
    """One exclusive gate or one of 128 concurrent lifetime slots.

    Fixed slot names bound scanning/storage; files are never unlinked, avoiding
    inode-split races on POSIX and open-file deletion races on Windows.
    """

    def __init__(self, path: str | Path, *, maintenance: bool = False):
        self.path = canonical_path(path)
        self.directory = lock_directory(self.path)
        self.maintenance = maintenance
        self.fd: int | None = None
        self.pid = os.getpid()

    @property
    def intent_path(self) -> Path:
        return self.directory / "recovery.json"

    def acquire(self):
        if self.fd is not None:
            raise RuntimeError("lease already acquired")
        if self.directory.is_symlink():
            raise ValueError("symlinked lock directories are unsupported")
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.directory.mkdir(mode=0o700, exist_ok=True)
        gate = _open_lock(self.directory / "gate.lock")
        acquired = False
        try:
            deadline = time.monotonic() + (0 if self.maintenance else 1)
            while True:
                acquired = try_acquire_exclusive(gate)
                if acquired or time.monotonic() >= deadline:
                    break
                time.sleep(0.005)
            if not acquired:
                raise MaintenanceBusy("database admission busy; retry after maintenance/startup")
            if not self.maintenance and self.intent_path.exists():
                raise MaintenanceBusy("unfinished embedding recovery; use admin embedding resume or rollback")
            for index in range(128):
                slot_path = self.directory / f"slot-{index:03d}.lock"
                if self.maintenance and not slot_path.exists():
                    continue
                slot = _open_lock(slot_path)
                locked = False
                try:
                    locked = try_acquire_exclusive(slot)
                    if self.maintenance:
                        if not locked:
                            raise MaintenanceBusy("database clients still connected; stop API, workers and direct clients")
                    elif locked:
                        self.fd = slot
                        slot = None
                        return self
                finally:
                    if slot is not None:
                        if locked:
                            release_exclusive(slot)
                        os.close(slot)
            if not self.maintenance:
                raise MaintenanceBusy("database runtime connection limit reached (128)")
            self.fd = gate
            gate = None
            return self
        finally:
            if gate is not None:
                if acquired:
                    release_exclusive(gate)
                os.close(gate)

    def assert_owner(self, path: str | Path):
        if not self.maintenance or self.fd is None or self.pid != os.getpid() or self.path != canonical_path(path):
            raise MaintenanceBusy("maintenance ownership required")

    def close(self):
        if self.fd is not None:
            if self.pid != os.getpid():
                raise RuntimeError("inherited leases cannot be released by child processes")
            release_exclusive(self.fd)
            os.close(self.fd)
            self.fd = None

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *args):
        self.close()


async def finish_before_cancellation(awaitable):
    """Finish SQLite thread work before allowing lease release, even on cancel."""
    task = asyncio.ensure_future(awaitable)
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    result = task.result()
    if cancelled:
        raise asyncio.CancelledError
    return result
