"""End-to-end regression for the disposable Core recovery smoke."""

from __future__ import annotations

import copy
import json
import sqlite3
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import scripts.core_recovery_smoke as recovery_smoke
from rka.infra.database import Database
from rka.models.journal import JournalEntryCreate
from rka.models.project import ProjectCreate
from rka.services.artifacts import ArtifactService
from rka.services.notes import NoteService
from rka.services.project import ProjectService
from scripts.core_recovery_smoke import (
    PORTABLE_CORE_TABLES,
    _compare_upgrade,
    _database_snapshot,
    _normalize_expected_target_row,
    _normalize_target_row,
    _restore_exact,
    _run,
    _source_manifest_comparison,
    main,
)


def test_source_manifest_oracle_detects_an_omitted_core_table() -> None:
    source_tables = {table: [] for table in PORTABLE_CORE_TABLES}
    source_tables["journal"] = [
        {"id": "jrn_source", "project_id": "proj_source", "content": "kept"}
    ]
    manifest_tables = dict(source_tables)
    manifest_tables.pop("journal")

    comparison = _source_manifest_comparison(source_tables, manifest_tables)

    assert comparison["passed"] is False
    assert comparison["missing_tables"] == ["journal"]
    assert comparison["mismatched_tables"] == ["journal"]


def test_forward_oracle_detects_an_unmapped_source_reference() -> None:
    source_row = {
        "id": "lnk_source",
        "project_id": "proj_source",
        "source_id": "jrn_source",
        "target_id": "dec_source",
    }
    id_map = {
        "lnk_source": "lnk_target",
        "jrn_source": "jrn_target",
        "dec_source": "dec_target",
    }
    expected = _normalize_expected_target_row(
        "entity_links",
        source_row,
        id_map=id_map,
        source_project_id="proj_source",
        target_project_id="proj_target",
    )
    bad_import = {
        "id": "lnk_target",
        "project_id": "proj_target",
        "source_id": "jrn_target",
        "target_id": "dec_source",
    }

    assert _normalize_target_row("entity_links", bad_import) != expected


def test_upgrade_comparison_rejects_ledger_rewrite(tmp_path: Path) -> None:
    source = tmp_path / "ledger.db"
    with sqlite3.connect(source) as connection:
        connection.execute(
            "CREATE TABLE schema_migrations (filename TEXT PRIMARY KEY)"
        )
        connection.execute(
            "CREATE TABLE runtime_schema_upgrades (name TEXT PRIMARY KEY)"
        )
        connection.execute("INSERT INTO schema_migrations VALUES ('001.sql')")
        connection.execute("INSERT INTO runtime_schema_upgrades VALUES ('phase2')")
    before = _database_snapshot(source)
    after = copy.deepcopy(before)
    after["schema_migrations"]["entries"] = []
    after["schema_migrations"]["count"] = 0

    comparison = _compare_upgrade(before, after)

    assert comparison["passed"] is False
    assert comparison["schema_migration_ledger_equal"] is False


def test_offline_restore_replaces_target_and_removes_stale_sidecars(
    tmp_path: Path,
) -> None:
    backup = tmp_path / "backup.db"
    target = tmp_path / "target.db"
    backup.write_bytes(b"known-good-backup")
    target.write_bytes(b"upgraded-target")
    sidecars = [
        Path(f"{target}{suffix}")
        for suffix in ("-wal", "-shm", "-journal", ".phase2.lock")
    ]
    for sidecar in sidecars:
        sidecar.write_bytes(b"stale-runtime-state")

    _restore_exact(backup, target)

    assert target.read_bytes() == backup.read_bytes()
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert all(not sidecar.exists() for sidecar in sidecars)


