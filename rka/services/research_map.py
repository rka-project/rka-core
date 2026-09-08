"""Research map service — composes the three-level view (v2.0)."""

from __future__ import annotations

import json

from rka.services.base import BaseService
from rka.services.currentness import review_projection
from rka.services.rendering import with_staleness_prefix


class ResearchMapService(BaseService):
    """Composes the three-level research map: RQs → Clusters → Claims."""

    async def get_research_questions(self) -> list[dict]:
        """Get all research questions (decisions with kind='research_question')."""
        rqs = await self.db.fetchall(
            """SELECT id, question, status, phase, kind, created_at
               FROM decisions
               WHERE project_id = ? AND kind = 'research_question'
               ORDER BY created_at DESC""",
            [self.project_id],
        )

        result = []
        for rq in rqs:
            # Count clusters and claims for this RQ
            stats = await self.db.fetchone(
                """SELECT
                       COUNT(*) as cluster_count,
                       COALESCE(SUM(claim_count), 0) as total_claims,
                       COALESCE(SUM(gap_count), 0) as total_gaps,
                       SUM(CASE WHEN confidence = 'contested' THEN 1 ELSE 0 END) as contradiction_count
                   FROM evidence_clusters
                   WHERE research_question_id = ? AND project_id = ?""",
                [rq["id"], self.project_id],
            )
            # Check for rq:* lifecycle tag
            rq_tag = await self.db.fetchone(
                "SELECT tag FROM tags WHERE project_id = ? AND entity_type = 'decision' AND entity_id = ? AND tag LIKE 'rq:%'",
                [self.project_id, rq["id"]],
            )
            display_status = rq_tag["tag"][3:] if rq_tag else rq["status"]
            result.append({
                "id": rq["id"],
                "question": rq["question"],
                "status": display_status,
                "phase": rq.get("phase"),
                "cluster_count": (stats["cluster_count"] or 0) if stats else 0,
                "total_claims": (stats["total_claims"] or 0) if stats else 0,
                "gap_count": (stats["total_gaps"] or 0) if stats else 0,
                "contradiction_count": (stats["contradiction_count"] or 0) if stats else 0,
                "created_at": rq.get("created_at"),
            })
        return result

    async def get_clusters_for_rq(self, rq_id: str) -> list[dict]:
        """Get evidence clusters for a specific research question."""
        clusters = await self.db.fetchall(
            """SELECT * FROM evidence_clusters
               WHERE research_question_id = ? AND project_id = ?
               ORDER BY claim_count DESC""",
            [rq_id, self.project_id],
        )

        result = []
        for c in clusters:
            # Get inter-cluster edges
            edges = await self.db.fetchall(
                """SELECT DISTINCT ce.relation, ce.target_claim_id, ce.confidence
                   FROM claim_edges ce
                   JOIN claim_edges ce2 ON ce.target_claim_id = ce2.source_claim_id AND ce.project_id = ce2.project_id
                   WHERE ce.project_id = ? AND ce.cluster_id = ? AND ce2.cluster_id != ?
                     AND ce.relation IN ('supports', 'contradicts', 'qualifies')
                   LIMIT 20""",
                [self.project_id, c["id"], c["id"]],
            )
            result.append({
                "id": c["id"],
                **review_projection(c),
                "label": c["label"],
                # Affordance B (Mission B): STALE prefix on stale synthesis.
                "synthesis": with_staleness_prefix(
                    c.get("synthesis"), c.get("needs_reprocessing")
                ),
                "confidence": c.get("confidence", "emerging"),
                "claim_count": c.get("claim_count", 0),
                "gap_count": c.get("gap_count", 0),
                "needs_reprocessing": bool(c.get("needs_reprocessing", 0)),
                "synthesized_by": c.get("synthesized_by", "llm"),
                "edges": [dict(e) for e in edges],
            })
        return result

    async def get_claims_for_cluster(self, cluster_id: str) -> list[dict]:
        """Get individual claims in a cluster with provenance.

        `scope_readiness` is derived, not stored: it comes from joining the
        claim's current scope contract and assessing it. This hand-built
        projection omitted it while the research-map UI rendered it as a
        required string, so opening any cluster threw on `undefined`.

        The scope projection, join and assessment are reused from
        ClaimsService rather than reimplemented, so readiness here means the
        same thing it means everywhere else — and keeps meaning it when the
        assessment rules change.
        """
        from rka.services.claims import ClaimService

        claims = await self.db.fetchall(
            f"""SELECT c.*, j.content as source_content, j.type as source_type,
                      j.source as source_actor,
                      {ClaimService._SCOPE_PROJECTION}
               FROM claims c
               JOIN claim_edges ce ON ce.source_claim_id = c.id AND ce.project_id = c.project_id AND ce.relation = 'member_of'
               LEFT JOIN journal j ON j.id = c.source_entry_id AND j.project_id = c.project_id
               {ClaimService._SCOPE_JOIN}
               WHERE ce.cluster_id = ? AND c.project_id = ?
               ORDER BY c.confidence DESC""",
            [cluster_id, self.project_id],
        )

        rows: list[dict] = []
        for c in claims:
            scope = ClaimService._scope_projection_to_model(c)
            readiness, _findings = ClaimService._assess_scope(c, scope)
            rows.append(
                {
                    "id": c["id"],
                    **review_projection(c),
                    "claim_type": c["claim_type"],
                    "content": c["content"],
                    "confidence": c.get("confidence", 0.5),
                    "verified": bool(c.get("verified", 0)),
                    "evidence_status": c.get("evidence_status", "unassessed"),
                    "scope_readiness": readiness,
                    "stale": bool(c.get("stale", 0)),
                    "source_entry_id": c["source_entry_id"],
                    "source_offset_start": c.get("source_offset_start"),
                    "source_offset_end": c.get("source_offset_end"),
                    "source_type": c.get("source_type"),
                    "source_actor": c.get("source_actor"),
                }
            )
        return rows

    async def get_cluster_detail(self, cluster_id: str) -> dict | None:
        """Get a cluster with claims, contradiction edges, and pending review items."""
        cluster = await self.db.fetchone(
            "SELECT * FROM evidence_clusters WHERE id = ? AND project_id = ?",
            [cluster_id, self.project_id],
        )
        if cluster is None:
            return None

        claims = await self.get_claims_for_cluster(cluster_id)
        contradiction_rows = await self.db.fetchall(
            """SELECT
                   ce.id,
                   ce.confidence,
                   ce.source_claim_id,
                   sc.content AS source_claim_content,
                   sc.source_entry_id AS source_entry_id,
                   ce.target_claim_id,
                   tc.content AS target_claim_content,
                   tc.source_entry_id AS target_source_entry_id,
                   ce.created_at
               FROM claim_edges ce
               JOIN claims sc
                 ON sc.id = ce.source_claim_id
                AND sc.project_id = ce.project_id
               LEFT JOIN claims tc
                 ON tc.id = ce.target_claim_id
                AND tc.project_id = ce.project_id
               WHERE ce.cluster_id = ?
                 AND ce.project_id = ?
                 AND ce.relation = 'contradicts'
               ORDER BY ce.confidence DESC, ce.created_at DESC""",
            [cluster_id, self.project_id],
        )
        review_rows = await self.db.fetchall(
            """SELECT *
               FROM review_queue
               WHERE item_type = 'cluster'
                 AND item_id = ?
                 AND project_id = ?
                 AND status = 'pending'
               ORDER BY priority ASC, created_at ASC""",
            [cluster_id, self.project_id],
        )

        research_question = None
        if cluster.get("research_question_id"):
            rq = await self.db.fetchone(
                """SELECT id, question
                   FROM decisions
                   WHERE id = ? AND project_id = ?""",
                [cluster["research_question_id"], self.project_id],
            )
            if rq:
                research_question = {"id": rq["id"], "question": rq["question"]}

        return {
            "id": cluster["id"],
            **review_projection(cluster),
            "research_question_id": cluster.get("research_question_id"),
            "label": cluster["label"],
            # Affordance B (Mission B): STALE prefix on stale synthesis.
            "synthesis": with_staleness_prefix(
                cluster.get("synthesis"), cluster.get("needs_reprocessing")
            ),
            "confidence": cluster.get("confidence", "emerging"),
            "claim_count": len(claims),
            "gap_count": cluster.get("gap_count", 0),
            "needs_reprocessing": bool(cluster.get("needs_reprocessing", 0)),
            "synthesized_by": cluster.get("synthesized_by", "llm"),
            "project_id": cluster.get("project_id", "proj_default"),
            "created_at": cluster.get("created_at"),
            "updated_at": cluster.get("updated_at"),
            "research_question": research_question,
            "claims": claims,
            "contradictions": [
                {
                    "id": row["id"],
                    "confidence": row.get("confidence", 0.5),
                    "source_claim_id": row["source_claim_id"],
                    "source_claim_content": row["source_claim_content"],
                    "source_entry_id": row["source_entry_id"],
                    "target_claim_id": row.get("target_claim_id"),
                    "target_claim_content": row.get("target_claim_content"),
                    "target_source_entry_id": row.get("target_source_entry_id"),
                    "created_at": row.get("created_at"),
                }
                for row in contradiction_rows
            ],
            "review_items": [self._review_row_to_dict(row) for row in review_rows],
        }

    @staticmethod
    def _review_row_to_dict(row: dict) -> dict:
        context = row.get("context")
        if context and isinstance(context, str):
            try:
                context = json.loads(context)
            except (json.JSONDecodeError, TypeError):
                pass
        return {
            "id": row["id"],
            "item_type": row["item_type"],
            "item_id": row["item_id"],
            "flag": row["flag"],
            "context": context,
            "priority": row.get("priority", 100),
            "status": row.get("status", "pending"),
            "raised_by": row.get("raised_by", "llm"),
            "resolved_by": row.get("resolved_by"),
            "resolution": row.get("resolution"),
            "project_id": row.get("project_id", "proj_default"),
            "created_at": row.get("created_at"),
            "resolved_at": row.get("resolved_at"),
        }

    async def get_full_map(self) -> dict:
        """Get the complete three-level research map."""
        rqs = await self.get_research_questions()

        # Attach top clusters to each RQ
        for rq in rqs:
            clusters = await self.db.fetchall(
                """SELECT *
                   FROM evidence_clusters
                   WHERE research_question_id = ? AND project_id = ?
                   ORDER BY claim_count DESC""",
                [rq["id"], self.project_id],
            )
            rq["clusters"] = [
                {
                    "id": c["id"],
                    **review_projection(c),
                    "label": c["label"],
                    "confidence": c.get("confidence", "emerging"),
                    "claim_count": c.get("claim_count", 0),
                    "staleness": c.get("staleness", "green"),
                }
                for c in clusters
            ]

        # Unassigned clusters (no RQ)
        unassigned = await self.db.fetchall(
            """SELECT * FROM evidence_clusters
               WHERE research_question_id IS NULL AND project_id = ?
               ORDER BY claim_count DESC""",
            [self.project_id],
        )

        # Summary stats
        total_claims = await self.db.fetchone(
            "SELECT COUNT(*) as cnt FROM claims WHERE project_id = ? AND stale = 0",
            [self.project_id],
        )
        total_clusters = await self.db.fetchone(
            "SELECT COUNT(*) as cnt FROM evidence_clusters WHERE project_id = ?",
            [self.project_id],
        )
        pending_review = await self.db.fetchone(
            "SELECT COUNT(*) as cnt FROM review_queue WHERE project_id = ? AND status = 'pending'",
            [self.project_id],
        )

        return {
            "research_questions": rqs,
            "unassigned_clusters": [
                {
                    "id": c["id"],
                    **review_projection(c),
                    "label": c["label"],
                    "claim_count": c.get("claim_count", 0),
                    "confidence": c.get("confidence", "emerging"),
                }
                for c in unassigned
            ],
            "summary": {
                "total_rqs": len(rqs),
                "total_clusters": total_clusters["cnt"] if total_clusters else 0,
                "total_claims": total_claims["cnt"] if total_claims else 0,
                "total_gaps": sum(rq.get("gap_count", 0) for rq in rqs),
                "total_contradictions": sum(rq.get("contradiction_count", 0) for rq in rqs),
                "pending_review": pending_review["cnt"] if pending_review else 0,
            },
        }
