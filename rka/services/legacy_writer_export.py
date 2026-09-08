"""Read-only export of legacy Writer state for standalone Writer staging.

The general KnowledgePack format intentionally omits installation-local Writer
state.  E2.3 needs a different artifact: a one-way compatibility bundle that
preserves the frozen Writer tables exactly so ``rka-writer`` can verify them in
staging before any authority switch.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sqlite3
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rka import __version__
from rka.infra.sqlite_backup import (
    fsync_directory,
    fsync_file,
    is_protected_sqlite_path,
)


LEGACY_WRITER_EXPORT_CONTRACT = "rka-legacy-writer-export/v1"
LEGACY_WRITER_EXPORT_VERSION = 1
LEGACY_WRITER_BUNDLE_SUFFIX = ".rka-writer-export.zip"

# Authority: docs/superpowers/plans/2026-08-25-rka-ecosystem-ownership-inventory.md
# Keep this list frozen for v1. A changed table set requires a new bundle version.
LEGACY_WRITER_TABLES: tuple[str, ...] = (
    "manuscript_checkpoints",
    "manuscript_claim_evidence",
    "manuscript_claim_ratifications",
    "manuscript_claim_units",
    "manuscript_claim_verification_attestations",
    "manuscript_claim_versions",
    "manuscript_claims",
    "manuscript_evaluation_events",
    "manuscript_migration_issues",
    "manuscript_planning_artifact_versions",
    "manuscript_planning_artifacts",
    "manuscript_planning_branch_events",
    "manuscript_planning_branches",
    "manuscript_planning_evidence_bindings",
    "manuscript_planning_promotion_events",
    "manuscript_reference_members",
    "manuscript_source_events",
    "manuscript_source_proposals",
    "manuscript_unit_citations",
    "manuscript_unit_evidence",
    "manuscript_unit_outline_profiles",
    "manuscript_units",
    "manuscripts",
    "reference_validation_attestations",
    "reference_validation_migration_issues",
    "semantic_patch_context_manifests",
    "semantic_patch_proposal_events",
    "semantic_patch_proposals",
    "semantic_patch_provider_events",
)

# Frozen against a freshly initialized Core database at migration 053.  The
# digest covers SQLite column descriptors, primary-key order, and foreign-key
# descriptors.  An additive or breaking Writer-table change requires a new
# export contract instead of silently changing v1.
LEGACY_WRITER_SCHEMA_SHA256: dict[str, str] = {
    "manuscript_checkpoints": "3492dbc3451e85301d076dfb913a8a1e66a2fbb82f270c8369be74c8f1593c1e",
    "manuscript_claim_evidence": "88e9683da5c1368cf3b162189c3d085f43f62d464dea8554e34ad54dbe0cdfa0",
    "manuscript_claim_ratifications": "d58b4cf0560d60b69499ec957350efab6689723316ea66236132c9acc4896e4f",
    "manuscript_claim_units": "82fd8644e92184bb8b8e2e1346352eca0b7125d209211acb6bd2bf773cbbe3aa",
    "manuscript_claim_verification_attestations": "2b5ba3b4e0014aa3e7fe01d51eeb1a848b8aca6c95cc4617c0dabc846702f7db",
    "manuscript_claim_versions": "9a4775fa1f0d725c8ac0fe56d206aa88d95bdfecd9dd246c388c6ab1b8c7feae",
    "manuscript_claims": "e7db3b47f5c591e415091643193cf91d4d4c2618c3211ee3c81d41882f981009",
    "manuscript_evaluation_events": "a9e015b22e1cd3fa9be4cc4e9c3ecf28bda7245c6e62b130f6caea334c397705",
    "manuscript_migration_issues": "e79aa30d15a00f06a7561d4ceb83232429501a0da9ca7a424307255c5853d5c2",
    "manuscript_planning_artifact_versions": "9a938767c64edd1b15b63f5eb1fa65c62e54f4ce6660bee5545cf7f8c924fc61",
    "manuscript_planning_artifacts": "206e1cad35881cdc9991618135123ea8944cd3942dd8e4bc71b6f76b667c1fa3",
    "manuscript_planning_branch_events": "58a582a80f81a9d15d7d241c7b6edfe969b85dcb0f4800fa05a2f0fe094075d1",
    "manuscript_planning_branches": "de91a2e943eaf1a9b4b49d26f3a44cf149a5ac3d971dfdb3ea58cc55f0bb200c",
    "manuscript_planning_evidence_bindings": "16c38337d46da74cc347ee029fb37d9599c0c17f6e3d5e33b6aa05c68801cec8",
    "manuscript_planning_promotion_events": "15d0c8c4c56dbfff384038c9d15ecd1dbf6c2cdd4960357257b04d5741111a6a",
    "manuscript_reference_members": "5912e0c37c4ad9d69069e4ae3cdc9cf36662a79639f27a1b5a586b1e23961ef7",
    "manuscript_source_events": "6ea855cde71f8cf0e93e545dd9b380debf675d442d46bd35423b5f754a859ee6",
    "manuscript_source_proposals": "431c0fda3dfafb9f6056d6aeaa591e469a86b25f28d78a39c416adcd2ea1ac47",
    "manuscript_unit_citations": "bcc3478b1a3d9556d40461dc31dc96342ea0dc9e7d3e9f50f86b23e0650a0903",
    "manuscript_unit_evidence": "53cfbe4f6b1dc06ba7449e4f33eaae04538424c835e2dc8c070bb8d4be0e5341",
    "manuscript_unit_outline_profiles": "2a9ed85994928f569f7400cac4c7ddfdc7d815352edd51a5b9d9375a6f9c2a4a",
    "manuscript_units": "45cc1b3e6d04c4df34b792e80af9b62e8fad41ccf62ec87fc53919fa29821f89",
    "manuscripts": "a5aa60e4dfb7ab64700e4864614789b802ec458e2b2cb8a71e62c4c8ccd536cf",
    "reference_validation_attestations": "2bafbd628a64f2bbc4f6e9691c72c0d860239402edf0d499e8069f22a70180f4",
    "reference_validation_migration_issues": "4a40956c2ac2e6311d147a8a497c25983dad44d912765133096b7041bd7ce267",
    "semantic_patch_context_manifests": "9875a8773d294989126e4481a3111790130bc252c0c149f9de7c7f582b140803",
    "semantic_patch_proposal_events": "d24fe986f180106ca0e37369f24dbcff4688610c4ec48abf36980902a318ebfc",
    "semantic_patch_proposals": "5b557df613f9a87b11f57b9dc8f805afd18d93c91f8815e86f947136d8b9b275",
    "semantic_patch_provider_events": "d3dfd98f2ef516e739539e43a6bebe71cc082e838364f94d8a752b3652ead401",
}

LEGACY_WRITER_SCHEMA_FINGERPRINT = (
    "9008c196a5da9bc4151f44c9eea9332f994041d2691a2fcb5f3ceb5cf52059ff"
)

_CORE_ENTITY_TABLES: dict[str, str] = {
    "project": "projects",
    "journal": "journal",
    "literature": "literature",
    "decision": "decisions",
    "claim": "claims",
    "claim_scope": "claim_scope_versions",
    "cluster": "evidence_clusters",
    "interpretation_candidate": "interpretation_candidates",
    "interpretation_hint": "interpretation_candidate_hints",
    "interpretation_review": "interpretation_review_events",
    "interpretation_promotion": "interpretation_promotions",
    "experiment": "experiments",
    "experiment_plan_version": "experiment_plan_versions",
    "experiment_run": "experiment_runs",
    "experiment_observation": "experiment_observations",
    "evidence_locator": "evidence_locators",
    "artifact": "artifacts",
    "mission": "missions",
    "job": "jobs",
    "checkpoint": "checkpoints",
    "figure": "figures",
    "topic": "topics",
    "review": "review_queue",
    "event": "events",
    "link": "entity_links",
    "claim_edge": "claim_edges",
    "decision_option": "decision_options",
    "reference_validation": "reference_validation_attestations",
}

_INTERNAL_WRITER_ENTITY_TABLES: dict[str, str] = {
    "manuscript": "manuscripts",
    "manuscript_claim": "manuscript_claims",
    "manuscript_claim_ratification": "manuscript_claim_ratifications",
    "manuscript_unit": "manuscript_units",
    "semantic_patch_proposal": "semantic_patch_proposals",
    "manuscript_checkpoint": "manuscript_checkpoints",
    "manuscript_claim_verification": "manuscript_claim_verification_attestations",
    "manuscript_reference": "manuscript_reference_members",
    "reference_validation": "reference_validation_attestations",
}
_INTERNAL_WRITER_ENTITY_TYPES = frozenset(_INTERNAL_WRITER_ENTITY_TABLES)

_DIRECT_CORE_REFERENCES: tuple[tuple[str, str, str], ...] = (
    ("manuscripts", "legacy_journal_id", "journal"),
    ("manuscript_migration_issues", "legacy_journal_id", "journal"),
    ("manuscript_checkpoints", "decision_id", "decision"),
    ("manuscript_claim_ratifications", "decision_id", "decision"),
    ("manuscript_claim_evidence", "evidence_claim_id", "claim"),
    ("manuscript_unit_evidence", "evidence_claim_id", "claim"),
    ("manuscript_reference_members", "literature_id", "literature"),
    ("reference_validation_attestations", "legacy_journal_id", "journal"),
    ("reference_validation_attestations", "validation_job_id", "job"),
    ("reference_validation_attestations", "literature_id", "literature"),
    ("manuscript_evaluation_events", "mission_id", "mission"),
    ("manuscript_planning_promotion_events", "decision_id", "decision"),
)


class LegacyWriterExportError(RuntimeError):
    """The source snapshot cannot produce a complete v1 Writer bundle."""


@dataclass(frozen=True)
class LegacyWriterExportResult:
    path: Path
    sha256: str
    project_id: str
    table_count: int
    row_count: int
    tables_sha256: str
    semantic_root_sha256: str


def export_legacy_writer_bundle(
    source_db: str | Path,
    project_id: str,
    output: str | Path,
) -> LegacyWriterExportResult:
    """Export one project's frozen Writer tables from a portable DB snapshot.

    The source is opened using SQLite ``mode=ro`` plus ``query_only``. Runtime
    sidecars are rejected so operators use the single-file output of
    ``rka backup`` rather than a live WAL database.
    """

    source_path = Path(source_db).expanduser().resolve()
    output_input = Path(output).expanduser()
    if output_input.is_symlink():
        raise LegacyWriterExportError("Writer export output must not be a symbolic link")
    output_path = output_input.resolve()
    project_id = project_id.strip()
    if not project_id:
        raise LegacyWriterExportError("project_id cannot be empty")
    if not source_path.is_file():
        raise LegacyWriterExportError(f"SQLite snapshot not found: {source_path}")
    if is_protected_sqlite_path(source_path, output_path):
        raise LegacyWriterExportError("Writer export must not replace the source database")

    runtime_sidecars = [
        path
        for path in (
            Path(f"{source_path}-wal"),
            Path(f"{source_path}-shm"),
            Path(f"{source_path}-journal"),
        )
        if path.exists()
    ]
    if runtime_sidecars:
        raise LegacyWriterExportError(
            "Source has SQLite runtime sidecars; run 'rka backup' and export "
            "from that portable snapshot"
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_fd, temporary_name = tempfile.mkstemp(
        prefix=f".{output_path.name}.", suffix=".tmp", dir=output_path.parent
    )
    os.close(temporary_fd)
    temporary_path = Path(temporary_name)

    try:
        source_sha256 = _sha256_file(source_path)
        manifest, table_payloads = _read_snapshot(
            source_path,
            project_id,
            source_sha256=source_sha256,
        )
        with zipfile.ZipFile(
            temporary_path,
            "w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
        ) as archive:
            for table in LEGACY_WRITER_TABLES:
                _write_zip_bytes(
                    archive,
                    manifest["tables"][table]["path"],
                    table_payloads[table],
                )
            _write_zip_bytes(
                archive,
                "manifest.json",
                json.dumps(
                    manifest,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                ).encode("utf-8"),
            )

        if _sha256_file(source_path) != source_sha256:
            raise LegacyWriterExportError("SQLite source changed during Writer export")

        os.chmod(temporary_path, 0o600)
        fsync_file(temporary_path)
        bundle_sha256 = _sha256_file(temporary_path)
        os.replace(temporary_path, output_path)
        fsync_directory(output_path.parent)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise

    return LegacyWriterExportResult(
        path=output_path,
        sha256=bundle_sha256,
        project_id=project_id,
        table_count=len(LEGACY_WRITER_TABLES),
        row_count=sum(item["row_count"] for item in manifest["tables"].values()),
        tables_sha256=manifest["tables_sha256"],
        semantic_root_sha256=manifest["semantic_root_sha256"],
    )


def _read_snapshot(
    source_path: Path,
    project_id: str,
    *,
    source_sha256: str,
) -> tuple[dict[str, Any], dict[str, bytes]]:
    from rka.infra.runtime_lease import RuntimeLease
    with RuntimeLease(source_path):
        return _read_snapshot_locked(source_path, project_id, source_sha256=source_sha256)


def _read_snapshot_locked(source_path, project_id, *, source_sha256):
    source_uri = f"{source_path.as_uri()}?mode=ro&immutable=1"
    try:
        connection = sqlite3.connect(source_uri, uri=True, timeout=30)
    except sqlite3.Error as exc:
        raise LegacyWriterExportError(f"Cannot open SQLite snapshot: {exc}") from exc

    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only = ON")
        integrity = [str(row[0]) for row in connection.execute("PRAGMA integrity_check")]
        if integrity != ["ok"]:
            raise LegacyWriterExportError(
                "SQLite snapshot failed integrity_check: " + "; ".join(integrity[:5])
            )

        project = connection.execute(
            "SELECT id, name, created_at FROM projects WHERE id = ?", [project_id]
        ).fetchone()
        if project is None:
            raise LegacyWriterExportError(f"Project '{project_id}' not found in snapshot")

        tables_present = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        missing_tables = sorted(set(LEGACY_WRITER_TABLES) - tables_present)
        if missing_tables:
            raise LegacyWriterExportError(
                "Snapshot does not support the v1 Writer export; missing tables: "
                + ", ".join(missing_tables)
            )

        writer_fk_violations = _project_writer_fk_violations(
            connection,
            project_id=project_id,
        )
        if writer_fk_violations:
            tables = sorted({str(row["table"]) for row in writer_fk_violations})
            raise LegacyWriterExportError(
                "Writer state contains missing referenced records in tables: " + ", ".join(tables)
            )

        table_payloads: dict[str, bytes] = {}
        table_descriptors: dict[str, dict[str, Any]] = {}
        rows_by_table: dict[str, list[dict[str, Any]]] = {}
        for table in LEGACY_WRITER_TABLES:
            columns = [dict(row) for row in connection.execute(f"PRAGMA table_info([{table}])")]
            if not columns or "project_id" not in {column["name"] for column in columns}:
                raise LegacyWriterExportError(
                    f"Writer table '{table}' is unsupported: project_id column is required"
                )
            primary_key = [
                column["name"]
                for column in sorted(columns, key=lambda column: int(column["pk"] or 0))
                if int(column["pk"] or 0) > 0
            ]
            if not primary_key:
                raise LegacyWriterExportError(
                    f"Writer table '{table}' is unsupported: primary key is required"
                )
            order_by = ", ".join(f"[{column}]" for column in primary_key)
            foreign_keys = [
                dict(row) for row in connection.execute(f"PRAGMA foreign_key_list([{table}])")
            ]
            schema_payload = _canonical_json(
                {"columns": columns, "foreign_keys": foreign_keys, "primary_key": primary_key}
            )
            schema_sha256 = hashlib.sha256(schema_payload).hexdigest()
            expected_schema_sha256 = LEGACY_WRITER_SCHEMA_SHA256[table]
            if schema_sha256 != expected_schema_sha256:
                raise LegacyWriterExportError(
                    f"Writer table '{table}' has unsupported v1 schema: "
                    f"expected {expected_schema_sha256}, got {schema_sha256}"
                )
            rows = _read_project_rows(
                connection,
                table=table,
                project_id=project_id,
                order_by=order_by,
            )
            rows_by_table[table] = rows
            payload = _canonical_json(rows)
            primary_key_payload = _canonical_json(
                [{column: row[column] for column in primary_key} for row in rows]
            )
            path = f"tables/{table}.json"
            table_payloads[table] = payload
            table_descriptors[table] = {
                "path": path,
                "row_count": len(rows),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "primary_key_sha256": hashlib.sha256(primary_key_payload).hexdigest(),
                "schema_sha256": schema_sha256,
                "primary_key": primary_key,
                "columns": columns,
                "foreign_keys": foreign_keys,
            }

        _validate_embedded_payloads(rows_by_table)
        _validate_internal_writer_relationships(rows_by_table)
        core_references = _collect_core_references(
            connection,
            rows_by_table=rows_by_table,
            project_id=project_id,
        )

        migration_rows = connection.execute(
            "SELECT filename FROM schema_migrations ORDER BY filename"
        ).fetchall()
        migration_filenames = [str(row[0]) for row in migration_rows]
    except sqlite3.Error as exc:
        raise LegacyWriterExportError(f"Cannot read Writer state: {exc}") from exc
    finally:
        connection.close()

    table_roots = {
        table: {
            "row_count": table_descriptors[table]["row_count"],
            "sha256": table_descriptors[table]["sha256"],
            "primary_key_sha256": table_descriptors[table]["primary_key_sha256"],
            "schema_sha256": table_descriptors[table]["schema_sha256"],
        }
        for table in LEGACY_WRITER_TABLES
    }
    tables_sha256 = hashlib.sha256(_canonical_json(table_roots)).hexdigest()
    core_references_sha256 = hashlib.sha256(_canonical_json(core_references)).hexdigest()
    semantic_root_sha256 = hashlib.sha256(
        _canonical_json(
            {
                "contract": LEGACY_WRITER_EXPORT_CONTRACT,
                "project_id": project_id,
                "schema_fingerprint": LEGACY_WRITER_SCHEMA_FINGERPRINT,
                "tables_sha256": tables_sha256,
                "core_references_sha256": core_references_sha256,
            }
        )
    ).hexdigest()
    manifest: dict[str, Any] = {
        "contract": LEGACY_WRITER_EXPORT_CONTRACT,
        "format_version": LEGACY_WRITER_EXPORT_VERSION,
        "schema_fingerprint": LEGACY_WRITER_SCHEMA_FINGERPRINT,
        "source": {
            "core_version": __version__,
            "backup_sha256": source_sha256,
            "backup_size_bytes": source_path.stat().st_size,
            "project_id": project_id,
            "project_name": str(project["name"]),
            "project_created_at": project["created_at"],
            "schema_migrations": migration_filenames,
        },
        "authority": {
            "source": "rka-core legacy Writer compatibility state",
            "target": "rka-writer staging only",
            "authority_switched": False,
        },
        "required_tables": list(LEGACY_WRITER_TABLES),
        "tables": table_descriptors,
        "table_count": len(LEGACY_WRITER_TABLES),
        "row_count": sum(item["row_count"] for item in table_descriptors.values()),
        "tables_sha256": tables_sha256,
        "core_references": core_references,
        "core_references_sha256": core_references_sha256,
        "semantic_root_sha256": semantic_root_sha256,
        "nonportable_fields": [
            "manuscripts.workspace_ref",
            "manuscript_source_proposals.relative_path",
            "manuscript_source_proposals.recovery_manifest_path",
        ],
        "sensitive_fields": ["manuscript_source_proposals.proposed_content"],
    }
    return manifest, table_payloads


def _read_project_rows(
    connection: sqlite3.Connection,
    *,
    table: str,
    project_id: str,
    order_by: str,
) -> list[dict[str, Any]]:
    if table == "manuscript_migration_issues":
        query = f"""SELECT issue.* FROM [{table}] AS issue
                    WHERE issue.project_id = ?
                       OR (
                           issue.project_id IS NULL
                           AND EXISTS (
                               SELECT 1 FROM journal AS source
                               WHERE source.id = issue.legacy_journal_id
                                 AND source.project_id = ?
                           )
                       )
                    ORDER BY {order_by}"""
        values: list[Any] = [project_id, project_id]
    else:
        query = f"SELECT * FROM [{table}] WHERE project_id = ? ORDER BY {order_by}"
        values = [project_id]
    return [_json_row(row) for row in connection.execute(query, values)]


def _project_writer_fk_violations(
    connection: sqlite3.Connection,
    *,
    project_id: str,
) -> list[dict[str, Any]]:
    violations: list[dict[str, Any]] = []
    for raw in connection.execute("PRAGMA foreign_key_check"):
        table = str(raw[0])
        if table not in LEGACY_WRITER_TABLES:
            continue
        rowid = raw[1]
        if rowid is None:
            violations.append(dict(raw))
            continue
        source = connection.execute(
            f"SELECT project_id FROM [{table}] WHERE rowid = ?", [rowid]
        ).fetchone()
        if source is not None and source["project_id"] == project_id:
            violations.append(dict(raw))
    return violations


def _validate_embedded_payloads(rows_by_table: dict[str, list[dict[str, Any]]]) -> None:
    for row in rows_by_table["manuscript_source_proposals"]:
        content = row.get("proposed_content")
        expected = row.get("proposed_content_hash")
        if not isinstance(content, str) or hashlib.sha256(content.encode()).hexdigest() != expected:
            raise LegacyWriterExportError(
                f"Source proposal {row.get('id')!r} has an invalid proposed_content_hash"
            )

    for row in rows_by_table["semantic_patch_context_manifests"]:
        payload = {
            "schema_version": "rka.context-manifest/v1",
            "project_id": row["project_id"],
            "origin": row["origin"],
            "provider": row["provider"],
            "model": row["model"],
            "boundary": row["boundary"],
            "selected_context": _parse_json(row, "selected_context"),
            "resolved_context": _parse_json(row, "resolved_context"),
            "target_bases": _parse_json(row, "target_bases"),
            "constraints": _parse_json(row, "constraints"),
            "omissions": _parse_json(row, "omissions"),
            "truncation_notes": _parse_json(row, "truncation_notes"),
        }
        actual = hashlib.sha256(_canonical_json(payload)).hexdigest()
        if actual != row.get("manifest_hash"):
            raise LegacyWriterExportError(
                f"Semantic context manifest {row.get('id')!r} has an invalid manifest_hash"
            )

    json_fields = {
        "manuscript_checkpoints": ("dependency_snapshot",),
        "manuscript_claim_verification_attestations": (
            "dependency_snapshot",
            "full_json_payload",
        ),
        "manuscript_evaluation_events": ("details",),
        "manuscript_planning_artifact_versions": (
            "payload",
            "unresolved_items",
            "readiness_missing",
        ),
        "manuscript_planning_branch_events": ("details",),
        "manuscript_planning_promotion_events": ("details",),
        "manuscript_source_events": ("details",),
        "manuscript_source_proposals": ("validation_findings",),
        "reference_validation_attestations": (
            "sources_tried",
            "sources_confirmed",
            "stage_trace",
            "full_json_payload",
        ),
        "reference_validation_migration_issues": ("details",),
        "semantic_patch_proposal_events": ("details",),
        "semantic_patch_proposals": (
            "operations",
            "target_bases",
            "semantic_diff",
            "validation_findings",
        ),
        "semantic_patch_provider_events": ("details",),
    }
    for table, fields in json_fields.items():
        for row in rows_by_table[table]:
            for field in fields:
                if row.get(field) is not None:
                    _parse_json(row, field)


def _validate_internal_writer_relationships(
    rows_by_table: dict[str, list[dict[str, Any]]],
) -> None:
    """Validate non-FK Writer pointers that must round-trip as one graph."""

    versions = {
        str(row["id"]): row
        for row in rows_by_table["manuscript_planning_artifact_versions"]
    }
    for artifact in rows_by_table["manuscript_planning_artifacts"]:
        current_version = int(artifact["current_version"])
        current_version_id = artifact.get("current_version_id")
        if current_version == 0 and current_version_id is None:
            continue
        version = versions.get(str(current_version_id))
        if (
            version is None
            or version.get("artifact_id") != artifact.get("id")
            or int(version.get("version") or 0) != current_version
        ):
            raise LegacyWriterExportError(
                f"Planning artifact {artifact.get('id')!r} has an invalid current version pointer"
            )

    attestation_ids = {
        str(row["id"]) for row in rows_by_table["reference_validation_attestations"]
    }
    for issue in rows_by_table["reference_validation_migration_issues"]:
        if str(issue.get("attestation_id")) not in attestation_ids:
            raise LegacyWriterExportError(
                f"Reference validation migration issue {issue.get('id')!r} has a "
                "missing attestation"
            )


def _parse_json(row: dict[str, Any], field: str) -> Any:
    value = row.get(field)
    if not isinstance(value, str):
        raise LegacyWriterExportError(
            f"Writer record {row.get('id')!r} field {field!r} is not JSON text"
        )
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        raise LegacyWriterExportError(
            f"Writer record {row.get('id')!r} field {field!r} contains malformed JSON"
        ) from exc


def _collect_core_references(
    connection: sqlite3.Connection,
    *,
    rows_by_table: dict[str, list[dict[str, Any]]],
    project_id: str,
) -> list[dict[str, Any]]:
    requests: list[tuple[str, dict[str, Any], str, str, str]] = []
    for table, field, entity_type in _DIRECT_CORE_REFERENCES:
        for row in rows_by_table[table]:
            entity_id = row.get(field)
            if entity_id:
                requests.append((table, row, field, entity_type, str(entity_id)))

    for row in rows_by_table["manuscript_units"]:
        artifact_ref = row.get("artifact_ref")
        if isinstance(artifact_ref, str) and artifact_ref.startswith(("art_", "fig_")):
            entity_type = "artifact" if artifact_ref.startswith("art_") else "figure"
            requests.append(
                ("manuscript_units", row, "artifact_ref", entity_type, artifact_ref)
            )

    for row in rows_by_table["manuscript_planning_evidence_bindings"]:
        entity_type = str(row["entity_type"])
        if entity_type in _INTERNAL_WRITER_ENTITY_TYPES:
            _validate_internal_writer_reference(
                rows_by_table,
                source_table="manuscript_planning_evidence_bindings",
                source_row=row,
                source_field="entity_id",
                entity_type=entity_type,
                entity_id=str(row["entity_id"]),
            )
        else:
            requests.append(
                (
                    "manuscript_planning_evidence_bindings",
                    row,
                    "entity_id",
                    entity_type,
                    str(row["entity_id"]),
                )
            )

    for table in (
        "manuscript_planning_artifact_versions",
        "manuscript_planning_promotion_events",
        "manuscript_evaluation_events",
    ):
        for row in rows_by_table[table]:
            promoted = row.get("promotion_target_type") is not None
            entity_type = row.get("promotion_target_type") or row.get("target_type")
            entity_id = row.get("promotion_target_id") or row.get("target_id")
            if not entity_type or not entity_id:
                continue
            source_field = "promotion_target_id" if promoted else "target_id"
            if entity_type in _INTERNAL_WRITER_ENTITY_TYPES:
                _validate_internal_writer_reference(
                    rows_by_table,
                    source_table=table,
                    source_row=row,
                    source_field=source_field,
                    entity_type=str(entity_type),
                    entity_id=str(entity_id),
                )
            else:
                requests.append((table, row, source_field, str(entity_type), str(entity_id)))

    for row in rows_by_table["semantic_patch_context_manifests"]:
        for selection in _parse_json(row, "selected_context"):
            if not isinstance(selection, dict) or not selection.get("entity_id"):
                raise LegacyWriterExportError(
                    f"Semantic context manifest {row.get('id')!r} has invalid selected_context"
                )
            entity_id = str(selection["entity_id"])
            entity_type = _entity_type_from_id(entity_id)
            if entity_type is None:
                raise LegacyWriterExportError(
                    f"Semantic context manifest {row.get('id')!r} references "
                    f"unknown entity ID {entity_id!r}"
                )
            if entity_type in _INTERNAL_WRITER_ENTITY_TYPES:
                _validate_internal_writer_reference(
                    rows_by_table,
                    source_table="semantic_patch_context_manifests",
                    source_row=row,
                    source_field="selected_context.entity_id",
                    entity_type=entity_type,
                    entity_id=entity_id,
                )
            else:
                requests.append(
                    (
                        "semantic_patch_context_manifests",
                        row,
                        "selected_context.entity_id",
                        entity_type,
                        entity_id,
                    )
                )

    references = [
        _resolve_core_reference(
            connection,
            project_id=project_id,
            source_table=table,
            source_row=row,
            source_field=field,
            entity_type=entity_type,
            entity_id=entity_id,
        )
        for table, row, field, entity_type, entity_id in requests
    ]
    references.sort(
        key=lambda item: (
            item["source_table"],
            _canonical_json(item["source_primary_key"]),
            item["source_field"],
            item["entity_type"],
            item["entity_id"],
        )
    )
    return references


def _validate_internal_writer_reference(
    rows_by_table: dict[str, list[dict[str, Any]]],
    *,
    source_table: str,
    source_row: dict[str, Any],
    source_field: str,
    entity_type: str,
    entity_id: str,
) -> None:
    """Fail closed when a polymorphic Writer reference is absent from the bundle."""

    target_table = _INTERNAL_WRITER_ENTITY_TABLES.get(entity_type)
    if target_table is None:
        raise LegacyWriterExportError(
            f"Writer record references unsupported internal entity type {entity_type!r}"
        )
    if not any(row.get("id") == entity_id for row in rows_by_table[target_table]):
        source_id = source_row.get("id") or source_row.get("legacy_journal_id")
        raise LegacyWriterExportError(
            f"Writer record {source_table}:{source_id!r} field {source_field!r} "
            f"has missing internal reference {entity_type}:{entity_id}"
        )


def _resolve_core_reference(
    connection: sqlite3.Connection,
    *,
    project_id: str,
    source_table: str,
    source_row: dict[str, Any],
    source_field: str,
    entity_type: str,
    entity_id: str,
) -> dict[str, Any]:
    target_table = _CORE_ENTITY_TABLES.get(entity_type)
    if target_table is None:
        raise LegacyWriterExportError(
            f"Writer record references unsupported Core entity type {entity_type!r}"
        )
    target_columns = {
        str(row["name"]) for row in connection.execute(f"PRAGMA table_info([{target_table}])")
    }
    if "id" not in target_columns or (
        target_table != "projects" and "project_id" not in target_columns
    ):
        raise LegacyWriterExportError(
            f"Core reference table {target_table!r} lacks project-scoped identity"
        )
    matches = [
        _json_row(row)
        for row in connection.execute(f"SELECT * FROM [{target_table}] WHERE id = ?", [entity_id])
    ]
    target = next(
        (
            row
            for row in matches
            if (
                row.get("id") == project_id
                if target_table == "projects"
                else row.get("project_id") == project_id
            )
        ),
        None,
    )
    if target is None:
        status = "wrong_project" if matches else "missing"
        raise LegacyWriterExportError(
            f"Writer record has {status} Core reference {entity_type}:{entity_id}"
        )

    writer_pk = _primary_key_columns(connection, source_table)
    metadata = {
        key: target[key]
        for key in (
            "version",
            "revision",
            "content_hash",
            "sha256",
            "updated_at",
        )
        if key in target and target[key] is not None
    }
    return {
        "source_table": source_table,
        "source_primary_key": {key: source_row[key] for key in writer_pk},
        "source_field": source_field,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "target_table": target_table,
        "source_version": source_row.get("source_version") or source_row.get("target_version"),
        "stored_content_hash": source_row.get("content_hash"),
        "snapshot_fingerprint": hashlib.sha256(_canonical_json(target)).hexdigest(),
        "snapshot_metadata": metadata,
        "resolution_status": "resolved",
    }


def _primary_key_columns(connection: sqlite3.Connection, table: str) -> list[str]:
    columns = [dict(row) for row in connection.execute(f"PRAGMA table_info([{table}])")]
    return [
        str(column["name"])
        for column in sorted(columns, key=lambda column: int(column["pk"] or 0))
        if int(column["pk"] or 0) > 0
    ]


def _entity_type_from_id(entity_id: str) -> str | None:
    prefix = entity_id.partition("_")[0]
    return {
        "jrn": "journal",
        "lit": "literature",
        "dec": "decision",
        "clm": "claim",
        "csc": "claim_scope",
        "ecl": "cluster",
        "icd": "interpretation_candidate",
        "ich": "interpretation_hint",
        "icv": "interpretation_review",
        "ipm": "interpretation_promotion",
        "exp": "experiment",
        "epv": "experiment_plan_version",
        "run": "experiment_run",
        "obs": "experiment_observation",
        "elc": "evidence_locator",
        "art": "artifact",
        "mis": "mission",
        "prj": "project",
        "chk": "checkpoint",
        "fig": "figure",
        "top": "topic",
        "rev": "review",
        "evt": "event",
        "lnk": "link",
        "ced": "claim_edge",
        "dop": "decision_option",
        "rvd": "reference_validation",
        "man": "manuscript",
        "mcl": "manuscript_claim",
        "mra": "manuscript_claim_ratification",
        "mun": "manuscript_unit",
        "mck": "manuscript_checkpoint",
        "mva": "manuscript_claim_verification",
        "mrf": "manuscript_reference",
        "spp": "semantic_patch_proposal",
    }.get(prefix)


def _json_row(row: sqlite3.Row) -> dict[str, Any]:
    return {key: _json_value(row[key]) for key in row.keys()}


def _json_value(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"$rka_base64": base64.b64encode(value).decode("ascii")}
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise LegacyWriterExportError(
        f"Writer state contains unsupported SQLite value type: {type(value).__name__}"
    )


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _write_zip_bytes(archive: zipfile.ZipFile, name: str, payload: bytes) -> None:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o600 << 16
    archive.writestr(info, payload)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
