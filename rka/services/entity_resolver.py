"""Read-only, project-attested bulk entity resolution.

The resolver deliberately returns an outcome for every requested identifier.
It never fills a missing ``project_id`` from caller context and never returns a
foreign-project record as resolved.  Consumers therefore have enough
information to fail closed instead of treating an ID prefix as proof of
ownership.

Packet contract (``rka-entity-resolution/v1``):

* ``requested_ids`` is the sorted, de-duplicated request.
* ``entities`` is keyed by every requested ID and, when source closure is
  enabled, every directly referenced claim source.
* each entity has ``found``, ``outcome``, canonical ``type``, ``project_id``,
  ``revision``, ``currentness``, and sorted ``tags``; unresolved entities
  never disclose an owning project;
* resolved records are flattened for existing Writer consumers and retained
  as a normalized stored-state object under ``record`` (where a journal's
  native ``type`` remains available);
* unresolved outcomes are ``missing``, ``wrong_project``, ``unscoped``, or
  ``unknown_type`` and never contain record content;
* ``terminal_sources`` maps each requested claim to its direct terminal
  journal resolution when ``include_sources`` is true;
* optional edge arrays contain only rows whose stored ``project_id`` matches
  the requested project.

Claim currency has two deliberately separate stored signals. ``stale`` is a
hard structural invalidation (for example, the source decision or promoted
interpretation was superseded). ``staleness`` is the freshness-review workflow
state: ``yellow`` warns and ``red`` invalidates, while ``green`` only means no
freshness-review warning is open. It does not cancel ``stale=True``. Consumers
must use the derived ``currentness`` object as the canonical currency result
rather than interpreting either similarly named field in isolation.

``revision.fingerprint`` is a stable digest of normalized stored state, tags,
and server-derived claim contradiction state.  It is suitable for change
detection, not as proof that an entity is scientifically valid.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

from rka.infra.database import Database


PACKET_SCHEMA_VERSION = "rka-entity-resolution/v1"
_BATCH_SIZE = 250
_RESERVED_PACKET_FIELDS = {
    "found",
    "outcome",
    "type",
    "project_id",
    "revision",
    "currentness",
    "tags",
    "record",
}
_INACTIVE_STATUSES = {
    "abandoned",
    "cancelled",
    "excluded",
    "merged",
    "rejected",
    "removed",
    "retracted",
    "superseded",
}
_INACTIVE_STATES = {"archived", "rejected", "retired", "withdrawn"}
_INACTIVE_DISPOSITIONS = {"historical", "retired", "retracted", "superseded"}
_KNOWN_STALENESS = {
    "dismissed",
    "green",
    "historical",
    "red",
    "retired",
    "retracted",
    "superseded",
    "yellow",
}


@dataclass(frozen=True)
class _EntitySpec:
    entity_type: str
    table: str
    json_fields: tuple[str, ...] = ()
    bool_fields: tuple[str, ...] = ()
    project_from_id: bool = False
    version_fields: tuple[str, ...] = ()


_ENTITY_SPECS: dict[str, _EntitySpec] = {
    "prj": _EntitySpec("project", "projects", project_from_id=True),
    "dec": _EntitySpec(
        "decision",
        "decisions",
        json_fields=(
            "options",
            "related_missions",
            "related_literature",
            "related_journal",
            "assumptions",
        ),
        version_fields=("scope_version",),
    ),
    "lit": _EntitySpec(
        "literature",
        "literature",
        json_fields=("authors", "key_findings", "related_decisions"),
    ),
    "jrn": _EntitySpec(
        "journal",
        "journal",
        json_fields=("related_decisions", "related_literature"),
        bool_fields=("pinned",),
    ),
    "mis": _EntitySpec(
        "mission",
        "missions",
        json_fields=("tasks", "report"),
        version_fields=("iteration",),
    ),
    "chk": _EntitySpec(
        "checkpoint",
        "checkpoints",
        json_fields=("options",),
        bool_fields=("blocking",),
    ),
    "clm": _EntitySpec(
        "claim",
        "claims",
        bool_fields=("verified", "stale", "embedding_pending"),
        version_fields=("scope_revision",),
    ),
    "csc": _EntitySpec(
        "claim_scope",
        "claim_scope_versions",
        json_fields=(
            "conditions",
            "allowed_extensions",
            "prohibited_extensions",
            "disconfirming_claim_ids",
        ),
        version_fields=("revision",),
    ),
    "icd": _EntitySpec(
        "interpretation_candidate",
        "interpretation_candidates",
        json_fields=("scope_conditions",),
        version_fields=("revision",),
    ),
    "ich": _EntitySpec(
        "interpretation_hint",
        "interpretation_candidate_hints",
    ),
    "icv": _EntitySpec(
        "interpretation_review",
        "interpretation_review_events",
    ),
    "ipm": _EntitySpec(
        "interpretation_promotion",
        "interpretation_promotions",
    ),
    "ecl": _EntitySpec(
        "cluster",
        "evidence_clusters",
        bool_fields=("needs_reprocessing",),
    ),
    "art": _EntitySpec("artifact", "artifacts", json_fields=("metadata",)),
    "fig": _EntitySpec(
        "figure",
        "figures",
        json_fields=("bbox", "claims"),
    ),
    "top": _EntitySpec("topic", "topics"),
    "rev": _EntitySpec("review", "review_queue", json_fields=("context",)),
    "evt": _EntitySpec("event", "events", json_fields=("details",)),
    "lnk": _EntitySpec("link", "entity_links"),
    "ced": _EntitySpec("claim_edge", "claim_edges"),
    "dop": _EntitySpec(
        "decision_option",
        "decision_options",
        json_fields=("pros", "cons", "evidence", "confidence_known_unknowns"),
        bool_fields=("is_recommended",),
    ),
    "rvd": _EntitySpec(
        "reference_validation",
        "reference_validation_attestations",
        json_fields=(
            "input_authors",
            "sources_tried",
            "sources_confirmed",
            "notes",
            "stage_trace",
            "full_json_payload",
        ),
        bool_fields=("retraction_check_enabled", "retraction_checked"),
    ),
    "man": _EntitySpec(
        "manuscript",
        "manuscripts",
        version_fields=("revision",),
    ),
    "mcl": _EntitySpec("manuscript_claim", "manuscript_claims"),
    "mra": _EntitySpec(
        "manuscript_claim_ratification",
        "manuscript_claim_ratifications",
    ),
    "mun": _EntitySpec("manuscript_unit", "manuscript_units"),
    "mck": _EntitySpec("manuscript_checkpoint", "manuscript_checkpoints"),
    "mva": _EntitySpec(
        "manuscript_claim_verification",
        "manuscript_claim_verification_attestations",
        json_fields=("dependency_snapshot", "full_json_payload"),
    ),
    "mrf": _EntitySpec(
        "manuscript_reference",
        "manuscript_reference_members",
    ),
}


def _chunks(values: Sequence[str], size: int = _BATCH_SIZE) -> Iterable[list[str]]:
    for start in range(0, len(values), size):
        yield list(values[start : start + size])


def _prefix(entity_id: str) -> str:
    return entity_id.partition("_")[0] if "_" in entity_id else ""


def _json_safe(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _json_safe(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, bytes):
        return {"encoding": "hex", "value": value.hex()}
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _parse_json_if_valid(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return value


def _normalize_record(spec: _EntitySpec, raw: Mapping[str, Any]) -> dict[str, Any]:
    record = {str(key): _json_safe(value) for key, value in raw.items()}
    for field in spec.json_fields:
        if field in record and record[field] is not None:
            record[field] = _json_safe(_parse_json_if_valid(record[field]))
    for field in spec.bool_fields:
        if field in record and record[field] is not None:
            record[field] = bool(record[field])
    return record


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _currentness(record: Mapping[str, Any], *, as_of: datetime) -> dict[str, Any]:
    """Derive conservative currency from explicit stored state only."""
    reasons: list[str] = []
    warnings: list[str] = []

    status = str(record.get("status") or "").strip().lower()
    if status in _INACTIVE_STATUSES:
        reasons.append(f"status:{status}")

    state = str(record.get("state") or "").strip().lower()
    if state in _INACTIVE_STATES:
        reasons.append(f"state:{state}")
    elif state == "on_hold":
        warnings.append("state:on_hold")

    confidence = str(record.get("confidence") or "").strip().lower()
    if confidence in {"retracted", "superseded"}:
        reasons.append(f"confidence:{confidence}")

    if record.get("superseded_by"):
        reasons.append("superseded_by")
    if bool(record.get("stale")):
        reasons.append("stale")
    if bool(record.get("needs_reprocessing")):
        reasons.append("needs_reprocessing")

    staleness = str(record.get("staleness") or "").strip().lower()
    if staleness:
        if staleness not in _KNOWN_STALENESS:
            reasons.append(f"invalid_staleness:{staleness}")
        elif staleness == "red" or staleness in _INACTIVE_DISPOSITIONS:
            reasons.append(f"staleness:{staleness}")
        elif staleness == "yellow":
            warnings.append("staleness:yellow")

    verdict = str(record.get("staleness_verdict") or "").strip().lower()
    if verdict in _INACTIVE_DISPOSITIONS:
        reasons.append(f"staleness_verdict:{verdict}")
    elif verdict and verdict not in {"current", "dismissed"}:
        reasons.append(f"invalid_staleness_verdict:{verdict}")

    valid_from_raw = record.get("valid_from")
    if valid_from_raw:
        valid_from = _parse_timestamp(valid_from_raw)
        if valid_from is None:
            reasons.append("invalid_valid_from")
        elif as_of < valid_from:
            reasons.append("not_yet_valid")

    validity_end_field = (
        "valid_until"
        if record.get("valid_until")
        else "synthesis_valid_until"
        if record.get("synthesis_valid_until")
        else None
    )
    if validity_end_field:
        valid_until = _parse_timestamp(record[validity_end_field])
        if valid_until is None:
            reasons.append(f"invalid_{validity_end_field}")
        elif as_of >= valid_until:
            reasons.append("expired")

    reasons = sorted(set(reasons))
    warnings = sorted(set(warnings))
    return {
        "is_current": not reasons,
        "state": "current" if not reasons else "not_current",
        "reasons": reasons,
        "warnings": warnings,
    }


def _unresolved_currentness(outcome: str) -> dict[str, Any]:
    return {
        "is_current": False,
        "state": "unresolved",
        "reasons": [outcome],
        "warnings": [],
    }


def _revision(
    spec: _EntitySpec,
    record: Mapping[str, Any],
    *,
    tags: Sequence[str],
) -> dict[str, Any]:
    payload = {
        "entity_type": spec.entity_type,
        "record": _json_safe(record),
        "tags": list(tags),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    version = next(
        (record.get(field) for field in spec.version_fields if record.get(field) is not None),
        None,
    )
    timestamp = next(
        (
            record.get(field)
            for field in ("updated_at", "completed_at", "resolved_at", "created_at")
            if record.get(field)
        ),
        None,
    )
    return {
        "fingerprint": f"sha256:{hashlib.sha256(encoded).hexdigest()}",
        "version": version,
        "timestamp": timestamp,
    }


class EntityResolverService:
    """Resolve heterogeneous RKA IDs in bounded, read-only SQL batches."""

    def __init__(self, db: Database):
        self.db = db

    async def resolve_entities(
        self,
        project_id: str,
        ids: Sequence[str],
        include_sources: bool = False,
        include_edges: bool = False,
    ) -> dict[str, Any]:
        """Return a deterministic, project-attested resolution packet.

        ``found`` means "resolved in the requested project", not merely that a
        row with that ID exists somewhere in the shared database.  A
        wrong-project row is reported opaquely with no owner or record
        content.  The managed read transaction keeps entity rows, tags,
        contradiction projections, terminal sources, and optional edges on one
        SQLite snapshot.
        """
        async with self.db.transaction(write=False):
            return await self._resolve_entities_snapshot(
                project_id,
                ids,
                include_sources=include_sources,
                include_edges=include_edges,
            )

    async def _resolve_entities_snapshot(
        self,
        project_id: str,
        ids: Sequence[str],
        *,
        include_sources: bool,
        include_edges: bool,
    ) -> dict[str, Any]:
        if not isinstance(project_id, str) or not project_id.strip():
            raise ValueError("project_id must be a non-empty string")
        if project_id != project_id.strip():
            raise ValueError("project_id must not contain surrounding whitespace")
        if isinstance(ids, (str, bytes)):
            raise ValueError("ids must be a sequence of entity-id strings")

        project = await self.db.fetchone(
            "SELECT id FROM projects WHERE id = ?",
            [project_id],
        )
        if project is None:
            raise ValueError(f"Unknown project_id: {project_id}")

        normalized_ids: list[str] = []
        for entity_id in ids:
            if not isinstance(entity_id, str) or not entity_id:
                raise ValueError("ids must contain only non-empty strings")
            if entity_id != entity_id.strip():
                raise ValueError("entity ids must not contain surrounding whitespace")
            normalized_ids.append(entity_id)
        requested_ids = sorted(set(normalized_ids))
        duplicates_removed = len(normalized_ids) - len(requested_ids)

        specs_by_id: dict[str, _EntitySpec | None] = {
            entity_id: _ENTITY_SPECS.get(_prefix(entity_id)) for entity_id in requested_ids
        }
        rows_by_id = await self._fetch_entity_rows(specs_by_id)

        terminal_sources: dict[str, dict[str, Any]] = {}
        if include_sources:
            terminal_sources, source_specs = self._plan_terminal_sources(
                requested_ids,
                specs_by_id,
                rows_by_id,
                project_id=project_id,
            )
            missing_source_specs = {
                entity_id: spec
                for entity_id, spec in source_specs.items()
                if entity_id not in specs_by_id
            }
            specs_by_id.update(missing_source_specs)
            rows_by_id.update(await self._fetch_entity_rows(missing_source_specs))

        resolved_pairs: dict[str, tuple[_EntitySpec, dict[str, Any]]] = {}
        for entity_id, spec in specs_by_id.items():
            row = rows_by_id.get(entity_id)
            if spec is None or row is None:
                continue
            stored_project = self._stored_project_id(spec, row)
            if stored_project == project_id:
                resolved_pairs[entity_id] = (spec, row)

        tags_by_id = await self._fetch_tags(resolved_pairs, project_id=project_id)
        contradicted_claims = await self._fetch_contradicted_claims(
            [
                entity_id
                for entity_id, (spec, _row) in resolved_pairs.items()
                if spec.entity_type == "claim"
            ],
            project_id=project_id,
        )

        as_of = datetime.now(timezone.utc)
        entities = {
            entity_id: self._build_resolution(
                entity_id,
                specs_by_id.get(entity_id),
                rows_by_id.get(entity_id),
                project_id=project_id,
                tags=tags_by_id.get(entity_id, []),
                contradicted=(
                    entity_id in contradicted_claims
                    if specs_by_id.get(entity_id) and specs_by_id[entity_id].entity_type == "claim"
                    else None
                ),
                as_of=as_of,
            )
            for entity_id in sorted(specs_by_id)
        }

        if include_sources:
            for claim_id, closure in terminal_sources.items():
                if closure["outcome"] != "pending":
                    continue
                source_id = closure.get("source_entry_id")
                source = entities.get(source_id) if source_id else None
                if source is None:
                    closure["outcome"] = "missing_source_reference"
                elif source.get("type") != "journal":
                    closure["outcome"] = "invalid_source_type"
                else:
                    closure["outcome"] = source["outcome"]
                closure["terminal"] = closure["outcome"] == "resolved"

        packet: dict[str, Any] = {
            "schema_version": PACKET_SCHEMA_VERSION,
            "project_id": project_id,
            "requested_ids": requested_ids,
            "entities": entities,
            "summary": self._summary(
                requested_ids,
                entities,
                duplicates_removed=duplicates_removed,
            ),
            "mode": "read_only",
        }
        if include_sources:
            packet["terminal_sources"] = {
                claim_id: terminal_sources[claim_id] for claim_id in sorted(terminal_sources)
            }
        if include_edges:
            resolved_ids = sorted(
                entity_id for entity_id, resolution in entities.items() if resolution["found"]
            )
            packet["entity_links"] = await self._fetch_entity_links(
                resolved_ids,
                project_id=project_id,
            )
            packet["claim_edges"] = await self._fetch_claim_edges(
                resolved_ids,
                project_id=project_id,
            )
        return packet

    async def _fetch_entity_rows(
        self,
        specs_by_id: Mapping[str, _EntitySpec | None],
    ) -> dict[str, dict[str, Any]]:
        grouped: dict[_EntitySpec, list[str]] = {}
        for entity_id, spec in specs_by_id.items():
            if spec is not None:
                grouped.setdefault(spec, []).append(entity_id)

        rows_by_id: dict[str, dict[str, Any]] = {}
        for spec in sorted(grouped, key=lambda item: item.entity_type):
            entity_ids = sorted(grouped[spec])
            for chunk in _chunks(entity_ids):
                placeholders = ",".join("?" for _ in chunk)
                rows = await self.db.fetchall(
                    f"SELECT * FROM {spec.table} WHERE id IN ({placeholders})",
                    chunk,
                )
                for row in rows:
                    row_id = row.get("id")
                    if isinstance(row_id, str) and row_id in specs_by_id:
                        rows_by_id[row_id] = row
        return rows_by_id

    @staticmethod
    def _stored_project_id(spec: _EntitySpec, row: Mapping[str, Any]) -> str | None:
        value = row.get("id") if spec.project_from_id else row.get("project_id")
        return value if isinstance(value, str) and value else None

    @staticmethod
    def _plan_terminal_sources(
        requested_ids: Sequence[str],
        specs_by_id: Mapping[str, _EntitySpec | None],
        rows_by_id: Mapping[str, Mapping[str, Any]],
        *,
        project_id: str,
    ) -> tuple[dict[str, dict[str, Any]], dict[str, _EntitySpec | None]]:
        closures: dict[str, dict[str, Any]] = {}
        source_specs: dict[str, _EntitySpec | None] = {}
        for entity_id in requested_ids:
            spec = specs_by_id.get(entity_id)
            if spec is None or spec.entity_type != "claim":
                continue
            row = rows_by_id.get(entity_id)
            stored_project = EntityResolverService._stored_project_id(spec, row) if row else None
            source_id = row.get("source_entry_id") if row and stored_project == project_id else None
            source_id = source_id if isinstance(source_id, str) and source_id else None
            closures[entity_id] = {
                "claim_id": entity_id,
                "source_entry_id": source_id,
                "outcome": "pending" if source_id else "claim_unresolved",
                "terminal": False,
            }
            if source_id:
                source_specs[source_id] = _ENTITY_SPECS.get(_prefix(source_id))
        return closures, source_specs

    async def _fetch_tags(
        self,
        resolved: Mapping[str, tuple[_EntitySpec, Mapping[str, Any]]],
        *,
        project_id: str,
    ) -> dict[str, list[str]]:
        if not resolved:
            return {}
        allowed_pairs = {
            (spec.entity_type, entity_id) for entity_id, (spec, _row) in resolved.items()
        }
        tags: dict[str, set[str]] = {}
        for chunk in _chunks(sorted(resolved)):
            placeholders = ",".join("?" for _ in chunk)
            rows = await self.db.fetchall(
                f"""SELECT entity_type, entity_id, tag
                    FROM tags
                    WHERE project_id = ? AND entity_id IN ({placeholders})""",
                [project_id, *chunk],
            )
            for row in rows:
                pair = (row.get("entity_type"), row.get("entity_id"))
                tag = row.get("tag")
                if pair in allowed_pairs and isinstance(tag, str) and tag:
                    tags.setdefault(pair[1], set()).add(tag)
        return {
            entity_id: sorted(values, key=lambda value: (value.casefold(), value))
            for entity_id, values in tags.items()
        }

    async def _fetch_contradicted_claims(
        self,
        claim_ids: Sequence[str],
        *,
        project_id: str,
    ) -> set[str]:
        contradicted: set[str] = set()
        for chunk in _chunks(sorted(set(claim_ids))):
            placeholders = ",".join("?" for _ in chunk)
            rows = await self.db.fetchall(
                f"""SELECT source_claim_id, target_claim_id
                    FROM claim_edges
                    WHERE project_id = ? AND relation = 'contradicts'
                      AND (
                          source_claim_id IN ({placeholders})
                          OR target_claim_id IN ({placeholders})
                      )""",
                [project_id, *chunk, *chunk],
            )
            requested = set(chunk)
            for row in rows:
                for field in ("source_claim_id", "target_claim_id"):
                    value = row.get(field)
                    if value in requested:
                        contradicted.add(value)
        return contradicted

    def _build_resolution(
        self,
        entity_id: str,
        spec: _EntitySpec | None,
        row: Mapping[str, Any] | None,
        *,
        project_id: str,
        tags: Sequence[str],
        contradicted: bool | None,
        as_of: datetime,
    ) -> dict[str, Any]:
        if spec is None:
            return self._unresolved(entity_id, "unknown", "unknown_type", None)
        if row is None:
            return self._unresolved(entity_id, spec.entity_type, "missing", None)

        stored_project = self._stored_project_id(spec, row)
        if stored_project is None:
            return self._unresolved(entity_id, spec.entity_type, "unscoped", None)
        if stored_project != project_id:
            return self._unresolved(
                entity_id,
                spec.entity_type,
                "wrong_project",
                None,
            )

        record = _normalize_record(spec, row)
        if spec.entity_type == "claim":
            record["contradicted"] = bool(contradicted)
        sorted_tags = sorted(set(tags), key=lambda value: (value.casefold(), value))
        flattened = {
            key: value for key, value in record.items() if key not in _RESERVED_PACKET_FIELDS
        }
        flattened.update(
            {
                "id": entity_id,
                "found": True,
                "outcome": "resolved",
                "type": spec.entity_type,
                "project_id": stored_project,
                "revision": _revision(spec, record, tags=sorted_tags),
                "currentness": _currentness(record, as_of=as_of),
                "tags": sorted_tags,
                "record": record,
            }
        )
        return flattened

    @staticmethod
    def _unresolved(
        entity_id: str,
        entity_type: str,
        outcome: str,
        stored_project_id: str | None,
    ) -> dict[str, Any]:
        return {
            "id": entity_id,
            "found": False,
            "outcome": outcome,
            "type": entity_type,
            "project_id": stored_project_id,
            "revision": None,
            "currentness": _unresolved_currentness(outcome),
            "tags": [],
            "record": None,
        }

    @staticmethod
    def _summary(
        requested_ids: Sequence[str],
        entities: Mapping[str, Mapping[str, Any]],
        *,
        duplicates_removed: int,
    ) -> dict[str, int]:
        outcomes: dict[str, int] = {}
        for entity_id in requested_ids:
            outcome = str(entities[entity_id]["outcome"])
            outcomes[outcome] = outcomes.get(outcome, 0) + 1
        return {
            "requested": len(requested_ids),
            "duplicates_removed": duplicates_removed,
            "resolved": outcomes.get("resolved", 0),
            "missing": outcomes.get("missing", 0),
            "wrong_project": outcomes.get("wrong_project", 0),
            "unscoped": outcomes.get("unscoped", 0),
            "unknown_type": outcomes.get("unknown_type", 0),
        }

    async def _fetch_entity_links(
        self,
        entity_ids: Sequence[str],
        *,
        project_id: str,
    ) -> list[dict[str, Any]]:
        rows_by_id: dict[str, dict[str, Any]] = {}
        for chunk in _chunks(sorted(set(entity_ids))):
            placeholders = ",".join("?" for _ in chunk)
            rows = await self.db.fetchall(
                f"""SELECT * FROM entity_links
                    WHERE project_id = ?
                      AND (
                          source_id IN ({placeholders})
                          OR target_id IN ({placeholders})
                      )""",
                [project_id, *chunk, *chunk],
            )
            for row in rows:
                rows_by_id[str(row["id"])] = _normalize_record(
                    _ENTITY_SPECS["lnk"],
                    row,
                )
        return sorted(
            rows_by_id.values(),
            key=lambda row: (
                str(row.get("source_type") or ""),
                str(row.get("source_id") or ""),
                str(row.get("link_type") or ""),
                str(row.get("target_type") or ""),
                str(row.get("target_id") or ""),
                str(row.get("id") or ""),
            ),
        )

    async def _fetch_claim_edges(
        self,
        entity_ids: Sequence[str],
        *,
        project_id: str,
    ) -> list[dict[str, Any]]:
        rows_by_id: dict[str, dict[str, Any]] = {}
        for chunk in _chunks(sorted(set(entity_ids))):
            placeholders = ",".join("?" for _ in chunk)
            rows = await self.db.fetchall(
                f"""SELECT * FROM claim_edges
                    WHERE project_id = ?
                      AND (
                          source_claim_id IN ({placeholders})
                          OR target_claim_id IN ({placeholders})
                          OR cluster_id IN ({placeholders})
                      )""",
                [project_id, *chunk, *chunk, *chunk],
            )
            for row in rows:
                rows_by_id[str(row["id"])] = _normalize_record(
                    _ENTITY_SPECS["ced"],
                    row,
                )
        return sorted(
            rows_by_id.values(),
            key=lambda row: (
                str(row.get("source_claim_id") or ""),
                str(row.get("relation") or ""),
                str(row.get("target_claim_id") or ""),
                str(row.get("cluster_id") or ""),
                str(row.get("id") or ""),
            ),
        )
