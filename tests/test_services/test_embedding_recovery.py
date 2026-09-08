"""Offline recovery on disposable stores with real sqlite-vec, no providers."""

import json
from pathlib import Path
import subprocess
import sys

import pytest

from rka.infra.runtime_lease import MaintenanceBusy, RuntimeLease
from rka.services import embedding_recovery as recovery
from rka.services.embedding_inspection import inspect_embedding_index
from rka.services.worker import EnrichmentWorker
from rka.infra.embeddings import EmbeddingService
from tests.test_cli.test_embedding_inspect import setup_store
from tests.test_services.test_embedding_documents import RecordingBackend


async def prepare(db, dim=384):
    config = await setup_store(db)
    folder = Path(db.db_path).parent
    candidate = folder / "candidate.json"
    config.config["dim"] = dim
    candidate.write_text(config.model_dump_json())
    await db.close()
    return dict(data_dir=folder, db_path=Path(db.db_path), target_config=candidate, peers_stopped=True)


@pytest.mark.asyncio
async def test_populated_dimension_transition_and_real_worker_queue(db):
    args = await prepare(db)
    result = await recovery.recover_embedding_index(**args)
    assert result["status"] == "prepared" and result["backfill"] == "queued"
    assert (Path(result["backup_directory"]) / "database.sqlite").is_file()
    await db.connect()
    await db._load_sqlite_vec()
    assert (await db.fetchone("SELECT count(*) n FROM journal"))["n"] == 1
    assert (await db.fetchone("SELECT count(*) n FROM embedding_metadata"))["n"] == 0
    state = await db.fetchone("SELECT * FROM embedding_index_state")
    assert state["dimensions"] == 384 and state["status"] == "reindexing"
    backend = RecordingBackend()
    backend.dim = 384
    service = EmbeddingService(db=db, backend=backend)
    service.bind_index_generation(state["generation"], space_signature=state["space_signature"])
    worker = EnrichmentWorker(db=db, embeddings=service)
    assert await worker.run_once()
    assert (await db.fetchone("SELECT status FROM embedding_index_state"))["status"] == "ready"
    assert (await db.fetchone("SELECT count(*) n FROM embedding_metadata"))["n"] == 7


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["after_intent", "before_database_commit", "after_database_commit", "after_config_publish"])
@pytest.mark.parametrize("operation", ["resume", "rollback"])
async def test_failure_boundaries_resume_or_restore_coherent_pair(db, monkeypatch, stage, operation):
    args = await prepare(db)
    original_config = (args["data_dir"] / "embedding_config.json").read_bytes()
    def fail(at):
        if at == stage:
            raise RuntimeError("injected")
    monkeypatch.setattr(recovery, "_checkpoint", fail)
    with pytest.raises(RuntimeError, match="injected"):
        await recovery.recover_embedding_index(**args)
    with pytest.raises(MaintenanceBusy, match="unfinished"):
        await db.connect()
    monkeypatch.setattr(recovery, "_checkpoint", lambda at: None)
    args.pop("target_config")
    result = await recovery.recover_embedding_index(**args, operation=operation)
    assert result["status"] == ("prepared" if operation == "resume" else "rolled_back")
    await db.connect()
    await db._load_sqlite_vec()
    state = await db.fetchone("SELECT dimensions, generation FROM embedding_index_state")
    assert state["dimensions"] == (384 if operation == "resume" else 768)
    assert state["generation"] == (2 if operation == "resume" else 1)
    assert (await db.fetchone("SELECT count(*) n FROM journal"))["n"] == 1
    if operation == "rollback":
        assert (args["data_dir"] / "embedding_config.json").read_bytes() == original_config
        assert (await db.fetchone("SELECT count(*) n FROM embedding_metadata"))["n"] == 7


@pytest.mark.asyncio
async def test_live_client_and_missing_operator_confirmation_are_refused(db):
    await setup_store(db)
    args = dict(data_dir=Path(db.db_path).parent, db_path=Path(db.db_path))
    with pytest.raises(recovery.EmbeddingRecoveryError, match="confirm"):
        await recovery.recover_embedding_index(**args)
    with pytest.raises(MaintenanceBusy, match="still connected"):
        await recovery.recover_embedding_index(**args, peers_stopped=True)


