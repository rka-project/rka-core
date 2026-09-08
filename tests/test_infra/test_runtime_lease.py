"""Native, spawn-based lifetime admission; no real services or research data."""

import asyncio
import multiprocessing

import pytest

from rka.infra.database import Database
from rka.infra.runtime_lease import MaintenanceBusy, RuntimeLease


def _hold(path, maintenance, ready, release):
    with RuntimeLease(path, maintenance=maintenance):
        ready.set()
        release.wait(20)


@pytest.mark.parametrize("maintenance", [False, True])
@pytest.mark.parametrize("kill", [False, True])
def test_native_lifetime_contention_and_owner_death(tmp_path, maintenance, kill):
    path = tmp_path / "db.sqlite"
    ctx = multiprocessing.get_context("spawn")
    ready, release = ctx.Event(), ctx.Event()
    proc = ctx.Process(target=_hold, args=(str(path), maintenance, ready, release))
    proc.start()
    try:
        assert ready.wait(10)
        if not maintenance:
            with RuntimeLease(path):
                with pytest.raises(MaintenanceBusy):
                    RuntimeLease(path, maintenance=True).acquire()
        else:
            with pytest.raises(MaintenanceBusy):
                RuntimeLease(path).acquire()
        with pytest.raises(MaintenanceBusy):
            RuntimeLease(path, maintenance=True).acquire()
        if kill:
            proc.kill()
        else:
            release.set()
        proc.join(10)
        assert not proc.is_alive()
        with RuntimeLease(path, maintenance=True):
            pass
    finally:
        if proc.is_alive():
            proc.kill()
        proc.join(10)
        proc.close()


@pytest.mark.asyncio
async def test_database_and_readonly_handles_participate(tmp_path):
    from rka.infra.readonly_sqlite import readonly_sqlite
    path = tmp_path / "db.sqlite"
    first, second = Database(str(path)), Database(str(path))
    await first.connect()
    await second.connect()
    with pytest.raises(MaintenanceBusy):
        RuntimeLease(path, maintenance=True).acquire()
    await first.close()
    with pytest.raises(MaintenanceBusy):
        RuntimeLease(path, maintenance=True).acquire()
    await second.close()
    async with readonly_sqlite(path):
        with pytest.raises(MaintenanceBusy):
            RuntimeLease(path, maintenance=True).acquire()
    with RuntimeLease(path, maintenance=True):
        with pytest.raises(MaintenanceBusy):
            await first.connect()
        with pytest.raises(MaintenanceBusy):
            async with readonly_sqlite(path):
                pass


@pytest.mark.asyncio
async def test_failed_connect_releases_only_after_cleanup(tmp_path, monkeypatch):
    path = tmp_path / "db.sqlite"
    db = Database(str(path))
    async def fail():
        raise RuntimeError("injected")
    monkeypatch.setattr(db, "_connect", fail)
    with pytest.raises(RuntimeError, match="injected"):
        await db.connect()
    with RuntimeLease(path, maintenance=True):
        pass


@pytest.mark.asyncio
async def test_cancelled_close_keeps_lease_until_sqlite_closes(tmp_path, monkeypatch):
    path = tmp_path / "db.sqlite"
    db = Database(str(path))
    await db.connect()
    entered, release = asyncio.Event(), asyncio.Event()
    original = db._conn.close
    async def delayed():
        entered.set()
        await release.wait()
        await original()
    monkeypatch.setattr(db._conn, "close", delayed)
    task = asyncio.create_task(db.close())
    await entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    with pytest.raises(MaintenanceBusy):
        RuntimeLease(path, maintenance=True).acquire()
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    with RuntimeLease(path, maintenance=True):
        pass
    assert db._conn is None


@pytest.mark.asyncio
async def test_close_failure_keeps_lease_for_retry(tmp_path, monkeypatch):
    path = tmp_path / "db.sqlite"
    db = Database(str(path))
    await db.connect()
    original = db._conn.close
    async def fail():
        raise OSError("injected close error")
    monkeypatch.setattr(db._conn, "close", fail)
    with pytest.raises(OSError):
        await db.close()
    with pytest.raises(MaintenanceBusy):
        RuntimeLease(path, maintenance=True).acquire()
    monkeypatch.setattr(db._conn, "close", original)
    await db.close()


