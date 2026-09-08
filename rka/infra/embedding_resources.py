"""Bounded, non-transforming admission for built-in embedding backends.

Budgets are UTF-8 bytes, not tokenizer measurements or hard RSS limits. A native
future owns its slot until inference actually ends, not until its caller leaves.
"""

from __future__ import annotations

import asyncio
import contextvars
import math
import threading
from collections.abc import Awaitable, Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, fields, replace
from typing import Any, TypeVar


class EmbeddingResourceError(RuntimeError):
    code = "embedding_resource_error"

    def __init__(self, detail: str):
        self.detail = detail
        super().__init__(f"{self.code}: {detail}")


class EmbeddingInputLimit(EmbeddingResourceError):
    code = "embedding_input_limit"


class EmbeddingBusy(EmbeddingResourceError):
    code = "embedding_resource_busy"


class EmbeddingCallTimeout(EmbeddingResourceError):
    code = "embedding_call_timeout"


@dataclass(frozen=True)
class EmbeddingResourceLimits:
    max_input_bytes: int = 8192
    max_batch_inputs: int = 8
    max_batch_bytes: int = 16384
    max_padding_bytes: int = 16384
    max_call_inputs: int = 128
    max_call_bytes: int = 262144
    call_timeout_seconds: float = 120.0

    def __post_init__(self):
        for field in fields(self):
            value = getattr(self, field.name)
            if field.name == "call_timeout_seconds":
                valid = isinstance(value, (int, float))
            else:
                valid = isinstance(value, int)
            if isinstance(value, bool) or not valid or not 0 < value <= field.default:
                raise ValueError(f"resource_limits.{field.name} must be > 0 and <= {field.default}")
        if not (
            self.max_input_bytes <= self.max_batch_bytes <= self.max_call_bytes
            and self.max_input_bytes <= self.max_padding_bytes
            and self.max_batch_inputs <= self.max_call_inputs
        ):
            raise ValueError("resource_limits has inconsistent input/batch/call budgets")

    @classmethod
    def from_config(cls, config: Mapping[str, Any] | None) -> "EmbeddingResourceLimits":
        if config is None:
            return cls()
        if not isinstance(config, Mapping):
            raise ValueError("resource_limits must be an object")
        unknown = set(config) - {field.name for field in fields(cls)}
        if unknown:
            raise ValueError("resource_limits contains unknown fields")
        return cls(**config)

    def _size(self, text: str) -> int:
        if not isinstance(text, str):
            raise EmbeddingInputLimit("inputs must be strings")
        # Avoid allocating an encoded copy of an arbitrarily large raw string.
        if len(text) > self.max_input_bytes:
            raise EmbeddingInputLimit(f"input exceeds max_input_bytes={self.max_input_bytes}")
        try:
            size = len(text.encode("utf-8"))
        except UnicodeEncodeError as exc:
            raise EmbeddingInputLimit("input must be valid UTF-8 text") from exc
        if size > self.max_input_bytes:
            raise EmbeddingInputLimit(f"input exceeds max_input_bytes={self.max_input_bytes}")
        return size

    def plan(
        self,
        texts: Sequence[str],
        prepare: Callable[[str], str] = lambda text: text,
    ) -> list[list[str]]:
        """Validate the entire logical call, then return ordered provider batches."""
        if isinstance(texts, (str, bytes)) or not isinstance(texts, (list, tuple)):
            raise EmbeddingInputLimit("batch inputs must be a list or tuple of strings")
        if len(texts) > self.max_call_inputs:
            raise EmbeddingInputLimit(f"call exceeds max_call_inputs={self.max_call_inputs}")
        batches: list[list[str]] = []
        batch: list[str] = []
        total = batch_bytes = longest = 0
        for text in texts:
            self._size(text)
            prepared = prepare(text)
            size = self._size(prepared)
            total += size
            if total > self.max_call_bytes:
                raise EmbeddingInputLimit(f"call exceeds max_call_bytes={self.max_call_bytes}")
            count = len(batch) + 1
            if batch and (
                count > self.max_batch_inputs
                or batch_bytes + size > self.max_batch_bytes
                or max(longest, size) * count > self.max_padding_bytes
            ):
                batches.append(batch)
                batch, batch_bytes, longest = [], 0, 0
            batch.append(prepared)
            batch_bytes += size
            longest = max(longest, size)
        if batch:
            batches.append(batch)
        return batches


# Admission is process-wide across backend instances and event loops. No caller
# waits in an unbounded semaphore/executor queue; durable jobs own retry policy.
_ADMISSION = threading.BoundedSemaphore(1)
_NATIVE_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="rka-embedding")
_HTTP_TASKS: set[asyncio.Task] = set()
T = TypeVar("T")


def native_resource_limits(config=None):
    """Tighter ONNX ceilings, established by the default-model RSS gate.

    Preserve compatible saved configuration while applying min(config, native
    ceiling). This changes admission only, not accepted text or vector identity.
    HTTP providers retain the shared configurable maxima.
    """
    limits = EmbeddingResourceLimits.from_config(config)
    return replace(limits, max_input_bytes=min(limits.max_input_bytes, 2048),
                   max_batch_bytes=min(limits.max_batch_bytes, 4096),
                   max_padding_bytes=min(limits.max_padding_bytes, 4096))


def request_timeout(value: Any) -> float:
    if isinstance(value, bool):
        raise ValueError("timeout_seconds must be positive and finite")
    try:
        timeout = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("timeout_seconds must be positive and finite") from exc
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout_seconds must be positive and finite")
    return timeout


def _acquire() -> None:
    if not _ADMISSION.acquire(blocking=False):
        raise EmbeddingBusy("one provider call is already active in this process; retry later")


def _consume(future) -> None:
    if not future.cancelled():
        future.exception()


def _http_finished(task) -> None:
    _HTTP_TASKS.discard(task)
    _ADMISSION.release()
    _consume(task)


async def run_http(call: Callable[[], Awaitable[T]], *, timeout: float) -> T:
    _acquire()
    try:
        task = asyncio.ensure_future(call())
    except BaseException:
        _ADMISSION.release()
        raise
    _HTTP_TASKS.add(task)
    task.add_done_callback(_http_finished)
    try:
        return await asyncio.wait_for(asyncio.shield(task), timeout)
    except TimeoutError as exc:
        task.cancel()
        raise EmbeddingCallTimeout(f"logical call exceeded {timeout:g} seconds") from exc
    except asyncio.CancelledError:
        task.cancel()
        raise


async def run_native(call: Callable[[], T], *, timeout: float) -> T:
    _acquire()
    try:
        context = contextvars.copy_context()
        future = _NATIVE_EXECUTOR.submit(context.run, call)
    except BaseException:
        _ADMISSION.release()
        raise
    # concurrent.futures owns this release, independent of asyncio cancellation
    # and event-loop teardown. The native thread is not claimed to be killable.
    future.add_done_callback(lambda completed: _ADMISSION.release())
    wrapped = asyncio.wrap_future(future)
    wrapped.add_done_callback(_consume)
    try:
        return await asyncio.wait_for(asyncio.shield(wrapped), timeout)
    except TimeoutError as exc:
        raise EmbeddingCallTimeout(f"logical call exceeded {timeout:g} seconds") from exc
