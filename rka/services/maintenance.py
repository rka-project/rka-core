"""Maintenance manifest service — pure SQL gap detection for knowledge base hygiene."""

from __future__ import annotations

from rka.services.currentness import pending_review_sql

import logging
from typing import Any

from rka.infra.database import Database
from rka.services.base import BaseService

logger = logging.getLogger(__name__)


class MaintenanceService(BaseService):
    """Detects provenance gaps, orphaned entities, and missing enrichments via SQL queries.

    No LLM required — all checks are structural queries against the database.
    """

    def __init__(self, db: Database, project_id: str = "proj_default"):
        super().__init__(db, project_id=project_id)

    async def get_backlog_summary(self) -> dict[str, Any]:
        """Lightweight COUNT-only summary of the maintenance backlog.

        Returns {total_items, top_categories: [{name, count}, ...]} sorted by
        count descending, top 3. Designed to be sub-100ms so it can be
        appended to high-traffic responses (rka_get_status, rka_search).
        Per dec_01KQQPER3XSSBACGZANFJCVQ66 / dec_01KQQRZRY9NN0PYQNXPW07D732.
        """
        pid = self.project_id

        # Each query is a COUNT(*) over an indexed table. Mirrors the WHERE
        # predicates of the corresponding _<category> method above; if those
        # change, change these in lockstep.
        counts: dict[str, int] = {}

        async def _count(name: str, sql: str, params: list[Any]) -> None:
            row = await self.db.fetchone(sql, params)
            counts[name] = int(row["c"]) if row else 0

        await _count(
            "entries_without_tags",
            """SELECT COUNT(*) AS c FROM journal j
               WHERE j.project_id = ? AND j.status != 'retracted'
                 AND NOT EXISTS (
                     SELECT 1 FROM tags t
                     WHERE t.entity_type = 'journal' AND t.entity_id = j.id AND t.project_id = ?
                 )""",
            [pid, pid],
        )
        await _count(
            "entries_without_claims",
            """SELECT COUNT(*) AS c FROM journal j
               WHERE j.project_id = ?
                 AND j.type IN ('note', 'finding', 'insight', 'methodology', 'observation')
                 AND j.status != 'retracted'
                 AND NOT EXISTS (
                     SELECT 1 FROM claims c
                     WHERE c.source_entry_id = j.id AND c.project_id = ?
                 )""",
            [pid, pid],
        )
        await _count(
            "clusters_needing_synthesis",
            """SELECT COUNT(*) AS c FROM evidence_clusters ec
               WHERE ec.project_id = ? AND ec.claim_count > 0
                 AND (ec.staleness_verdict IS NULL OR ec.staleness_verdict IN ('current', 'dismissed'))
                 AND ((ec.synthesis IS NULL OR ec.synthesis = '') OR ec.needs_reprocessing = 1)""",
            [pid],
        )
        await _count(
            "flagged_contradictions",
            """SELECT COUNT(*) AS c FROM review_queue rq
               WHERE rq.project_id = ? AND rq.flag = 'potential_contradiction' AND rq.status = 'pending'""",
            [pid],
        )
        await _count(
            "entries_missing_cross_refs",
            """SELECT COUNT(*) AS c FROM journal j
               WHERE j.project_id = ? AND j.status != 'retracted'
                 AND (j.related_decisions IS NULL OR j.related_decisions = '[]' OR j.related_decisions = 'null')
                 AND NOT EXISTS (SELECT 1 FROM entity_links el WHERE el.source_id = j.id AND el.project_id = ?)
                 AND NOT EXISTS (SELECT 1 FROM entity_links el WHERE el.target_id = j.id AND el.project_id = ?)""",
            [pid, pid, pid],
        )
        await _count(
            "decisions_without_justified_by",
            """SELECT COUNT(*) AS c FROM decisions d
               WHERE d.project_id = ? AND d.status = 'active'
                 AND NOT EXISTS (
                     SELECT 1 FROM entity_links el
                     WHERE el.source_type = 'decision' AND el.source_id = d.id
                       AND el.link_type = 'justified_by' AND el.project_id = ?
                 )""",
            [pid, pid],
        )
        await _count(
            "missions_without_motivated_by",
            # Affordance F (Mission B): exclude missions tagged
            # 'motivated-by-explained' — kept in lockstep with
            # _missions_without_motivated_by above.
            """SELECT COUNT(*) AS c FROM missions m
               WHERE m.project_id = ? AND m.status NOT IN ('cancelled')
                 AND NOT EXISTS (
                     SELECT 1 FROM entity_links el
                     WHERE el.target_type = 'mission' AND el.target_id = m.id
                       AND el.link_type = 'motivated' AND el.project_id = ?
                 )
                 AND NOT EXISTS (
                     SELECT 1 FROM tags t
                     WHERE t.entity_type = 'mission' AND t.entity_id = m.id
                       AND t.tag = 'motivated-by-explained' AND t.project_id = ?
                 )""",
            [pid, pid, pid],
        )
        # Affordance A: parent-chain walk requires Python-side traversal —
        # delegate to the full method, then count its result. Counts are
        # capped to _GATE_AUDIT_LIMIT (10) by construction; that's the
        # intended advisory-cap behavior for the summary.
        gate_audit = await self._missions_without_upstream_gate(pid)
        counts["missions_without_upstream_gate"] = gate_audit["count"]
        await _count(
            "unassigned_clusters",
            """SELECT COUNT(*) AS c FROM evidence_clusters ec
               WHERE ec.project_id = ? AND ec.claim_count > 0
                 AND (ec.research_question_id IS NULL OR ec.research_question_id = '')""",
            [pid],
        )
        await _count(
            "stale_claims",
            f"SELECT COUNT(*) AS c FROM claims WHERE project_id = ? AND {pending_review_sql('claim')}",
            [pid],
        )
        await _count(
            "stale_clusters",
            f"SELECT COUNT(*) AS c FROM evidence_clusters WHERE project_id = ? AND {pending_review_sql('cluster')}",
            [pid],
        )

        from rka.services.lifecycle import DirectiveDependencyService
        counts["directive_dependency_gaps"] = (await DirectiveDependencyService(self.db, project_id=pid).review_candidates())["count"]
        total = sum(counts.values())
        top = sorted(
            [{"name": k, "count": v} for k, v in counts.items() if v > 0],
            key=lambda d: d["count"],
            reverse=True,
        )[:3]
        return {"total_items": total, "top_categories": top}

    async def get_pending_maintenance(self) -> dict[str, Any]:
        """Run all gap-detection queries and return a compact manifest."""
        pid = self.project_id

        entries_without_tags = await self._entries_without_tags(pid)
        entries_without_claims = await self._entries_without_claims(pid)
        clusters_needing_synthesis = await self._clusters_needing_synthesis(pid)
        flagged_contradictions = await self._flagged_contradictions(pid)
        entries_missing_cross_refs = await self._entries_missing_cross_refs(pid)
        decisions_without_justified_by = await self._decisions_without_justified_by(pid)
        missions_without_motivated_by = await self._missions_without_motivated_by(pid)
        missions_without_upstream_gate = await self._missions_without_upstream_gate(pid)
        unassigned_clusters = await self._unassigned_clusters(pid)
        stale_claims = await self._stale_claims(pid)
        stale_clusters = await self._stale_clusters(pid)

        from rka.services.lifecycle import DirectiveDependencyService
        directive_dependencies = await DirectiveDependencyService(self.db, project_id=pid).review_candidates()
        categories = {
            "directive_dependency_gaps": directive_dependencies,
            "entries_without_tags": entries_without_tags,
            "entries_without_claims": entries_without_claims,
            "clusters_needing_synthesis": clusters_needing_synthesis,
            "flagged_contradictions": flagged_contradictions,
            "entries_missing_cross_refs": entries_missing_cross_refs,
            "decisions_without_justified_by": decisions_without_justified_by,
            "missions_without_motivated_by": missions_without_motivated_by,
            "missions_without_upstream_gate": missions_without_upstream_gate,
            "unassigned_clusters": unassigned_clusters,
            "stale_claims": stale_claims,
            "stale_clusters": stale_clusters,
        }

        total_items = sum(len(c["ids"]) for c in categories.values())
        # Estimate: ~1 tool call per item to fix
        estimated_tool_calls = sum(c["fix_calls_per_item"] * len(c["ids"]) for c in categories.values())

        return {
            "total_items": total_items,
            "estimated_tool_calls": estimated_tool_calls,
            "categories": categories,
        }

    # ---- Individual gap-detection queries ----

    async def _entries_without_tags(self, pid: str) -> dict:
        rows = await self.db.fetchall(
            """SELECT j.id FROM journal j
               WHERE j.project_id = ?
                 AND j.status != 'retracted'
                 AND NOT EXISTS (
                     SELECT 1 FROM tags t
                     WHERE t.entity_type = 'journal' AND t.entity_id = j.id AND t.project_id = ?
                 )
               ORDER BY j.created_at DESC LIMIT 50""",
            [pid, pid],
        )
        return {
            "count": len(rows),
            "ids": [r["id"] for r in rows],
            "description": "Journal entries with no tags",
            "fix_action": "rka_update_note(id, tags=[...])",
            "fix_calls_per_item": 1,
        }

    async def _entries_without_claims(self, pid: str) -> dict:
        rows = await self.db.fetchall(
            """SELECT j.id FROM journal j
               WHERE j.project_id = ?
                 AND j.type IN ('note', 'finding', 'insight', 'methodology', 'observation')
                 AND j.status != 'retracted'
                 AND NOT EXISTS (
                     SELECT 1 FROM claims c
                     WHERE c.source_entry_id = j.id AND c.project_id = ?
                 )
               ORDER BY j.created_at DESC LIMIT 50""",
            [pid, pid],
        )
        return {
            "count": len(rows),
            "ids": [r["id"] for r in rows],
            "description": "Substantive entries with no claims extracted",
            "fix_action": "Brain reads entry and manually extracts claims via rka_add_note or review",
            "fix_calls_per_item": 2,
        }

    async def _clusters_needing_synthesis(self, pid: str) -> dict:
        # Surfaces both: (a) clusters with claims but no synthesis yet, and
        # (b) clusters whose existing synthesis was invalidated by a downstream
        # event (e.g. a contradicts edge inserted between cluster members; see
        # ClaimService.create_edge / dec_01KQQPE47H56E40A8KBDDT4BZT).
        rows = await self.db.fetchall(
            """SELECT ec.id FROM evidence_clusters ec
               WHERE ec.project_id = ?
                 AND ec.claim_count > 0
                 AND (ec.staleness_verdict IS NULL OR ec.staleness_verdict IN ('current', 'dismissed'))
                 AND (
                     (ec.synthesis IS NULL OR ec.synthesis = '')
                     OR ec.needs_reprocessing = 1
                 )
               ORDER BY ec.needs_reprocessing DESC, ec.claim_count DESC LIMIT 50""",
            [pid],
        )
        return {
            "count": len(rows),
            "ids": [r["id"] for r in rows],
            "description": "Evidence clusters with claims but no synthesis, or whose synthesis is stale",
            "fix_action": "rka_review_cluster(cluster_id, confidence, synthesis)",
            "fix_calls_per_item": 1,
        }

    async def _flagged_contradictions(self, pid: str) -> dict:
        rows = await self.db.fetchall(
            """SELECT rq.id, rq.item_id FROM review_queue rq
               WHERE rq.project_id = ?
                 AND rq.flag = 'potential_contradiction'
                 AND rq.status = 'pending'
               ORDER BY rq.priority DESC LIMIT 50""",
            [pid],
        )
        return {
            "count": len(rows),
            "ids": [r["id"] for r in rows],
            "description": "Pending contradiction flags in review queue",
            "fix_action": "rka_resolve_contradiction(cluster_id, resolution)",
            "fix_calls_per_item": 1,
        }

    async def _entries_missing_cross_refs(self, pid: str) -> dict:
        rows = await self.db.fetchall(
            """SELECT j.id FROM journal j
               WHERE j.project_id = ?
                 AND j.status != 'retracted'
                 AND (j.related_decisions IS NULL OR j.related_decisions = '[]' OR j.related_decisions = 'null')
                 AND NOT EXISTS (
                     SELECT 1 FROM entity_links el
                     WHERE el.source_id = j.id AND el.project_id = ?
                 )
                 AND NOT EXISTS (
                     SELECT 1 FROM entity_links el
                     WHERE el.target_id = j.id AND el.project_id = ?
                 )
               ORDER BY j.created_at DESC LIMIT 50""",
            [pid, pid, pid],
        )
        return {
            "count": len(rows),
            "ids": [r["id"] for r in rows],
            "description": "Journal entries with no cross-references (no related_decisions and no entity_links)",
            "fix_action": "rka_update_note(id, related_decisions=[...]) or Brain adds links",
            "fix_calls_per_item": 1,
        }

    async def _decisions_without_justified_by(self, pid: str) -> dict:
        rows = await self.db.fetchall(
            """SELECT d.id FROM decisions d
               WHERE d.project_id = ?
                 AND d.status = 'active'
                 AND NOT EXISTS (
                     SELECT 1 FROM entity_links el
                     WHERE el.source_type = 'decision' AND el.source_id = d.id
                       AND el.link_type = 'justified_by' AND el.project_id = ?
                 )
               ORDER BY d.created_at DESC LIMIT 50""",
            [pid, pid],
        )
        return {
            "count": len(rows),
            "ids": [r["id"] for r in rows],
            "description": "Active decisions with no justified_by links",
            "fix_action": "Brain adds related_journal when updating decision or adds entity_links",
            "fix_calls_per_item": 1,
        }

    async def _missions_without_motivated_by(self, pid: str) -> dict:
        # Affordance F (Mission B / mis_01KR209WY4M6WQFEXRH79KC2ZF):
        # explained-gap suppression. A mission tagged 'motivated-by-explained'
        # opts out of this advisory category — used for missions whose
        # missing motivated_by_decision is documented (e.g., Bug A which
        # PI-directed a deliberate unlinked mission, or orphan-FK missions
        # whose motivating decision was lost in pack import).
        rows = await self.db.fetchall(
            """SELECT m.id FROM missions m
               WHERE m.project_id = ?
                 AND m.status NOT IN ('cancelled')
                 AND NOT EXISTS (
                     SELECT 1 FROM entity_links el
                     WHERE el.target_type = 'mission' AND el.target_id = m.id
                       AND el.link_type = 'motivated' AND el.project_id = ?
                 )
                 AND NOT EXISTS (
                     SELECT 1 FROM tags t
                     WHERE t.entity_type = 'mission' AND t.entity_id = m.id
                       AND t.tag = 'motivated-by-explained' AND t.project_id = ?
                 )
               ORDER BY m.created_at DESC LIMIT 50""",
            [pid, pid, pid],
        )
        return {
            "count": len(rows),
            "ids": [r["id"] for r in rows],
            "description": "Missions without motivated_by_decision links (excluding tagged 'motivated-by-explained')",
            "fix_action": "Add motivated_by_decision via rka_update_mission, OR tag 'motivated-by-explained' if the gap is intentional",
            "fix_calls_per_item": 1,
        }

    async def _unassigned_clusters(self, pid: str) -> dict:
        rows = await self.db.fetchall(
            """SELECT ec.id FROM evidence_clusters ec
               WHERE ec.project_id = ?
                 AND (ec.research_question_id IS NULL OR ec.research_question_id = '')
                 AND ec.claim_count > 0
               ORDER BY ec.claim_count DESC LIMIT 50""",
            [pid],
        )
        return {
            "count": len(rows),
            "ids": [r["id"] for r in rows],
            "description": "Evidence clusters not assigned to any research question",
            "fix_action": "rka_review_cluster(cluster_id, ..., research_question_id=dec_id)",
            "fix_calls_per_item": 1,
        }

    async def _stale_claims(self, pid: str) -> dict:
        rows = await self.db.fetchall(
            f"""SELECT id FROM claims
               WHERE project_id = ? AND {pending_review_sql('claim')}
               ORDER BY updated_at DESC LIMIT 50""",
            [pid],
        )
        return {
            "count": len(rows),
            "ids": [r["id"] for r in rows],
            "description": "Claims flagged stale needing review",
            "fix_action": "Brain reviews claim, updates or resolves staleness",
            "fix_calls_per_item": 1,
        }

    async def _stale_clusters(self, pid: str) -> dict:
        rows = await self.db.fetchall(
            f"""SELECT id FROM evidence_clusters
               WHERE project_id = ? AND {pending_review_sql('cluster')}
               ORDER BY updated_at DESC LIMIT 50""",
            [pid],
        )
        return {
            "count": len(rows),
            "ids": [r["id"] for r in rows],
            "description": "Clusters with stale evidence needing re-synthesis",
            "fix_action": "Brain re-reviews cluster with fresh evidence",
            "fix_calls_per_item": 1,
        }

    # Affordance A (Mission B / mis_01KR209WY4M6WQFEXRH79KC2ZF) — gate-invariant
    # audit. Replaces the rejected "first-class gate schema" recommendation
    # from the v2.3.3 revision report at a fraction of the cost: mission's
    # motivated_by_decision parent chain (decision.parent_id) is walked to
    # find an ancestor decision tagged 'gate'. If none found, the mission is
    # flagged. Surfaces in the manifest as advisory; lets PI/Brain decide
    # whether write-time enforcement is worth pursuing later.
    _GATE_AUDIT_LIMIT = 10
    _GATE_AUDIT_MAX_DEPTH = 16  # Cycle / pathological-depth guard.

    async def _missions_without_upstream_gate(self, pid: str) -> dict:
        # Step 1: candidate missions — those with motivated_by_decision set
        # AND not cancelled. A mission with no motivated_by_decision is
        # already covered by _missions_without_motivated_by.
        candidates = await self.db.fetchall(
            """SELECT m.id, m.motivated_by_decision
               FROM missions m
               WHERE m.project_id = ?
                 AND m.status NOT IN ('cancelled')
                 AND m.motivated_by_decision IS NOT NULL
               ORDER BY m.created_at DESC""",
            [pid],
        )
        if not candidates:
            return {
                "count": 0, "ids": [],
                "description": "Missions whose motivated_by_decision parent chain has no 'gate'-tagged ancestor",
                "fix_action": "Tag the relevant decision with 'gate' or revisit motivated_by_decision",
                "fix_calls_per_item": 1,
            }

        # Step 2: gate-tagged decisions in this project (single fetch).
        gate_rows = await self.db.fetchall(
            """SELECT t.entity_id FROM tags t
               WHERE t.entity_type = 'decision'
                 AND t.tag = 'gate'
                 AND t.project_id = ?""",
            [pid],
        )
        gate_set = {r["entity_id"] for r in gate_rows}

        # Step 3: parent_id map for all decisions in this project (one query;
        # avoids per-mission walk hitting the DB N times).
        parent_rows = await self.db.fetchall(
            """SELECT id, parent_id FROM decisions
               WHERE project_id = ? AND parent_id IS NOT NULL""",
            [pid],
        )
        parent_map: dict[str, str] = {r["id"]: r["parent_id"] for r in parent_rows}

        # Step 4: per-mission walk from motivated_by_decision up the parent
        # chain. Visited-set + max-depth guard against cycles or pathological
        # depths.
        flagged: list[str] = []
        for cand in candidates:
            cur = cand["motivated_by_decision"]
            visited: set[str] = set()
            found_gate = False
            depth = 0
            while cur is not None and depth < self._GATE_AUDIT_MAX_DEPTH:
                if cur in visited:
                    break  # cycle guard
                visited.add(cur)
                if cur in gate_set:
                    found_gate = True
                    break
                cur = parent_map.get(cur)
                depth += 1
            if not found_gate:
                flagged.append(cand["id"])
                if len(flagged) >= self._GATE_AUDIT_LIMIT:
                    break

        return {
            "count": len(flagged),
            "ids": flagged,
            "description": (
                "Missions whose motivated_by_decision parent chain has no "
                "'gate'-tagged ancestor decision (advisory; manifest cap "
                f"= {self._GATE_AUDIT_LIMIT})"
            ),
            "fix_action": "Tag the relevant decision with 'gate' or revisit motivated_by_decision",
            "fix_calls_per_item": 1,
        }

    # ------------------------------------------------------------------
    # Research-health metrics (eval-v3 theme D, 2026-06-12)
    # ------------------------------------------------------------------

    async def research_health(self) -> dict[str, Any]:
        """The paper's section-7.1 instruments, computed live.

        Provenance coverage, research-debt trajectory (weekly), mission-cycle
        stats, and the bookkeeping-overhead share of recorded actions. Pure
        SQL over existing tables; descriptive, not gating.
        """
        pid = self.project_id

        async def _one(sql: str, params: list[Any]) -> int:
            row = await self.db.fetchone(sql, params)
            return int(list(dict(row).values())[0] or 0) if row else 0

        # --- provenance coverage ---
        dec_total = await _one(
            "SELECT COUNT(*) FROM decisions WHERE project_id = ? AND status != 'superseded'",
            [pid])
        dec_justified = await _one(
            """SELECT COUNT(DISTINCT d.id) FROM decisions d
               WHERE d.project_id = ? AND d.status != 'superseded' AND (
                 (d.related_journal IS NOT NULL AND d.related_journal NOT IN ('[]',''))
                 OR EXISTS (SELECT 1 FROM entity_links el WHERE el.project_id = d.project_id
                            AND el.source_id = d.id AND el.link_type = 'justified_by'))""",
            [pid])
        mis_total = await _one(
            "SELECT COUNT(*) FROM missions WHERE project_id = ?", [pid])
        mis_motivated = await _one(
            """SELECT COUNT(*) FROM missions m WHERE m.project_id = ?
               AND (m.motivated_by_decision IS NOT NULL
                    OR EXISTS (SELECT 1 FROM entity_links el WHERE el.project_id = m.project_id
                               AND el.target_id = m.id AND el.link_type = 'motivated'))""",
            [pid])
        clm_total = await _one("SELECT COUNT(*) FROM claims WHERE project_id = ?", [pid])
        clm_sourced = await _one(
            "SELECT COUNT(*) FROM claims WHERE project_id = ? AND source_entry_id IS NOT NULL",
            [pid])
        sup_decisions = await _one(
            "SELECT COUNT(*) FROM decisions WHERE project_id = ? AND status = 'superseded'",
            [pid])
        sup_orphans = await _one(
            """SELECT COUNT(*) FROM decisions WHERE project_id = ?
               AND status = 'superseded' AND superseded_by IS NULL""",
            [pid])

        # --- research-debt trajectory: per ISO week, decisions created vs covered ---
        weekly = await self.db.fetchall(
            """SELECT strftime('%Y-W%W', created_at) AS week,
                      COUNT(*) AS created,
                      SUM(CASE WHEN (related_journal IS NOT NULL
                                     AND related_journal NOT IN ('[]','')) THEN 1 ELSE 0 END)
                          AS covered
               FROM decisions WHERE project_id = ?
               GROUP BY week ORDER BY week DESC LIMIT 26""",
            [pid])

        # --- mission-cycle metrics ---
        cycle = await self.db.fetchone(
            """SELECT COUNT(*) AS completed,
                      AVG(julianday(completed_at) - julianday(created_at)) AS avg_days,
                      MAX(julianday(completed_at) - julianday(created_at)) AS max_days
               FROM missions WHERE project_id = ? AND status = 'complete'
                 AND completed_at IS NOT NULL""",
            [pid])
        chk = await self.db.fetchone(
            """SELECT COUNT(*) AS total,
                      SUM(CASE WHEN status = 'open' THEN 1 ELSE 0 END) AS open
               FROM checkpoints WHERE project_id = ?""",
            [pid])

        # --- bookkeeping overhead: write/read mix of recorded actions ---
        audit_mix = await self.db.fetchall(
            "SELECT action, COUNT(*) AS n FROM audit_log WHERE project_id = ? GROUP BY action",
            [pid])
        mix = {r["action"]: r["n"] for r in audit_mix}
        writes = sum(v for k, v in mix.items() if k in ("create", "update", "delete"))
        total_actions = sum(mix.values()) or 1

        def _pct(n: int, d: int) -> float:
            return round(100.0 * n / d, 1) if d else 0.0

        return {
            "provenance_coverage": {
                "decisions_with_evidence_pct": _pct(dec_justified, dec_total),
                "decisions": {"covered": dec_justified, "total": dec_total},
                "missions_with_motivation_pct": _pct(mis_motivated, mis_total),
                "missions": {"covered": mis_motivated, "total": mis_total},
                "claims_with_source_pct": _pct(clm_sourced, clm_total),
                "claims": {"covered": clm_sourced, "total": clm_total},
                "supersede_chain_integrity": {
                    "superseded_decisions": sup_decisions,
                    "orphaned_pointers": sup_orphans,
                },
            },
            "research_debt_trajectory_weekly": [dict(r) for r in weekly],
            "mission_cycle": {
                "completed": (cycle["completed"] if cycle else 0) or 0,
                "avg_days_to_complete": round(cycle["avg_days"], 2)
                    if cycle and cycle["avg_days"] else None,
                "max_days_to_complete": round(cycle["max_days"], 2)
                    if cycle and cycle["max_days"] else None,
                "checkpoints_total": (chk["total"] if chk else 0) or 0,
                "checkpoints_open": (chk["open"] if chk else 0) or 0,
            },
            "bookkeeping_overhead": {
                "recorded_actions": dict(mix),
                "write_share_pct": _pct(writes, total_actions),
            },
        }