def test_pending_intent_blocks_normal_admission_and_is_not_erased(tmp_path):
    path = tmp_path / "db.sqlite"
    with RuntimeLease(path, maintenance=True) as lease:
        lease.intent_path.write_text("untrusted unfinished record")
    with pytest.raises(MaintenanceBusy, match="unfinished"):
        RuntimeLease(path).acquire()
    with RuntimeLease(path, maintenance=True):
        pass
    assert lease.intent_path.exists()


def test_alias_and_unsafe_lock_paths(tmp_path):
    path = tmp_path / "db.sqlite"
    path.touch()
    with RuntimeLease(path):
        with pytest.raises(MaintenanceBusy):
            RuntimeLease(tmp_path / "." / "db.sqlite", maintenance=True).acquire()
    linked = tmp_path / "hardlink.sqlite"
    linked.hardlink_to(path)
    with pytest.raises(ValueError, match="hardlinked"):
        RuntimeLease(linked)
    with pytest.raises(ValueError, match="URI"):
        RuntimeLease("file:unsafe.db?mode=ro")


def test_config_writes_are_excluded(tmp_path):
    from rka.services.embedding_config import EmbeddingConfigService, DEFAULT_CONFIG
    with RuntimeLease(tmp_path / "embedding_config.json", maintenance=True):
        with pytest.raises(MaintenanceBusy):
            EmbeddingConfigService(tmp_path).save_config(DEFAULT_CONFIG, "system")
    assert not (tmp_path / "embedding_config.json").exists()


@pytest.mark.asyncio
async def test_api_failed_startup_closes_lifetime_lease(tmp_path, monkeypatch):
    from rka.api.app import create_app, lifespan
    from rka.config import RKAConfig
    config = RKAConfig(data_dir=tmp_path, embeddings_enabled=False, llm_enabled=False)
    app = create_app(config)
    async def fail(self):
        raise RuntimeError("injected startup")
    monkeypatch.setattr(Database, "initialize_schema", fail)
    with pytest.raises(RuntimeError, match="startup"):
        async with lifespan(app):
            pass
    with RuntimeLease(config.database_url, maintenance=True):
        pass


@pytest.mark.asyncio
async def test_cancel_during_open_waits_for_connection_and_closes_it(tmp_path, monkeypatch):
    path = tmp_path / "db.sqlite"
    db = Database(str(path))
    original = db._connect
    entered, release = asyncio.Event(), asyncio.Event()
    async def delayed():
        entered.set()
        await release.wait()
        await original()
    monkeypatch.setattr(db, "_connect", delayed)
    task = asyncio.create_task(db.connect())
    await entered.wait()
    task.cancel()
    with pytest.raises(MaintenanceBusy):
        RuntimeLease(path, maintenance=True).acquire()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert db._conn is None
    with RuntimeLease(path, maintenance=True):
        pass


@pytest.mark.asyncio
async def test_close_cannot_release_lease_while_connect_is_in_flight(tmp_path, monkeypatch):
    path = tmp_path / "db.sqlite"
    db = Database(str(path))
    original = db._connect
    entered, release = asyncio.Event(), asyncio.Event()
    async def delayed():
        entered.set()
        await release.wait()
        await original()
    monkeypatch.setattr(db, "_connect", delayed)
    opening = asyncio.create_task(db.connect())
    await entered.wait()
    closing = asyncio.create_task(db.close())
    try:
        await asyncio.sleep(0.02)
        with pytest.raises(MaintenanceBusy):
            RuntimeLease(path, maintenance=True).acquire().close()
    finally:
        release.set()
        await asyncio.gather(opening, closing)
        await db.close()
    assert db._conn is None


@pytest.mark.asyncio
async def test_concurrent_connect_does_not_replace_a_live_connection(tmp_path):
    path = tmp_path / "db.sqlite"
    db = Database(str(path))
    results = await asyncio.gather(db.connect(), db.connect(), return_exceptions=True)
    try:
        assert sum(isinstance(result, RuntimeError) for result in results) == 1
        assert sum(result is None for result in results) == 1
        with pytest.raises(MaintenanceBusy):
            RuntimeLease(path, maintenance=True).acquire()
    finally:
        await db.close()
    with RuntimeLease(path, maintenance=True):
        pass
