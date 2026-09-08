"""Operator-only, globally exclusive, backed-up embedding schema recovery.

Offline work transitions derived schema/config and durably queues the existing
Core worker. Inference resumes after the operator restarts services. Unfinished
intents block runtime admission; rollback is allowed only before that admission.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import time
import uuid
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

from rka.infra.database import Database
from rka.infra.embedding_documents import DOCUMENT_SPECS
from rka.infra.readonly_sqlite import readonly_sqlite
from rka.infra.runtime_lease import RuntimeLease, canonical_path
from rka.infra.sqlite_backup import backup_sqlite_database, fsync_directory, _sha256
from rka.services.embedding_inspection import _read_config, _inspect_snapshot, plan_embedding_recovery
from rka.services.embedding_index import reconcile_embedding_index, get_embedding_index_state
from rka.services.embedding_jobs import EmbeddingJobs, BACKFILL_JOB_TYPES
from rka.services.embedding_verification import iter_stored_document_checks


class EmbeddingRecoveryError(RuntimeError):
    """Fixed non-sensitive operator error code."""


def _digest(raw):
    return hashlib.sha256(raw).hexdigest()


def _atomic(path: Path, raw: bytes):
    if path.is_symlink() or (path.exists() and path.stat().st_nlink != 1):
        raise EmbeddingRecoveryError("unsafe_recovery_file")
    fd, name = tempfile.mkstemp(prefix=".recovery-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        os.replace(name, path)
        fsync_directory(path.parent)
    finally:
        Path(name).unlink(missing_ok=True)


def _json_write(path, value):
    _atomic(path, json.dumps(value, sort_keys=True).encode())


def _read_json(path):
    if path.is_symlink():
        raise EmbeddingRecoveryError("unsafe_recovery_file")
    with path.open("rb") as stream:
        raw = stream.read(65537)
    if len(raw) > 65536:
        raise EmbeddingRecoveryError("recovery_record_too_large")
    return json.loads(raw)


def _checkpoint(stage):
    """Failure-injection seam; production intentionally has no environment hook."""


async def _snapshot(source, identity, lease):
    async with readonly_sqlite(source, maintenance_lease=lease, timeout_seconds=60) as db:
        return await _inspect_snapshot(db, identity, max_rows=100000, deadline=time.monotonic() + 60)


def _verify(intent, lease, config_path):
    if (intent.get("version") != 1 or intent.get("database") != str(lease.path)
            or intent.get("config_path") != str(config_path)
            or intent.get("mode") not in {"rebuild", "rollback"}):
        raise EmbeddingRecoveryError("recovery_identity_mismatch")
    identifier = intent.get("id", "")
    if len(identifier) != 32 or any(c not in "0123456789abcdef" for c in identifier):
        raise EmbeddingRecoveryError("recovery_identity_mismatch")
    folder = lease.directory / identifier
    if folder.is_symlink() or not folder.is_dir():
        raise EmbeddingRecoveryError("recovery_backup_missing")
    for name, key in (("database.sqlite", "database_sha256"), ("original.json", "original_sha256"), ("target.json", "target_sha256")):
        path = folder / name
        if path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1 or _sha256(path) != intent[key]:
            raise EmbeddingRecoveryError("recovery_backup_verification_failed")
    if _digest(_read_config(config_path)[0]) not in {intent["original_sha256"], intent["target_sha256"]}:
        raise EmbeddingRecoveryError("configuration_changed_outside_recovery")
    info = lease.path.stat()
    if [info.st_dev, info.st_ino] != intent["database_identity"]:
        raise EmbeddingRecoveryError("database_replaced_outside_recovery")
    return folder


async def _repair_hashes(db, target):
    async for check in iter_stored_document_checks(db, model_name=target["model_name"], dimensions=target["dimensions"]):
        if check.status == "verified":
            continue
        spec = DOCUMENT_SPECS[check.entity_type]
        extra = " AND entity_type=?" if spec.vec_table == "vec_artifacts" else ""
        await db.execute(f"DELETE FROM {spec.vec_table} WHERE id=? AND project_id=?{extra}",
                         [check.entity_id, check.project_id, *([check.entity_type] if extra else [])])
        await db.execute("DELETE FROM embedding_metadata WHERE entity_type=? AND entity_id=? AND project_id=?",
                         [check.entity_type, check.entity_id, check.project_id])
        if check.entity_type == "claim":
            await db.execute("UPDATE claims SET embedding_pending=1 WHERE id=? AND project_id=?", [check.entity_id, check.project_id])


async def _transition(intent, lease, folder):
    target_raw, target = _read_config(folder / "target.json")
    db = Database(str(lease.path), maintenance_lease=lease)
    await db.connect()
    try:
        await db._load_sqlite_vec()
        if not db.vec_available:
            raise EmbeddingRecoveryError("sqlite_vec_required")
        async with db.transaction(migration_lock=True):
            # A private kv ledger commits with DDL/jobs. It disambiguates
            # death after COMMIT but before configuration publication.
            key = "embedding_recovery_commit:" + intent["id"]
            prior = await db.fetchone("SELECT value FROM kv_store WHERE key=?", [key])
            if prior:
                state = await get_embedding_index_state(db)
                if (not state or state.generation != int(prior["value"])
                        or state.space_signature != target["space_signature"]):
                    raise EmbeddingRecoveryError("generation_changed_outside_recovery")
            else:
                current = await get_embedding_index_state(db)
                if (current.generation if current else None) != intent["source_generation"]:
                    raise EmbeddingRecoveryError("generation_changed_outside_recovery")
                result = await reconcile_embedding_index(
                    db, space_signature=target["space_signature"], model_name=target["model_name"], dim=target["dimensions"],
                    allow_legacy_adoption=target["legacy_adoption_safe"], maintenance_lease=lease,
                    force_rebuild=intent["action"] in {"offline_rebuild", "rebuild_space"},
                )
                state = result.state
                if intent["action"] in {"repair_rows", "adopt_legacy"}:
                    await _repair_hashes(db, target)
                if intent["action"] != "none":
                    # Exclusion proves no live owners. Retain prior job history,
                    # but revoke stale same-generation scope/lease before enqueue.
                    await db.execute("UPDATE jobs SET status='failed', lease_until=NULL, worker_id=NULL, lease_token=NULL, last_error='offline_recovery_replaced_job' WHERE job_type IN (?,?) AND status IN ('pending','running')", list(BACKFILL_JOB_TYPES))
                    identity = SimpleNamespace(index_generation=state.generation, space_signature=state.space_signature,
                                               model_name=state.model_name, dim=state.dimensions)
                    await EmbeddingJobs(db).request(identity, check_hashes=True)
                await db.execute("INSERT INTO kv_store (key, value) VALUES (?,?)", [key, str(state.generation)])
                _checkpoint("before_database_commit")
        _checkpoint("after_database_commit")
        return state.generation, target_raw
    finally:
        await db.close()


def _rollback(folder, source):
    # SQLite's backup API restores into the same inode and handles WAL safely.
    # Never rename a DB over live sidecars or delete a user's recovery copy.
    with closing(sqlite3.connect((folder / "database.sqlite").as_uri() + "?mode=ro", uri=True)) as backup:
        backup.execute("PRAGMA query_only=ON")
        with closing(sqlite3.connect(source)) as destination:
            backup.backup(destination)
            if destination.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise EmbeddingRecoveryError("rollback_integrity_failed")


async def recover_embedding_index(*, data_dir: Path, db_path: Path | None = None,
                                  target_config: Path | None = None, operation="rebuild", peers_stopped=False):
    """Explicit operator stop is mandatory; never stop/kill services ourselves."""
    if not peers_stopped:
        raise EmbeddingRecoveryError("operator_must_confirm_all_peers_stopped")
    if operation not in {"rebuild", "resume", "rollback"}:
        raise EmbeddingRecoveryError("unsupported_recovery_operation")
    config_path = canonical_path(data_dir / "embedding_config.json")
    source = canonical_path(db_path or data_dir / "rka.db")
    if not source.is_file():
        raise EmbeddingRecoveryError("database_missing_initialize_core_first")
    # Lock both namespaces: standalone config saves also participate. Fail-fast
    # acquisition avoids deadlock with normal DB -> config-save entrypoints.
    with RuntimeLease(config_path, maintenance=True) as config_lease, RuntimeLease(source, maintenance=True) as lease:
        if operation == "rebuild":
            if lease.intent_path.exists():
                raise EmbeddingRecoveryError("unfinished_recovery_use_resume_or_rollback")
            original, identity = _read_config(config_path)
            target_raw, target = _read_config(target_config) if target_config else (original, identity)
            report = await _snapshot(source, identity, lease)
            plan = plan_embedding_recovery(report, target)
            if plan["action"] in {"blocked", "resolve_source_errors"}:
                raise EmbeddingRecoveryError("index_preflight_blocked")
            identifier = uuid.uuid4().hex
            folder = lease.directory / identifier
            folder.mkdir(mode=0o700)
            result = backup_sqlite_database(source, folder / "database.sqlite", maintenance_lease=lease)
            if result.foreign_key_violations:
                raise EmbeddingRecoveryError("backup_has_foreign_key_violations")
            _atomic(folder / "original.json", original)
            _atomic(folder / "target.json", target_raw)
            if _read_config(config_path)[0] != original or (target_config and _read_config(target_config)[0] != target_raw):
                raise EmbeddingRecoveryError("config_changed_during_backup")
            info = source.stat()
            intent = {"version": 1, "id": identifier, "mode": "rebuild", "database": str(source),
                      "database_identity": [info.st_dev, info.st_ino], "config_path": str(config_path),
                      "database_sha256": result.sha256, "original_sha256": _digest(original),
                      "target_sha256": _digest(target_raw), "action": plan["action"],
                      "source_generation": report["generation"]["generation"] if report["generation"] else None}
            _json_write(folder / "manifest.json", intent)
            _json_write(lease.intent_path, intent)
            _json_write(config_lease.intent_path, {"database": str(source)})
            _checkpoint("after_intent")
        else:
            if not lease.intent_path.exists():
                raise EmbeddingRecoveryError("no_unfinished_recovery")
            intent = _read_json(lease.intent_path)
        folder = _verify(intent, lease, config_path)
        _json_write(config_lease.intent_path, {"database": str(source)})
        if operation == "rollback" or intent["mode"] == "rollback":
            intent["mode"] = "rollback"
            _json_write(lease.intent_path, intent)
            _rollback(folder, source)
            _checkpoint("after_rollback_database")
            _atomic(config_path, (folder / "original.json").read_bytes())
            result = {"status": "rolled_back", "generation": intent["source_generation"]}
        else:
            generation, target_raw = await _transition(intent, lease, folder)
            _atomic(config_path, target_raw)
            _checkpoint("after_config_publish")
            result = {"status": "prepared", "generation": generation,
                      "backfill": "not_needed" if intent["action"] == "none" else "queued",
                      "next_step": "restart_core_services_for_durable_backfill"}
        result.update({"version": 1, "scope": "global", "recovery_id": intent["id"], "backup_directory": str(folder)})
        _json_write(folder / "result.json", result)
        config_lease.intent_path.unlink()
        fsync_directory(config_lease.directory)
        lease.intent_path.unlink()
        fsync_directory(lease.directory)
        return result
