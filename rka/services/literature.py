"""Literature service."""

from __future__ import annotations

from rka.infra.ids import generate_id
from rka.models.literature import Literature, LiteratureCreate, LiteratureUpdate
from rka.services.base import BaseService, _precise_now
from rka.services.jobs import JobQueue


class LiteratureNotFoundError(ValueError):
    """Raised when a literature entry is absent from the active project scope."""


class LiteratureService(BaseService):
    """Manages literature entries."""

    def _job_dedupe_key(self, lit_id: str, operation: str) -> str:
        return f"{self.project_id}:literature:{lit_id}:{operation}"

    async def _enqueue_enrichment_jobs(
        self,
        lit_id: str,
        *,
        include_embedding: bool,
    ) -> None:
        if not include_embedding:
            return
        queue = JobQueue(self.db)
        await queue.enqueue(
            "literature_embed",
            project_id=self.project_id,
            entity_type="literature",
            entity_id=lit_id,
            dedupe_key=self._job_dedupe_key(lit_id, "embed"),
            priority=110,
        )

    async def create(self, data: LiteratureCreate, actor: str | None = None) -> Literature:
        """Create a new literature entry."""
        lit_id = generate_id("literature")
        # Source attribution is not the event-actor vocabulary. Only derive
        # system for an imported record when no execution actor was supplied.
        source = self._validate_actor(
            actor if actor is not None else "system" if data.added_by == "import" else data.added_by
        )
        related_decisions = (
            None
            if data.related_decisions is None
            else self._canonical_link_ids(data.related_decisions)
        )

        async with self.db.transaction():
            await self.db.execute(
                """INSERT INTO literature
                   (id, title, authors, year, venue, doi, url, bibtex, pdf_path,
                    abstract, status, key_findings, methodology_notes, relevance,
                    relevance_score, related_decisions, added_by, notes,
                    zotero_item_key, zotero_match_method, project_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    lit_id,
                    data.title,
                    self._json_dumps(data.authors),
                    data.year,
                    data.venue,
                    data.doi,
                    data.url,
                    data.bibtex,
                    data.pdf_path,
                    data.abstract,
                    data.status,
                    self._json_dumps(data.key_findings),
                    data.methodology_notes,
                    data.relevance,
                    data.relevance_score,
                    self._json_dumps(related_decisions),
                    data.added_by,
                    data.notes,
                    data.zotero_item_key,
                    data.zotero_match_method,
                    self.project_id,
                ],
            )

            if data.tags:
                await self._set_tags("literature", lit_id, data.tags)

            await self._replace_outgoing_links(
                source_type="literature",
                source_id=lit_id,
                link_type="informed_by",
                target_type="decision",
                target_ids=related_decisions,
                created_by=source,
            )
            await self._sync_fts(
                "literature",
                lit_id,
                {
                    "title": data.title,
                    "abstract": data.abstract,
                    "notes": data.notes,
                },
            )
            await self._enqueue_enrichment_jobs(
                lit_id,
                include_embedding=bool(self.embeddings),
            )
            await self.emit_event(
                event_type="literature_added",
                entity_type="literature",
                entity_id=lit_id,
                actor=source,
                summary=f"Added: {data.title[:100]}",
            )
            await self.audit("create", "literature", lit_id, source)
        return await self.get(lit_id)

    async def get(self, lit_id: str) -> Literature | None:
        """Get a single literature entry by ID."""
        row = await self.db.fetchone(
            "SELECT * FROM literature WHERE id = ? AND project_id = ?",
            [lit_id, self.project_id],
        )
        if row is None:
            return None
        return await self._row_to_model(row)

    async def list(
        self,
        status: str | None = None,
        year_min: int | None = None,
        year_max: int | None = None,
        venue: str | None = None,
        query: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Literature]:
        """List literature with filters."""
        conditions = []
        params = [self.project_id]

        conditions.append("project_id = ?")

        if status:
            conditions.append("status = ?")
            params.append(status)
        if year_min:
            conditions.append("year >= ?")
            params.append(year_min)
        if year_max:
            conditions.append("year <= ?")
            params.append(year_max)
        if venue:
            conditions.append("venue LIKE ?")
            params.append(f"%{venue}%")
        if query:
            conditions.append("(title LIKE ? OR abstract LIKE ?)")
            params.extend([f"%{query}%", f"%{query}%"])

        where = " AND ".join(conditions) if conditions else "1=1"
        params.extend([limit, offset])

        rows = await self.db.fetchall(
            f"SELECT * FROM literature WHERE {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            params,
        )
        return [await self._row_to_model(row) for row in rows]

    async def update(self, lit_id: str, data: LiteratureUpdate, actor: str = "system") -> Literature:
        """Update a literature entry."""
        dump = data.model_dump(exclude_none=True)
        tags = dump.pop("tags", None)
        replace_related_decisions = "related_decisions" in dump
        if replace_related_decisions:
            dump["related_decisions"] = self._canonical_link_ids(
                dump["related_decisions"]
            )

        updates = {}
        for field, value in dump.items():
            if field in ("authors", "key_findings", "related_decisions"):
                updates[field] = self._json_dumps(value)
            else:
                updates[field] = value

        async with self.db.transaction():
            owned = await self.db.fetchone(
                "SELECT id FROM literature WHERE id = ? AND project_id = ?",
                [lit_id, self.project_id],
            )
            if owned is None:
                raise LiteratureNotFoundError(
                    f"literature {lit_id!r} not found in project "
                    f"{self.project_id}"
                )

            if tags is not None:
                await self._set_tags("literature", lit_id, tags)

            if not updates:
                current = await self.get(lit_id)
                if current is None:  # pragma: no cover - protected by write lock
                    raise RuntimeError("literature update target disappeared")
                return current

            updates["updated_at"] = _precise_now()
            set_clause = ", ".join(f"{k} = ?" for k in updates)
            values = list(updates.values()) + [lit_id]
            cursor = await self.db.execute(
                f"UPDATE literature SET {set_clause} WHERE id = ? AND project_id = ?",
                values + [self.project_id],
            )
            if cursor.rowcount != 1:  # pragma: no cover - protected by write lock
                raise LiteratureNotFoundError(
                    f"literature {lit_id!r} not found in project "
                    f"{self.project_id}"
                )

            if replace_related_decisions:
                await self._replace_outgoing_links(
                    source_type="literature",
                    source_id=lit_id,
                    link_type="informed_by",
                    target_type="decision",
                    target_ids=dump["related_decisions"],
                    created_by=actor,
                )

            if data.status == "cited":
                await self.emit_event(
                    event_type="literature_cited",
                    entity_type="literature",
                    entity_id=lit_id,
                    actor=actor,
                    summary="Literature cited",
                )

            if any(f in updates for f in ("title", "abstract", "notes")):
                row = await self.db.fetchone(
                    "SELECT title, abstract, notes FROM literature "
                    "WHERE id = ? AND project_id = ?",
                    [lit_id, self.project_id],
                )
                if row:
                    await self._sync_fts("literature", lit_id, dict(row))
                    await self._enqueue_enrichment_jobs(
                        lit_id,
                        include_embedding=bool(self.embeddings),
                    )

            await self.audit(
                "update",
                "literature",
                lit_id,
                actor,
                {"fields": list(updates.keys())},
            )
        return await self.get(lit_id)

    async def _row_to_model(self, row: dict) -> Literature:
        tags = await self._get_tags("literature", row["id"])
        enrichment_status = await self._get_enrichment_status("literature", row["id"])
        return Literature(
            id=row["id"],
            project_id=row["project_id"],
            title=row["title"],
            authors=self._json_loads(row.get("authors")),
            year=row.get("year"),
            venue=row.get("venue"),
            doi=row.get("doi"),
            url=row.get("url"),
            bibtex=row.get("bibtex"),
            pdf_path=row.get("pdf_path"),
            abstract=row.get("abstract"),
            status=row["status"],
            key_findings=self._json_loads(row.get("key_findings")),
            methodology_notes=row.get("methodology_notes"),
            relevance=row.get("relevance"),
            relevance_score=row.get("relevance_score"),
            related_decisions=self._json_loads(row.get("related_decisions")),
            added_by=row.get("added_by"),
            notes=row.get("notes"),
            tags=tags,
            enrichment_status=enrichment_status,
            zotero_item_key=row.get("zotero_item_key"),
            zotero_match_method=row.get("zotero_match_method"),
            created_at=row.get("created_at"),
            updated_at=row.get("updated_at"),
        )

    # ---- Background job handlers ----

    async def process_auto_tag_job(self, lit_id: str) -> dict[str, str | int]:
        """Generate tags for a literature entry when none are present."""
        row = await self.db.fetchone(
            "SELECT title, abstract FROM literature WHERE id = ? AND project_id = ?",
            [lit_id, self.project_id],
        )
        if row is None:
            return {"outcome": "missing"}
        if not self.llm:
            return {"outcome": "skipped", "reason": "llm_disabled"}

        existing_tags = await self._get_tags("literature", lit_id)
        if existing_tags:
            return {"outcome": "skipped", "reason": "tags_present"}

        text_for_tags = f"{row['title']}. {row.get('abstract') or ''}"
        auto_tags = await self._auto_enrich_tags(text_for_tags, existing_tags)
        if not auto_tags:
            return {"outcome": "noop"}

        await self._set_tags("literature", lit_id, auto_tags)
        return {"outcome": "updated", "tag_count": len(auto_tags)}

    async def process_embedding_job(self, lit_id: str) -> dict[str, str | int]:
        """Generate or refresh the literature embedding."""
        row = await self.db.fetchone(
            "SELECT title, abstract FROM literature WHERE id = ? AND project_id = ?",
            [lit_id, self.project_id],
        )
        if row is None:
            return {"outcome": "missing"}
        if not self.embeddings:
            return {"outcome": "skipped", "reason": "embeddings_disabled"}

        from rka.infra.embedding_documents import compose_document

        text = compose_document("literature", row)
        if not text:
            return {"outcome": "skipped", "reason": "empty"}

        await self.embeddings.embed_and_store("literature", lit_id, text, project_id=self.project_id)
        return {"outcome": "updated", "char_count": len(text)}
