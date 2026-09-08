#!/usr/bin/env python3
"""Validate an RKA backup, upgrade, pack round trip, and offline rollback.

The source database is opened only by SQLite's online-backup API. All schema
initialization, imports, and recovery writes happen under a temporary directory.
The JSON report contains counts and hashes, never research text or raw IDs.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from rka.infra.database import Database  # noqa: E402
from rka.infra.sqlite_backup import (  # noqa: E402
    backup_sqlite_database,
    fsync_directory,
    is_protected_sqlite_path,
)
from rka.services.knowledge_pack import (  # noqa: E402
    _INSERT_ORDER,
    KnowledgePackService,
)


REPORT_SCHEMA = "rka-core-recovery/v1"
LEDGER_TABLES = ("schema_migrations", "runtime_schema_upgrades")
INDEX_PREFIXES = ("fts_", "vec_")
LINK_TABLES = {
    "claim_edges",
    "claim_evidence_relations",
    "entity_links",
    "entity_topics",
    "evidence_locators",
    "interpretation_promotions",
    "tags",
}
INSTALLATION_LOCAL_INTEGRITY_CATEGORIES = {
    "index_check_incomplete",
    "orphaned_fts_rows",
    "orphaned_vector_rows",
    "stranded_entities",
}

# Portable Core semantics in the current pack contract. Writer compatibility
# rows are still imported and preserved, but their extraction contract belongs
# to E2 and is deliberately not redefined by this E1 recovery smoke.
PORTABLE_CORE_TABLES = {
    "literature",
    "decisions",
    "decision_options",
    "missions",
    "journal",
    "checkpoints",
    "interpretation_candidates",
    "interpretation_candidate_hints",
    "interpretation_review_events",
    "interpretation_promotions",
    "experiments",
    "experiment_plan_versions",
    "experiment_runs",
    "experiment_run_events",
    "experiment_observations",
    "evidence_locators",
    "claims",
    "claim_scope_versions",
    "claim_evidence_relations",
    "evidence_clusters",
    "claim_edges",
    "entity_links",
    "tags",
    "calibration_outcomes",
    "hooks",
    "artifacts",
    "figures",
}

IMPORT_IGNORED_COLUMNS: dict[str, set[str]] = {
    # Imported files live below the target project's managed artifact root.
    "artifacts": {"filepath", "pack_file"},
    # This hash is intentionally refreshed when a fresh claim is re-keyed and
    # is checked independently by _claim_scope_hashes_preserved.
    "claim_scope_versions": {"claim_content_hash"},
}


class CapturingKnowledgePackService(KnowledgePackService):
    """Expose the importer-created bijection only to this validation script."""

    imported_id_map: dict[str, str]

    def _build_id_map(self, tables: dict[str, list[dict[str, Any]]]) -> dict[str, str]:
        mapping = super()._build_id_map(tables)
        self.imported_id_map = dict(mapping)
        return mapping


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _quote_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _value_bytes(value: Any) -> bytes:
    if value is None:
        payload = b""
        marker = b"N"
    elif isinstance(value, bytes):
        payload = value
        marker = b"B"
    elif isinstance(value, float):
        payload = value.hex().encode()
        marker = b"F"
    elif isinstance(value, int):
        payload = str(value).encode()
        marker = b"I"
    else:
        payload = str(value).encode("utf-8", errors="surrogatepass")
        marker = b"T"
    return marker + len(payload).to_bytes(8, "big") + payload


def _row_digest(row: dict[str, Any], *, ignored: set[str] | None = None) -> str:
    ignored = ignored or set()
    digest = hashlib.sha256()
    for column in sorted(set(row) - ignored):
        digest.update(_value_bytes(column))
        digest.update(_value_bytes(row[column]))
    return digest.hexdigest()


def _rows_digest(
    rows: list[dict[str, Any]],
    *,
    ignored: set[str] | None = None,
) -> str:
    digest = hashlib.sha256()
    for row_hash in sorted(_row_digest(row, ignored=ignored) for row in rows):
        digest.update(row_hash.encode())
    return digest.hexdigest()


def _list_user_tables(connection: sqlite3.Connection) -> list[str]:
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
    ).fetchall()
    return [
        str(row[0])
        for row in rows
        if not str(row[0]).startswith("sqlite_")
        and not str(row[0]).startswith(INDEX_PREFIXES)
        and str(row[0]) not in LEDGER_TABLES
    ]


def _ledger(connection: sqlite3.Connection, table: str, column: str) -> dict[str, Any]:
    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        [table],
    ).fetchone()
    if exists is None:
        return {"count": 0, "entries": [], "digest": _rows_digest([])}
    entries = [
        str(row[0])
        for row in connection.execute(
            f"SELECT {_quote_identifier(column)} FROM {_quote_identifier(table)} "
            f"ORDER BY {_quote_identifier(column)}"
        ).fetchall()
    ]
    return {
        "count": len(entries),
        "entries": entries,
        "digest": hashlib.sha256("\n".join(entries).encode()).hexdigest(),
    }


def _database_snapshot(path: Path) -> dict[str, Any]:
    uri = f"{path.resolve().as_uri()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        connection.row_factory = sqlite3.Row
        integrity = [str(row[0]) for row in connection.execute("PRAGMA integrity_check")]
        foreign_keys = [dict(row) for row in connection.execute("PRAGMA foreign_key_check")]
        tables: dict[str, Any] = {}
        for table in _list_user_tables(connection):
            quoted = _quote_identifier(table)
            columns = [
                str(row[1])
                for row in connection.execute(f"PRAGMA table_info({quoted})").fetchall()
            ]
            rows = [dict(row) for row in connection.execute(f"SELECT * FROM {quoted}")]
            ids = sorted(str(row["id"]) for row in rows if "id" in row)
            tables[table] = {
                "count": len(rows),
                "columns": columns,
                "row_digest": _rows_digest(rows),
                "id_count": len(ids),
                "id_digest": hashlib.sha256("\n".join(ids).encode()).hexdigest(),
                "has_revision": "revision" in columns or table.endswith("_versions"),
            }
        return {
            "sqlite_integrity_ok": integrity == ["ok"],
            "sqlite_integrity_finding_count": 0 if integrity == ["ok"] else len(integrity),
            "sqlite_integrity_digest": hashlib.sha256("\n".join(integrity).encode()).hexdigest(),
            "foreign_key_violation_count": len(foreign_keys),
            "foreign_key_digest": _rows_digest(foreign_keys),
            "schema_migrations": _ledger(connection, "schema_migrations", "filename"),
            "runtime_schema_upgrades": _ledger(
                connection,
                "runtime_schema_upgrades",
                "name",
            ),
            "tables": tables,
        }


def _compare_upgrade(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    before_tables = before["tables"]
    after_tables = after["tables"]
    shared = sorted(set(before_tables) & set(after_tables))
    changed = [table for table in shared if before_tables[table] != after_tables[table]]
    changed_ids = [
        table
        for table in shared
        if before_tables[table]["id_digest"] != after_tables[table]["id_digest"]
    ]
    changed_links = [table for table in changed if table in LINK_TABLES]
    changed_revisions = [
        table
        for table in changed
        if before_tables[table]["has_revision"] or after_tables[table]["has_revision"]
    ]
    foreign_keys_equal = (
        before["foreign_key_violation_count"] == after["foreign_key_violation_count"]
        and before["foreign_key_digest"] == after["foreign_key_digest"]
    )
    schema_ledger_equal = before["schema_migrations"] == after["schema_migrations"]
    runtime_ledger_equal = (
        before["runtime_schema_upgrades"] == after["runtime_schema_upgrades"]
    )
    removed = sorted(set(before_tables) - set(after_tables))
    return {
        "passed": not changed
        and not removed
        and not changed_ids
        and foreign_keys_equal
        and schema_ledger_equal
        and runtime_ledger_equal
        and after["sqlite_integrity_ok"],
        "changed_tables": changed,
        "changed_id_sets": changed_ids,
        "changed_link_tables": changed_links,
        "changed_revision_tables": changed_revisions,
        "new_tables": sorted(set(after_tables) - set(before_tables)),
        "removed_tables": removed,
        "foreign_keys_equal": foreign_keys_equal,
        "schema_migration_ledger_equal": schema_ledger_equal,
        "runtime_upgrade_ledger_equal": runtime_ledger_equal,
        "schema_migrations_added": sorted(
            set(after["schema_migrations"]["entries"])
            - set(before["schema_migrations"]["entries"])
        ),
        "runtime_upgrades_added": sorted(
            set(after["runtime_schema_upgrades"]["entries"])
            - set(before["runtime_schema_upgrades"]["entries"])
        ),
    }


async def _upgrade_database(path: Path) -> int:
    database = Database(str(path))
    await database.connect()
    try:
        await database.initialize_schema()
        await database.initialize_phase2_schema()
        await database.run_migrations()
        return await database.run_migrations()
    finally:
        await database.close()


async def _read_project_tables(
    database: Database,
    project_id: str,
) -> dict[str, list[dict[str, Any]]]:
    """Read the portable Core contract directly from the source database."""

    tables: dict[str, list[dict[str, Any]]] = {}
    for table in sorted(PORTABLE_CORE_TABLES):
        columns = await database.fetchall(f"PRAGMA table_info({_quote_identifier(table)})")
        names = {str(column["name"]) for column in columns}
        if "project_id" not in names:
            raise RuntimeError(f"Portable Core table lacks project_id: {table}")
        tables[table] = await database.fetchall(
            f"SELECT * FROM {_quote_identifier(table)} WHERE project_id = ?",
            [project_id],
        )
    return tables


def _source_manifest_comparison(
    source_database_tables: dict[str, list[dict[str, Any]]],
    manifest_tables: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    """Compare exporter output with an independently read source snapshot."""

    missing_tables = sorted(PORTABLE_CORE_TABLES - set(manifest_tables))
    table_results: dict[str, Any] = {}
    for table in sorted(PORTABLE_CORE_TABLES):
        source_rows = [dict(row) for row in source_database_tables.get(table, [])]
        manifest_rows = [dict(row) for row in manifest_tables.get(table, [])]
        ignored = {"pack_file"} if table == "artifacts" else set()
        source_digest = _rows_digest(source_rows, ignored=ignored)
        manifest_digest = _rows_digest(manifest_rows, ignored=ignored)
        table_results[table] = {
            "database_count": len(source_rows),
            "manifest_count": len(manifest_rows),
            "database_digest": source_digest,
            "manifest_digest": manifest_digest,
            "matched": source_digest == manifest_digest,
        }
    mismatched = [
        table for table, details in table_results.items() if not details["matched"]
    ]
    return {
        "passed": not missing_tables and not mismatched,
        "missing_tables": missing_tables,
        "mismatched_tables": mismatched,
        "tables": table_results,
    }


def _forward_rekey_value(
    value: Any,
    *,
    id_map: dict[str, str],
    source_project_id: str,
    target_project_id: str,
) -> Any:
    """Build an independent source-to-target expectation for imported strings."""

    if not isinstance(value, str):
        return value
    normalized = value.replace(source_project_id, target_project_id)
    for source_id, target_id in sorted(
        id_map.items(),
        key=lambda item: len(item[0]),
        reverse=True,
    ):
        normalized = normalized.replace(source_id, target_id)
    return _canonicalize_json_text(normalized)


def _canonicalize_json_text(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return value
    if not isinstance(parsed, (dict, list)):
        return value
    return json.dumps(parsed, sort_keys=True, separators=(",", ":"))


def _normalize_expected_target_row(
    table: str,
    row: dict[str, Any],
    *,
    id_map: dict[str, str],
    source_project_id: str,
    target_project_id: str,
) -> dict[str, Any]:
    ignored = IMPORT_IGNORED_COLUMNS.get(table, set())
    return {
        column: _forward_rekey_value(
            value,
            id_map=id_map,
            source_project_id=source_project_id,
            target_project_id=target_project_id,
        )
        for column, value in row.items()
        if column not in ignored and column != "pack_file"
    }


def _normalize_target_row(table: str, row: dict[str, Any]) -> dict[str, Any]:
    ignored = IMPORT_IGNORED_COLUMNS.get(table, set())
    return {
        column: _canonicalize_json_text(value)
        for column, value in row.items()
        if column not in ignored
    }


def _normalize_manifest_row(table: str, row: dict[str, Any]) -> dict[str, Any]:
    ignored = IMPORT_IGNORED_COLUMNS.get(table, set())
    return {
        column: _canonicalize_json_text(value)
        for column, value in row.items()
        if column not in ignored and column != "pack_file"
    }


def _mismatched_columns(
    expected_rows: list[dict[str, Any]],
    actual_rows: list[dict[str, Any]],
) -> list[str]:
    columns = set().union(
        *(set(row) for row in expected_rows),
        *(set(row) for row in actual_rows),
    )
    return sorted(
        column
        for column in columns
        if _rows_digest([{"value": row.get(column)} for row in expected_rows])
        != _rows_digest([{"value": row.get(column)} for row in actual_rows])
    )


def _claim_scope_hashes_preserved(
    source_tables: dict[str, list[dict[str, Any]]],
    imported_tables: dict[str, list[dict[str, Any]]],
    *,
    inverse_id_map: dict[str, str],
) -> bool:
    """Verify that fresh scope hashes refresh and stale hashes stay stale."""

    source_claims = {
        str(row["id"]): row for row in source_tables.get("claims", []) if row.get("id")
    }
    imported_claims = {
        inverse_id_map.get(str(row.get("id")), str(row.get("id"))): row
        for row in imported_tables.get("claims", [])
        if row.get("id")
    }
    imported_scopes = {
        inverse_id_map.get(str(row.get("id")), str(row.get("id"))): row
        for row in imported_tables.get("claim_scope_versions", [])
        if row.get("id")
    }
    for source_scope in source_tables.get("claim_scope_versions", []):
        source_scope_id = str(source_scope.get("id") or "")
        source_claim_id = str(source_scope.get("claim_id") or "")
        source_claim = source_claims.get(source_claim_id)
        imported_scope = imported_scopes.get(source_scope_id)
        imported_claim = imported_claims.get(source_claim_id)
        if source_claim is None or imported_scope is None or imported_claim is None:
            return False
        source_hash = hashlib.sha256(
            f"{source_claim.get('claim_type', '')}\0{source_claim.get('content', '')}".encode()
        ).hexdigest()
        imported_hash = hashlib.sha256(
            f"{imported_claim.get('claim_type', '')}\0{imported_claim.get('content', '')}".encode()
        ).hexdigest()
        expected_hash = (
            imported_hash
            if source_scope.get("claim_content_hash") == source_hash
            else source_scope.get("claim_content_hash")
        )
        if imported_scope.get("claim_content_hash") != expected_hash:
            return False
    return True


def _artifact_bytes_preserved(
    source_tables: dict[str, list[dict[str, Any]]],
    imported_tables: dict[str, list[dict[str, Any]]],
    *,
    inverse_id_map: dict[str, str],
) -> bool:
    source_hashes = {
        str(row["id"]): str(row.get("content_hash") or "")
        for row in source_tables.get("artifacts", [])
        if row.get("id")
    }
    imported = imported_tables.get("artifacts", [])
    if len(imported) != len(source_hashes):
        return False
    for row in imported:
        source_id = inverse_id_map.get(str(row.get("id") or ""))
        expected_hash = source_hashes.get(str(source_id or ""))
        filepath = row.get("filepath")
        if not expected_hash or not isinstance(filepath, str):
            return False
        path = Path(filepath)
        if not path.is_file() or _sha256_file(path) != expected_hash:
            return False
    return True


def _normalized_integrity(issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        [
            {
                "category": issue.get("category"),
                "severity": issue.get("severity"),
                "sample_count": issue.get("count"),
            }
            for issue in issues
            if issue.get("category") not in INSTALLATION_LOCAL_INTEGRITY_CATEGORIES
        ],
        key=lambda issue: (
            str(issue["category"]),
            str(issue["severity"]),
            int(issue["sample_count"] or 0),
        ),
    )


def _installation_integrity_summary(
    issues: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    return sorted(
        [
            {
                "category": issue.get("category"),
                "severity": issue.get("severity"),
                "sample_count": issue.get("count"),
            }
            for issue in issues
            if issue.get("category") in INSTALLATION_LOCAL_INTEGRITY_CATEGORIES
        ],
        key=lambda issue: str(issue["category"]),
    )


def _integrity_signature(issues: list[dict[str, Any]]) -> str:
    return _rows_digest(_normalized_integrity(issues))


async def _validate_pack_round_trip(
    source_path: Path,
    project_id: str,
    work_dir: Path,
    ordinal: int,
) -> dict[str, Any]:
    source_db = Database(str(source_path))
    await source_db.connect()
    target_db = Database(str(work_dir / f"pack-target-{ordinal}.db"))
    await target_db.connect()
    await target_db.initialize_schema()
    await target_db.initialize_phase2_schema()
    pack_path: Path | None = None
    try:
        source_service = KnowledgePackService(source_db, project_id=project_id)
        source_issues = await source_service.check_integrity(project_id)
        source_database_tables = await _read_project_tables(source_db, project_id)
        source_project = await source_db.fetchone(
            "SELECT * FROM projects WHERE id = ?",
            [project_id],
        )
        source_state = await source_db.fetchone(
            "SELECT * FROM project_states WHERE project_id = ?",
            [project_id],
        )
        pack_name, _filename = await source_service.export_pack(project_id)
        pack_path = Path(pack_name)
        with zipfile.ZipFile(pack_path) as archive:
            manifest = json.loads(archive.read("manifest.json"))

        source_tables = manifest.get("tables", {})
        source_manifest = _source_manifest_comparison(
            source_database_tables,
            source_tables,
        )
        project_manifest_matches = (
            source_project is not None
            and _rows_digest([dict(source_project)])
            == _rows_digest([dict(manifest.get("project") or {})])
        )
        state_manifest_matches = (
            (source_state is None and manifest.get("project_state") is None)
            or (
                source_state is not None
                and _rows_digest([dict(source_state)])
                == _rows_digest([dict(manifest.get("project_state") or {})])
            )
        )

        target_project_id = f"recovery_pack_{ordinal:03d}"
        target_project_name = f"Recovery Pack {ordinal:03d}"
        importer = CapturingKnowledgePackService(target_db)
        with pack_path.open("rb") as pack_file:
            result = await importer.import_pack(
                pack_file,
                project_id=target_project_id,
                project_name=target_project_name,
                defer_indexing=True,
            )

        inverse_id_map = {
            target_id: source_id
            for source_id, target_id in importer.imported_id_map.items()
        }
        table_results: dict[str, Any] = {}
        imported_tables: dict[str, list[dict[str, Any]]] = {}
        source_ids: set[str] = set()
        for table in sorted(PORTABLE_CORE_TABLES):
            actual = await target_db.fetchall(
                f"SELECT * FROM {_quote_identifier(table)} WHERE project_id = ?",
                [target_project_id],
            )
            imported_tables[table] = [dict(row) for row in actual]
            expected_rows = [
                _normalize_expected_target_row(
                    table,
                    dict(row),
                    id_map=importer.imported_id_map,
                    source_project_id=project_id,
                    target_project_id=target_project_id,
                )
                for row in source_tables.get(table, [])
            ]
            normalized_actual = [
                _normalize_target_row(table, dict(row))
                for row in actual
            ]
            source_ids.update(
                str(row["id"])
                for row in source_tables.get(table, [])
                if row.get("id")
            )
            expected_digest = _rows_digest(expected_rows)
            actual_digest = _rows_digest(normalized_actual)
            table_results[table] = {
                "source_count": len(source_tables.get(table, [])),
                "imported_count": len(actual),
                "expected_digest": expected_digest,
                "actual_digest": actual_digest,
                "matched": expected_digest == actual_digest,
                "mismatched_columns": _mismatched_columns(
                    expected_rows,
                    normalized_actual,
                ),
            }

        target_project = await target_db.fetchone(
            "SELECT * FROM projects WHERE id = ?",
            [target_project_id],
        )
        expected_target_project = dict(manifest["project"])
        expected_target_project["id"] = target_project_id
        expected_target_project["name"] = target_project_name
        target_project_matches = (
            target_project is not None
            and _rows_digest([dict(target_project)])
            == _rows_digest([expected_target_project])
        )
        target_state = await target_db.fetchone(
            "SELECT * FROM project_states WHERE project_id = ?",
            [target_project_id],
        )
        source_manifest_state = manifest.get("project_state") or {}
        expected_target_state = {
            "project_id": target_project_id,
            "project_name": target_project_name,
            "project_description": manifest["project"].get("description"),
            "current_phase": source_manifest_state.get("current_phase"),
            "phases_config": source_manifest_state.get("phases_config"),
            "summary": source_manifest_state.get("summary"),
            "blockers": source_manifest_state.get("blockers"),
            "metrics": source_manifest_state.get("metrics"),
            "created_at": source_manifest_state.get("created_at")
            or manifest["project"].get("created_at"),
            "updated_at": source_manifest_state.get("updated_at")
            or manifest["project"].get("updated_at"),
        }
        target_state_matches = (
            target_state is not None
            and expected_target_state["phases_config"] is not None
            and _rows_digest([dict(target_state)])
            == _rows_digest([expected_target_state])
        )
        id_map_complete = source_ids.issubset(importer.imported_id_map)
        id_map_values_unique = len(importer.imported_id_map.values()) == len(
            set(importer.imported_id_map.values())
        )
        scope_hashes_preserved = _claim_scope_hashes_preserved(
            source_tables,
            imported_tables,
            inverse_id_map=inverse_id_map,
        )
        artifact_bytes_preserved = _artifact_bytes_preserved(
            source_tables,
            imported_tables,
            inverse_id_map=inverse_id_map,
        )

        target_issues = await importer.check_integrity(target_project_id)
        critical = [issue for issue in target_issues if issue.get("severity") == "critical"]
        count_matches = all(
            result.imported_counts.get(table, 0) == len(rows)
            for table, rows in source_tables.items()
            if table in _INSERT_ORDER
        )
        mismatched_tables = [
            table for table, details in table_results.items() if not details["matched"]
        ]
        source_signature = _integrity_signature(source_issues)
        target_signature = _integrity_signature(target_issues)
        manifest_counts_match = (
            set(manifest.get("table_counts", {})) == set(source_tables)
            and all(
                manifest.get("table_counts", {}).get(table) == len(rows)
                for table, rows in source_tables.items()
            )
        )
        return {
            "project_fingerprint": hashlib.sha256(project_id.encode()).hexdigest()[:16],
            "passed": source_manifest["passed"]
            and project_manifest_matches
            and state_manifest_matches
            and target_project_matches
            and target_state_matches
            and id_map_complete
            and id_map_values_unique
            and scope_hashes_preserved
            and artifact_bytes_preserved
            and manifest_counts_match
            and not mismatched_tables
            and count_matches
            and not critical
            and source_signature == target_signature,
            "id_map_size": len(importer.imported_id_map),
            "id_map_complete": id_map_complete,
            "id_map_values_unique": id_map_values_unique,
            "source_manifest": source_manifest,
            "project_manifest_matches_database": project_manifest_matches,
            "state_manifest_matches_database": state_manifest_matches,
            "target_project_matches_manifest": target_project_matches,
            "target_state_matches_manifest": target_state_matches,
            "claim_scope_hashes_preserved": scope_hashes_preserved,
            "artifact_bytes_preserved": artifact_bytes_preserved,
            "manifest_counts_match_payload": manifest_counts_match,
            "imported_counts_match": count_matches,
            "mismatched_tables": mismatched_tables,
            "source_integrity_signature": source_signature,
            "target_integrity_signature": target_signature,
            "source_integrity_summary": _normalized_integrity(source_issues),
            "target_integrity_summary": _normalized_integrity(target_issues),
            "source_installation_findings": _installation_integrity_summary(source_issues),
            "target_installation_findings": _installation_integrity_summary(target_issues),
            "target_critical_issue_count": len(critical),
            "tables": table_results,
        }
    finally:
        if pack_path is not None:
            pack_path.unlink(missing_ok=True)
        await target_db.close()
        await source_db.close()


def _restore_exact(backup: Path, destination: Path) -> None:
    temporary = destination.with_name(f".{destination.name}.restore.tmp")
    shutil.copyfile(backup, temporary)
    os.chmod(temporary, 0o600)
    with temporary.open("rb") as restored:
        os.fsync(restored.fileno())
    for suffix in ("-wal", "-shm", "-journal", ".phase2.lock"):
        Path(f"{destination}{suffix}").unlink(missing_ok=True)
    os.replace(temporary, destination)
    fsync_directory(destination.parent)


def _write_report_atomic(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(report, output, indent=2, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    source = args.source_db.expanduser().resolve()
    source_before = {
        "size_bytes": source.stat().st_size,
        "mtime_ns": source.stat().st_mtime_ns,
    }
    with tempfile.TemporaryDirectory(prefix="rka-core-recovery-") as temporary:
        work_dir = Path(temporary)
        baseline = work_dir / "baseline.db"
        upgrade = work_dir / "upgrade.db"
        rollback = work_dir / "rollback.db"

        backup = backup_sqlite_database(source, baseline)
        backup_sqlite_database(baseline, upgrade)
        before = _database_snapshot(upgrade)
        second_pass_migrations = await _upgrade_database(upgrade)
        after = _database_snapshot(upgrade)
        upgrade_comparison = _compare_upgrade(before, after)

        pack_results = [
            await _validate_pack_round_trip(upgrade, project_id, work_dir, ordinal)
            for ordinal, project_id in enumerate(args.project_id, start=1)
        ]

        _restore_exact(baseline, rollback)
        rollback_snapshot = _database_snapshot(rollback)
        rollback_exact = _sha256_file(rollback) == _sha256_file(baseline)
        rollback_logical = rollback_snapshot == _database_snapshot(baseline)
        source_after = {
            "size_bytes": source.stat().st_size,
            "mtime_ns": source.stat().st_mtime_ns,
        }

        passed = (
            upgrade_comparison["passed"]
            and second_pass_migrations == 0
            and bool(pack_results)
            and all(result["passed"] for result in pack_results)
            and rollback_exact
            and rollback_logical
        )
        return {
            "schema": REPORT_SCHEMA,
            "passed": passed,
            "source_observation": {
                "before": source_before,
                "after": source_after,
                "note": (
                    "Metadata equality is informational for a live source; concurrent RKA "
                    "writers may change it while online backup remains read-only."
                ),
            },
            "backup": {
                "sha256": backup.sha256,
                "size_bytes": backup.size_bytes,
                "page_count": backup.page_count,
                "foreign_key_violations": backup.foreign_key_violations,
            },
            "upgrade": {
                "second_idempotence_pass_applied": second_pass_migrations,
                "before": before,
                "after": after,
                "comparison": upgrade_comparison,
            },
            "knowledge_packs": pack_results,
            "rollback": {
                "exact_backup_bytes_restored": rollback_exact,
                "logical_snapshot_restored": rollback_logical,
                "previous_runtime": "requires isolated pinned-image runbook step",
            },
        }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-db", type=Path, required=True)
    parser.add_argument("--project-id", action="append", default=[])
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()

    source_path = args.source_db.expanduser().resolve()
    report_input = args.report.expanduser()
    if report_input.is_symlink():
        parser.error("--report must not be a symbolic link")
    report_path = report_input.resolve()
    if is_protected_sqlite_path(source_path, report_path):
        parser.error("--report must not replace the source database or its runtime files")
    try:
        report = asyncio.run(_run(args))
    except Exception as exc:
        print(
            f"Recovery validation failed ({type(exc).__name__}): {exc}",
            file=sys.stderr,
        )
        report = {
            "schema": REPORT_SCHEMA,
            "passed": False,
            "stage": "recovery_validation",
            "error_type": type(exc).__name__,
        }
    _write_report_atomic(report_path, report)
    print(f"Recovery report: {report_path}")
    if not report.get("passed"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
