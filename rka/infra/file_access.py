"""Operator-owned file authority, bounded enumeration, and private parser snapshots.

Request paths and manifests never grant authority. POSIX opens every component
relative to an already-open directory without following links. Windows keeps
non-reparse component handles open without write/delete sharing while reading
or enumerating. Parsers only reopen a private snapshot, never a request path.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Iterator

DEFAULT_MAX_BYTES = 50 * 1024 * 1024
MAX_SCAN_BYTES = 200 * 1024 * 1024
MAX_SCAN_FILES = 2000
MAX_SCAN_ENTRIES = 10000
MAX_SCAN_DEPTH = 32
MAX_TEXT_BYTES = 2 * 1024 * 1024
_FILE_SLOTS = threading.BoundedSemaphore(4)
_SCAN_SLOTS = threading.BoundedSemaphore(4)


@contextmanager
def scan_slot():
    if not _SCAN_SLOTS.acquire(blocking=False):
        raise FileAccessError(
            "File scanning is busy; retry later", code="file_access_busy", status_code=503
        )
    try:
        yield
    finally:
        _SCAN_SLOTS.release()


@contextmanager
def _file_slot():
    if not _FILE_SLOTS.acquire(blocking=False):
        raise FileAccessError(
            "File processing is busy; retry later", code="file_access_busy", status_code=503
        )
    try:
        yield
    finally:
        _FILE_SLOTS.release()


def require_bounded_text(content: str) -> None:
    if len(content) > MAX_TEXT_BYTES or len(content.encode("utf-8")) > MAX_TEXT_BYTES:
        raise _limit(f"Text payload exceeds {MAX_TEXT_BYTES} UTF-8 bytes")


class FileAccessError(PermissionError):
    def __init__(self, message: str, *, code="file_access_denied", status_code=403):
        self.code = code
        self.status_code = status_code
        super().__init__(message)


def _denied() -> FileAccessError:
    return FileAccessError(
        "File access denied: use an operator-authorized regular file; symlinks are not allowed"
    )


def _limit(message: str) -> FileAccessError:
    return FileAccessError(message, code="file_access_limit", status_code=413)


def _native_absolute(value: str | Path) -> Path:
    raw = str(value)
    win = PureWindowsPath(raw)
    # Never initiate UNC/device access or accept a foreign/drive-relative path.
    if not raw or "\x00" in raw or raw.startswith(("\\\\", "//")):
        raise _denied()
    if os.name != "nt" and (win.drive or "\\" in raw):
        raise _denied()
    path = Path(raw).expanduser()
    if not path.is_absolute() or ".." in path.parts:
        raise _denied()
    # macOS's OS-owned aliases are not caller-supplied symlinks. Avoid resolving
    # arbitrary user components while supporting normal tempfile/home paths.
    if sys.platform == "darwin" and path.parts[1:2] in [("var",), ("tmp",), ("etc",)]:
        path = Path("/private") / path.relative_to("/")
    if os.name == "nt":
        win = PureWindowsPath(path)
        if not win.is_absolute() or len(win.drive) != 2:
            raise _denied()
        reserved = {"CON", "PRN", "AUX", "NUL", "CLOCK$"}
        reserved |= {f"{prefix}{n}" for prefix in ("COM", "LPT") for n in range(1, 10)}
        for part in path.parts[1:]:
            if ":" in part or part.endswith((".", " ")) or part.split(".")[0].upper() in reserved:
                raise _denied()
    return path


@contextmanager
def _posix_open(path: Path, *, directory: bool = False):
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    current = os.open(path.anchor, flags)
    try:
        for index, part in enumerate(path.parts[1:]):
            final = index == len(path.parts) - 2
            child_flags = (
                flags if directory or not final else os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            )
            child = os.open(part, child_flags, dir_fd=current)
            os.close(current)
            current = child
        opened = os.fstat(current)
        if not (stat.S_ISDIR(opened.st_mode) if directory else stat.S_ISREG(opened.st_mode)):
            raise _denied()
        yield current
    finally:
        os.close(current)


@contextmanager
def _windows_open(path: Path, *, directory: bool = False):
    """Pin every component; do not follow junctions, symlinks, or device paths."""
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create.restype = wintypes.HANDLE
    close = kernel.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL
    info = kernel.GetFileInformationByHandleEx
    info.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    info.restype = wintypes.BOOL

    class AttributeTag(ctypes.Structure):
        _fields_ = [("attributes", wintypes.DWORD), ("tag", wintypes.DWORD)]

    handles = []
    try:
        parts = [Path(path.anchor)]
        for component in path.parts[1:]:
            parts.append(parts[-1] / component)
        for index, component in enumerate(parts):
            is_dir = directory or index < len(parts) - 1
            handle = create(
                str(component),
                0x80 if is_dir else 0x80000000,
                1,
                None,
                3,
                0x00200000 | 0x02000000,
                None,
            )  # attributes/read, SHARE_READ only, OPEN_EXISTING, REPARSE|BACKUP
            if handle == wintypes.HANDLE(-1).value:
                raise _denied()
            handles.append(handle)
            attributes = AttributeTag()
            if not info(handle, 9, ctypes.byref(attributes), ctypes.sizeof(attributes)):
                raise _denied()
            if attributes.attributes & 0x400 or bool(attributes.attributes & 0x10) != is_dir:
                raise _denied()
        yield handles[-1]
    finally:
        for handle in reversed(handles):
            close(handle)


def _windows_read(handle: int, limit: int) -> bytes:
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    get_size = kernel.GetFileSizeEx
    get_size.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_longlong)]
    get_size.restype = wintypes.BOOL
    size = ctypes.c_longlong()
    if not get_size(handle, ctypes.byref(size)):
        raise _denied()
    if size.value > limit:
        raise _limit(f"File exceeds maximum size of {limit} bytes")
    read = kernel.ReadFile
    read.argtypes = [
        wintypes.HANDLE,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPVOID,
    ]
    read.restype = wintypes.BOOL
    payload = bytearray()
    while len(payload) <= limit:
        buffer = ctypes.create_string_buffer(min(65536, limit + 1 - len(payload)))
        count = wintypes.DWORD()
        if not read(handle, buffer, len(buffer), ctypes.byref(count), None):
            raise _denied()
        if count.value == 0:
            break
        payload.extend(buffer.raw[: count.value])
    if len(payload) > limit:
        raise _limit(f"File exceeds maximum size of {limit} bytes")
    return bytes(payload)


@dataclass
class ScanBudget:
    max_entries: int = MAX_SCAN_ENTRIES
    max_files: int = MAX_SCAN_FILES
    max_bytes: int = MAX_SCAN_BYTES
    entries: int = 0
    files: int = 0
    bytes_read: int = 0
    stopped: str = ""
    skipped: int = 0
    deadline: float = 0

    def __post_init__(self):
        if (
            not 1 <= self.max_files <= MAX_SCAN_FILES
            or not 1 <= self.max_entries <= MAX_SCAN_ENTRIES
            or not 1 <= self.max_bytes <= MAX_SCAN_BYTES
        ):
            raise ValueError("Scan limits must be positive and within operator limits")
        self.deadline = time.monotonic() + 30

    def check(self) -> bool:
        if self.entries >= self.max_entries:
            self.stopped = f"Entry cap reached ({self.max_entries}); counts are partial"
        elif self.files >= self.max_files:
            self.stopped = f"File cap reached ({self.max_files}); counts are partial"
        elif time.monotonic() >= self.deadline:
            self.stopped = "Scan time budget reached; counts are partial"
        return not self.stopped


class FileAccessPolicy:
    def __init__(self, roots=(), *, max_bytes=DEFAULT_MAX_BYTES, setting="RKA_SERVER_FILE_ROOTS"):
        if not 1 <= max_bytes <= 500 * 1024 * 1024:
            raise ValueError("File byte limit must be between 1 and 524288000")
        self.roots = tuple(_native_absolute(root) for root in roots)
        self.max_bytes = max_bytes
        self.setting = setting
        for root in self.roots:
            with self._open(root, directory=True):
                pass

    @classmethod
    def host(cls):
        try:
            roots = json.loads(os.environ.get("RKA_HOST_FILE_ROOTS", "[]"))
            if not isinstance(roots, list) or any(not isinstance(root, str) for root in roots):
                raise ValueError
        except ValueError as exc:
            raise ValueError(
                "RKA_HOST_FILE_ROOTS must be a JSON array of absolute directories"
            ) from exc
        limit = int(os.environ.get("RKA_REGISTERED_SOURCE_MAX_BYTES", DEFAULT_MAX_BYTES))
        return cls(roots, max_bytes=limit, setting="RKA_HOST_FILE_ROOTS")

    def authorize(self, value: str | Path) -> Path:
        if not self.roots:
            raise FileAccessError(
                f"Path reads are disabled; configure {self.setting} in the operator environment or supply bytes",
                code="file_access_disabled",
            )
        path = _native_absolute(value)
        if not any(path.is_relative_to(root) for root in self.roots):
            raise _denied()
        return path

    def relative_file(self, root: str | Path, relative: str) -> Path:
        base = self.authorize(root)
        win = PureWindowsPath(relative)
        rel = Path(relative)
        if not relative or rel.is_absolute() or win.drive or win.root or ".." in rel.parts:
            raise _denied()
        # Backslashes are ambiguous across host/container operating systems.
        if os.name != "nt" and "\\" in relative:
            raise _denied()
        path = self.authorize(base / rel)
        if not path.is_relative_to(base):
            raise _denied()
        return path

    @contextmanager
    def _open(self, path: Path, *, directory=False):
        try:
            opener = _windows_open if os.name == "nt" else _posix_open
            with opener(path, directory=directory) as descriptor:
                yield descriptor
        except FileAccessError:
            raise
        except (OSError, ValueError, RuntimeError) as exc:
            raise _denied() from exc

    def directory(self, value: str | Path) -> Path:
        path = self.authorize(value)
        with self._open(path, directory=True):
            return path

    def check_file(self, value: str | Path) -> Path:
        path = self.authorize(value)
        with self._open(path):
            return path

    def read_bytes(
        self, value: str | Path, *, max_bytes=None, budget: ScanBudget | None = None
    ) -> bytes:
        with _file_slot():
            return self._read_bytes(value, max_bytes=max_bytes, budget=budget)

    def _read_bytes(self, value, *, max_bytes=None, budget=None):
        path = self.authorize(value)
        limit = min(self.max_bytes, max_bytes if max_bytes is not None else self.max_bytes)
        if limit < 1:
            raise _limit("File byte budget exhausted")
        if budget is not None:
            if time.monotonic() >= budget.deadline:
                budget.stopped = "Scan time budget reached; counts are partial"
            if budget.stopped:
                raise _limit(budget.stopped)
            limit = min(limit, budget.max_bytes - budget.bytes_read)
            if limit < 1:
                budget.stopped = "Scan byte budget exhausted; counts are partial"
                raise _limit(budget.stopped)
        with self._open(path) as descriptor:
            if os.name == "nt":
                data = _windows_read(descriptor, limit)
            else:
                if os.fstat(descriptor).st_size > limit:
                    raise _limit(f"File exceeds maximum size of {limit} bytes")
                with os.fdopen(os.dup(descriptor), "rb") as source:
                    data = source.read(limit + 1)
                if len(data) > limit:
                    raise _limit(f"File exceeds maximum size of {limit} bytes")
        if budget is not None:
            budget.bytes_read += len(data)
        return data

    @contextmanager
    def snapshot(self, value: str | Path, *, max_bytes=None, budget=None):
        """Copy verified, bounded bytes for libraries that require a file path."""
        path = self.authorize(value)
        with _file_slot():
            data = self._read_bytes(path, max_bytes=max_bytes, budget=budget)
            with tempfile.TemporaryDirectory(prefix="rka-file-snapshot-") as temporary:
                snapshot = Path(temporary) / path.name
                snapshot.write_bytes(data)
                os.chmod(snapshot, 0o600)
                del data
                yield snapshot

    @contextmanager
    def entries(self, value: str | Path):
        path = self.authorize(value)
        with self._open(path, directory=True) as descriptor:
            with os.scandir(path if os.name == "nt" else descriptor) as entries:
                yield entries

    def walk(
        self,
        root: str | Path,
        *,
        ignores=(),
        budget: ScanBudget | None = None,
        max_depth=MAX_SCAN_DEPTH,
    ) -> Iterator[Path]:
        from rka.services.classify import is_ignored

        root = self.directory(root)
        budget = budget if budget is not None else ScanBudget()
        if not 0 <= max_depth <= MAX_SCAN_DEPTH:
            raise ValueError(f"max_depth must be between 0 and {MAX_SCAN_DEPTH}")

        def visit(directory, depth):
            with self.entries(directory) as entries:
                while budget.check():
                    try:
                        entry = next(entries)
                    except StopIteration:
                        break
                    budget.entries += 1
                    path = directory / entry.name
                    if is_ignored(path, root, set(ignores)):
                        continue
                    try:
                        info = entry.stat(follow_symlinks=False)
                        if (
                            stat.S_ISLNK(info.st_mode)
                            or getattr(info, "st_file_attributes", 0) & 0x400
                        ):
                            continue
                        if stat.S_ISDIR(info.st_mode):
                            if depth < max_depth:
                                yield from visit(path, depth + 1)
                        elif stat.S_ISREG(info.st_mode):
                            budget.files += 1
                            yield path
                    except OSError:
                        continue

        with scan_slot():
            yield from visit(root, 0)