@pytest.mark.parametrize("suffix", ["", "-wal", "-shm", "-journal", ".phase2.lock"])
def test_recovery_report_cannot_replace_database_runtime_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    suffix: str,
) -> None:
    source = tmp_path / "source.db"
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE records (id INTEGER PRIMARY KEY)")
    report = Path(f"{source}{suffix}")
    if report != source:
        report.write_bytes(b"runtime-state")
    before = report.read_bytes()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "core_recovery_smoke.py",
            "--source-db",
            str(source),
            "--project-id",
            "proj_unused",
            "--report",
            str(report),
        ],
    )

    with pytest.raises(SystemExit):
        main()

    assert report.read_bytes() == before


@pytest.mark.parametrize("use_alias_path", [False, True])
def test_recovery_report_rejects_runtime_symlink_and_its_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    use_alias_path: bool,
) -> None:
    source = tmp_path / "source.db"
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE records (id INTEGER PRIMARY KEY)")
    victim = tmp_path / "victim.bin"
    victim.write_bytes(b"do-not-replace")
    sidecar_alias = Path(f"{source}.phase2.lock")
    sidecar_alias.symlink_to(victim)
    report = sidecar_alias if use_alias_path else victim
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "core_recovery_smoke.py",
            "--source-db",
            str(source),
            "--project-id",
            "proj_unused",
            "--report",
            str(report),
        ],
    )

    with pytest.raises(SystemExit):
        main()

    assert victim.read_bytes() == b"do-not-replace"
    assert sidecar_alias.is_symlink()


def test_failure_report_omits_exception_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.db"
    source.write_bytes(b"not-opened-by-this-test")
    report = tmp_path / "report.json"

    async def fail(_args) -> dict:
        raise RuntimeError("/sensitive/path prj_sensitive")

    monkeypatch.setattr(recovery_smoke, "_run", fail)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "core_recovery_smoke.py",
            "--source-db",
            str(source),
            "--project-id",
            "proj_unused",
            "--report",
            str(report),
        ],
    )

    with pytest.raises(SystemExit):
        main()

    persisted = json.loads(report.read_text())
    assert persisted == {
        "schema": "rka-core-recovery/v1",
        "passed": False,
        "stage": "recovery_validation",
        "error_type": "RuntimeError",
    }
    assert "sensitive" not in report.read_text()


@pytest.mark.asyncio
async def test_recovery_smoke_preserves_upgrade_pack_and_rollback(tmp_path: Path) -> None:
    source_path = tmp_path / "source.db"
    database = Database(str(source_path))
    await database.connect()
    await database.initialize_schema()
    await database.initialize_phase2_schema()
    try:
        await ProjectService(database).create_project(
            ProjectCreate(id="proj_recovery_source", name="Recovery Source"),
            actor="system",
        )
        await NoteService(database, project_id="proj_recovery_source").create(
            JournalEntryCreate(
                content="The recovery smoke preserves this canonical journal entry.",
                type="finding",
                source="executor",
                confidence="tested",
            ),
            actor="executor",
        )
        artifact_path = tmp_path / "recovery-evidence.txt"
        artifact_path.write_bytes(b"portable recovery evidence")
        from rka.infra.file_access import FileAccessPolicy
        await ArtifactService(database, project_id="proj_recovery_source", file_policy=FileAccessPolicy([tmp_path])).register(
            filepath=str(artifact_path),
            created_by="system",
            metadata={"kind": "recovery", "ordinal": 1},
        )
    finally:
        await database.close()

    report = await _run(
        SimpleNamespace(
            source_db=source_path,
            project_id=["proj_recovery_source"],
        )
    )

    assert report["passed"] is True
    assert report["upgrade"]["comparison"]["changed_tables"] == []
    assert report["upgrade"]["second_idempotence_pass_applied"] == 0
    assert report["knowledge_packs"][0]["passed"] is True
    assert report["knowledge_packs"][0]["artifact_bytes_preserved"] is True
    assert report["rollback"]["exact_backup_bytes_restored"] is True
    assert report["rollback"]["logical_snapshot_restored"] is True
