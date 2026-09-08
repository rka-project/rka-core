"""Validate portable references and original digests before any ID rewrite."""

import hashlib
import json


# Intentional runtime-only exclusion. The completed attestation remains; its
# original worker identity is retained as source_job_id in the payload.
EXCLUDED_FOREIGN_KEYS = {("reference_validation_attestations", "validation_job_id")}
# The legacy DB column is nullable, but the public checkpoint lifecycle
# requires a mission. A pack must not manufacture a detached checkpoint.
REQUIRED_LOGICAL_REFERENCES = {("checkpoints", "mission_id")}
LOGICAL_REFERENCES = {
    "journal": {"superseded_by": "journal", "related_mission": "missions"},
    "claims": {"staleness_resolution_journal_id": "journal"},
    "evidence_clusters": {"staleness_resolution_journal_id": "journal"},
    "checkpoints": {"linked_decision_id": "decisions"},
    "calibration_outcomes": {"decision_id": "decisions"},
}
JSON_LIST_REFERENCES = {
    "journal": {"related_decisions": "decisions", "related_literature": "literature"},
    "literature": {"related_decisions": "decisions"},
    "decisions": {
        "related_missions": "missions",
        "related_literature": "literature",
        "related_journal": "journal",
    },
    "claim_scope_versions": {"disconfirming_claim_ids": "claims"},
    "exploration_summaries": {"entry_ids": "journal"},
}


def issue(category, table, row, column, target=None, severity="critical"):
    return {
        "category": category,
        "severity": severity,
        "count": 1,
        "ids": [str(row.get("id", ""))],
        "table": table,
        "column": column,
        "target": target,
        "description": f"{table}.{column}: {category}",
        "fix_action": "Restore required same-project reference or original untampered content",
    }


def manifest_digest(row, project_id):
    payload = {
        "schema_version": "rka.context-manifest/v1",
        "project_id": project_id,
        **{key: row.get(key) for key in ("origin", "provider", "model", "boundary")},
        **{
            key: json.loads(row[key])
            for key in (
                "selected_context",
                "resolved_context",
                "target_bases",
                "constraints",
                "omissions",
                "truncation_notes",
            )
        },
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def original_hash_issues(tables, project_id):
    issues = []
    for row in tables.get("semantic_patch_context_manifests", []):
        try:
            valid = row.get("manifest_hash") == manifest_digest(row, project_id)
        except (ValueError, TypeError, KeyError):
            valid = False
        if not valid:
            issues.append(
                issue(
                    "source_manifest_hash_invalid",
                    "semantic_patch_context_manifests",
                    row,
                    "manifest_hash",
                )
            )
    for row in tables.get("manuscript_checkpoints", []):
        try:
            snapshot = json.loads(row.get("dependency_snapshot") or "null")
            if isinstance(snapshot, dict) and isinstance(snapshot.get("components"), dict):
                digest = hashlib.sha256(
                    json.dumps(
                        snapshot["components"], sort_keys=True, separators=(",", ":"), default=str
                    ).encode()
                ).hexdigest()
                if snapshot.get("sha256") != digest:
                    issues.append(
                        issue(
                            "source_dependency_hash_invalid",
                            "manuscript_checkpoints",
                            row,
                            "dependency_snapshot",
                        )
                    )
        except (ValueError, TypeError):
            issues.append(
                issue(
                    "source_dependency_hash_invalid",
                    "manuscript_checkpoints",
                    row,
                    "dependency_snapshot",
                )
            )
    return issues


async def validate_references(db, tables, project_id, endpoint_tables):
    """PRAGMA is the FK authority; logical/polymorphic refs are explicit."""
    issues = []
    ids = {
        table: {str(row["id"]) for row in rows if row.get("id") is not None}
        for table, rows in tables.items()
    }
    ids["projects"] = {project_id}
    seen = set()
    for table, rows in tables.items():
        # Table names have already passed the closed import table registry.
        fk_rows = await db.fetchall(f"PRAGMA foreign_key_list([{table}])")
        required = {
            r["name"]
            for r in await db.fetchall(f"PRAGMA table_info([{table}])")
            if r["notnull"] and r["dflt_value"] is None
        }
        references = {row["from"]: (row["table"], row["to"] or "id") for row in fk_rows}
        references.update(
            {key: (target, "id") for key, target in LOGICAL_REFERENCES.get(table, {}).items()}
        )
        for row in rows:
            if "project_id" in row and row["project_id"] != project_id:
                issues.append(issue("source_project_mismatch", table, row, "project_id"))
            if row.get("id") is not None:
                # Import uses one global ID map, so cross-table collisions are
                # ambiguous too (even where SQLite would accept separate PKs).
                identity = str(row["id"])
                if identity in seen:
                    issues.append(issue("duplicate_source_id", table, row, "id"))
                seen.add(identity)
            for column, (target, target_column) in references.items():
                value = row.get(column)
                if value is None:
                    if (column in required or (table, column) in REQUIRED_LOGICAL_REFERENCES) and (
                        table,
                        column,
                    ) not in EXCLUDED_FOREIGN_KEYS:
                        issues.append(issue("missing_pack_reference", table, row, column))
                    continue
                if (table, column) in EXCLUDED_FOREIGN_KEYS:
                    issues.append(
                        issue("excluded_runtime_reference", table, row, column, value, "warning")
                    )
                    continue
                candidates = (
                    ids.get(target, set())
                    if target_column == "id"
                    else {str(r.get(target_column)) for r in tables.get(target, [])}
                )
                if str(value) not in candidates:
                    issues.append(issue("missing_pack_reference", table, row, column, value))
            if table == "directive_dependencies":
                directive = next(
                    (
                        r
                        for r in tables.get("journal", [])
                        if r.get("id") == row.get("directive_id")
                    ),
                    {},
                )
                if directive.get("type") != "directive":
                    issues.append(issue("invalid_directive_dependency", table, row, "directive_id"))
            if table == "decisions":
                for column in ("recommended_option_id", "pi_selected_option_id"):
                    if row.get(column) is not None:
                        option = next(
                            (
                                r
                                for r in tables.get("decision_options", [])
                                if r.get("id") == row[column]
                            ),
                            {},
                        )
                        if option.get("decision_id") != row.get("id"):
                            issues.append(
                                issue("option_decision_mismatch", table, row, column, row[column])
                            )
            for column, target in JSON_LIST_REFERENCES.get(table, {}).items():
                if not row.get(column):
                    continue
                try:
                    values = json.loads(row[column])
                    valid = values is None or (
                        isinstance(values, list)
                        and all(isinstance(v, str) and v in ids.get(target, set()) for v in values)
                    )
                except (ValueError, TypeError):
                    valid = False
                if not valid:
                    issues.append(issue("invalid_json_reference", table, row, column))
            pairs = []
            if table == "entity_links":
                pairs = [
                    (row.get("source_type"), "source_id"),
                    (row.get("target_type"), "target_id"),
                ]
            elif table == "review_queue":
                pairs = [(row.get("item_type"), "item_id")]
            elif table in {"tags", "entity_topics"}:
                pairs = [(row.get("entity_type"), "entity_id")]
            for kind, column in pairs:
                target = endpoint_tables.get(kind)
                if target is None or str(row.get(column)) not in ids.get(target, set()):
                    category = (
                        (
                            "orphaned_entity_link_sources"
                            if column == "source_id"
                            else "orphaned_entity_link_targets"
                        )
                        if table == "entity_links"
                        else "invalid_polymorphic_reference"
                    )
                    issues.append(issue(category, table, row, column, row.get(column)))
    return issues
