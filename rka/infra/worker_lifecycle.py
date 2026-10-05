"""CLI-owned signal handling and bounded drain for the durable worker.

Signals are installed only by the CLI, never when embedding the worker as a
library. Cancellation still goes through the worker's lease-fenced failure path.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import threading
from contextlib import contextmanager

logger = logging.getLogger(__name__)


@contextmanager
def worker_shutdown_signals(stop_event: asyncio.Event):
    """Handle SIGTERM/CTRL-C, including event loops without add_signal_handler.

    Python only permits signal registration on the main thread. Embedded CLI
    callers on other threads retain their host's signal policy.
    """
    loop = asyncio.get_running_loop()
    previous = {}

    def request_stop(signum, frame):
        loop.call_soon_threadsafe(stop_event.set)

    try:
        if threading.current_thread() is threading.main_thread():
            for sig in (signal.SIGTERM, signal.SIGINT):
                previous[sig] = signal.getsignal(sig)
                signal.signal(sig, request_stop)
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


async def drain_worker(
    worker, stop_event: asyncio.Event, *, grace_seconds: float, once: bool = False,
) -> bool | None:
    """Stop claiming jobs, allow a bounded drain, then cancel and await cleanup.

    The grace bounds work, not asynchronous DB/provider cleanup. Container stop
    grace must leave additional time for that cleanup; it remains the outer
    safety bound if an external dependency cannot be cancelled cooperatively.
    """
    if stop_event.is_set():
        return None
    running = asyncio.create_task(worker.run_once() if once else worker.run_forever(stop_event))
    stopping = asyncio.create_task(stop_event.wait())
    try:
        done, _ = await asyncio.wait((running, stopping), return_when=asyncio.FIRST_COMPLETED)
        if running in done:
            return await running  # Do not hide an unexpected worker failure.
        logger.info("Worker shutdown requested; draining the current job")
        done, _ = await asyncio.wait((running,), timeout=grace_seconds)
        if running not in done:
            logger.info("Worker drain deadline reached; cancelling the current job")
            running.cancel()
            try:
                await running
            except asyncio.CancelledError:
                pass
        else:
            return await running
    finally:
        stopping.cancel()
        if not running.done():
            running.cancel()
        await asyncio.gather(stopping, running, return_exceptions=True)
