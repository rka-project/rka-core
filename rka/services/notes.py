"""Journal (notes) service."""

from __future__ import annotations

import logging

from rka.infra.ids import generate_id
from rka.models.journal import JournalEntry, JournalEntryCreate, JournalEntryUpdate
from rka.services.base import BaseService, _now
from rka.services.jobs import JobQueue

logger = logging.getLogger(__name__)


class NoteNotFoundError(ValueError):
    """Raised when a journal entry is absent from the active project scope."""


class NoteService(BaseService):
    """Manages research journal entries."""

    def _job_dedupe_key(self, entry_id: str, operation: str) -> str:
        return f"{self.project_id}:journal:{entry_id}:{operation}"

    async def _enqueue_enrichment_jobs(
        self,
        entry_id: str,
        *,
        include_embedding: bool,
    ) -> None:
        if not include_embedding:
            return
        queue = JobQueue(self.db)
        await queue.enqueue(
            "note_embed",
            project_id=self.project_id,
            entity_type="journal",
            entity_id=entry_id,
            dedupe_key=self._job_dedupe_key(entry_id, "embed"),
            priority=110,
        )

    async def create(self, data: JournalEntryCreate, actor: str | None = None) -> JournalEntry:
        """Create a new journal entry."""
        entry_id = generate_id("journal")
        # An import executor is not the author of the supplied note. Keep
        # source/verbatim attribution intact and use actor only for execution
        # provenance (events, audit and graph links).
        source = data.source
        actor_val = self._validate_actor(actor if actor is not None else source)
        related_decisions = (
            None
            if data.related_decisions is None
            else self._canonical_link_ids(data.related_decisions)
        )
        related_literature = (
            None
            if data.related_literature is None
            else self._canonical_link_ids(data.related_literature)
        )

        async with self.db.transaction():
            await self.db.execute(
                """INSERT INTO journal
                   (id, type, content, summary, source, phase, verbatim_input,
                    related_decisions, related_literature, related_mission,
                    supersedes, confidence, importance, status, pinned, project_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    entry_id,
                    data.type,
                    data.content,
                    data.summary,
                    source,
                    data.phase,
                    data.verbatim_input,
                    self._json_dumps(related_decisions),
                    self._json_dumps(related_literature),
                    data.related_mission,
                    data.supersedes,
                    data.confidence,
                    data.importance,
                    data.status,
                    int(data.pinned),
                    self.project_id,
                ],
            )

            # If superseding another entry, update the back-reference.
            if data.supersedes:
                # `status` is what list() filters on for hide_superseded
                # (see the `status != 'superseded'` condition below); setting
                # only `confidence` meant the flag never hid anything. 26
                # superseded entries were live, 0 of them filterable.
                # `decisions.py` has always set status here.
                await self.db.execute(
                    "UPDATE journal SET superseded_by = ?, confidence = 'superseded', "
                    "status = 'superseded', updated_at = ? "
                    "WHERE id = ? AND project_id = ?",
                    [entry_id, _now(), data.supersedes, self.project_id],
                )

            # Save user-provided tags immediately; auto-tags are deferred.
            if data.tags:
                await self._set_tags("journal", entry_id, data.tags)

            await self._replace_outgoing_links(
                source_type="journal",
                source_id=entry_id,
                link_type="references",
                target_type="decision",
                target_ids=related_decisions,
                created_by=actor_val,
            )
            await self._replace_outgoing_links(
                source_type="journal",
                source_id=entry_id,
                link_type="cites",
                target_type="literature",
                target_ids=related_literature,
                created_by=actor_val,
            )
            await self._replace_incoming_links(
                target_type="journal",
                target_id=entry_id,
                link_type="produced",
                source_type="mission",
                source_ids=[data.related_mission] if data.related_mission else [],
                created_by=actor_val,
            )

            # Materialize supersession for graph-only traversal surfaces.
            if data.supersedes:
                await self.add_link(
                    "journal",
                    entry_id,
                    "supersedes",
                    "journal",
                    data.supersedes,
                    created_by=actor_val,
                )

            # Sync cheap deterministic FTS now; enrichment is queued.
            # `summary` is the condensed restatement — the text most likely
            # to be searched for. Passing "" here left it unsearchable until
            # someone edited the entry or ran a reindex; update() has always
            # passed the real row.
            await self._sync_fts(
                "journal",
                entry_id,
                {"content": data.content, "summary": data.summary or ""},
            )
            await self._enqueue_enrichment_jobs(
                entry_id,
                include_embedding=bool(self.embeddings),
            )

            event_type_map = {
                "note": "finding_recorded",
                "log": "finding_recorded",
                "directive": "pi_instruction",
                "finding": "finding_recorded",
                "insight": "insight_recorded",
                "pi_instruction": "pi_instruction",
            }
            event_type = event_type_map.get(data.type, "finding_recorded")
            await self.emit_event(
                event_type=event_type,
                entity_type="journal",
                entity_id=entry_id,
                actor=actor_val,
                summary=f"{data.type}: {data.content[:100]}",
                phase=data.phase,
            )
            await self.audit("create", "journal", entry_id, actor_val)

            # Hook failures remain non-fatal, but their successful writes now
            # share the journal aggregate's transaction.
            try:
                from rka.services.hook_dispatcher import HookDispatcher

                await HookDispatcher(self.db).fire(
                    event="post_journal_create",
                    payload={
                        "entry_id": entry_id,
                        "type": data.type,
                        "importance": data.importance,
                        "tags": list(data.tags or []),
                        "source": source,
                    },
                    project_id=self.project_id,
                )
            except Exception as exc:  # extra defense-in-depth
                logger.warning("post_journal_create hook fire failed: %s", exc)

        return await self.get(entry_id)

    async def get(self, entry_id: str) -> JournalEntry | None:
        """Get a single journal entry by ID."""
        row = await self.db.fetchone(
            "SELECT * FROM journal WHERE id = ? AND project_id = ?",
            [entry_id, self.project_id],
        )
        if row is None:
            return None
        return await self._row_to_model(row)

    async def list(
        self,
        type: str | None = None,
        phase: str | None = None,
        confidence: str | None = None,
        importance: str | None = None,
        source: str | None = None,
        status: str | None = None,
        since: str | None = None,
        hide_superseded: bool = True,
        limit: int = 50,
        offset: int = 0,
    ) -> list[JournalEntry]:
        """List journal entries with filters."""
        conditions = []
        params = [self.project_id]

        conditions.append("project_id = ?")

        if type:
            conditions.append("type = ?")
            params.append(type)
        if phase:
            conditions.append("phase = ?")
            params.append(phase)
        if confidence:
            conditions.append("confidence = ?")
            params.append(confidence)
        if importance:
            conditions.append("importance = ?")
            params.append(importance)
        if source:
            conditions.append("source = ?")
            params.append(source)
        if status:
            conditions.append("status = ?")
            params.append(status)
        if since:
            conditions.append("created_at >= ?")
            params.append(since)
        if hide_superseded:
            conditions.append("status != 'superseded'")

        where = " AND ".join(conditions) if conditions else "1=1"
        params.extend([limit, offset])

        rows = await self.db.fetchall(
            f"SELECT * FROM journal WHERE {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            params,
        )
        return [await self._row_to_model(row) for row in rows]

    async def update(self, entry_id: str, data: JournalEntryUpdate) -> JournalEntry:
        """Update a journal entry."""
        dump = data.model_dump(exclude_none=True)
        tags = dump.pop("tags", None)
        replace_related_decisions = "related_decisions" in dump
        replace_related_literature = "related_literature" in dump
        replace_related_mission = "related_mission" in dump
        if replace_related_decisions:
            dump["related_decisions"] = self._canonical_link_ids(
                dump["related_decisions"]
            )
        if replace_related_literature:
            dump["related_literature"] = self._canonical_link_ids(
                dump["related_literature"]
            )

        updates = {}
        for field, value in dump.items():
            if field in ("related_decisions", "related_literature"):
                updates[field] = self._json_dumps(value)
            else:
                updates[field] = value

        async with self.db.transaction():
            owned = await self.db.fetchone(
                "SELECT id FROM journal WHERE id = ? AND project_id = ?",
                [entry_id, self.project_id],
            )
            if owned is None:
                raise NoteNotFoundError(
                    f"journal entry {entry_id!r} not found in project "
                    f"{self.project_id}"
                )

            if tags is not None:
                await self._set_tags("journal", entry_id, tags)

            if not updates:
                current = await self.get(entry_id)
                if current is None:  # pragma: no cover - protected by write lock
                    raise RuntimeError("journal update target disappeared")
                return current

            updates["updated_at"] = _now()
            set_clause = ", ".join(f"{k} = ?" for k in updates)
            values = list(updates.values()) + [entry_id]
            cursor = await self.db.execute(
                f"UPDATE journal SET {set_clause} WHERE id = ? AND project_id = ?",
                values + [self.project_id],
            )
            if cursor.rowcount != 1:  # pragma: no cover - protected by write lock
                raise NoteNotFoundError(
                    f"journal entry {entry_id!r} not found in project "
                    f"{self.project_id}"
                )

            if replace_related_decisions:
                await self._replace_outgoing_links(
                    source_type="journal",
                    source_id=entry_id,
                    link_type="references",
                    target_type="decision",
                    target_ids=dump["related_decisions"],
                )
            if replace_related_literature:
                await self._replace_outgoing_links(
                    source_type="journal",
                    source_id=entry_id,
                    link_type="cites",
                    target_type="literature",
                    target_ids=dump["related_literature"],
                )
            if replace_related_mission:
                await self._replace_incoming_links(
                    target_type="journal",
                    target_id=entry_id,
                    link_type="produced",
                    source_type="mission",
                    source_ids=[data.related_mission] if data.related_mission else [],
                )

            # Re-sync FTS on content changes; defer embedding to job queue.
            if "content" in updates or "summary" in updates:
                row = await self.db.fetchone(
                    "SELECT content, summary FROM journal "
                    "WHERE id = ? AND project_id = ?",
                    [entry_id, self.project_id],
                )
                if row:
                    await self._sync_fts("journal", entry_id, dict(row))
                    if self.embeddings:
                        queue = JobQueue(self.db)
                        await queue.enqueue(
                            "note_embed",
                            project_id=self.project_id,
                            entity_type="journal",
                            entity_id=entry_id,
                            dedupe_key=self._job_dedupe_key(entry_id, "embed"),
                            priority=110,
                        )

            await self.audit(
                "update",
                "journal",
                entry_id,
                "system",
                {"fields": list(updates.keys())},
            )
        return await self.get(entry_id)

    async def _row_to_model(self, row: dict) -> JournalEntry:
        tags = await self._get_tags("journal", row["id"])
        enrichment_status = await self._get_enrichment_status("journal", row["id"])
        return JournalEntry(
            id=row["id"],
            project_id=row["project_id"],
            type=row["type"],
            content=row["content"],
            summary=row.get("summary"),
            source=row["source"],
            phase=row.get("phase"),
            verbatim_input=row.get("verbatim_input"),
            related_decisions=self._json_loads(row.get("related_decisions")),
            related_literature=self._json_loads(row.get("related_literature")),
            related_mission=row.get("related_mission"),
            supersedes=row.get("supersedes"),
            superseded_by=row.get("superseded_by"),
            confidence=row["confidence"],
            importance=row["importance"],
            status=row.get("status", "active"),
            pinned=bool(row.get("pinned", 0)),
            tags=tags,
            enrichment_status=enrichment_status,
            created_at=row.get("created_at"),
            updated_at=row.get("updated_at"),
        )

    # ---- Background job handlers ----

    async def process_auto_tag_job(self, entry_id: str) -> dict[str, str | int]:
        """Generate tags for a journal entry when none are present."""
        row = await self.db.fetchone(
            "SELECT content FROM journal WHERE id = ? AND project_id = ?",
            [entry_id, self.project_id],
        )
        if row is None:
            return {"outcome": "missing"}
        if not self.llm:
            return {"outcome": "skipped", "reason": "llm_disabled"}

        existing_tags = await self._get_tags("journal", entry_id)
        if existing_tags:
            return {"outcome": "skipped", "reason": "tags_present"}

        auto_tags = await self._auto_enrich_tags(row["content"], existing_tags)
        if not auto_tags:
            return {"outcome": "noop"}

        await self._set_tags("journal", entry_id, auto_tags)
        return {"outcome": "updated", "tag_count": len(auto_tags)}

    async def process_auto_link_job(self, entry_id: str) -> dict[str, str | int]:
        """Infer entity links for a journal entry via LLM."""
        row = await self.db.fetchone(
            "SELECT content, type, source, related_decisions, related_literature, related_mission "
            "FROM journal WHERE id = ? AND project_id = ?",
            [entry_id, self.project_id],
        )
        if row is None:
            return {"outcome": "missing"}
        if not self.llm:
            return {"outcome": "skipped", "reason": "llm_disabled"}

        links = await self._auto_link(row["content"], row["type"])
        if not links:
            return {"outcome": "noop"}

        link_updates: dict = {}
        if links.related_decision_ids:
            link_updates["related_decisions"] = self._json_dumps(links.related_decision_ids)
        if links.related_literature_ids:
            link_updates["related_literature"] = self._json_dumps(links.related_literature_ids)
        if links.related_mission_id:
            link_updates["related_mission"] = links.related_mission_id
        if links.suggested_type and links.suggested_type != row["type"]:
            link_updates["type"] = links.suggested_type

        source = row.get("source") or "system"
        async with self.db.transaction():
            if link_updates:
                set_clause = ", ".join(f"{k} = ?" for k in link_updates)
                await self.db.execute(
                    f"UPDATE journal SET {set_clause} "
                    "WHERE id = ? AND project_id = ?",
                    list(link_updates.values()) + [entry_id, self.project_id],
                )

            final_row = await self.db.fetchone(
                "SELECT related_decisions, related_literature, related_mission "
                "FROM journal WHERE id = ? AND project_id = ?",
                [entry_id, self.project_id],
            )
            if final_row:
                await self._replace_outgoing_links(
                    source_type="journal",
                    source_id=entry_id,
                    link_type="references",
                    target_type="decision",
                    target_ids=self._json_loads(
                        final_row.get("related_decisions"),
                        [],
                    ),
                    created_by=source,
                )
                await self._replace_outgoing_links(
                    source_type="journal",
                    source_id=entry_id,
                    link_type="cites",
                    target_type="literature",
                    target_ids=self._json_loads(
                        final_row.get("related_literature"),
                        [],
                    ),
                    created_by=source,
                )
                await self._replace_incoming_links(
                    target_type="journal",
                    target_id=entry_id,
                    link_type="produced",
                    source_type="mission",
                    source_ids=(
                        [final_row["related_mission"]]
                        if final_row.get("related_mission")
                        else []
                    ),
                    created_by=source,
                )

        link_count = len(link_updates)
        return {"outcome": "updated" if link_count else "noop", "link_count": link_count}

    async def process_auto_summarize_job(self, entry_id: str) -> dict[str, str | int]:
        """Generate a one-line summary for a journal entry via LLM."""
        row = await self.db.fetchone(
            "SELECT content FROM journal WHERE id = ? AND project_id = ?",
            [entry_id, self.project_id],
        )
        if row is None:
            return {"outcome": "missing"}
        if not self.llm:
            return {"outcome": "skipped", "reason": "llm_disabled"}

        summary = await self._auto_summarize(row["content"])
        if not summary:
            return {"outcome": "noop"}

        async with self.db.transaction():
            await self.db.execute(
                "UPDATE journal SET summary = ? WHERE id = ? AND project_id = ?",
                [summary, entry_id, self.project_id],
            )

            # Keep the source mutation and its searchable projection in one
            # managed unit. The LLM call above remains outside the transaction.
            await self._sync_fts("journal", entry_id, {
                "content": row["content"], "summary": summary,
            })

        return {"outcome": "updated", "summary_len": len(summary)}

    async def process_embedding_job(self, entry_id: str) -> dict[str, str | int]:
        """Generate or refresh the journal entry embedding."""
        row = await self.db.fetchone(
            "SELECT content, summary FROM journal WHERE id = ? AND project_id = ?",
            [entry_id, self.project_id],
        )
        if row is None:
            return {"outcome": "missing"}
        if not self.embeddings:
            return {"outcome": "skipped", "reason": "embeddings_disabled"}

        from rka.infra.embedding_documents import compose_document

        text = compose_document("journal", row)
        if not text:
            return {"outcome": "skipped", "reason": "empty"}

        await self.embeddings.embed_and_store("journal", entry_id, text, project_id=self.project_id)
        return {"outcome": "updated", "char_count": len(text)}
