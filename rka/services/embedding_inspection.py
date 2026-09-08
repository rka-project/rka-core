"""Read-only global index inspection and advisory plans, with no provider.

Inspection is deliberately separate from writable Database initialization.
Plans describe required work; they never grant maintenance ownership or enqueue
work. Cross-process exclusion and executable offline recovery are later gates.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path

from rka.infra.embedding_documents import DOCUMENT_SPECS, compose_document, embedding_content_hash
from rka.infra.readonly_sqlite import readonly_sqlite
from rka.services.embedding_config import DEFAULT_CONFIG, EmbeddingConfig
from rka.services.embedding_index import embedding_space_signature, legacy_index_adoption_safe


class EmbeddingInspectionError(ValueError):
    """Fixed, non-sensitive error codes, never provider/config/source text."""


def _read_config(path: Path):
    try:
        with path.open("rb") as stream:
            raw = stream.read(65537)
        if len(raw) > 65536:
            raise EmbeddingInspectionError("config_too_large")
        config = EmbeddingConfig.model_validate(json.loads(raw))
        sub = config.config
        model = sub.get("model_name", DEFAULT_CONFIG.config["model_name"]) if config.backend == "fastembed" else sub.get("model")
        dim = sub.get("dim", 768 if config.backend == "fastembed" else None)
        if config.backend == "fastembed" and dim is None:
            dim = 768
        if not isinstance(model, str) or not model.strip() or len(model) > 4096:
            raise EmbeddingInspectionError("model_identity_unknown")
        if type(dim) is not int or not 0 < dim <= 65536:
            raise EmbeddingInspectionError("dimension_unknown_or_invalid")
        explicit = sub.get("embedding_space_id")
        if explicit is not None and not isinstance(explicit, str):
            raise EmbeddingInspectionError("space_identity_invalid")
        if config.backend == "openai_compat":
            # The validator is pure: it does not construct a backend/client.
            from rka.infra.embedding_backends.openai_compat import OpenAICompatBackend
            for field in ("query_template", "document_template"):
                OpenAICompatBackend._validate_template(field, sub.get(field, "{text}"))
            if not isinstance(sub.get("base_url"), str) or not sub["base_url"]:
                raise EmbeddingInspectionError("endpoint_missing")
            model = (explicit or "").strip() or model
        from rka.infra.embedding_resources import EmbeddingResourceLimits
        EmbeddingResourceLimits.from_config(sub.get("resource_limits"))
        identity = {
            "source": "persisted", "backend": config.backend,
            "model_name": model, "dimensions": dim,
            "space_signature": embedding_space_signature(config, dimensions=dim),
            "legacy_adoption_safe": legacy_index_adoption_safe(config),
            "provider_validation": "not_run",
        }
        return raw, identity
    except EmbeddingInspectionError:
        raise
    except FileNotFoundError:
        raise EmbeddingInspectionError("persisted_config_missing") from None
    except Exception:
        raise EmbeddingInspectionError("persisted_config_invalid_or_unreadable") from None


def _counts():
    return dict.fromkeys((
        "source_rows", "eligible", "empty", "invalid_document", "unaddressable",
        "metadata_missing", "metadata_wrong_space", "hash_verified", "hash_mismatch",
        "vector_missing", "reusable", "needs_embedding",
    ), 0)


async def _schema(db):
    names = {"embedding_metadata", "embedding_index_state"}
    names.update(spec.source_table for spec in DOCUMENT_SPECS.values())
    names.update(spec.vec_table for spec in DOCUMENT_SPECS.values())
    rows = await db.fetchall(
        f'SELECT name, type, sql FROM sqlite_schema WHERE name IN ({", ".join("?" for _ in names)})',
        list(names),
    )
    tables = {row["name"]: row for row in rows}
    required = {"embedding_metadata": ("project_id", "entity_type", "entity_id", "content_hash", "model_name", "dimensions")}
    required.update({spec.source_table: ("id", "project_id", *spec.fields) for spec in DOCUMENT_SPECS.values()})
    for name, fields in required.items():
        entry = tables.get(name)
        if not entry or entry["type"] != "table" or "VIRTUAL TABLE" in (entry["sql"] or "").upper():
            raise EmbeddingInspectionError("source_schema_unsupported")
        columns = {row["name"] for row in await db.fetchall(f'PRAGMA table_info("{name}")')}
        if not set(fields) <= columns:
            raise EmbeddingInspectionError("source_schema_unsupported")
    return tables


async def _physical_tables(db, schema):
    physical = {}
    for table in dict.fromkeys(spec.vec_table for spec in DOCUMENT_SPECS.values()):
        entry = schema.get(table)
        sql = (entry or {}).get("sql") or ""
        match = re.search(r"\bembedding\s+float\[(\d+)\]", sql, re.I)
        valid = bool(entry and entry["type"] == "table" and re.search(r"\bUSING\s+vec0\s*\(", sql, re.I) and match)
        count = None
        if valid and db.vec_available:
            fields = {row["name"] for row in await db.fetchall(f'PRAGMA table_info("{table}")')}
            valid = {"id", "project_id", "embedding"} <= fields and (table != "vec_artifacts" or "entity_type" in fields)
            if valid:
                count = (await db.fetchone(f"SELECT count(*) AS n FROM {table}"))["n"]
        physical[table] = {"schema_supported": valid, "dimensions": int(match[1]) if match else None, "rows": count}
    return physical


async def _structural_counts(db, *, vectors_known):
    unknown_metadata = (await db.fetchone(
        f'SELECT count(*) AS n FROM embedding_metadata WHERE entity_type NOT IN ({", ".join("?" for _ in DOCUMENT_SPECS)})',
        list(DOCUMENT_SPECS),
    ))["n"]
    orphan_metadata = 0
    orphan_vectors = 0 if vectors_known else None
    missing_vectors = 0 if vectors_known else None
    for entity, spec in DOCUMENT_SPECS.items():
        orphan_metadata += (await db.fetchone(
            f"""SELECT count(*) AS n FROM embedding_metadata m WHERE m.entity_type = ?
                AND NOT EXISTS (SELECT 1 FROM {spec.source_table} s WHERE s.id=m.entity_id AND s.project_id=m.project_id)""",
            [entity],
        ))["n"]
        if vectors_known:
            vector_type = " AND v.entity_type=m.entity_type" if spec.vec_table == "vec_artifacts" else ""
            missing_vectors += (await db.fetchone(
                f"""SELECT count(*) AS n FROM embedding_metadata m WHERE m.entity_type=?
                    AND NOT EXISTS (SELECT 1 FROM {spec.vec_table} v
                    WHERE v.id=m.entity_id AND v.project_id=m.project_id{vector_type})""",
                [entity],
            ))["n"]
            shared = "v.entity_type = ? AND " if spec.vec_table == "vec_artifacts" else ""
            params = [entity, entity] if shared else [entity]
            orphan_vectors += (await db.fetchone(
                f"""SELECT count(*) AS n FROM {spec.vec_table} v WHERE {shared}
                    NOT EXISTS (SELECT 1 FROM embedding_metadata m
                    WHERE m.entity_type=? AND m.entity_id=v.id AND m.project_id=v.project_id)""",
                params,
            ))["n"]
    if vectors_known:
        orphan_vectors += (await db.fetchone(
            "SELECT count(*) AS n FROM vec_artifacts WHERE entity_type NOT IN ('artifact', 'figure') OR entity_type IS NULL"
        ))["n"]
    return {"unknown_metadata": unknown_metadata, "orphan_metadata": orphan_metadata,
            "orphan_vectors": orphan_vectors, "missing_vectors": missing_vectors}


async def _inspect_snapshot(db, identity, *, max_rows, deadline):
    schema = await _schema(db)
    physical = await _physical_tables(db, schema)
    vectors_known = db.vec_available and all(p["schema_supported"] for p in physical.values())
    generation = None
    if "embedding_index_state" in schema:
        if (schema["embedding_index_state"]["type"] != "table"
                or "VIRTUAL TABLE" in (schema["embedding_index_state"]["sql"] or "").upper()):
            raise EmbeddingInspectionError("generation_schema_unsupported")
        generation = await db.fetchone(
            "SELECT generation, space_signature, model_name, dimensions, status FROM embedding_index_state WHERE singleton=1"
        )
        if generation and (
            type(generation["generation"]) is not int or generation["generation"] < 1
            or type(generation["dimensions"]) is not int or generation["dimensions"] < 1
            or not isinstance(generation["space_signature"], str)
            or not isinstance(generation["model_name"], str)
            or generation["status"] not in {"ready", "reindexing", "failed"}
        ):
            raise EmbeddingInspectionError("generation_record_invalid")
    matching_generation = bool(generation and all(
        generation[key] == identity[key] for key in ("space_signature", "model_name", "dimensions")
    ))
    identity_proven = matching_generation or (generation is None and identity["legacy_adoption_safe"])
    totals, entities = _counts(), {}
    for entity, spec in DOCUMENT_SPECS.items():
        counts = _counts()
        last_id = None
        while True:
            if time.monotonic() > deadline:
                raise EmbeddingInspectionError("inspection_budget_exceeded")
            rows = await db.fetchall(
                f"""SELECT s.id, s.project_id, {", ".join("s." + f for f in spec.fields)},
                           m.entity_id AS meta_id, m.content_hash AS stored_hash,
                           m.model_name AS stored_model, m.dimensions AS stored_dim
                    FROM {spec.source_table} s LEFT JOIN embedding_metadata m
                      ON m.entity_type=? AND m.entity_id=s.id AND m.project_id=s.project_id
                    WHERE (? IS NULL OR s.id > ?) ORDER BY s.id LIMIT 8""",
                [entity, last_id, last_id],
            )
            if not rows:
                break
            for row in rows:
                counts["source_rows"] += 1
                if totals["source_rows"] + counts["source_rows"] > max_rows:
                    raise EmbeddingInspectionError("inspection_budget_exceeded")
                if not row["id"] or not row["project_id"]:
                    counts["unaddressable"] += 1
                    if row["id"] is None:
                        # Cannot advance a NULL keyset cursor safely; no partial
                        # report may masquerade as a complete assessment.
                        raise EmbeddingInspectionError("source_id_unaddressable")
                try:
                    text = compose_document(entity, row)
                    digest = embedding_content_hash(text)
                except Exception:
                    counts["invalid_document"] += 1
                    continue
                if not text:
                    counts["empty"] += 1
                    # A stored vector for empty text is unverified, not reusable.
                    if row["meta_id"] is not None:
                        counts["hash_mismatch"] += 1
                    continue
                counts["eligible"] += 1
                matching_hash = False
                if row["meta_id"] is None:
                    counts["metadata_missing"] += 1
                elif row["stored_model"] != identity["model_name"] or row["stored_dim"] != identity["dimensions"]:
                    counts["metadata_wrong_space"] += 1
                elif row["stored_hash"] == digest:
                    counts["hash_verified"] += 1
                    matching_hash = True
                else:
                    counts["hash_mismatch"] += 1
                vector = None
                if vectors_known:
                    extra = " AND entity_type=?" if spec.vec_table == "vec_artifacts" else ""
                    vector = await db.fetchone(
                        f"SELECT 1 AS present FROM {spec.vec_table} WHERE id=? AND project_id=?{extra}",
                        [row["id"], row["project_id"], *([entity] if extra else [])],
                    )
                    if not vector:
                        counts["vector_missing"] += 1
                if (vector and matching_hash and row["id"] and row["project_id"]
                        and identity_proven and physical[spec.vec_table]["dimensions"] == identity["dimensions"]):
                    counts["reusable"] += 1
                else:
                    counts["needs_embedding"] += 1
            last_id = rows[-1]["id"]
        entities[entity] = counts
        for key, value in counts.items():
            totals[key] += value
    structural = await _structural_counts(db, vectors_known=vectors_known)
    model_mismatch = (await db.fetchone(
        "SELECT count(*) AS n FROM embedding_metadata WHERE model_name<>? OR dimensions<>?",
        [identity["model_name"], identity["dimensions"]],
    ))["n"]
    # Missing source coverage alone does not make stored pairs incoherent.
    coherent = bool(vectors_known and not any(structural.values()) and not model_mismatch
        and all(p["dimensions"] == identity["dimensions"] for p in physical.values()))
    problems = any(totals[k] for k in ("needs_embedding", "hash_mismatch", "invalid_document", "unaddressable"))
    healthy = coherent and not problems and matching_generation and generation["status"] == "ready"
    return {
        "version": 1, "scope": "global", "read_only": True,
        "identity": identity, "generation": generation, "generation_matches_config": matching_generation,
        "physical_tables": physical, "vectors_available": db.vec_available,
        "entities": entities, "totals": totals, "structural": {**structural, "model_mismatch": model_mismatch, "coherent": coherent},
        "assessment": {"complete": vectors_known, "status": "healthy" if healthy else ("needs_attention" if vectors_known else "incomplete")},
        "consistency": "database_snapshot_config_rechecked",
        "maintenance": {"ownership": "not_acquired", "execution_supported": False},
    }


def plan_embedding_recovery(report, target):
    """Advisory global impact only; never treat a preview as execution authority."""
    if not report["assessment"]["complete"]:
        action = "blocked"
    elif report["totals"]["invalid_document"] or report["totals"]["unaddressable"]:
        action = "resolve_source_errors"
    elif any(p["dimensions"] != target["dimensions"] for p in report["physical_tables"].values()):
        action = "offline_rebuild"
    elif target["space_signature"] != report["identity"]["space_signature"]:
        action = "rebuild_space"
    elif report["generation"] is None:
        action = "adopt_legacy" if target["legacy_adoption_safe"] and report["structural"]["coherent"] else "rebuild_space"
    elif not report["generation_matches_config"] or not report["structural"]["coherent"]:
        action = "rebuild_space"
    elif report["totals"]["needs_embedding"] or report["totals"]["hash_mismatch"]:
        action = "repair_rows"
    elif report["generation"]["status"] != "ready":
        action = "resume_generation"
    else:
        action = "none"
    preserve = action in {"none", "repair_rows", "adopt_legacy", "resume_generation"}
    return {"action": action, "scope": "global", "execution_supported": False,
            "preservable_pairs": report["totals"]["reusable"] if preserve else 0,
            "rows_to_embed": report["totals"]["needs_embedding"] if preserve else report["totals"]["eligible"],
            "target": target,
            "required_gates": [] if action == "none" else ["maintenance_ownership", "verified_backup", "execution_time_revalidation"],
            "warning": "advisory_only_no_work_queued"}


async def inspect_embedding_index(*, data_dir: Path, db_path: Path | None = None,
                                  target_config: Path | None = None, dry_run=False,
                                  max_rows=50000, timeout_seconds=30):
    """Inspect explicit local paths; missing config never falls back to env."""
    if not 1 <= max_rows <= 100000 or not 0 < timeout_seconds <= 60:
        raise EmbeddingInspectionError("inspection_budget_invalid")
    config_path = data_dir / "embedding_config.json"
    raw, identity = _read_config(config_path)
    target_raw, target = _read_config(target_config) if target_config else (raw, identity)
    source = db_path if db_path is not None else data_dir / "rka.db"
    deadline = time.monotonic() + timeout_seconds
    try:
        async with readonly_sqlite(source, timeout_seconds=timeout_seconds) as db:
            report = await _inspect_snapshot(db, identity, max_rows=max_rows, deadline=deadline)
            if _read_config(config_path)[0] != raw or (target_config and _read_config(target_config)[0] != target_raw):
                raise EmbeddingInspectionError("config_changed_during_inspection")
        # Record only a digest, never API keys, endpoint URLs or templates.
        report["config_digest"] = hashlib.sha256(raw).hexdigest()
        if dry_run:
            report["plan"] = plan_embedding_recovery(report, target)
        return report
    except EmbeddingInspectionError:
        raise
    except FileNotFoundError:
        raise EmbeddingInspectionError("database_missing") from None
    except Exception:
        raise EmbeddingInspectionError("database_unreadable_unsupported_or_budget_exceeded") from None
