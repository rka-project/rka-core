"""Shared project-scoped lifecycle impact discovery and application."""

from rka.infra.ids import generate_id
from rka.services.base import BaseService, _precise_now
from rka.services.currentness import currentness


async def find_affected_entries(db, project_id, decision_id):
    rows = await db.fetchall(
        """SELECT j.id FROM journal j WHERE j.project_id = ? AND (
        EXISTS (SELECT 1 FROM entity_links l WHERE l.project_id = j.project_id
          AND l.source_type = 'journal' AND l.source_id = j.id
          AND l.target_type = 'decision' AND l.target_id = ?
          AND l.link_type IN ('references', 'justified_by'))
        OR EXISTS (SELECT 1 FROM json_each(CASE WHEN json_valid(j.related_decisions)
          THEN j.related_decisions ELSE '[]' END) WHERE value = ?)
        OR EXISTS (SELECT 1 FROM directive_dependencies d WHERE d.project_id = j.project_id
          AND d.directive_id = j.id AND d.decision_id = ?))""",
        [project_id, decision_id, decision_id, decision_id],
    )
    return {row["id"] for row in rows}


async def invalidate_entry_dependents(db, project_id, entry_ids, now, *, reflag=True):
    for entry_id in sorted(entry_ids):
        await db.execute(
            "UPDATE claims SET stale = 1, updated_at = ? WHERE source_entry_id = ? AND project_id = ?"
            + ("" if reflag else " AND stale = 0"),
            [now, entry_id, project_id],
        )
        await db.execute(
            """UPDATE evidence_clusters SET needs_reprocessing = 1, updated_at = ?
            WHERE project_id = ? AND id IN (SELECT ce.cluster_id FROM claim_edges ce
            JOIN claims c ON c.id = ce.source_claim_id AND c.project_id = ce.project_id
            WHERE ce.project_id = ? AND c.source_entry_id = ? AND ce.relation = 'member_of')"""
            + ("" if reflag else " AND needs_reprocessing = 0"),
            [now, project_id, project_id, entry_id],
        )


async def apply_decision_impact(db, project_id, old_id, new_id, actor, now, *, reflag=True):
    affected = await find_affected_entries(db, project_id, old_id)
    service = BaseService(db, project_id=project_id)
    explicit = await db.fetchall(
        """SELECT j.id, j.status FROM directive_dependencies d JOIN journal j
        ON j.id = d.directive_id AND j.project_id = d.project_id
        WHERE d.project_id = ? AND d.decision_id = ? AND j.type = 'directive'""",
        [project_id, old_id],
    )
    for row in explicit:
        if row["status"] != "active":
            continue
        await db.execute(
            "UPDATE journal SET status = 'superseded', updated_at = ? WHERE id = ? AND project_id = ?",
            [now, row["id"], project_id],
        )
        await service.audit(
            "update",
            "journal",
            row["id"],
            actor,
            {
                "action": "dependency_superseded",
                "before": {"status": row["status"]},
                "after": {"status": "superseded"},
                "old_decision_id": old_id,
                "new_decision_id": new_id,
            },
        )
    await invalidate_entry_dependents(db, project_id, affected, now, reflag=reflag)
    return affected


class DirectiveDependencyService(BaseService):
    async def record(self, directive_id, decision_id, declared_by, reason):
        if declared_by not in {"brain", "executor", "pi"} or not reason.strip():
            raise ValueError("declared actor and nonblank reason required")
        async with self.db.transaction():
            directive = await self.db.fetchone(
                "SELECT * FROM journal WHERE id = ? AND project_id = ?",
                [directive_id, self.project_id],
            )
            decision = await self.db.fetchone(
                "SELECT * FROM decisions WHERE id = ? AND project_id = ?",
                [decision_id, self.project_id],
            )
            if not directive or directive["type"] != "directive" or not decision:
                raise ValueError("same-project directive and decision required")
            prior = await self.db.fetchone(
                "SELECT * FROM directive_dependencies WHERE project_id = ? AND directive_id = ? AND decision_id = ?",
                [self.project_id, directive_id, decision_id],
            )
            if prior:
                if prior["reason"] != reason or prior["declared_by"] != declared_by:
                    raise ValueError("dependency already declared with different intent")
                return dict(prior)
            if not currentness(directive)["is_current"] or not currentness(decision)["is_current"]:
                raise ValueError("new dependency requires current directive and decision")
            dep_id = generate_id("link")
            await self.db.execute(
                "INSERT INTO directive_dependencies VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    dep_id,
                    self.project_id,
                    directive_id,
                    decision_id,
                    declared_by,
                    reason,
                    _precise_now(),
                ],
            )
            await self.audit(
                "create",
                "journal",
                directive_id,
                declared_by,
                {
                    "action": "directive_dependency",
                    "dependency_id": dep_id,
                    "decision_id": decision_id,
                    "reason": reason,
                },
            )
            return dict(
                await self.db.fetchone(
                    "SELECT * FROM directive_dependencies WHERE id = ? AND project_id = ?",
                    [dep_id, self.project_id],
                )
            )

    async def list(self, directive_id):
        await self._require_link_entity("journal", directive_id, project_id=self.project_id)
        return await self.db.fetchall(
            "SELECT * FROM directive_dependencies WHERE directive_id = ? AND project_id = ? ORDER BY id",
            [directive_id, self.project_id],
        )

    async def review_candidates(self):
        rows = await self.db.fetchall(
            """SELECT j.id, d.id AS decision_id FROM journal j
            JOIN decisions d ON d.project_id = j.project_id
            WHERE j.project_id = ? AND j.type = 'directive' AND j.status = 'active'
            AND (d.status IN ('superseded', 'abandoned', 'merged') OR d.superseded_by IS NOT NULL)
            AND NOT EXISTS (SELECT 1 FROM directive_dependencies x WHERE x.project_id = j.project_id AND x.directive_id = j.id AND x.decision_id = d.id)
            AND (EXISTS (SELECT 1 FROM entity_links l WHERE l.project_id = j.project_id
              AND l.source_type = 'journal' AND l.source_id = j.id AND l.target_type = 'decision' AND l.target_id = d.id
              AND l.link_type IN ('references', 'justified_by'))
              OR EXISTS (SELECT 1 FROM json_each(CASE WHEN json_valid(j.related_decisions) THEN j.related_decisions ELSE '[]' END) WHERE value = d.id))
            ORDER BY j.id, d.id LIMIT 100""",
            [self.project_id],
        )
        return {
            "count": len(rows),
            "ids": sorted({r["id"] for r in rows}),
            "candidates": rows,
            "description": "Directives cite inactive decisions without explicit lifecycle dependency; review only",
            "fix_action": "Review independence; explicitly update lifecycle or declare a dependency on a current decision",
            "fix_calls_per_item": 1,
        }
