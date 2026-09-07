"""Entity-link repair and the enqueue-only legacy embedding adapter."""

from __future__ import annotations

import json
import logging

from rka.infra.database import Database
from rka.infra.embeddings import EmbeddingService
from rka.infra.ids import generate_id

logger = logging.getLogger(__name__)


async def backfill_entity_links(db: Database) -> dict[str, int]:
    """Scan all existing entities and create entity_links rows from their
    related_* JSON arrays. Idempotent — uses INSERT OR IGNORE.

    Returns counts of links created per source type.
    """
    counts: dict[str, int] = {
        "journal": 0,
        "decision": 0,
        "mission": 0,
    }

    # 1. Journal entries → decisions, literature, missions
    rows = await db.fetchall(
        "SELECT id, source, related_decisions, related_literature, related_mission, project_id FROM journal"
    )
    for r in rows:
        created_by = r.get("source") or "system"

        for dec_id in _parse_json_list(r.get("related_decisions")):
            if await _insert_link(db, "journal", r["id"], "references", "decision", dec_id, created_by, r.get("project_id")):
                counts["journal"] += 1

        for lit_id in _parse_json_list(r.get("related_literature")):
            if await _insert_link(db, "journal", r["id"], "cites", "literature", lit_id, created_by, r.get("project_id")):
                counts["journal"] += 1

        if r.get("related_mission"):
            if await _insert_link(db, "mission", r["related_mission"], "produced", "journal", r["id"], created_by, r.get("project_id")):
                counts["journal"] += 1

    # 2. Decisions → missions, literature
    rows = await db.fetchall(
        "SELECT id, decided_by, related_missions, related_literature, project_id FROM decisions"
    )
    for r in rows:
        created_by = r.get("decided_by") or "system"

        for mis_id in _parse_json_list(r.get("related_missions")):
            if await _insert_link(db, "decision", r["id"], "triggered", "mission", mis_id, created_by, r.get("project_id")):
                counts["decision"] += 1

        for lit_id in _parse_json_list(r.get("related_literature")):
            if await _insert_link(db, "decision", r["id"], "cites", "literature", lit_id, created_by, r.get("project_id")):
                counts["decision"] += 1

    # 3. Decisions parent-child → entity_links
    rows = await db.fetchall(
        "SELECT id, parent_id, decided_by, project_id FROM decisions WHERE parent_id IS NOT NULL"
    )
    for r in rows:
        if await _insert_link(db, "decision", r["parent_id"], "triggered", "decision", r["id"], r.get("decided_by") or "system", r.get("project_id")):
            counts["decision"] += 1

    # 4. Missions depends_on
    rows = await db.fetchall(
        "SELECT id, depends_on, project_id FROM missions WHERE depends_on IS NOT NULL"
    )
    for r in rows:
        if await _insert_link(db, "mission", r["depends_on"], "triggered", "mission", r["id"], "system", r.get("project_id")):
            counts["mission"] += 1

    # 5. Checkpoints → decisions
    rows = await db.fetchall(
        "SELECT id, mission_id, linked_decision_id, project_id FROM checkpoints WHERE linked_decision_id IS NOT NULL"
    )
    for r in rows:
        if await _insert_link(db, "checkpoint", r["id"], "resolved_as", "decision", r["linked_decision_id"], "system", r.get("project_id")):
            counts.setdefault("checkpoint", 0)
            counts["checkpoint"] += 1

    # 6. Journal supersedes
    rows = await db.fetchall(
        "SELECT id, supersedes, source, project_id FROM journal WHERE supersedes IS NOT NULL"
    )
    for r in rows:
        if await _insert_link(db, "journal", r["id"], "supersedes", "journal", r["supersedes"], r.get("source") or "system", r.get("project_id")):
            counts["journal"] += 1

    await db.commit()
    logger.info("Backfill complete: %s", counts)
    return counts


async def backfill_embeddings(
    db: Database,
    embeddings: EmbeddingService,
    project_id: str = "proj_default",
    batch_size: int = 50,
    *,
    include_artifacts: bool = True,
    include_figures: bool = True,
    include_claims: bool = True,
    force: bool = False,
) -> dict:
    """Compatibility enqueue adapter; return a job, never completion counts.

    The caller must supply a service bound to the persisted generation. The
    worker, not this legacy entry point, performs all model/vector work.
    """
    from rka.services.embedding_jobs import EmbeddingJobs

    types = [name for name, enabled in (
        ("artifact", include_artifacts), ("figure", include_figures), ("claim", include_claims),
    ) if enabled]
    return await EmbeddingJobs(db).request(
        embeddings, types, project_id=project_id, batch_size=batch_size, force=force,
        check_hashes=True,
    )


def _parse_json_list(val) -> list[str]:
    """Parse a JSON array string into a list of strings."""
    if not val:
        return []
    if isinstance(val, list):
        return val
    try:
        parsed = json.loads(val)
        return parsed if isinstance(parsed, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


async def _insert_link(
    db: Database,
    source_type: str,
    source_id: str,
    link_type: str,
    target_type: str,
    target_id: str,
    created_by: str,
    project_id: str | None,
) -> bool:
    """Insert a link, returning True if actually inserted (not duplicate)."""
    link_id = generate_id("link")
    try:
        await db.execute(
            """INSERT OR IGNORE INTO entity_links
               (id, source_type, source_id, link_type, target_type, target_id, created_by, project_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            [link_id, source_type, source_id, link_type, target_type, target_id, created_by, project_id or "proj_default"],
        )
        return True
    except Exception as exc:
        logger.debug("Link insert failed: %s", exc)
        return False