@pytest.mark.asyncio
async def test_selective_managed_hash_repair_keeps_other_six_vectors(db):
    args = await prepare(db, dim=768)
    await db.connect()
    await db._load_sqlite_vec()
    await db.execute("UPDATE embedding_metadata SET content_hash='wrong' WHERE entity_type='journal'")
    preserved = await db.fetchone("SELECT embedding FROM vec_claims")
    await db.close()
    await recovery.recover_embedding_index(**args)
    await db.connect()
    await db._load_sqlite_vec()
    assert await db.fetchone("SELECT embedding FROM vec_claims") == preserved
    assert (await db.fetchone("SELECT count(*) n FROM embedding_metadata"))["n"] == 6
    assert (await db.fetchone("SELECT generation FROM embedding_index_state"))["generation"] == 1


@pytest.mark.asyncio
async def test_backup_tamper_blocks_resume_and_preserves_intent(db, monkeypatch):
    args = await prepare(db)
    def fail(at):
        if at == "after_intent":
            raise RuntimeError("injected")
    monkeypatch.setattr(recovery, "_checkpoint", fail)
    with pytest.raises(RuntimeError):
        await recovery.recover_embedding_index(**args)
    lease = RuntimeLease(db.db_path, maintenance=True)
    intent = json.loads(lease.intent_path.read_text())
    (lease.directory / intent["id"] / "target.json").write_text("tampered")
    args.pop("target_config")
    with pytest.raises(recovery.EmbeddingRecoveryError, match="verification"):
        await recovery.recover_embedding_index(**args, operation="resume")
    assert lease.intent_path.exists()


@pytest.mark.asyncio
async def test_completed_recovery_cannot_roll_back_later_research(db):
    args = await prepare(db)
    await recovery.recover_embedding_index(**args)
    args.pop("target_config")
    with pytest.raises(recovery.EmbeddingRecoveryError, match="no_unfinished"):
        await recovery.recover_embedding_index(**args, operation="rollback")


@pytest.mark.asyncio
async def test_query_only_config_change_keeps_ready_generation(db):
    args = await prepare(db, dim=768)
    candidate = json.loads(args["target_config"].read_text())
    candidate["config"]["query_template"] = "query: {text}"
    args["target_config"].write_text(json.dumps(candidate))
    result = await recovery.recover_embedding_index(**args)
    assert result["backfill"] == "not_needed"
    report = await inspect_embedding_index(data_dir=args["data_dir"], db_path=args["db_path"])
    assert report["assessment"]["status"] == "healthy"
    assert report["totals"]["reusable"] == 7


@pytest.mark.asyncio
async def test_empty_store_first_dimension_configuration(db):
    from rka.services.embedding_config import EmbeddingConfig, EmbeddingConfigService
    folder = Path(db.db_path).parent
    config = EmbeddingConfig(backend="openai_compat", config={"base_url": "http://unused.invalid", "model": "synthetic", "dim": 384})
    EmbeddingConfigService(folder).save_config(config, "system")
    await db.close()
    result = await recovery.recover_embedding_index(data_dir=folder, db_path=Path(db.db_path), peers_stopped=True)
    assert result["generation"] == 1
    await db.connect()
    assert (await db.fetchone("SELECT dimensions FROM embedding_index_state"))["dimensions"] == 384


@pytest.mark.asyncio
async def test_global_transition_preserves_other_projects_and_reference_text(db):
    args = await prepare(db)
    await db.connect()
    await db.execute("INSERT INTO projects (id,name) VALUES ('proj_other', 'Synthetic other')")
    await db.execute("INSERT INTO journal (id,type,content,source,project_id) VALUES ('jrn_other', 'note', 'Preserved source', 'pi', 'proj_other')")
    before = await db.fetchall("SELECT id,content,project_id FROM journal ORDER BY id")
    await db.close()
    await recovery.recover_embedding_index(**args)
    await db.connect()
    assert await db.fetchall("SELECT id,content,project_id FROM journal ORDER BY id") == before
    payload = json.loads((await db.fetchone("SELECT payload FROM jobs WHERE status='pending'"))["payload"])
    assert payload["project_id"] is None and len(payload["entity_types"]) == 7


