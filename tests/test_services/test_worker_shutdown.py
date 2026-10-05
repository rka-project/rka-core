"""Bounded drain, lease-safe cancellation and real signals on disposable workers."""

import asyncio
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from rka.infra.worker_lifecycle import drain_worker, worker_shutdown_signals
from rka.services.worker import EnrichmentWorker


@pytest.mark.asyncio
async def test_idle_stop_interrupts_long_poll(db):
    stop = asyncio.Event()
    runner = EnrichmentWorker(db=db, embeddings_enabled=False, poll_interval=300)
    task = asyncio.create_task(drain_worker(runner, stop, grace_seconds=1))
    await asyncio.sleep(0.03)
    stop.set()
    await asyncio.wait_for(task, 0.5)


@pytest.mark.asyncio
async def test_shutdown_drains_current_job_without_claiming_next(db, monkeypatch):
    stop, started, finish = asyncio.Event(), asyncio.Event(), asyncio.Event()
    runner = EnrichmentWorker(db=db, embeddings_enabled=False)
    first = await runner.queue.enqueue("synthetic_drain")
    second = await runner.queue.enqueue("synthetic_drain")

    async def process(job):
        assert job["id"] == first
        started.set()
        await finish.wait()
        return {"ok": True}

    monkeypatch.setattr(runner, "_process_job", process)
    task = asyncio.create_task(drain_worker(runner, stop, grace_seconds=1))
    await started.wait()
    stop.set()
    await asyncio.sleep(0)
    finish.set()
    await asyncio.wait_for(task, 2)
    assert (await db.fetchone("SELECT status FROM jobs WHERE id=?", [first]))["status"] == "completed"
    row = await db.fetchone("SELECT status,attempts FROM jobs WHERE id=?", [second])
    assert row == {"status": "pending", "attempts": 0}


@pytest.mark.asyncio
@pytest.mark.parametrize("max_attempts,expected", [(5, "pending"), (1, "failed")])
async def test_drain_deadline_cancels_through_durable_queue(db, monkeypatch, max_attempts, expected):
    stop, started = asyncio.Event(), asyncio.Event()
    runner = EnrichmentWorker(db=db, embeddings_enabled=False)
    job_id = await runner.queue.enqueue("synthetic_drain", max_attempts=max_attempts)
    cleaned = []

    async def process(job):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.append(True)

    monkeypatch.setattr(runner, "_process_job", process)
    task = asyncio.create_task(drain_worker(runner, stop, grace_seconds=0.01))
    await started.wait()
    stop.set()
    await asyncio.wait_for(task, 2)
    assert cleaned == [True]
    row = await db.fetchone("SELECT status,last_error,lease_token,attempts FROM jobs WHERE id=?", [job_id])
    assert row == {"status": expected, "last_error": "embedding_worker_interrupted",
                   "lease_token": None, "attempts": 1}


@pytest.mark.asyncio
async def test_unexpected_worker_failure_propagates():
    class BrokenWorker:
        async def run_forever(self, stop):
            raise RuntimeError("synthetic failure")

    with pytest.raises(RuntimeError, match="synthetic failure"):
        await drain_worker(BrokenWorker(), asyncio.Event(), grace_seconds=1)


