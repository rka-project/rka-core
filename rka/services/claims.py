"""Claim extraction, grounding verification, evidence status, and CRUD service."""

from __future__ import annotations

from rka.services.currentness import review_projection

import hashlib
import json
import logging

from rka.infra.ids import generate_id
from rka.models.claim import (
    Claim,
    ClaimCreate,
    ClaimEdge,
    ClaimEdgeCreate,
    ClaimScopeFinding,
    ClaimScopeHistory,
    ClaimScopeReadiness,
    ClaimScopeVersion,
    ClaimScopeWrite,
    ClaimUpdate,
    EvidenceStatus,
)
from rka.services.base import BaseService, _now
from rka.services.jobs import JobQueue

logger = logging.getLogger(__name__)


class ClaimNotFoundError(ValueError):
    """Raised when a claim is absent from the active project scope."""


class ClaimScopeConflictError(ValueError):
    """Raised when a scope append loses its optimistic revision race."""


class ClaimService(BaseService):
    """Manages claims extracted from journal entries."""

    _FTS_CONFIG = {
        **BaseService._FTS_CONFIG,
        "claim": {"table": "fts_claims", "columns": ["id", "content"]},
    }

    # Keep contradiction state as a read-time graph projection rather than a
    # second mutable flag on ``claims``.  The correlated EXISTS avoids an N+1
    # query while project equality prevents a malformed cross-project edge
    # from contaminating the response.
    _CONTRADICTED_PROJECTION = """
        EXISTS (
            SELECT 1
            FROM claim_edges AS contradiction
            WHERE contradiction.project_id = c.project_id
              AND contradiction.relation = 'contradicts'
              AND (
                  contradiction.source_claim_id = c.id
                  OR contradiction.target_claim_id = c.id
              )
        ) AS contradicted
    """

    _SCOPE_PROJECTION = """
        scope.id AS scope_id,
        scope.claim_id AS scope_claim_id,
        scope.revision AS scope_version_revision,
        scope.claim_content_hash AS scope_claim_content_hash,
        scope.conditions AS scope_conditions,
        scope.uncertainty AS scope_uncertainty,
        scope.uncertainty_note AS scope_uncertainty_note,
        scope.extension_policy AS scope_extension_policy,
        scope.allowed_extensions AS scope_allowed_extensions,
        scope.prohibited_extensions AS scope_prohibited_extensions,
        scope.falsifier_status AS scope_falsifier_status,
        scope.falsifier AS scope_falsifier,
        scope.falsifier_rationale AS scope_falsifier_rationale,
        scope.disconfirming_claim_ids AS scope_disconfirming_claim_ids,
        scope.review_status AS scope_review_status,
        scope.created_by AS scope_created_by,
        scope.reason AS scope_reason,
        scope.source_candidate_id AS scope_source_candidate_id,
        scope.supersedes_scope_id AS scope_supersedes_scope_id,
        scope.created_at AS scope_created_at
    """

    _SCOPE_JOIN = """
        LEFT JOIN claim_scope_versions AS scope
          ON scope.claim_id = c.id
         AND scope.project_id = c.project_id
         AND scope.revision = c.scope_revision
    """

    def _job_dedupe_key(self, entity_id: str, operation: str) -> str:
        return f"{self.project_id}:claim:{entity_id}:{operation}"

    # ── CRUD ─────────────────────────────────────────────────

    async def create(self, data: ClaimCreate, *, actor: str = "llm") -> Claim:
        """Create a new claim through an explicit canonical write.

        ``actor`` is provenance only. Automated extraction now stops in
        Interpretation Staging; direct callers retain this method for
        intentionally canonical claims and candidate promotion composes it
        transactionally.
        """
        self._validate_actor(actor)
        claim_id = generate_id("claim")
        async with self.db.transaction():
            source = await self.db.fetchone(
                """SELECT 1 FROM journal
                   WHERE id = ? AND project_id = ?""",
                [data.source_entry_id, self.project_id],
            )
            if source is None:
                raise ValueError("source journal entry is not available in this project")
            await self.db.execute(
                """INSERT INTO claims
                   (id, source_entry_id, claim_type, content, confidence, verified,
                    evidence_status, stale, source_offset_start, source_offset_end,
                    project_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)""",
                [
                    claim_id,
                    data.source_entry_id,
                    data.claim_type,
                    data.content,
                    data.confidence,
                    int(data.verified),
                    data.evidence_status,
                    data.source_offset_start,
                    data.source_offset_end,
                    self.project_id,
                ],
            )

            await self._sync_fts("claim", claim_id, {"content": data.content})
            await self._replace_outgoing_links(
                source_type="claim",
                source_id=claim_id,
                link_type="derived_from",
                target_type="journal",
                target_ids=[data.source_entry_id],
                created_by=actor,
            )

            if self.embeddings:
                queue = JobQueue(self.db)
                await queue.enqueue(
                    "claim_embed",
                    project_id=self.project_id,
                    entity_type="claim",
                    entity_id=claim_id,
                    dedupe_key=self._job_dedupe_key(claim_id, "embed"),
                    priority=125,
                )

            await self.audit("create", "claim", claim_id, actor)
        return await self.get(claim_id)

    async def get(self, claim_id: str) -> Claim | None:
        row = await self.db.fetchone(
            f"""SELECT c.*, {self._CONTRADICTED_PROJECTION},
                       {self._SCOPE_PROJECTION}
                FROM claims AS c
                {self._SCOPE_JOIN}
                WHERE c.id = ? AND c.project_id = ?""",
            [claim_id, self.project_id],
        )
        if row is None:
            return None
        return self._row_to_model(row)

    async def list(
        self,
        source_entry_id: str | None = None,
        cluster_id: str | None = None,
        claim_type: str | None = None,
        verified: bool | None = None,
        evidence_status: EvidenceStatus | None = None,
        stale: bool | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Claim]:
        if cluster_id:
            conditions = [
                "membership.cluster_id = ?",
                "membership.project_id = ?",
                "c.project_id = ?",
            ]
            params: list = [cluster_id, self.project_id, self.project_id]
        else:
            conditions = ["c.project_id = ?"]
            params = [self.project_id]

        if source_entry_id:
            conditions.append("c.source_entry_id = ?")
            params.append(source_entry_id)
        if claim_type:
            conditions.append("c.claim_type = ?")
            params.append(claim_type)
        if verified is not None:
            conditions.append("c.verified = ?")
            params.append(int(verified))
        if evidence_status is not None:
            conditions.append("c.evidence_status = ?")
            params.append(evidence_status)
        if stale is not None:
            conditions.append("c.stale = ?")
            params.append(int(stale))

        where = " AND ".join(conditions)
        params.extend([limit, offset])

        if cluster_id:
            sql = f"""
                SELECT c.*, {self._CONTRADICTED_PROJECTION},
                       {self._SCOPE_PROJECTION}
                FROM claims AS c
                JOIN claim_edges AS membership
                  ON membership.source_claim_id = c.id
                 AND membership.relation = 'member_of'
                {self._SCOPE_JOIN}
                WHERE {where}
                ORDER BY c.created_at DESC LIMIT ? OFFSET ?
            """
        else:
            sql = f"""SELECT c.*, {self._CONTRADICTED_PROJECTION},
                       {self._SCOPE_PROJECTION}
                FROM claims AS c
                {self._SCOPE_JOIN}
                WHERE {where}
                ORDER BY c.created_at DESC LIMIT ? OFFSET ?"""

        rows = await self.db.fetchall(sql, params)
        return [self._row_to_model(row) for row in rows]

    async def update(self, claim_id: str, data: ClaimUpdate) -> Claim:
        dump = data.model_dump(exclude_none=True)
        if dump.get("stale") is False:
            raise ValueError("stale=false is unsupported; use resolve_stale for audited review; structural invalidation requires re-distillation")
        if not dump:
            current = await self.get(claim_id)
            if current is None:
                raise ClaimNotFoundError(
                    f"claim {claim_id!r} not found in project {self.project_id}"
                )
            return current

        # Convert bool fields to int for SQLite
        for bfield in ("verified", "stale"):
            if bfield in dump:
                dump[bfield] = int(dump[bfield])

        dump["updated_at"] = _now()
        set_clause = ", ".join(f"{k} = ?" for k in dump)
        values = list(dump.values()) + [claim_id, self.project_id]

        async with self.db.transaction():
            cursor = await self.db.execute(
                f"UPDATE claims SET {set_clause} WHERE id = ? AND project_id = ?",
                values,
            )
            if cursor.rowcount != 1:
                raise ClaimNotFoundError(
                    f"claim {claim_id!r} not found in project {self.project_id}"
                )

            # v2.7.0.7 — re-sync derived indexes when the searchable/embeddable
            # `content` field changed.
            if "content" in dump:
                await self._sync_fts("claim", claim_id, {"content": dump["content"]})
                if self.embeddings:
                    queue = JobQueue(self.db)
                    await queue.enqueue(
                        "claim_embed",
                        project_id=self.project_id,
                        entity_type="claim",
                        entity_id=claim_id,
                        dedupe_key=self._job_dedupe_key(claim_id, "embed"),
                        priority=125,
                    )

            await self.audit(
                "update",
                "claim",
                claim_id,
                "system",
                {"fields": list(dump.keys())},
            )
        return await self.get(claim_id)

    async def mark_stale_by_entry(self, entry_id: str) -> int:
        """Mark all claims from a journal entry as stale (for re-distillation)."""
        result = await self.db.execute(
            "UPDATE claims SET stale = 1, updated_at = ? WHERE source_entry_id = ? AND project_id = ?",
            [_now(), entry_id, self.project_id],
        )
        await self.db.commit()
        return result.rowcount if hasattr(result, "rowcount") else 0

    async def get_claims_for_entry(self, entry_id: str) -> list[Claim]:
        """Get all claims extracted from a specific journal entry."""
        return await self.list(source_entry_id=entry_id)

    # ── Immutable claim-scope contracts ─────────────────────

    async def get_scope_history(self, claim_id: str) -> ClaimScopeHistory | None:
        claim = await self.get(claim_id)
        if claim is None:
            return None
        rows = await self.db.fetchall(
            """SELECT * FROM claim_scope_versions
               WHERE claim_id = ? AND project_id = ?
               ORDER BY revision DESC""",
            [claim_id, self.project_id],
        )
        versions = [self._scope_row_to_model(row) for row in rows]
        return ClaimScopeHistory(
            claim_id=claim.id,
            project_id=claim.project_id,
            current_revision=claim.scope_revision,
            scope_readiness=claim.scope_readiness,
            findings=claim.scope_findings,
            current=claim.scope_contract,
            versions=versions,
        )

    async def append_scope(
        self,
        claim_id: str,
        data: ClaimScopeWrite,
    ) -> ClaimScopeHistory:
        """Append and select one immutable, revision-guarded scope contract."""
        self._validate_actor(data.actor)
        async with self.db.transaction():
            claim = await self.db.fetchone(
                """SELECT id, claim_type, content, scope_revision
                   FROM claims WHERE id = ? AND project_id = ?""",
                [claim_id, self.project_id],
            )
            if claim is None:
                raise ClaimNotFoundError(
                    f"claim {claim_id!r} not found in project {self.project_id}"
                )
            current_revision = int(claim.get("scope_revision") or 0)
            if current_revision != data.expected_revision:
                raise ClaimScopeConflictError(
                    "claim scope revision changed; reload before appending"
                )

            if data.source_candidate_id:
                candidate = await self.db.fetchone(
                    """SELECT 1 FROM interpretation_candidates
                       WHERE id = ? AND project_id = ?""",
                    [data.source_candidate_id, self.project_id],
                )
                if candidate is None:
                    raise ValueError("source candidate is not available in this project")

            if claim_id in data.disconfirming_claim_ids:
                raise ValueError("a claim cannot disconfirm itself")
            if data.disconfirming_claim_ids:
                placeholders = ",".join("?" for _ in data.disconfirming_claim_ids)
                rows = await self.db.fetchall(
                    f"""SELECT id FROM claims
                        WHERE project_id = ? AND id IN ({placeholders})""",
                    [self.project_id, *data.disconfirming_claim_ids],
                )
                found = {row["id"] for row in rows}
                missing = sorted(set(data.disconfirming_claim_ids) - found)
                if missing:
                    raise ValueError(
                        "disconfirming claims are not available in this project: "
                        + ", ".join(missing)
                    )

            supersedes_scope_id = None
            if current_revision:
                previous = await self.db.fetchone(
                    """SELECT id FROM claim_scope_versions
                       WHERE claim_id = ? AND project_id = ? AND revision = ?""",
                    [claim_id, self.project_id, current_revision],
                )
                if previous is None:
                    raise ClaimScopeConflictError(
                        "claim scope pointer is inconsistent with immutable history"
                    )
                supersedes_scope_id = previous["id"]

            scope_id = generate_id("claim_scope")
            next_revision = current_revision + 1
            content_hash = self._claim_content_hash(claim["claim_type"], claim["content"])
            await self.db.execute(
                """INSERT INTO claim_scope_versions (
                       id, claim_id, project_id, revision, claim_content_hash,
                       conditions, uncertainty, uncertainty_note,
                       extension_policy, allowed_extensions,
                       prohibited_extensions, falsifier_status, falsifier,
                       falsifier_rationale, disconfirming_claim_ids,
                       review_status, created_by, reason, source_candidate_id,
                       supersedes_scope_id
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    scope_id,
                    claim_id,
                    self.project_id,
                    next_revision,
                    content_hash,
                    json.dumps(
                        [condition.model_dump(mode="json") for condition in data.conditions],
                        sort_keys=True,
                    ),
                    data.uncertainty,
                    data.uncertainty_note,
                    data.extension_policy,
                    json.dumps(data.allowed_extensions, sort_keys=True),
                    json.dumps(data.prohibited_extensions, sort_keys=True),
                    data.falsifier_status,
                    data.falsifier,
                    data.falsifier_rationale,
                    json.dumps(data.disconfirming_claim_ids, sort_keys=True),
                    data.review_status,
                    data.actor,
                    data.reason,
                    data.source_candidate_id,
                    supersedes_scope_id,
                ],
            )
            cursor = await self.db.execute(
                """UPDATE claims SET scope_revision = ?, updated_at = ?
                   WHERE id = ? AND project_id = ? AND scope_revision = ?""",
                [
                    next_revision,
                    _now(),
                    claim_id,
                    self.project_id,
                    data.expected_revision,
                ],
            )
            if cursor.rowcount != 1:
                raise ClaimScopeConflictError(
                    "claim scope revision changed; reload before appending"
                )
            await self.audit(
                "create",
                "claim_scope",
                scope_id,
                data.actor,
                {
                    "claim_id": claim_id,
                    "revision": next_revision,
                    "review_status": data.review_status,
                },
            )

        history = await self.get_scope_history(claim_id)
        if history is None:  # pragma: no cover - claim exists transactionally
            raise ClaimNotFoundError(f"claim {claim_id!r} disappeared")
        return history

    # ── Claim Edges ──────────────────────────────────────────

    async def create_edge(self, data: ClaimEdgeCreate) -> ClaimEdge:
        edge_id = generate_id("claim_edge")
        stored_confidence = data.confidence
        async with self.db.transaction():
            if data.relation == "member_of":
                if data.cluster_id is None or data.target_claim_id is not None:
                    raise ValueError("member_of edges require one cluster and no target claim")
            elif data.relation == "contradicts":
                if data.target_claim_id is None and data.cluster_id is None:
                    raise ValueError("contradicts edges require a target claim or cluster")
            elif data.target_claim_id is None:
                raise ValueError(f"{data.relation} edges require a target claim")
            if data.target_claim_id == data.source_claim_id:
                raise ValueError("claim edges cannot target their source claim")

            source = await self.db.fetchone(
                """SELECT 1 FROM claims
                   WHERE id = ? AND project_id = ?""",
                [data.source_claim_id, self.project_id],
            )
            if source is None:
                raise ValueError("source claim is not available in this project")
            if data.target_claim_id is not None:
                target = await self.db.fetchone(
                    """SELECT 1 FROM claims
                       WHERE id = ? AND project_id = ?""",
                    [data.target_claim_id, self.project_id],
                )
                if target is None:
                    raise ValueError("target claim is not available in this project")
            if data.cluster_id is not None:
                cluster = await self.db.fetchone(
                    """SELECT claim_count FROM evidence_clusters
                       WHERE id = ? AND project_id = ?""",
                    [data.cluster_id, self.project_id],
                )
                if cluster is None:
                    raise ValueError("cluster is not available in this project")
            if data.relation == "member_of" and data.cluster_id:
                existing = await self.db.fetchone(
                    """SELECT id, confidence FROM claim_edges
                       WHERE project_id = ? AND source_claim_id = ?
                         AND cluster_id = ? AND relation = 'member_of'""",
                    [self.project_id, data.source_claim_id, data.cluster_id],
                )
                if existing:
                    edge_id = existing["id"]
                    stored_confidence = existing["confidence"]
                else:
                    await self.db.execute(
                        """INSERT INTO claim_edges
                           (id, source_claim_id, target_claim_id, cluster_id,
                            relation, confidence, project_id)
                           VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        [
                            edge_id,
                            data.source_claim_id,
                            data.target_claim_id,
                            data.cluster_id,
                            data.relation,
                            data.confidence,
                            self.project_id,
                        ],
                    )
                count_row = await self.db.fetchone(
                    """SELECT COUNT(DISTINCT source_claim_id) AS claim_count
                       FROM claim_edges
                       WHERE cluster_id = ? AND relation = 'member_of'
                         AND project_id = ?""",
                    [data.cluster_id, self.project_id],
                )
                actual_count = count_row["claim_count"] if count_row else 0
                # A retry that finds the same membership must be a true no-op:
                # updating the parent cluster would otherwise append a false
                # semantic change to the immutable change-events ledger.  A
                # stale cached count is still repaired on either a create or a
                # retry, including the nullable legacy state.
                if cluster["claim_count"] != actual_count:
                    await self.db.execute(
                        """UPDATE evidence_clusters
                           SET claim_count = ?, updated_at = ?
                           WHERE id = ? AND project_id = ?""",
                        [
                            actual_count,
                            _now(),
                            data.cluster_id,
                            self.project_id,
                        ],
                    )
            else:
                await self.db.execute(
                    """INSERT INTO claim_edges
                       (id, source_claim_id, target_claim_id, cluster_id, relation,
                        confidence, project_id)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    [
                        edge_id,
                        data.source_claim_id,
                        data.target_claim_id,
                        data.cluster_id,
                        data.relation,
                        data.confidence,
                        self.project_id,
                    ],
                )
            if data.relation == "contradicts":
                affected_claims = [
                    claim_id
                    for claim_id in (data.source_claim_id, data.target_claim_id)
                    if claim_id
                ]
                if affected_claims:
                    placeholders = ",".join("?" for _ in affected_claims)
                    await self.db.execute(
                        f"""UPDATE evidence_clusters
                            SET needs_reprocessing = 1, updated_at = ?
                            WHERE id IN (
                                SELECT DISTINCT cluster_id FROM claim_edges
                                WHERE source_claim_id IN ({placeholders})
                                  AND relation = 'member_of'
                                  AND project_id = ?
                            ) AND project_id = ?""",
                        [_now(), *affected_claims, self.project_id, self.project_id],
                    )
                if data.cluster_id:
                    await self.db.execute(
                        "UPDATE evidence_clusters SET needs_reprocessing = 1, "
                        "updated_at = ? WHERE id = ? AND project_id = ?",
                        [_now(), data.cluster_id, self.project_id],
                    )
        return ClaimEdge(
            id=edge_id,
            source_claim_id=data.source_claim_id,
            target_claim_id=data.target_claim_id,
            cluster_id=data.cluster_id,
            relation=data.relation,
            confidence=stored_confidence,
            project_id=self.project_id,
        )

    async def get_edges_for_cluster(self, cluster_id: str) -> list[ClaimEdge]:
        rows = await self.db.fetchall(
            "SELECT * FROM claim_edges WHERE cluster_id = ? AND project_id = ?",
            [cluster_id, self.project_id],
        )
        return [self._edge_to_model(row) for row in rows]

    # ── Background job handlers ──────────────────────────────

    async def process_extract_claims_job(self, entry_id: str) -> dict:
        """Extract reviewable interpretation candidates from a journal entry."""
        row = await self.db.fetchone(
            "SELECT content FROM journal WHERE id = ? AND project_id = ?",
            [entry_id, self.project_id],
        )
        if row is None:
            return {"outcome": "missing"}
        if not self.llm:
            return {"outcome": "skipped", "reason": "llm_disabled"}

        # Give the model both canonical and staged statements as duplicate
        # context. The model output is still only a hint; no row is promoted.
        existing_claims = await self.db.fetchall(
            "SELECT content FROM claims WHERE source_entry_id = ? AND project_id = ? AND stale = 0",
            [entry_id, self.project_id],
        )
        existing_candidates = await self.db.fetchall(
            """SELECT statement FROM interpretation_candidates
               WHERE source_type = 'journal' AND source_id = ?
                 AND project_id = ?""",
            [entry_id, self.project_id],
        )
        existing_contents = [r["content"] for r in existing_claims]
        existing_contents.extend(r["statement"] for r in existing_candidates)

        # Extract claims via LLM
        result = await self.llm.extract_claims(row["content"], existing_contents or None)

        from rka.models.interpretation import InterpretationCandidateCreate
        from rka.services.interpretation import InterpretationService

        interpretation = InterpretationService(self.db, project_id=self.project_id)
        extraction_model = (
            getattr(getattr(self.llm, "config", None), "llm_model", None)
            or self.llm.__class__.__name__
        )

        created_ids = []
        async with self.db.transaction():
            for extracted in result.claims:
                start = extracted.source_offset_start
                end = extracted.source_offset_end
                if start is None:
                    locator_kind = "record"
                    locator_value = "full_record"
                else:
                    locator_kind = "text_offset"
                    locator_value = None
                epistemic_kind = {
                    "hypothesis": "hypothesis",
                    "observation": "observation",
                    "evidence": "reported_fact",
                    "result": "reported_fact",
                    "method": "reported_fact",
                    "assumption": "hypothesis",
                }.get(extracted.claim_type, "inference")
                candidate = await interpretation.create(
                    InterpretationCandidateCreate(
                        source_type="journal",
                        source_id=entry_id,
                        locator_kind=locator_kind,
                        locator_start=start,
                        locator_end=end,
                        locator_value=locator_value,
                        statement=extracted.content,
                        epistemic_kind=epistemic_kind,
                        proposed_claim_type=extracted.claim_type,
                        created_by="llm",
                        extraction_tool="rka_background_claim_extractor",
                        extraction_model=extraction_model,
                    )
                )
                created_ids.append(candidate.id)

        return {
            "outcome": "updated",
            "candidates_created": len(created_ids),
            "candidate_ids": created_ids,
            "claims_created": 0,
            "claim_ids": [],
        }

    async def process_verify_claim_job(self, claim_id: str) -> dict:
        """Verify extraction fidelity against source text.

        This job checks source presence, numeric accuracy, and directional
        accuracy. It intentionally does not update ``evidence_status`` because
        grounding a proposition is distinct from scientifically supporting it.
        """
        claim_row = await self.db.fetchone(
            "SELECT * FROM claims WHERE id = ? AND project_id = ?",
            [claim_id, self.project_id],
        )
        if claim_row is None:
            return {"outcome": "missing"}
        if not self.llm:
            return {"outcome": "skipped", "reason": "llm_disabled"}

        source_row = await self.db.fetchone(
            "SELECT content FROM journal WHERE id = ? AND project_id = ?",
            [claim_row["source_entry_id"], self.project_id],
        )
        if source_row is None:
            return {"outcome": "skipped", "reason": "source_missing"}

        verification = await self.llm.verify_claim(claim_row["content"], source_row["content"])

        passed = (
            verification.exists_in_source
            and verification.number_accuracy
            and verification.direction_correct
        )
        async with self.db.transaction():
            await self.db.execute(
                """UPDATE claims
                   SET verified = ?, confidence = ?, updated_at = ?
                   WHERE id = ? AND project_id = ?""",
                [
                    int(passed),
                    verification.overall_confidence,
                    _now(),
                    claim_id,
                    self.project_id,
                ],
            )

            # If verification failed, persist the review flag with the claim
            # state change. The external verifier call above stays outside.
            if not passed:
                await self._flag_for_review(
                    claim_id,
                    "claim",
                    verification.issues,
                )

        return {
            "outcome": "updated",
            "verified": passed,
            "confidence": verification.overall_confidence,
        }

    async def process_embedding_job(self, claim_id: str) -> dict:
        """Generate embedding for a claim."""
        row = await self.db.fetchone(
            "SELECT content FROM claims WHERE id = ? AND project_id = ?",
            [claim_id, self.project_id],
        )
        if row is None:
            return {"outcome": "missing"}
        if not self.embeddings:
            return {"outcome": "skipped", "reason": "embeddings_disabled"}

        from rka.infra.embedding_documents import compose_document

        text = compose_document("claim", row)
        if not text:
            return {"outcome": "skipped", "reason": "empty"}
        await self.embeddings.embed_and_store(
            "claim", claim_id, text, project_id=self.project_id
        )
        return {"outcome": "updated", "char_count": len(text)}

    async def _flag_for_review(self, item_id: str, item_type: str, issues: list[str]) -> None:
        """Flag an item for Brain review."""
        review_id = generate_id("review")
        await self.db.execute(
            """INSERT OR IGNORE INTO review_queue
               (id, item_type, item_id, flag, context, priority, project_id)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            [
                review_id,
                item_type,
                item_id,
                "low_confidence_cluster",
                json.dumps({"issues": issues}),
                80,
                self.project_id,
            ],
        )
        await self.db.commit()

    # ── Helpers ──────────────────────────────────────────────

    @classmethod
    def _row_to_model(cls, row: dict) -> Claim:
        scope = cls._scope_projection_to_model(row)
        readiness, findings = cls._assess_scope(row, scope)
        return Claim(
            **review_projection(row),
            id=row["id"],
            source_entry_id=row["source_entry_id"],
            claim_type=row["claim_type"],
            content=row["content"],
            confidence=row.get("confidence", 0.5),
            verified=bool(row.get("verified", 0)),
            evidence_status=row.get("evidence_status", "unassessed"),
            contradicted=bool(row["contradicted"]),
            stale=bool(row.get("stale", 0)),
            source_offset_start=row.get("source_offset_start"),
            source_offset_end=row.get("source_offset_end"),
            scope_revision=int(row.get("scope_revision") or 0),
            scope_readiness=readiness,
            scope_contract=scope,
            scope_findings=findings,
            project_id=row["project_id"],
            created_at=row.get("created_at"),
            updated_at=row.get("updated_at"),
        )

    @staticmethod
    def _claim_content_hash(claim_type: str, content: str) -> str:
        material = f"{claim_type}\0{content}".encode("utf-8")
        return hashlib.sha256(material).hexdigest()

    @staticmethod
    def _json_list(raw: object) -> list:
        if isinstance(raw, list):
            return raw
        if not isinstance(raw, str) or not raw:
            return []
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, list) else []

    @classmethod
    def _scope_projection_to_model(cls, row: dict) -> ClaimScopeVersion | None:
        if not row.get("scope_id"):
            return None
        return ClaimScopeVersion(
            id=row["scope_id"],
            claim_id=row["scope_claim_id"],
            project_id=row["project_id"],
            revision=int(row["scope_version_revision"]),
            claim_content_hash=row["scope_claim_content_hash"],
            conditions=cls._json_list(row.get("scope_conditions")),
            uncertainty=row.get("scope_uncertainty") or "unknown",
            uncertainty_note=row.get("scope_uncertainty_note"),
            extension_policy=row.get("scope_extension_policy"),
            allowed_extensions=cls._json_list(row.get("scope_allowed_extensions")),
            prohibited_extensions=cls._json_list(row.get("scope_prohibited_extensions")),
            falsifier_status=row.get("scope_falsifier_status") or "unknown",
            falsifier=row.get("scope_falsifier"),
            falsifier_rationale=row.get("scope_falsifier_rationale"),
            disconfirming_claim_ids=cls._json_list(row.get("scope_disconfirming_claim_ids")),
            review_status=row.get("scope_review_status") or "draft",
            created_by=row.get("scope_created_by") or "import",
            reason=row.get("scope_reason") or "Imported scope history",
            source_candidate_id=row.get("scope_source_candidate_id"),
            supersedes_scope_id=row.get("scope_supersedes_scope_id"),
            created_at=row.get("scope_created_at"),
        )

    @classmethod
    def _scope_row_to_model(cls, row: dict) -> ClaimScopeVersion:
        return ClaimScopeVersion(
            id=row["id"],
            claim_id=row["claim_id"],
            project_id=row["project_id"],
            revision=int(row["revision"]),
            claim_content_hash=row["claim_content_hash"],
            conditions=cls._json_list(row.get("conditions")),
            uncertainty=row.get("uncertainty") or "unknown",
            uncertainty_note=row.get("uncertainty_note"),
            extension_policy=row.get("extension_policy"),
            allowed_extensions=cls._json_list(row.get("allowed_extensions")),
            prohibited_extensions=cls._json_list(row.get("prohibited_extensions")),
            falsifier_status=row.get("falsifier_status") or "unknown",
            falsifier=row.get("falsifier"),
            falsifier_rationale=row.get("falsifier_rationale"),
            disconfirming_claim_ids=cls._json_list(row.get("disconfirming_claim_ids")),
            review_status=row.get("review_status") or "draft",
            created_by=row["created_by"],
            reason=row["reason"],
            source_candidate_id=row.get("source_candidate_id"),
            supersedes_scope_id=row.get("supersedes_scope_id"),
            created_at=row.get("created_at"),
        )

    @classmethod
    def _assess_scope(
        cls,
        row: dict,
        scope: ClaimScopeVersion | None,
    ) -> tuple[ClaimScopeReadiness, list[ClaimScopeFinding]]:
        findings: list[ClaimScopeFinding] = []

        def add(code: str, severity: str, message: str) -> None:
            findings.append(
                ClaimScopeFinding(
                    code=code,
                    severity=severity,
                    message=message,
                )
            )

        if scope is None:
            readiness: ClaimScopeReadiness = "missing"
            add(
                "CLAIM_SCOPE_MISSING",
                "block",
                "canonical claim has no reviewed reuse boundary",
            )
            if int(row.get("scope_revision") or 0) > 0:
                add(
                    "CLAIM_SCOPE_POINTER_BROKEN",
                    "block",
                    "claim scope pointer does not resolve to immutable history",
                )
        elif scope.claim_content_hash != cls._claim_content_hash(row["claim_type"], row["content"]):
            readiness = "stale"
            add(
                "CLAIM_SCOPE_STALE",
                "block",
                "claim content or type changed after this scope version",
            )
        else:
            incomplete = False
            checks = [
                (
                    not scope.conditions,
                    "CLAIM_SCOPE_CONDITIONS_MISSING",
                    "at least one applicability condition is required",
                ),
                (
                    scope.uncertainty == "unknown",
                    "CLAIM_SCOPE_UNCERTAINTY_UNKNOWN",
                    "uncertainty has not been resolved",
                ),
                (
                    scope.extension_policy is None,
                    "CLAIM_SCOPE_EXTENSION_POLICY_MISSING",
                    "exact-only or bounded extension policy is required",
                ),
                (
                    scope.extension_policy == "bounded" and not scope.allowed_extensions,
                    "CLAIM_SCOPE_ALLOWED_EXTENSIONS_MISSING",
                    "bounded policy requires at least one allowed extension",
                ),
                (
                    not scope.prohibited_extensions,
                    "CLAIM_SCOPE_PROHIBITED_EXTENSIONS_MISSING",
                    "at least one prohibited extension is required",
                ),
                (
                    scope.falsifier_status == "unknown",
                    "CLAIM_SCOPE_FALSIFIER_UNRESOLVED",
                    "falsifier applicability has not been resolved",
                ),
            ]
            for failed, code, message in checks:
                if failed:
                    incomplete = True
                    add(code, "block", message)
            if incomplete:
                readiness = "incomplete"
            elif scope.review_status != "reviewed":
                readiness = "needs_review"
                add(
                    "CLAIM_SCOPE_REVIEW_REQUIRED",
                    "block",
                    "complete scope contract has not been explicitly reviewed",
                )
            else:
                readiness = "ready"

            if scope.disconfirming_claim_ids:
                add(
                    "CLAIM_SCOPE_DISCONFIRMING_OBSERVATIONS",
                    "warn",
                    "scope cites canonical claims that may bound or disconfirm use",
                )

        if bool(row.get("stale", 0)):
            add(
                "CLAIM_NOT_CURRENT",
                "block",
                "claim is marked stale independently of its scope contract",
            )
        if bool(row.get("contradicted", 0)):
            add(
                "CLAIM_CONTRADICTION_PRESENT",
                "block",
                "claim graph contains an unresolved contradiction",
            )
        if row.get("evidence_status") == "contradicted":
            add(
                "CLAIM_EVIDENCE_CONTRADICTED",
                "block",
                "scientific evidence assessment is contradicted",
            )
        elif row.get("evidence_status") == "unassessed":
            add(
                "CLAIM_EVIDENCE_UNASSESSED",
                "info",
                "scope readiness does not establish scientific support",
            )
        return readiness, findings

    @staticmethod
    def _edge_to_model(row: dict) -> ClaimEdge:
        return ClaimEdge(
            id=row["id"],
            source_claim_id=row["source_claim_id"],
            target_claim_id=row.get("target_claim_id"),
            cluster_id=row.get("cluster_id"),
            relation=row["relation"],
            confidence=row.get("confidence", 0.5),
            project_id=row.get("project_id", "proj_default"),
            created_at=row.get("created_at"),
        )
