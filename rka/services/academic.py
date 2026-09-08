"""Academic import services — BibTeX parsing, DOI lookup, document ingestion, batch import."""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from rka.models.literature import LiteratureCreate
from rka.models.journal import JournalCaptureMode, JournalEntryCreate
from rka.services.literature import LiteratureService
from rka.infra.file_access import require_bounded_text
from rka.services.bibtex import BibtexParseError, parse_basic_bibtex

if TYPE_CHECKING:
    from rka.services.notes import NoteService

logger = logging.getLogger(__name__)


class AcademicImportService:
    """Import and enrich literature from academic sources."""

    def __init__(self, lit_service: LiteratureService, note_service: "NoteService | None" = None, *, file_policy=None):
        self.lit = lit_service
        self._note_svc = note_service
        from rka.infra.file_access import FileAccessPolicy
        self.file_policy = file_policy if file_policy is not None else FileAccessPolicy()

    # ---- BibTeX Import ----

    async def import_bibtex(
        self,
        bibtex_content: str,
        default_status: str = "to_read",
        added_by: str = "import",
        skip_duplicates: bool = True,
    ) -> dict:
        """Parse BibTeX string and create literature entries.

        Returns:
            Dict with imported, skipped, errors counts and details.
        """
        require_bounded_text(bibtex_content)
        try:
            entries = self._parse_bibtex(bibtex_content)
        except BibtexParseError as exc:
            return {"imported": [], "skipped": [], "errors": [
                {"title": "BibTeX input", "code": "bibtex_parse_error", "error": str(exc)},
            ], "total_parsed": 0}
        results = {"imported": [], "skipped": [], "errors": [], "total_parsed": len(entries)}

        for entry in entries:
            try:
                # Check for duplicate by DOI
                if skip_duplicates and entry.get("doi"):
                    existing = await self.lit.db.fetchone(
                        "SELECT id FROM literature WHERE doi = ? AND project_id = ?",
                        [entry["doi"], self.lit.project_id],
                    )
                    if existing:
                        results["skipped"].append({
                            "title": entry.get("title", "Unknown"),
                            "reason": f"Duplicate DOI: {entry['doi']}",
                        })
                        continue

                # Check for duplicate by title (fuzzy)
                if skip_duplicates and entry.get("title"):
                    existing = await self.lit.db.fetchone(
                        "SELECT id FROM literature WHERE LOWER(title) = LOWER(?) AND project_id = ?",
                        [entry["title"], self.lit.project_id],
                    )
                    if existing:
                        results["skipped"].append({
                            "title": entry["title"],
                            "reason": "Duplicate title",
                        })
                        continue

                data = LiteratureCreate(
                    title=entry.get("title", "Untitled"),
                    authors=entry.get("authors"),
                    year=entry.get("year"),
                    venue=entry.get("venue"),
                    doi=entry.get("doi"),
                    url=entry.get("url"),
                    bibtex=entry.get("raw_bibtex"),
                    abstract=entry.get("abstract"),
                    status=default_status,
                    added_by=added_by,
                )
                # Import origin is retained on the record; event actors use
                # the existing execution vocabulary, not a new privilege.
                actor = "system" if added_by == "import" else added_by
                lit = await self.lit.create(data, actor=actor)
                results["imported"].append({"id": lit.id, "title": lit.title})

            except Exception as exc:
                results["errors"].append({
                    "title": entry.get("title", "Unknown"),
                    "error": str(exc),
                })

        return results

    async def import_bibtex_file(
        self, file_path: str, **kwargs
    ) -> dict:
        """Import from a .bib file path."""
        content = self.file_policy.read_bytes(file_path, max_bytes=2 * 1024 * 1024).decode("utf-8")
        return await self.import_bibtex(content, **kwargs)

    def _parse_bibtex(self, content: str) -> list[dict]:
        """Parse BibTeX content into a list of entry dicts.

        Prefer the academic extra; use a bounded subset only when it is absent.
        """
        try:
            return self._parse_bibtex_with_library(content)
        except ModuleNotFoundError as exc:
            if exc.name != "bibtexparser":
                raise
            logger.debug("bibtexparser not installed, using basic parser")
            return self._parse_bibtex_regex(content)

    def _parse_bibtex_with_library(self, content: str) -> list[dict]:
        """Parse using bibtexparser library."""
        import bibtexparser

        library = bibtexparser.parse_string(content)
        if library.failed_blocks:
            raise BibtexParseError(
                f"BibTeX contains {len(library.failed_blocks)} malformed or duplicate blocks; no entries imported"
            )
        entries = []
        for entry in library.entries:
            fields = {key.lower(): field for key, field in entry.fields_dict.items()}
            parsed = {
                "title": self._clean_bibtex_value(fields.get("title", {}).value if "title" in fields else ""),
                "authors": self._parse_bibtex_authors(
                    fields.get("author", {}).value if "author" in fields else ""
                ),
                "year": self._safe_int(fields.get("year", {}).value if "year" in fields else None),
                "venue": self._clean_bibtex_value(
                    fields.get("journal", {}).value if "journal" in fields
                    else fields.get("booktitle", {}).value if "booktitle" in fields
                    else ""
                ),
                "doi": self._clean_bibtex_value(fields.get("doi", {}).value if "doi" in fields else ""),
                "url": self._clean_bibtex_value(fields.get("url", {}).value if "url" in fields else ""),
                "abstract": self._clean_bibtex_value(
                    fields.get("abstract", {}).value if "abstract" in fields else ""
                ),
                "raw_bibtex": entry.raw,
            }
            entries.append({k: v for k, v in parsed.items() if v})
        return entries

    def _parse_bibtex_regex(self, content: str) -> list[dict]:
        """Compatibility name for the dependency-free balanced-value parser."""
        entries = []
        for entry in parse_basic_bibtex(content):
            fields = entry["fields"]
            parsed = {
                "title": self._clean_bibtex_value(fields.get("title", "")),
                "authors": self._parse_bibtex_authors(fields.get("author", "")),
                "year": self._safe_int(fields.get("year")),
                "venue": self._clean_bibtex_value(
                    fields.get("journal", "") or fields.get("booktitle", "")
                ),
                "doi": self._clean_bibtex_value(fields.get("doi", "")),
                "url": self._clean_bibtex_value(fields.get("url", "")),
                "abstract": self._clean_bibtex_value(fields.get("abstract", "")),
                "raw_bibtex": entry["raw"],
            }
            entries.append({k: v for k, v in parsed.items() if v})

        return entries

    @staticmethod
    def _clean_bibtex_value(val: str) -> str:
        """Remove BibTeX braces and extra whitespace."""
        if not val:
            return ""
        return re.sub(r"\s+", " ", val.replace("{", "").replace("}", "")).strip()

    @staticmethod
    def _parse_bibtex_authors(author_str: str) -> list[str]:
        """Parse BibTeX author string into list of names."""
        if not author_str:
            return []
        # BibTeX separates authors with " and "
        authors = re.split(r"\s+and\s+", author_str)
        return [
            re.sub(r"\s+", " ", a.replace("{", "").replace("}", "")).strip()
            for a in authors if a.strip()
        ]

    @staticmethod
    def _safe_int(val) -> int | None:
        if val is None:
            return None
        try:
            return int(str(val).strip())
        except (ValueError, TypeError):
            return None

    # ---- DOI Enrichment ----

    async def enrich_from_doi(self, lit_id: str) -> dict:
        """Enrich a literature entry by looking up its DOI via CrossRef.

        Requires: habanero package (pip install habanero).
        """
        lit = await self.lit.get(lit_id)
        if not lit or not lit.doi:
            return {"error": "Literature entry not found or has no DOI"}

        try:
            from habanero import Crossref
            cr = Crossref()
            result = cr.works(ids=lit.doi)
            msg = result.get("message", {})

            # Extract metadata
            updates = {}
            if not lit.title or lit.title == "Untitled":
                titles = msg.get("title", [])
                if titles:
                    updates["title"] = titles[0]

            if not lit.authors:
                author_list = msg.get("author", [])
                if author_list:
                    updates["authors"] = [
                        f"{a.get('given', '')} {a.get('family', '')}".strip()
                        for a in author_list
                    ]

            if not lit.year:
                issued = msg.get("issued", {}).get("date-parts", [[]])
                if issued and issued[0]:
                    updates["year"] = issued[0][0]

            if not lit.venue:
                venue = msg.get("container-title", [])
                if venue:
                    updates["venue"] = venue[0]

            if not lit.abstract:
                abstract = msg.get("abstract", "")
                if abstract:
                    # Clean HTML tags from CrossRef abstracts
                    updates["abstract"] = re.sub(r"<[^>]+>", "", abstract).strip()

            if not lit.url:
                url = msg.get("URL", "")
                if url:
                    updates["url"] = url

            if not updates:
                return {"status": "no_updates", "message": "All fields already populated"}

            from rka.models.literature import LiteratureUpdate
            await self.lit.update(lit_id, LiteratureUpdate(**updates), actor="system")
            return {"status": "enriched", "fields_updated": list(updates.keys())}

        except ImportError:
            return {"error": "habanero package not installed. Run: pip install habanero"}
        except Exception as exc:
            return {"error": f"DOI lookup failed: {exc}"}

    # ---- Mermaid Export ----

    async def export_decisions_mermaid(
        self, phase: str | None = None, active_only: bool = False
    ) -> str:
        """Export the decision tree as a Mermaid flowchart diagram."""
        from rka.services.decisions import DecisionService

        # We need to access the decision service's DB
        dec_svc = DecisionService(
            self.lit.db,
            llm=self.lit.llm,
            embeddings=self.lit.embeddings,
            project_id=self.lit.project_id,
        )
        tree = await dec_svc.get_tree(phase=phase, active_only=active_only)

        lines = ["graph TD"]
        self._mermaid_node(tree, lines, set())
        return "\n".join(lines)

    def _mermaid_node(self, nodes: list, lines: list[str], seen: set) -> None:
        """Recursively render decision tree nodes as Mermaid."""
        for node in nodes:
            if node.id in seen:
                continue
            seen.add(node.id)

            # Node shape based on status
            safe_q = node.question[:60].replace('"', "'").replace("\n", " ")
            node_id = node.id.replace("-", "_")

            if node.status == "active":
                if node.chosen:
                    lines.append(f'    {node_id}["{safe_q}<br/>✅ {node.chosen}"]')
                else:
                    lines.append(f'    {node_id}{{{{"{safe_q}"}}}}')
                lines.append(f"    style {node_id} fill:#d4edda,stroke:#28a745")
            elif node.status == "abandoned":
                lines.append(f'    {node_id}["{safe_q}<br/>❌ Abandoned"]')
                lines.append(f"    style {node_id} fill:#f8d7da,stroke:#dc3545,stroke-dasharray:5")
            elif node.status == "revisit":
                lines.append(f'    {node_id}{{{{"{safe_q}"}}}}')
                lines.append(f"    style {node_id} fill:#fff3cd,stroke:#ffc107")
            else:
                lines.append(f'    {node_id}["{safe_q}"]')

            # Children edges
            for child in node.children:
                child_id = child.id.replace("-", "_")
                lines.append(f"    {node_id} --> {child_id}")

            self._mermaid_node(node.children, lines, seen)

    # ---- Document Ingestion ----

    async def ingest_document(
        self,
        content: str,
        source: str = "brain",
        default_type: str = "finding",
        phase: str | None = None,
        tags: list[str] | None = None,
        related_literature: list[str] | None = None,
        related_decisions: list[str] | None = None,
        related_mission: str | None = None,
        split_by_headings: bool = True,
        *,
        capture_mode: JournalCaptureMode = "raw_capture",
    ) -> dict:
        """Ingest a markdown document by splitting it into journal entries.

        Splits the document by markdown headings (## or ###) and creates
        one journal entry per section. If split_by_headings is False or
        no headings are found, creates a single entry for the full content.

        Returns:
            Dict with created entries, total count, and any errors.
        """
        require_bounded_text(content)
        if not self._note_svc:
            from rka.services.notes import NoteService

            self._note_svc = NoteService(
                self.lit.db,
                llm=self.lit.llm,
                embeddings=self.lit.embeddings,
                project_id=self.lit.project_id,
            )

        sections = self._split_markdown(content) if split_by_headings else []

        # Fallback: if no sections found, treat the whole document as one entry
        if not sections:
            sections = [{"heading": None, "body": content.strip(), "original": content}]

        results = {"created": [], "errors": [], "total_sections": len(sections)}
        base_tags = tags or []

        for i, section in enumerate(sections):
            body = section["body"].strip()
            if not body:
                continue

            heading = section.get("heading")

            # Determine entry type from heading hints
            entry_type = self._classify_section(heading, body, default_type)

            # Build content: include heading as a prefix if present
            entry_content = f"**{heading}**\n\n{body}" if heading else body

            # Tags: base tags + heading-derived tag
            entry_tags = list(base_tags)
            if heading:
                # Create a tag from the heading (slugified)
                slug = re.sub(r"[^a-z0-9]+", "-", heading.lower()).strip("-")
                if slug and slug not in entry_tags:
                    entry_tags.append(slug)

            try:
                data = JournalEntryCreate(
                    content=entry_content,
                    capture_mode=capture_mode,
                    verbatim_input=section["original"] if capture_mode == "raw_capture" else None,
                    type=entry_type,
                    source=source,
                    phase=phase,
                    related_literature=related_literature,
                    related_decisions=related_decisions,
                    related_mission=related_mission,
                    tags=entry_tags,
                )
                entry = await self._note_svc.create(data, actor=source)
                results["created"].append({
                    "id": entry.id,
                    "type": entry_type,
                    "heading": heading or f"Section {i + 1}",
                    "length": len(body),
                })
            except Exception as exc:
                results["errors"].append({
                    "section": heading or f"Section {i + 1}",
                    "error": str(exc),
                })

        return results

    @staticmethod
    def _split_markdown(content: str) -> list[dict]:
        """Split markdown content by headings (## or ###).

        Returns heading/body for display plus exact original input slices.
        """
        # Match ## or ### headings
        heading_pattern = re.compile(r"^(#{2,3})\s+(.+)$", re.MULTILINE)
        matches = list(heading_pattern.finditer(content))

        if not matches:
            return []

        sections = []

        # Content before the first heading (preamble)
        preamble = content[:matches[0].start()].strip()
        if preamble:
            sections.append({"heading": None, "body": preamble,
                             "original": content[:matches[0].start()]})

        # Each heading + its body
        for i, match in enumerate(matches):
            heading = match.group(2).strip()
            start = match.end()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(content)
            body = content[start:end].strip()
            if body or heading:
                # Include any whitespace-only preamble in the first slice.
                raw_start = 0 if i == 0 and not preamble else match.start()
                sections.append({"heading": heading, "body": body,
                                 "original": content[raw_start:end]})

        return sections

    @staticmethod
    def _classify_section(heading: str | None, body: str, default: str) -> str:
        """Infer journal entry type from heading/body content.

        Returns the default type if no clear classification.
        """
        if not heading:
            return default

        h = heading.lower()

        # Type classification heuristics
        type_hints = {
            "finding": ["finding", "result", "outcome", "evidence", "observed"],
            "methodology": ["method", "approach", "technique", "procedure", "protocol", "design"],
            "insight": ["insight", "implication", "takeaway", "lesson", "reflection"],
            "hypothesis": ["hypothesis", "conjecture", "prediction", "expected"],
            "observation": ["observation", "note", "remark", "noticed"],
            "idea": ["idea", "proposal", "suggestion", "future", "next step", "todo"],
            "exploration": ["exploration", "experiment", "test", "trial", "pilot"],
        }

        for entry_type, keywords in type_hints.items():
            if any(kw in h for kw in keywords):
                return entry_type

        return default
