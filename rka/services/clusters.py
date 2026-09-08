"""Evidence cluster management and synthesis service (v2.0)."""

from __future__ import annotations

from rka.services.currentness import currentness, review_projection

import logging

from rka.infra.ids import generate_id
from rka.models.claim import (
    EvidenceCluster, EvidenceClusterCreate, EvidenceClusterUpdate,
    ClaimEdgeCreate,
)
from rka.services.base import BaseService, _now
from rka.services.rendering import with_staleness_prefix

logger = logging.getLogger(__name__)


class ClusterNotFoundError(ValueError):
    """Raised when a cluster is unavailable in the active project."""


class ClusterService(BaseService):
    """Manages evidence clusters, LLM clustering, and theme synthesis."""

    # ── CRUD ─────────────────────────────────────────────────

    async def create(self, data: EvidenceClusterCreate) -> EvidenceCluster:
        cluster_id = generate_id("cluster")
        async with self.db.transaction():
            if data.research_question_id:
                await self._validate_research_question(
                    data.research_question_id
                )
            await self.db.execute(
                """INSERT INTO evidence_clusters
                   (id, research_question_id, label, synthesis, confidence, project_id)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                [
                    cluster_id, data.research_question_id, data.label,
                    data.synthesis, data.confidence, self.project_id,
                ],
            )
            await self.db.commit()
            await self._sync_fts("cluster", cluster_id, {
                "label": data.label, "synthesis": data.synthesis or "",
            })
            # Mirror the FK as an entity_link so graph traversal sees it.
            # Migration 023 backfilled the existing rows; this hook keeps parity
            # going forward.
            if data.research_question_id:
                await self.add_link(
                    "cluster", cluster_id,
                    "answers",
                    "decision", data.research_question_id,
                )
            await self.audit("create", "cluster", cluster_id, "llm")
        return await self.get(cluster_id)

    async def get(self, cluster_id: str) -> EvidenceCluster | None:
        row = await self.db.fetchone(
            "SELECT * FROM evidence_clusters WHERE id = ? AND project_id = ?",
            [cluster_id, self.project_id],
        )
        if row is None:
            return None
        return self._row_to_model(row)

    async def list(
        self,
        research_question_id: str | None = None,
        confidence: str | None = None,
        needs_reprocessing: bool | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[EvidenceCluster]:
        conditions = ["project_id = ?"]
        params: list = [self.project_id]

        if research_question_id:
            conditions.append("research_question_id = ?")
            params.append(research_question_id)
        if confidence:
            conditions.append("confidence = ?")
            params.append(confidence)
        if needs_reprocessing is not None:
            conditions.append("needs_reprocessing = ?")
            params.append(int(needs_reprocessing))

        where = " AND ".join(conditions)
        params.extend([limit, offset])

        rows = await self.db.fetchall(
            f"SELECT * FROM evidence_clusters WHERE {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            params,
        )
        return [self._row_to_model(row) for row in rows]

    async def update(self, cluster_id: str, data: EvidenceClusterUpdate) -> EvidenceCluster:
        rq_assignment_supplied = "research_question_id" in data.model_fields_set
        dump = data.model_dump(exclude_none=True)
        if rq_assignment_supplied:
            # Explicit null clears both the FK and its graph projection.
            dump["research_question_id"] = data.research_question_id
        if not dump:
            current = await self.get(cluster_id)
            if current is None:
                raise ClusterNotFoundError(
                    f"cluster {cluster_id!r} not found in project "
                    f"{self.project_id}"
                )
            return current

        if "needs_reprocessing" in dump:
            dump["needs_reprocessing"] = int(dump["needs_reprocessing"])

        dump["updated_at"] = _now()
        set_clause = ", ".join(f"{k} = ?" for k in dump)
        values = list(dump.values()) + [cluster_id, self.project_id]

        async with self.db.transaction():
            owned = await self.db.fetchone(
                """SELECT id FROM evidence_clusters
                   WHERE id = ? AND project_id = ?""",
                [cluster_id, self.project_id],
            )
            if owned is None:
                raise ClusterNotFoundError(
                    f"cluster {cluster_id!r} not found in project "
                    f"{self.project_id}"
                )

            # Keep validation and mutation in one write snapshot so the parent
            # decision cannot change between the check and the FK assignment.
            if dump.get("research_question_id"):
                await self._validate_research_question(
                    dump["research_question_id"]
                )

            cursor = await self.db.execute(
                f"UPDATE evidence_clusters SET {set_clause} "
                "WHERE id = ? AND project_id = ?",
                values,
            )
            if cursor.rowcount != 1:  # pragma: no cover - protected by write lock
                raise ClusterNotFoundError(
                    f"cluster {cluster_id!r} not found in project "
                    f"{self.project_id}"
                )
            await self.db.commit()
            if "label" in dump or "synthesis" in dump:
                row = await self.db.fetchone(
                    "SELECT label, synthesis FROM evidence_clusters "
                    "WHERE id = ? AND project_id = ?",
                    [cluster_id, self.project_id],
                )
                if row:
                    await self._sync_fts("cluster", cluster_id, dict(row))
            # Keep the FK and graph projection exactly aligned, including
            # replacement and explicit clearing.
            if rq_assignment_supplied:
                rq_id = dump["research_question_id"]
                await self._replace_outgoing_links(
                    source_type="cluster",
                    source_id=cluster_id,
                    link_type="answers",
                    target_type="decision",
                    target_ids=[rq_id] if rq_id else [],
                    project_id=self.project_id,
                )
            await self.audit(
                "update",
                "cluster",
                cluster_id,
                "system",
                {"fields": list(dump.keys())},
            )
        return await self.get(cluster_id)

    async def _validate_research_question(self, research_question_id: str) -> None:
        """Require an RQ decision owned by the active project."""
        rq = await self.db.fetchone(
            """SELECT id, kind FROM decisions
               WHERE id = ? AND project_id = ?""",
            [research_question_id, self.project_id],
        )
        if rq is None:
            raise ValueError(
                f"Decision {research_question_id} not found in project "
                f"{self.project_id}"
            )
        if rq["kind"] != "research_question":
            raise ValueError(
                f"Decision {research_question_id} is not a research_question "
                f"(kind={rq['kind']})"
            )

    async def mark_needs_reprocessing(self, cluster_id: str) -> None:
        """Flag a cluster for re-distillation."""
        await self.db.execute(
            "UPDATE evidence_clusters SET needs_reprocessing = 1, updated_at = ? WHERE id = ? AND project_id = ?",
            [_now(), cluster_id, self.project_id],
        )
        await self.db.commit()

    # ── Background job handlers ──────────────────────────────

    async def process_cluster_update_job(self, claim_id: str) -> dict:
        """Assign a source-grounded claim to a cluster and score relations."""
        claim_row = await self.db.fetchone(
            "SELECT * FROM claims WHERE id = ? AND project_id = ?",
            [claim_id, self.project_id],
        )
        if claim_row is None:
            return {"outcome": "missing"}
        if not self.llm:
            return {"outcome": "skipped", "reason": "llm_disabled"}

        # Get existing clusters for this project
        clusters = await self.db.fetchall(
            "SELECT id, label, claim_count FROM evidence_clusters WHERE project_id = ? ORDER BY claim_count DESC LIMIT 30",
            [self.project_id],
        )

        # Get nearby claims whose extraction is grounded in their source. This
        # does not imply that their scientific evidence_status is supported.
        nearby = await self.db.fetchall(
            """SELECT id, claim_type, content FROM claims
               WHERE project_id = ? AND verified = 1 AND stale = 0 AND id != ?
               ORDER BY created_at DESC LIMIT 20""",
            [self.project_id, claim_id],
        )

        assignment = await self.llm.assign_to_cluster(
            claim_content=claim_row["content"],
            claim_type=claim_row["claim_type"],
            existing_clusters=[dict(c) for c in clusters],
            nearby_claims=[dict(n) for n in nearby],
        )

        # The external LLM assignment above intentionally completes before the
        # write transaction. Cluster creation, membership, relation-driven
        # reprocessing flags, and the final count are one mutation unit.
        from rka.services.claims import ClaimService

        claim_svc = ClaimService(
            self.db,
            llm=self.llm,
            embeddings=self.embeddings,
            project_id=self.project_id,
        )
        async with self.db.transaction():
            # Create or find cluster.
            if assignment.cluster_id:
                cluster_id = assignment.cluster_id
            else:
                cluster = await self.create(EvidenceClusterCreate(
                    label=assignment.cluster_label,
                ))
                cluster_id = cluster.id

            # Create member_of edge.
            await claim_svc.create_edge(ClaimEdgeCreate(
                source_claim_id=claim_id,
                cluster_id=cluster_id,
                relation="member_of",
            ))

            # Create inter-claim relation edges. Contradiction edges may mark
            # the affected cluster for reprocessing inside create_edge().
            for rel in assignment.relations:
                # Validate target exists.
                target = await self.db.fetchone(
                    "SELECT id FROM claims WHERE id = ? AND project_id = ?",
                    [rel.target_claim_id, self.project_id],
                )
                if target:
                    await claim_svc.create_edge(ClaimEdgeCreate(
                        source_claim_id=claim_id,
                        target_claim_id=rel.target_claim_id,
                        cluster_id=cluster_id,
                        relation=rel.relation,
                        confidence=rel.confidence,
                    ))

            # Recompute the count defensively after all edge writes.
            count_row = await self.db.fetchone(
                """SELECT COUNT(DISTINCT source_claim_id) AS cnt FROM claim_edges
                   WHERE cluster_id = ? AND relation = 'member_of'
                     AND project_id = ?""",
                [cluster_id, self.project_id],
            )
            claim_count = count_row["cnt"] if count_row else 0
            await self.db.execute(
                """UPDATE evidence_clusters
                   SET claim_count = ?, updated_at = ?
                   WHERE id = ? AND project_id = ?""",
                [claim_count, _now(), cluster_id, self.project_id],
            )
            await self.db.commit()

        # Note: theme synthesis and contradiction checks are now Brain tasks,
        # not automated LLM jobs. Use rka_review_cluster and rka_resolve_contradiction.

        return {"outcome": "updated", "cluster_id": cluster_id, "relations": len(assignment.relations)}

    async def process_theme_synthesize_job(self, cluster_id: str) -> dict:
        """Generate/regenerate synthesis for a cluster."""
        cluster = await self.get(cluster_id)
        if cluster is None:
            return {"outcome": "missing"}
        if cluster.staleness_verdict in {"historical", "retired", "superseded", "retracted"}:
            return {"outcome": "noop", "reason": "inactive_disposition"}
        if not self.llm:
            return {"outcome": "skipped", "reason": "llm_disabled"}

        # Get all claims in this cluster
        claims = await self.db.fetchall(
            """SELECT c.* FROM claims c
               JOIN claim_edges ce ON ce.source_claim_id = c.id AND ce.project_id = c.project_id
               WHERE ce.cluster_id = ? AND ce.project_id = ? AND ce.relation = 'member_of' AND c.stale = 0
               ORDER BY c.confidence DESC""",
            [cluster_id, self.project_id],
        )
        claims = [row for row in claims if currentness(row)["is_current"]]
        if not claims:
            return {"outcome": "noop", "reason": "no_claims"}

        synthesis = await self.llm.synthesize_theme(
            cluster_label=cluster.label,
            claims=[dict(c) for c in claims],
        )

        # The external LLM call above intentionally runs before the write
        # transaction. Persist the synthesis, its search projection, and any
        # review flag as one mutation unit.
        needs_review = len(claims) >= 10 or synthesis.confidence == "contested"
        review_id = generate_id("review") if needs_review else None
        flag = (
            "complex_synthesis_needed"
            if len(claims) >= 10
            else "potential_contradiction"
        )
        async with self.db.transaction():
            await self.db.execute(
                """UPDATE evidence_clusters
                   SET synthesis = ?, confidence = ?, gap_count = ?,
                       needs_reprocessing = 0, updated_at = ?
                   WHERE id = ? AND project_id = ?""",
                [
                    synthesis.synthesis,
                    synthesis.confidence,
                    len(synthesis.gaps),
                    _now(),
                    cluster_id,
                    self.project_id,
                ],
            )
            await self._sync_fts(
                "cluster",
                cluster_id,
                {
                    "label": cluster.label,
                    "synthesis": synthesis.synthesis,
                },
            )

            # Flag for Brain review if complex.
            if needs_review:
                import json

                await self.db.execute(
                    """INSERT OR IGNORE INTO review_queue
                       (id, item_type, item_id, flag, context, priority,
                        project_id)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    [
                        review_id,
                        "cluster",
                        cluster_id,
                        flag,
                        json.dumps({
                            "claim_count": len(claims),
                            "confidence": synthesis.confidence,
                            "gaps": synthesis.gaps,
                            "contradictions": synthesis.contradictions,
                        }),
                        70 if flag == "potential_contradiction" else 90,
                        self.project_id,
                    ],
                )
            await self.db.commit()

        return {
            "outcome": "updated",
            "confidence": synthesis.confidence,
            "gap_count": len(synthesis.gaps),
        }

    async def process_contradiction_check_job(self, claim_id: str, payload: dict | None = None) -> dict:
        """Check if a new claim contradicts existing claims in the same cluster."""
        claim_row = await self.db.fetchone(
            "SELECT * FROM claims WHERE id = ? AND project_id = ?",
            [claim_id, self.project_id],
        )
        if claim_row is None:
            return {"outcome": "missing"}

        cluster_id = (payload or {}).get("cluster_id")
        if not cluster_id:
            return {"outcome": "skipped", "reason": "no_cluster"}

        # Check existing contradicts edges
        contradictions = await self.db.fetchall(
            """SELECT * FROM claim_edges
               WHERE cluster_id = ? AND relation = 'contradicts' AND project_id = ?""",
            [cluster_id, self.project_id],
        )

        if contradictions:
            # Flag for review
            import json
            review_id = generate_id("review")
            await self.db.execute(
                """INSERT OR IGNORE INTO review_queue
                   (id, item_type, item_id, flag, context, priority, project_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                [
                    review_id, "cluster", cluster_id, "potential_contradiction",
                    json.dumps({"claim_id": claim_id, "contradiction_count": len(contradictions)}),
                    70, self.project_id,
                ],
            )
            await self.db.commit()
            return {"outcome": "flagged", "contradictions": len(contradictions)}

        return {"outcome": "clean"}

    # ── Helpers ──────────────────────────────────────────────

    @staticmethod
    def _row_to_model(row: dict) -> EvidenceCluster:
        # Affordance B (Mission B / mis_01KR209WY4M6WQFEXRH79KC2ZF): apply
        # STALE prefix to the synthesis field whenever needs_reprocessing=1
        # so that any consumer of EvidenceCluster (rka_list_clusters and
        # rka_get_cluster among others) sees the stale signal prominently.
        return EvidenceCluster(
            **review_projection(row),
            id=row["id"],
            research_question_id=row.get("research_question_id"),
            label=row["label"],
            synthesis=with_staleness_prefix(
                row.get("synthesis"), row.get("needs_reprocessing")
            ),
            confidence=row.get("confidence", "emerging"),
            claim_count=row.get("claim_count", 0),
            gap_count=row.get("gap_count", 0),
            needs_reprocessing=bool(row.get("needs_reprocessing", 0)),
            synthesized_by=row.get("synthesized_by", "llm"),
            project_id=row["project_id"],
            created_at=row.get("created_at"),
            updated_at=row.get("updated_at"),
        )