@pytest.mark.asyncio
async def test_legacy_adoption_keeps_verified_pairs(db):
    args = await prepare(db, dim=768)
    await db.connect()
    await db._load_sqlite_vec()
    await db.execute("DELETE FROM embedding_index_state")
    await db.execute("UPDATE embedding_metadata SET content_hash='wrong' WHERE entity_type='journal'")
    await db.close()
    await recovery.recover_embedding_index(**args)
    await db.connect()
    assert (await db.fetchone("SELECT count(*) n FROM embedding_metadata"))["n"] == 6


@pytest.mark.asyncio
async def test_changed_document_policy_rebuilds_same_dimension(db):
    args = await prepare(db, dim=768)
    candidate = json.loads(args["target_config"].read_text())
    candidate["config"]["document_template"] = "document: {text}"
    args["target_config"].write_text(json.dumps(candidate))
    await recovery.recover_embedding_index(**args)
    await db.connect()
    assert (await db.fetchone("SELECT generation FROM embedding_index_state"))["generation"] == 2
    assert (await db.fetchone("SELECT count(*) n FROM embedding_metadata"))["n"] == 0


@pytest.mark.asyncio
async def test_failed_backup_never_sets_intent_or_changes_database(db, monkeypatch):
    args = await prepare(db)
    original = Path(db.db_path).read_bytes()
    def fail(*args, **kwargs):
        raise OSError("injected disk full")
    monkeypatch.setattr(recovery, "backup_sqlite_database", fail)
    with pytest.raises(OSError):
        await recovery.recover_embedding_index(**args)
    assert Path(db.db_path).read_bytes() == original
    assert not RuntimeLease(db.db_path).intent_path.exists()
    await db.connect()


@pytest.mark.asyncio
async def test_rollback_itself_is_restartable(db, monkeypatch):
    args = await prepare(db)
    def fail(at):
        if at in {"after_database_commit", "after_rollback_database"}:
            raise RuntimeError("injected")
    monkeypatch.setattr(recovery, "_checkpoint", fail)
    with pytest.raises(RuntimeError):
        await recovery.recover_embedding_index(**args)
    args.pop("target_config")
    with pytest.raises(RuntimeError):
        await recovery.recover_embedding_index(**args, operation="rollback")
    monkeypatch.setattr(recovery, "_checkpoint", lambda at: None)
    result = await recovery.recover_embedding_index(**args, operation="resume")
    assert result["status"] == "rolled_back"
    await db.connect()
    assert (await db.fetchone("SELECT count(*) n FROM embedding_metadata"))["n"] == 7


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["after_intent", "before_database_commit", "after_database_commit", "after_config_publish"])
@pytest.mark.parametrize("operation", ["resume", "rollback"])
async def test_real_process_death_recovery(db, stage, operation):
    args = await prepare(db)
    script = """
import asyncio, os, sys
from pathlib import Path
from rka.services import embedding_recovery as recovery
def crash(stage):
    if stage == sys.argv[3]:
        os._exit(17)
recovery._checkpoint = crash
asyncio.run(recovery.recover_embedding_index(data_dir=Path(sys.argv[1]).parent,
    db_path=Path(sys.argv[1]), target_config=Path(sys.argv[2]), peers_stopped=True))
"""
    child = subprocess.run([sys.executable, "-c", script, db.db_path, str(args["target_config"]), stage],
                           capture_output=True, text=True, timeout=30)
    assert child.returncode == 17, child.stderr
    with pytest.raises(MaintenanceBusy, match="unfinished"):
        await db.connect()
    args.pop("target_config")
    result = await recovery.recover_embedding_index(**args, operation=operation)
    assert result["status"] == ("prepared" if operation == "resume" else "rolled_back")
    await db.connect()
    assert (await db.fetchone("SELECT generation FROM embedding_index_state"))["generation"] == (2 if operation == "resume" else 1)