@pytest.mark.asyncio
async def test_parent_cancellation_cleans_up_job_and_heartbeat(db, monkeypatch):
    started = asyncio.Event()
    runner = EnrichmentWorker(db=db, embeddings_enabled=False)
    job_id = await runner.queue.enqueue("synthetic_drain")

    async def process(job):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(runner, "_process_job", process)
    before = asyncio.all_tasks()
    task = asyncio.create_task(drain_worker(runner, asyncio.Event(), grace_seconds=1))
    await asyncio.wait_for(started.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    row = await db.fetchone("SELECT status,last_error,lease_token FROM jobs WHERE id=?", [job_id])
    assert row == {"status": "pending", "last_error": "embedding_worker_interrupted",
                   "lease_token": None}
    assert not (asyncio.all_tasks() - before)


@pytest.mark.asyncio
@pytest.mark.parametrize("once", [False, True])
async def test_stop_during_startup_prevents_first_claim(db, once):
    stop = asyncio.Event()
    stop.set()
    runner = EnrichmentWorker(db=db, embeddings_enabled=False)
    job_id = await runner.queue.enqueue("synthetic_drain")
    await drain_worker(runner, stop, grace_seconds=1, once=once)
    assert (await db.fetchone("SELECT attempts FROM jobs WHERE id=?", [job_id]))["attempts"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("handled", [True, False])
async def test_once_preserves_processed_result(handled):
    class OnceWorker:
        async def run_once(self):
            return handled

    assert await drain_worker(OnceWorker(), asyncio.Event(), grace_seconds=1, once=True) is handled


@pytest.mark.parametrize("value", [-1, 301, float("nan"), float("inf")])
def test_invalid_shutdown_grace_rejected(tmp_path, value):
    from rka.config import RKAConfig

    with pytest.raises(ValueError):
        RKAConfig(_env_file=None, data_dir=tmp_path, job_shutdown_grace_seconds=value)


@pytest.mark.asyncio
async def test_signal_handlers_restore_even_on_failure():
    before = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}
    stop = asyncio.Event()
    with pytest.raises(RuntimeError), worker_shutdown_signals(stop):
        signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
        await asyncio.sleep(0)
        assert stop.is_set()
        raise RuntimeError("synthetic failure")
    assert before == {s: signal.getsignal(s) for s in before}


@pytest.mark.asyncio
async def test_embedded_thread_does_not_replace_host_signals():
    def embedded():
        async def run():
            with worker_shutdown_signals(asyncio.Event()):
                pass
        asyncio.run(run())

    before = signal.getsignal(signal.SIGTERM)
    await asyncio.to_thread(embedded)
    assert signal.getsignal(signal.SIGTERM) == before


@pytest.mark.skipif(os.name != "posix", reason="Windows terminate() does not deliver SIGTERM")
@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
@pytest.mark.parametrize("busy,once", [(False, False), (True, False), (True, True)])
def test_real_cli_signal_releases_database_and_requeues(tmp_path, sig, busy, once):
    # A synthetic worker method avoids any network/model and records readiness
    # only after CLI signal registration. No production state or process is used.
    marker = tmp_path / "ready"
    code = '''
import asyncio, os
from pathlib import Path
from rka.cli import main
from rka.services.worker import EnrichmentWorker
original = EnrichmentWorker.run_forever
async def process(self, job):
    Path(os.environ["TEST_READY"]).touch()
    await asyncio.Event().wait()
async def run(self, stop_event=None):
    if os.environ["TEST_BUSY"] == "1":
        await self.queue.enqueue("synthetic_drain")
    else:
        Path(os.environ["TEST_READY"]).touch()
    await original(self, stop_event)
EnrichmentWorker._process_job = process
EnrichmentWorker.run_forever = run
args = ["worker"]
if os.environ["TEST_ONCE"] == "1":
    original_once = EnrichmentWorker.run_once
    async def run_once(self):
        await self.queue.enqueue("synthetic_drain")
        return await original_once(self)
    EnrichmentWorker.run_once = run_once
    args.append("--once")
main(args)
'''
    env = {key: value for key, value in os.environ.items()
           if not key.startswith("RKA_") and key not in {"PYTHONPATH", "PYTHONHOME"}}
    env.update(RKA_DATA_DIR=str(tmp_path), RKA_EMBEDDINGS_ENABLED="false",
               RKA_JOB_SHUTDOWN_GRACE_SECONDS="0.05", PYTHONUNBUFFERED="1",
               PYTHONPATH=str(Path(__file__).resolve().parents[2]),
               TEST_READY=str(marker), TEST_BUSY="1" if busy else "0",
               TEST_ONCE="1" if once else "0")
    process = subprocess.Popen([sys.executable, "-c", code], cwd=tmp_path, env=env,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 20
        while not marker.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert marker.exists(), process.communicate(timeout=1)
        process.send_signal(sig)
        out, err = process.communicate(timeout=5)
        assert process.returncode == 0, (out, err)
        import sqlite3

        from rka.infra.runtime_lease import RuntimeLease
        lease = RuntimeLease(tmp_path / "rka.db", maintenance=True)
        with lease, sqlite3.connect(tmp_path / "rka.db") as conn:
            assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            if busy:
                row = conn.execute("SELECT status,last_error,lease_token FROM jobs").fetchone()
                assert row == ("pending", "embedding_worker_interrupted", None)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)
