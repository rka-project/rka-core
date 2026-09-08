"""Decision tree service."""

from __future__ import annotations

import json
from collections import defaultdict

from rka.infra.ids import generate_id
from rka.models.decision import (
    Decision,
    DecisionCreate,
    DecisionOption,
    DecisionSupersedeBody,
    DecisionTreeNode,
    DecisionUpdate,
)
from rka.services.base import BaseService, _now
from rka.services.jobs import JobQueue


class DecisionNotFoundError(ValueError):
    """Raised when a decision is absent from the active project scope."""


class DecisionService(BaseService):
    """Manages the decision tree."""

    def _job_dedupe_key(self, dec_id: str, operation: str) -> str:
        return f"{self.project_id}:decision:{dec_id}:{operation}"

    async def _enqueue_enrichment_jobs(
        self,
        dec_id: str,
        *,
        include_embedding: bool,
    ) -> None:
        if not include_embedding:
            return
        queue = JobQueue(self.db)
        await queue.enqueue(
            "decision_embed",
            project_id=self.project_id,
            entity_type="decision",
            entity_id=dec_id,
            dedupe_key=self._job_dedupe_key(dec_id, "embed"),
            priority=110,
        )

    async def create(self, data: DecisionCreate, actor: str | None = None) -> Decision:
        """Create a new decision node."""
        dec_id = generate_id("decision")
        actor_val = actor or data.decided_by
        related_journal = (
            None
            if data.related_journal is None
            else self._canonical_link_ids(data.related_journal)
        )
        related_literature = (
            None
            if data.related_literature is None
            else self._canonical_link_ids(data.related_literature)
        )
        related_missions = (
            None
            if data.related_missions is None
            else self._canonical_link_ids(data.related_missions)
        )

        options_json = None
        if data.options:
            options_json = json.dumps([o.model_dump() for o in data.options])

        async with self.db.transaction():
            await self.db.execute(
                """INSERT INTO decisions
                   (id, parent_id, phase, question, options, chosen, rationale,
                    decided_by, status, related_missions, related_literature,
                    related_journal, kind, assumptions, project_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    dec_id,
                    data.parent_id,
                    data.phase,
                    data.question,
                    options_json,
                    data.chosen,
                    data.rationale,
                    data.decided_by,
                    data.status,
                    self._json_dumps(related_missions),
                    self._json_dumps(related_literature),
                    self._json_dumps(related_journal),
                    data.kind,
                    self._json_dumps(data.assumptions),
                    self.project_id,
                ],
            )

            if data.tags:
                await self._set_tags("decision", dec_id, data.tags)

            await self._replace_outgoing_links(
                source_type="decision",
                source_id=dec_id,
                link_type="justified_by",
                target_type="journal",
                target_ids=related_journal,
                created_by=actor_val,
            )
            await self._replace_incoming_links(
                target_type="decision",
                target_id=dec_id,
                link_type="informed_by",
                source_type="literature",
                source_ids=related_literature,
                created_by=actor_val,
            )
            await self._replace_outgoing_links(
                source_type="decision",
                source_id=dec_id,
                link_type="triggered",
                target_type="mission",
                target_ids=related_missions,
                created_by=actor_val,
            )
            await self._replace_incoming_links(
                target_type="decision",
                target_id=dec_id,
                link_type="triggered",
                source_type="decision",
                source_ids=[data.parent_id] if data.parent_id else [],
                created_by=actor_val,
            )

            await self._sync_fts(
                "decision",
                dec_id,
                {"question": data.question, "rationale": data.rationale},
            )
            await self._enqueue_enrichment_jobs(
                dec_id,
                include_embedding=bool(self.embeddings),
            )
            await self.emit_event(
                event_type="decision_created",
                entity_type="decision",
                entity_id=dec_id,
                actor=actor_val,
                summary=f"Decision: {data.question[:100]}",
                phase=data.phase,
            )
            await self.audit("create", "decision", dec_id, actor_val)
        return await self.get(dec_id)

    async def get(self, dec_id: str) -> Decision | None:
        """Get a single decision by ID."""
        row = await self.db.fetchone(
            "SELECT * FROM decisions WHERE id = ? AND project_id = ?",
            [dec_id, self.project_id],
        )
        if row is None:
            return None
        return await self._row_to_model(row)

    async def list(
        self,
        phase: str | None = None,
        status: str | None = None,
        parent_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Decision]:
        """List decisions with filters."""
        conditions = []
        params = [self.project_id]

        conditions.append("project_id = ?")

        if phase:
            conditions.append("phase = ?")
            params.append(phase)
        if status:
            conditions.append("status = ?")
            params.append(status)
        if parent_id is not None:
            if parent_id == "":
                conditions.append("parent_id IS NULL")
            else:
                conditions.append("parent_id = ?")
                params.append(parent_id)

        where = " AND ".join(conditions) if conditions else "1=1"
        params.extend([limit, offset])

        rows = await self.db.fetchall(
            f"SELECT * FROM decisions WHERE {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            params,
        )
        return [await self._row_to_model(row) for row in rows]

    async def update(self, dec_id: str, data: DecisionUpdate, actor: str = "system") -> Decision:
        """Update a decision."""
        dump = data.model_dump(exclude_none=True)
        tags = dump.pop("tags", None)
        replace_related_missions = "related_missions" in dump
        replace_related_literature = "related_literature" in dump
        replace_related_journal = "related_journal" in dump
        replace_parent = "parent_id" in dump
        for field in (
            "related_missions",
            "related_literature",
            "related_journal",
        ):
            if field in dump:
                dump[field] = self._canonical_link_ids(dump[field])

        updates = {}
        for field, value in dump.items():
            if field == "options":
                updates[field] = json.dumps([o.model_dump() for o in value])
            elif field in ("related_missions", "related_literature", "related_journal", "assumptions"):
                updates[field] = self._json_dumps(value)
            else:
                updates[field] = value

        async with self.db.transaction():
            owned = await self.db.fetchone(
                """SELECT id, status, superseded_by
                   FROM decisions
                   WHERE id = ? AND project_id = ?""",
                [dec_id, self.project_id],
            )
            if owned is None:
                raise DecisionNotFoundError(
                    f"decision {dec_id!r} not found in project "
                    f"{self.project_id}"
                )

            if (owned["superseded_by"] or owned["status"] == "superseded") and (
                "status" in dump and dump["status"] != owned["status"]
            ):
                raise ValueError("A superseded decision cannot be reopened; update the active successor instead")

            # A generic update may not flip status to 'superseded' without
            # naming the successor. Check the owned row under the same write
            # lock that protects all subsequent aggregate side effects.
            if (
                dump.get("status") == "superseded"
                and not dump.get("superseded_by")
                and not owned.get("superseded_by")
            ):
                raise ValueError(
                    f"Cannot set decision {dec_id} status='superseded' without a "
                    f"successor: use supersede_decision (POST "
                    f"/api/decisions/{dec_id}/supersede), which records "
                    f"superseded_by, the supersedes graph edge, and the "
                    f"staleness cascade atomically."
                )

            if tags is not None:
                await self._set_tags("decision", dec_id, tags)

            if not updates:
                current = await self.get(dec_id)
                if current is None:  # pragma: no cover - protected by write lock
                    raise RuntimeError("decision update target disappeared")
                return current

            updates["updated_at"] = _now()
            set_clause = ", ".join(f"{k} = ?" for k in updates)
            values = list(updates.values()) + [dec_id]
            cursor = await self.db.execute(
                f"UPDATE decisions SET {set_clause} WHERE id = ? AND project_id = ?",
                values + [self.project_id],
            )
            if cursor.rowcount != 1:  # pragma: no cover - protected by write lock
                raise DecisionNotFoundError(
                    f"decision {dec_id!r} not found in project "
                    f"{self.project_id}"
                )

            if replace_related_journal:
                await self._replace_outgoing_links(
                    source_type="decision",
                    source_id=dec_id,
                    link_type="justified_by",
                    target_type="journal",
                    target_ids=dump["related_journal"],
                    created_by=actor,
                )
            if replace_related_literature:
                await self._replace_incoming_links(
                    target_type="decision",
                    target_id=dec_id,
                    link_type="informed_by",
                    source_type="literature",
                    source_ids=dump["related_literature"],
                    created_by=actor,
                )
            if replace_related_missions:
                await self._replace_outgoing_links(
                    source_type="decision",
                    source_id=dec_id,
                    link_type="triggered",
                    target_type="mission",
                    target_ids=dump["related_missions"],
                    created_by=actor,
                )
            if replace_parent:
                await self._replace_incoming_links(
                    target_type="decision",
                    target_id=dec_id,
                    link_type="triggered",
                    source_type="decision",
                    source_ids=[data.parent_id] if data.parent_id else [],
                    created_by=actor,
                )

            if data.status:
                event_type = {
                    "abandoned": "decision_abandoned",
                }.get(data.status, "decision_updated")
                await self.emit_event(
                    event_type=event_type,
                    entity_type="decision",
                    entity_id=dec_id,
                    actor=actor,
                    summary=f"Decision updated: status → {data.status}",
                )

            if "question" in updates or "rationale" in updates:
                row = await self.db.fetchone(
                    "SELECT question, rationale FROM decisions "
                    "WHERE id = ? AND project_id = ?",
                    [dec_id, self.project_id],
                )
                if row:
                    await self._sync_fts("decision", dec_id, dict(row))
                    await self._enqueue_enrichment_jobs(
                        dec_id,
                        include_embedding=bool(self.embeddings),
                    )

            await self.audit(
                "update",
                "decision",
                dec_id,
                actor,
                {"fields": list(updates.keys())},
            )
        return await self.get(dec_id)

    async def supersede_decision(
        self,
        old_decision_id: str,
        new_data: DecisionCreate | DecisionSupersedeBody,
        actor: str = "brain",
    ) -> Decision:
        """Own creation and transition together; exact retries return the same successor."""
        self._validate_actor(actor)
        intent = {"data": new_data.model_dump(mode="json"), "actor": actor}
        async with self.db.transaction():
            old = await self.get(old_decision_id)
            if old is None:
                raise ValueError("decision not found in project")
            previous = await self.db.fetchone(
                "SELECT details FROM audit_log WHERE project_id = ? AND entity_type = 'decision' AND entity_id = ? "
                "AND action = 'update' AND json_extract(details, '$.action') = 'supersede_decision' ORDER BY id DESC LIMIT 1",
                [self.project_id, old_decision_id])
            if previous:
                receipt = json.loads(previous["details"])
                if receipt["intent"] == intent:
                    successor = await self.get(receipt["successor_id"])
                    if successor is None:
                        raise ValueError("supersession receipt target is missing")
                    return successor
                raise ValueError("decision already superseded with different intent")
            if old.status not in {"active", "revisit"} or old.superseded_by:
                raise ValueError("only an active unsuperseded decision may be replaced")
            if new_data.status not in {"active", "revisit"}:
                raise ValueError("the successor must be active or revisit")
            result = await self._supersede_decision_in_transaction(old_decision_id, new_data, actor)
            await self.audit("update", "decision", old_decision_id, actor, {
                "action": "supersede_decision", "intent": intent, "successor_id": result.id,
                "before_revision": old.scope_version, "after_revision": result.scope_version,
            })
            return result

    async def _supersede_decision_in_transaction(
        self,
        old_decision_id: str,
        new_data: DecisionCreate | DecisionSupersedeBody,
        actor: str = "brain",
    ) -> Decision:
        """Atomically supersede a decision and flag affected knowledge for Brain review.

        1. Mark old decision as superseded
        2. Create new decision with incremented scope_version
        3. Find journal entries linked to old decision
        4. Mark claims from those entries as stale
        5. Mark clusters whose member-claims came from those entries as needs_reprocessing
        6. Insert a review_queue row tagged 're_distill_review' so Brain re-extracts
           claims during maintenance. (Re-distillation is a Brain task; this is a
           bookkeeper.)

        v2.7.0.6 — `phase` inheritance. When the incoming `new_data.phase`
        is empty or None, inherit from the OLD decision's phase.
        Semantic: supersede 'overturns the decision in its original phase
        slot'. Callers crossing phases must supply `phase` explicitly.
        Inheritance guard: if BOTH old.phase and new_data.phase are empty,
        we raise ValueError pointing at `rka admin repair-supersedes` —
        that admin command is the appropriate path for chains that pre-date
        the inheritance landing.
        """
        old = await self.get(old_decision_id)
        if old is None:
            raise ValueError(f"Decision {old_decision_id} not found")

        # v2.7.0.6 — phase inheritance. Branches on incoming data shape:
        # DecisionSupersedeBody allows phase=None; DecisionCreate enforces
        # `phase: str` so the only empty case there is "". Treat both as
        # "Brain omitted phase".
        incoming_phase = getattr(new_data, "phase", None) or ""
        if not incoming_phase:
            if not old.phase:
                raise ValueError(
                    f"Cannot supersede {old_decision_id}: both old.phase and "
                    f"new_data.phase are empty. Supply an explicit phase, or "
                    f"if this is a v2.7.0.4-era orphan, run "
                    f"`rka admin repair-supersedes` instead."
                )
            new_data = new_data.model_copy(update={"phase": old.phase})
        # Coerce DecisionSupersedeBody to DecisionCreate for self.create —
        # `phase` is guaranteed non-empty by the block above.
        if isinstance(new_data, DecisionSupersedeBody):
            new_data = DecisionCreate(**new_data.model_dump())

        # The outer aggregate transaction also owns this nested creation.
        new_decision = await self.create(new_data, actor=actor)

        # Validate the actor once, before the transaction, so an invalid actor
        # raises cleanly rather than mid-transaction.
        actor_v = self._validate_actor(actor)

        # Wrap all post-create supersede bookkeeping in one managed
        # transaction. Existing service-helper commits are deferred by
        # Database.transaction(), so any future helper extraction preserves
        # this aggregate boundary.
        new_version = (old.scope_version or 1) + 1
        now = _now()
        link_id = generate_id("link")
        event_id = generate_id("event")

        async with self.db.transaction():
            # Scope-version bump on the new decision (drives change feeds +
            # staleness propagation).
            await self.db.execute(
                "UPDATE decisions SET scope_version = ?, updated_at = ? "
                "WHERE id = ? AND project_id = ?",
                [new_version, now, new_decision.id, self.project_id],
            )
            # Mark old decision superseded (+ FK to the new head). Both columns
            # set in one UPDATE — the admin-repair orphan signature
            # (status='superseded' AND superseded_by IS NULL) is therefore
            # never crash-reachable from the live path.
            await self.db.execute(
                "UPDATE decisions SET status = 'superseded', superseded_by = ?, "
                "updated_at = ? WHERE id = ? AND project_id = ?",
                [new_decision.id, now, old_decision_id, self.project_id],
            )
            # supersedes entity-link (inlined add_link, INSERT OR IGNORE for
            # idempotency).
            await self.db.execute(
                """INSERT OR IGNORE INTO entity_links
                   (id, source_type, source_id, link_type, target_type, target_id,
                    created_by, project_id)
                   VALUES (?, 'decision', ?, 'supersedes', 'decision', ?, ?, ?)""",
                [link_id, new_decision.id, old_decision_id, actor_v, self.project_id],
            )

            from rka.services.lifecycle import apply_decision_impact
            affected_entry_ids = await apply_decision_impact(
                self.db, self.project_id, old_decision_id, new_decision.id, actor_v, now)

            # decision_superseded event (inlined emit_event).
            await self.db.execute(
                """INSERT INTO events
                   (id, event_type, entity_type, entity_id, actor, summary,
                    caused_by_event, caused_by_entity, phase, details, project_id)
                   VALUES (?, 'decision_superseded', 'decision', ?, ?, ?, NULL, NULL, NULL, ?, ?)""",
                [
                    event_id, old_decision_id, actor_v,
                    f"Decision superseded by {new_decision.id}: {new_data.question[:80]}",
                    json.dumps({
                        "new_decision_id": new_decision.id,
                        "affected_entries": len(affected_entry_ids),
                    }),
                    self.project_id,
                ],
            )

            # Flag for Brain re-distillation review.
            if affected_entry_ids:
                review_id = generate_id("review")
                await self.db.execute(
                    """INSERT OR IGNORE INTO review_queue
                       (id, item_type, item_id, flag, context, priority, project_id)
                       VALUES (?, 'decision', ?, 're_distill_review', ?, 60, ?)""",
                    [
                        review_id, new_decision.id,
                        json.dumps({
                            "old_decision_id": old_decision_id,
                            "affected_entries": list(affected_entry_ids),
                        }),
                        self.project_id,
                    ],
                )

        return await self.get(new_decision.id)

    async def get_tree(self, phase: str | None = None, active_only: bool = False) -> list[DecisionTreeNode]:
        """Build the decision tree as nested nodes."""
        conditions = ["project_id = ?"]
        params = [self.project_id]
        if phase:
            conditions.append("phase = ?")
            params.append(phase)
        if active_only:
            conditions.append("status = 'active'")

        where = " AND ".join(conditions) if conditions else "1=1"
        rows = await self.db.fetchall(
            f"SELECT id, parent_id, question, status, chosen, phase FROM decisions WHERE {where} ORDER BY created_at",
            params,
        )

        # Build tree from flat list
        nodes_by_id = {}
        children_map = defaultdict(list)

        for row in rows:
            node = DecisionTreeNode(
                id=row["id"],
                question=row["question"],
                status=row["status"],
                chosen=row.get("chosen"),
                phase=row["phase"],
            )
            nodes_by_id[row["id"]] = node
            parent = row.get("parent_id")
            if parent:
                children_map[parent].append(row["id"])

        # Attach children
        for parent_id, child_ids in children_map.items():
            if parent_id in nodes_by_id:
                nodes_by_id[parent_id].children = [
                    nodes_by_id[cid] for cid in child_ids if cid in nodes_by_id
                ]

        # Return root nodes (no parent)
        roots = [
            nodes_by_id[row["id"]]
            for row in rows
            if not row.get("parent_id") or row["parent_id"] not in nodes_by_id
        ]
        return roots

    async def _row_to_model(self, row: dict) -> Decision:
        tags = await self._get_tags("decision", row["id"])
        enrichment_status = await self._get_enrichment_status("decision", row["id"])
        options = None
        if row.get("options"):
            raw = self._json_loads(row["options"], [])
            options = [DecisionOption(**o) for o in raw]

        return Decision(
            id=row["id"],
            project_id=row["project_id"],
            parent_id=row.get("parent_id"),
            phase=row["phase"],
            question=row["question"],
            options=options,
            chosen=row.get("chosen"),
            rationale=row.get("rationale"),
            decided_by=row["decided_by"],
            status=row["status"],
            abandonment_reason=row.get("abandonment_reason"),
            related_missions=self._json_loads(row.get("related_missions")),
            related_literature=self._json_loads(row.get("related_literature")),
            related_journal=self._json_loads(row.get("related_journal")),
            superseded_by=row.get("superseded_by"),
            scope_version=row.get("scope_version", 1),
            kind=row.get("kind", "decision"),
            tags=tags,
            assumptions=self._json_loads(row.get("assumptions")),
            recommended_option_id=row.get("recommended_option_id"),
            pi_selected_option_id=row.get("pi_selected_option_id"),
            pi_override_rationale=row.get("pi_override_rationale"),
            presentation_method=row.get("presentation_method"),
            enrichment_status=enrichment_status,
            created_at=row.get("created_at"),
            updated_at=row.get("updated_at"),
        )

    # ---- Background job handlers ----

    async def process_auto_tag_job(self, dec_id: str) -> dict[str, str | int]:
        """Generate tags for a decision when none are present."""
        row = await self.db.fetchone(
            "SELECT question FROM decisions WHERE id = ? AND project_id = ?",
            [dec_id, self.project_id],
        )
        if row is None:
            return {"outcome": "missing"}
        if not self.llm:
            return {"outcome": "skipped", "reason": "llm_disabled"}

        existing_tags = await self._get_tags("decision", dec_id)
        if existing_tags:
            return {"outcome": "skipped", "reason": "tags_present"}

        auto_tags = await self._auto_enrich_tags(row["question"], existing_tags)
        if not auto_tags:
            return {"outcome": "noop"}

        await self._set_tags("decision", dec_id, auto_tags)
        return {"outcome": "updated", "tag_count": len(auto_tags)}

    async def process_embedding_job(self, dec_id: str) -> dict[str, str | int]:
        """Generate or refresh the decision embedding."""
        row = await self.db.fetchone(
            "SELECT question, rationale FROM decisions WHERE id = ? AND project_id = ?",
            [dec_id, self.project_id],
        )
        if row is None:
            return {"outcome": "missing"}
        if not self.embeddings:
            return {"outcome": "skipped", "reason": "embeddings_disabled"}

        from rka.infra.embedding_documents import compose_document

        text = compose_document("decision", row)
        if not text:
            return {"outcome": "skipped", "reason": "empty"}

        await self.embeddings.embed_and_store("decision", dec_id, text, project_id=self.project_id)
        return {"outcome": "updated", "char_count": len(text)}
