"""Workspace bootstrap service — scan, classify, ingest research files."""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING

from rka.infra.ids import generate_id
from rka.infra.file_access import FileAccessError, FileAccessPolicy, ScanBudget, MAX_SCAN_FILES
from rka.models.workspace import (
    BootstrapReview,
    ContentHint,
    FileCategory,
    HostScanRequest,
    IngestResult,
    IngestionTarget,
    ReviewSuggestion,
    ScanCapabilities,
    ScanManifest,
    ScanSummary,
    ScannedFile,
    WorkspaceIngestRequest,
    WorkspaceIngestResponse,
)
from rka.services.classify import (
    EXTENSION_CATEGORY,
    CATEGORY_TARGET,
    DEFAULT_IGNORES,
    HEADING_PATTERN,
    detect_content_hint,
    hint_to_type,
    hash_file,
    safe_read_text,
    extract_module_docstring,
    extract_pdf_preview,
    extract_pdf_metadata_raw,
    extract_docx_text,
    detect_capabilities,
    is_ignored,
)

if TYPE_CHECKING:
    from rka.infra.database import Database
    from rka.infra.llm import LLMClient
    from rka.services.academic import AcademicImportService
    from rka.services.literature import LiteratureService
    from rka.services.notes import NoteService

logger = logging.getLogger(__name__)

# Consecutive LLM failures before a scan stops asking. One is not evidence of
# a broken backend — `LLMClient.extract` reports a schema-invalid response and
# a refused connection as the same exception — but three in a row is.
_LLM_FAILURE_THRESHOLD = 3


@dataclass
class _LlmScanHealth:
    """Per-scan LLM failure streak.

    Deliberately not stored on the service: two scans can share an instance,
    and one scan's dead backend must not disable another's.
    """

    consecutive_failures: int = 0

    def record_success(self) -> None:
        self.consecutive_failures = 0

# Backward-compat aliases for any code referencing the old private names
_EXTENSION_CATEGORY = EXTENSION_CATEGORY
_CATEGORY_TARGET = CATEGORY_TARGET
_DEFAULT_IGNORES = DEFAULT_IGNORES
_HEADING_PATTERN = HEADING_PATTERN


class WorkspaceService:
    """Scan, classify, and ingest a workspace folder into the knowledge base.

    Designed for the RKA → Brain → Executor workflow:
    1. RKA (this service) does fast scan + ingest with regex + optional LLM classification
    2. Brain reviews the bootstrap via review() and reorganizes
    3. Executor is delegated deep analysis tasks
    """

    def __init__(
        self,
        db: "Database",
        academic_service: "AcademicImportService",
        note_service: "NoteService",
        literature_service: "LiteratureService",
        llm: "LLMClient | None" = None,
        *,
        file_policy=None,
    ):
        self.db = db
        self.academic = academic_service
        self.notes = note_service
        self.lit = literature_service
        self.llm = llm
        self.project_id = note_service.project_id
        self.file_policy = file_policy if file_policy is not None else FileAccessPolicy()

    # ================================================================
    # Public: Scan
    # ================================================================

    async def scan(
        self,
        folder_path: str,
        ignore_patterns: list[str] | None = None,
        include_preview: bool = True,
        max_file_size_mb: float = 50.0,
        use_llm: bool = True,
        max_files: int = 5000,
    ) -> ScanManifest:
        """Scan a folder and classify files for ingestion.

        Args:
            max_files: Requested cap (legacy default 5 000); the operator
                       ceiling is 2 000. Enumeration stops at the cap and
                       returned counts are partial, not a full inventory.

        Returns a ScanManifest (ephemeral, not stored in DB).
        """
        root = self.file_policy.directory(folder_path)
        if not math.isfinite(max_file_size_mb) or max_file_size_mb <= 0 or max_files <= 0:
            raise ValueError("Scan limits must be positive")

        scan_id = generate_id("scan")
        capabilities = self._detect_capabilities(use_llm)
        llm_health = _LlmScanHealth()
        ignores = _DEFAULT_IGNORES | set(ignore_patterns or [])
        max_bytes = int(max_file_size_mb * 1024 * 1024)

        files: list[ScannedFile] = []
        warnings: list[str] = []
        total_found = 0
        budget = ScanBudget(max_files=min(max_files, MAX_SCAN_FILES))

        for path in self.file_policy.walk(root, ignores=ignores, budget=budget):
            total_found += 1
            try:
                with self.file_policy.snapshot(path, max_bytes=max_bytes, budget=budget) as snapshot:
                    scanned = await self._classify_file(
                        snapshot, snapshot.parent, include_preview, capabilities, warnings, llm_health,
                    )
                scanned = scanned.model_copy(update={"path": str(path), "relative_path": str(path.relative_to(root))})
                files.append(scanned)
            except FileAccessError as exc:
                if exc.code == "file_access_busy":
                    raise
                warnings.append(f"Skipped {path.relative_to(root)}: {exc}")
                if budget.stopped:
                    break
            except Exception as exc:
                warnings.append(f"Error scanning {path.relative_to(root)}: {exc}")
        if budget.stopped:
            warnings.append(budget.stopped)

        # Check duplicates
        await self._check_duplicates(files)

        # Build summary
        summary = self._build_summary(files)

        return ScanManifest(
            scan_id=scan_id,
            root_path=str(root),
            total_files_found=total_found,
            total_files_scanned=len(files),
            files=files,
            summary=summary,
            warnings=warnings,
            capabilities=capabilities,
        )

    # ================================================================
    # Public: Ingest
    # ================================================================

    async def ingest(
        self, request: WorkspaceIngestRequest,
    ) -> WorkspaceIngestResponse:
        """Ingest files from a scan manifest into the knowledge base."""
        manifest = request.manifest
        root = self.file_policy.directory(manifest.root_path)
        if len(manifest.files) > MAX_SCAN_FILES:
            raise FileAccessError("Manifest file limit exceeded", code="file_access_limit", status_code=413)
        # Validate every manifest path before any write, including dry-run/skip
        # entries. Request-supplied roots, path fields, and hashes grant no authority.
        for scanned in manifest.files:
            path = self.file_policy.relative_file(root, scanned.relative_path)
            if self.file_policy.authorize(scanned.path) != path:
                raise FileAccessError("Manifest path fields disagree")
            self.file_policy.check_file(path)
        budget = ScanBudget()
        skip_set = set(request.skip_files)
        results: list[IngestResult] = []
        total_created = 0
        total_skipped = 0
        total_errors = 0

        for scanned in manifest.files:
            # Skip duplicates and explicitly skipped files
            if scanned.relative_path in skip_set:
                total_skipped += 1
                continue
            if scanned.is_duplicate:
                total_skipped += 1
                results.append(IngestResult(
                    relative_path=scanned.relative_path,
                    category=scanned.category.value,
                    ingestion_target=scanned.ingestion_target.value,
                    success=True,
                    error="Duplicate — already ingested",
                ))
                continue
            if scanned.ingestion_target == IngestionTarget.skip:
                total_skipped += 1
                continue

            if request.dry_run:
                results.append(IngestResult(
                    relative_path=scanned.relative_path,
                    category=scanned.category.value,
                    ingestion_target=scanned.ingestion_target.value,
                    success=True,
                    entity_count=1,
                ))
                total_created += 1
                continue

            try:
                result = await self._ingest_single_file(
                    scanned=scanned,
                    root_path=manifest.root_path,
                    phase=request.phase,
                    override_tags=request.override_tags,
                    source=request.source,
                    scan_id=manifest.scan_id,
                    budget=budget,
                )
            except FileAccessError as exc:
                # Batch ingestion already reports per-file failures. Preserve
                # successful records and their IDs if a later file changes.
                result = IngestResult(
                    relative_path=scanned.relative_path,
                    category=scanned.category.value,
                    ingestion_target=scanned.ingestion_target.value,
                    success=False,
                    error=str(exc),
                )
            results.append(result)
            if result.success:
                total_created += result.entity_count
            else:
                total_errors += 1

        return WorkspaceIngestResponse(
            scan_id=manifest.scan_id,
            total_processed=len(results),
            total_created=total_created,
            total_skipped=total_skipped,
            total_errors=total_errors,
            results=results,
        )

    # ================================================================
    # Public: Review (for Brain handoff)
    # ================================================================

    async def review(self, scan_id: str) -> BootstrapReview:
        """Produce a bootstrap review for the Brain to start reorganization."""
        # Query bootstrap_log for all entities from this scan
        rows = await self.db.fetchall(
            "SELECT entity_type, entity_id, category FROM bootstrap_log WHERE scan_id = ? AND project_id = ?",
            [scan_id, self.project_id],
        )
        if not rows:
            return BootstrapReview(
                scan_id=scan_id,
                total_entries_created=0,
                suggestions=[ReviewSuggestion(
                    priority="high",
                    action="No entries found for this scan ID",
                    details="Run rka_bootstrap_workspace first to ingest files.",
                )],
            )

        entity_ids = [r["entity_id"] for r in rows]
        type_counter: Counter = Counter()
        cat_counter: Counter = Counter()
        all_tags_counter: Counter = Counter()
        needs_attention: list[str] = []

        for row in rows:
            cat_counter[row["category"]] += 1

        # Count by entry type + gather tags
        for eid in entity_ids:
            # Check journal entries
            jrow = await self.db.fetchone(
                "SELECT type FROM journal WHERE id = ? AND project_id = ?", [eid, self.project_id],
            )
            if jrow:
                type_counter[jrow["type"]] += 1

            # Check literature entries
            lrow = await self.db.fetchone(
                "SELECT abstract FROM literature WHERE id = ? AND project_id = ?", [eid, self.project_id],
            )
            if lrow is not None and not lrow["abstract"]:
                needs_attention.append(eid)

            # Gather tags
            tag_rows = await self.db.fetchall(
                "SELECT tag FROM tags WHERE entity_id = ? AND project_id = ?", [eid, self.project_id],
            )
            for tr in tag_rows:
                all_tags_counter[tr["tag"]] += 1

        all_tags = sorted(all_tags_counter.keys())
        singleton_tags = [t for t, c in all_tags_counter.items() if c == 1]

        # Build suggestions
        suggestions: list[ReviewSuggestion] = []

        if needs_attention:
            suggestions.append(ReviewSuggestion(
                priority="high",
                action=f"Enrich {len(needs_attention)} literature entries missing abstracts",
                details=(
                    f"IDs: {', '.join(needs_attention[:10])}"
                    f"{' (and more)' if len(needs_attention) > 10 else ''}"
                    f" — consider having Executor read these PDFs and summarize."
                ),
            ))

        if singleton_tags:
            suggestions.append(ReviewSuggestion(
                priority="medium",
                action=f"Review {len(singleton_tags)} singleton tags for consolidation",
                details=(
                    f"Tags used only once: {', '.join(singleton_tags[:15])}"
                    f" — merge similar tags or add them to related entries."
                ),
            ))

        if len(entity_ids) > 5:
            suggestions.append(ReviewSuggestion(
                priority="medium",
                action="Create cross-references between related entries",
                details="Scan for entries that share tags or topics and link them via related_decisions/related_literature.",
            ))

        suggestions.append(ReviewSuggestion(
            priority="low",
            action="Create decisions from recurring themes",
            details="Review entries for common themes that warrant formal decision records.",
        ))

        # Optional LLM narrative
        narrative: str | None = None
        if self.llm:
            try:
                # Build a concise summary of what was ingested
                summary_parts = [
                    f"Bootstrap ingested {len(entity_ids)} entries from scan {scan_id}.",
                    f"Types: {dict(type_counter)}",
                    f"Categories: {dict(cat_counter)}",
                    f"Tags ({len(all_tags)}): {', '.join(all_tags[:20])}",
                ]
                if needs_attention:
                    summary_parts.append(
                        f"{len(needs_attention)} entries need attention (missing abstracts)."
                    )
                from rka.infra.llm import NarrativeSummary
                result = await self.llm.extract(
                    NarrativeSummary,
                    messages=[{
                        "role": "user",
                        "content": (
                            "Produce a brief narrative overview of this research knowledge base bootstrap. "
                            "Highlight themes, gaps, and priorities for reorganization.\n\n"
                            + "\n".join(summary_parts)
                        ),
                    }],
                )
                if result:
                    narrative = result.narrative
            except Exception as exc:
                logger.warning("Review narrative generation failed: %s", exc)
                narrative = (
                    "Narrative unavailable: the configured language model did "
                    f"not answer ({exc})."
                )

        return BootstrapReview(
            scan_id=scan_id,
            total_entries_created=len(entity_ids),
            entries_by_type=dict(type_counter),
            entries_by_category=dict(cat_counter),
            all_tags=all_tags,
            singleton_tags=singleton_tags,
            needs_attention=needs_attention,
            suggestions=suggestions,
            narrative=narrative,
        )

    # ================================================================
    # Private: Classification
    # ================================================================

    async def _classify_file(
        self,
        path: Path,
        root: Path,
        include_preview: bool,
        capabilities: ScanCapabilities,
        warnings: list[str],
        llm_health: "_LlmScanHealth",
    ) -> ScannedFile:
        """Classify a single file."""
        ext = path.suffix.lower()
        category = _EXTENSION_CATEGORY.get(ext, FileCategory.unknown)
        target = _CATEGORY_TARGET.get(category, IngestionTarget.skip)
        file_hash = self._hash_file(path)
        size_bytes = path.stat().st_size

        content_hint = ContentHint.general
        preview: str | None = None
        proposed_type = "finding"
        proposed_tags: list[str] = []
        llm_classified = False
        title_suggestion: str | None = None

        # Text-readable files: extract preview and apply heuristics
        if category in (FileCategory.markdown, FileCategory.text, FileCategory.document):
            content = self._safe_read_text(path, capabilities)
            if content:
                preview = content[:500] if include_preview else None
                content_hint = self._detect_content_hint(content)
                proposed_type = self._hint_to_type(content_hint)

                # For text files without headings, use single journal entry
                if category == FileCategory.text and not _HEADING_PATTERN.search(content):
                    target = IngestionTarget.journal_entry

                # LLM-enhanced classification
                if capabilities.llm_available and self.llm:
                    try:
                        classification = await self.llm.classify_file(
                            path.name, content[:2000], ext,
                        )
                        if classification and classification.confidence > 0.7:
                            content_hint = ContentHint(classification.content_type)
                            proposed_type = classification.journal_type
                            proposed_tags = [t.lower() for t in classification.tags]
                            title_suggestion = classification.title_suggestion
                            llm_classified = True
                        llm_health.record_success()
                    except Exception as exc:
                        self._record_llm_failure(
                            llm_health, capabilities, warnings,
                            "classifying", path.name, exc,
                        )

        elif category == FileCategory.code:
            content = self._safe_read_text(path)
            if content:
                docstring = self._extract_module_docstring(content)
                first_lines = "\n".join(content.splitlines()[:50])
                preview_text = f"{docstring}\n\n{first_lines}" if docstring else first_lines
                preview = preview_text[:500] if include_preview else None
                proposed_type = "methodology"
                content_hint = ContentHint.code_documentation

        elif category == FileCategory.pdf:
            pdf_preview = self._extract_pdf_preview(path, capabilities)
            if pdf_preview:
                preview = pdf_preview[:500] if include_preview else None
                title_suggestion = pdf_preview.split("\n")[0][:200]  # first line as title

                # LLM-enhanced PDF metadata
                if capabilities.llm_available and self.llm and pdf_preview:
                    try:
                        meta = await self.llm.extract_pdf_metadata(pdf_preview[:3000])
                        if meta:
                            title_suggestion = meta.title
                            llm_classified = True
                        llm_health.record_success()
                    except Exception as exc:
                        self._record_llm_failure(
                            llm_health, capabilities, warnings,
                            "reading PDF metadata from", path.name, exc,
                        )

        elif category == FileCategory.data:
            proposed_type = "observation"
            preview = f"Data file: {path.name} ({size_bytes:,} bytes)" if include_preview else None

        return ScannedFile(
            path=str(path),
            relative_path=str(path.relative_to(root)),
            filename=path.name,
            extension=ext,
            size_bytes=size_bytes,
            category=category,
            content_hint=content_hint,
            ingestion_target=target,
            proposed_type=proposed_type,
            proposed_tags=proposed_tags,
            file_hash=file_hash,
            preview=preview,
            llm_classified=llm_classified,
            title_suggestion=title_suggestion,
        )

    @staticmethod
    def _detect_content_hint(content: str) -> ContentHint:
        """Apply regex heuristics in priority order to classify content."""
        return detect_content_hint(content)

    @staticmethod
    def _hint_to_type(hint: ContentHint) -> str:
        """Map ContentHint to a default journal entry type."""
        return hint_to_type(hint)

    # ================================================================
    # Private: Ingestion
    # ================================================================

    async def _ingest_single_file(
        self,
        scanned: ScannedFile,
        root_path: str,
        phase: str | None,
        override_tags: list[str],
        source: str,
        scan_id: str,
        budget: ScanBudget | None = None,
    ) -> IngestResult:
        """Ingest a single file into the knowledge base."""
        full_path = self.file_policy.relative_file(root_path, scanned.relative_path)
        with self.file_policy.snapshot(full_path, budget=budget) as snapshot:
            if self._hash_file(snapshot) != scanned.file_hash:
                raise FileAccessError("File changed since scan; rescan before ingestion", code="file_changed", status_code=409)
            return await self._ingest_snapshot(snapshot, scanned, phase, override_tags, source, scan_id)

    async def _ingest_snapshot(self, full_path, scanned, phase, override_tags, source, scan_id):
        tags = list(set(scanned.proposed_tags + override_tags))

        try:
            if scanned.ingestion_target == IngestionTarget.ingest_document:
                return await self._ingest_as_document(
                    full_path, scanned, phase, tags, source, scan_id,
                )
            elif scanned.ingestion_target == IngestionTarget.import_bibtex:
                return await self._ingest_as_bibtex(
                    full_path, scanned, scan_id,
                )
            elif scanned.ingestion_target == IngestionTarget.journal_entry:
                return await self._ingest_as_journal(
                    full_path, scanned, phase, tags, source, scan_id,
                )
            elif scanned.ingestion_target == IngestionTarget.literature_entry:
                return await self._ingest_as_literature(
                    full_path, scanned, tags, scan_id,
                )
            else:
                return IngestResult(
                    relative_path=scanned.relative_path,
                    category=scanned.category.value,
                    ingestion_target=scanned.ingestion_target.value,
                    success=False,
                    error=f"Unsupported ingestion target: {scanned.ingestion_target}",
                )
        except Exception as exc:
            return IngestResult(
                relative_path=scanned.relative_path,
                category=scanned.category.value,
                ingestion_target=scanned.ingestion_target.value,
                success=False,
                error=str(exc),
            )

    async def _ingest_as_document(
        self, path: Path, scanned: ScannedFile,
        phase: str | None, tags: list[str], source: str, scan_id: str,
    ) -> IngestResult:
        """Ingest a text-based file via academic.ingest_document()."""
        capabilities = self._detect_capabilities(False)
        content = self._safe_read_text(path, capabilities)
        if not content:
            return IngestResult(
                relative_path=scanned.relative_path,
                category=scanned.category.value,
                ingestion_target=scanned.ingestion_target.value,
                success=False,
                error="Could not read file content",
            )

        result = await self.academic.ingest_document(
            content=content,
            source=source,
            default_type=scanned.proposed_type,
            phase=phase,
            tags=tags,
        )

        entity_ids = [e["id"] for e in result.get("created", [])]

        # Log to bootstrap_log
        for eid in entity_ids:
            await self._log_bootstrap(
                scan_id, scanned.file_hash, scanned.relative_path,
                scanned.category.value, "journal", eid,
            )

        return IngestResult(
            relative_path=scanned.relative_path,
            category=scanned.category.value,
            ingestion_target=scanned.ingestion_target.value,
            success=True,
            entity_ids=entity_ids,
            entity_count=len(entity_ids),
        )

    async def _ingest_as_bibtex(
        self, path: Path, scanned: ScannedFile, scan_id: str,
    ) -> IngestResult:
        """Ingest a BibTeX file via academic.import_bibtex()."""
        content = self._safe_read_text(path)
        if not content:
            return IngestResult(
                relative_path=scanned.relative_path,
                category=scanned.category.value,
                ingestion_target=scanned.ingestion_target.value,
                success=False,
                error="Could not read BibTeX file",
            )

        result = await self.academic.import_bibtex(
            bibtex_content=content,
            added_by="import",
        )

        entity_ids = [e["id"] for e in result.get("imported", [])]

        for eid in entity_ids:
            await self._log_bootstrap(
                scan_id, scanned.file_hash, scanned.relative_path,
                scanned.category.value, "literature", eid,
            )

        return IngestResult(
            relative_path=scanned.relative_path,
            category=scanned.category.value,
            ingestion_target=scanned.ingestion_target.value,
            success=True,
            entity_ids=entity_ids,
            entity_count=len(entity_ids),
        )

    async def _ingest_as_journal(
        self, path: Path, scanned: ScannedFile,
        phase: str | None, tags: list[str], source: str, scan_id: str,
    ) -> IngestResult:
        """Ingest a single file as one journal entry."""
        from rka.models.journal import JournalEntryCreate

        if scanned.category == FileCategory.code:
            content = self._safe_read_text(path)
            if not content:
                return IngestResult(
                    relative_path=scanned.relative_path,
                    category=scanned.category.value,
                    ingestion_target=scanned.ingestion_target.value,
                    success=False,
                    error="Could not read code file",
                )
            docstring = self._extract_module_docstring(content)
            first_lines = "\n".join(content.splitlines()[:50])
            entry_content = (
                f"**Code: {scanned.filename}**\n\n"
                f"{f'Docstring: {docstring}' + chr(10) + chr(10) if docstring else ''}"
                f"```{scanned.extension.lstrip('.')}\n{first_lines}\n```"
            )
        elif scanned.category == FileCategory.data:
            entry_content = (
                f"**Data file: {scanned.filename}**\n\n"
                f"Size: {scanned.size_bytes:,} bytes\n"
                f"Format: {scanned.extension}"
            )
        else:
            content = self._safe_read_text(path)
            entry_content = content or f"File: {scanned.filename}"

        data = JournalEntryCreate(
            content=entry_content,
            type=scanned.proposed_type,
            source=source,
            phase=phase,
            tags=tags,
        )
        entry = await self.notes.create(data, actor=source)

        await self._log_bootstrap(
            scan_id, scanned.file_hash, scanned.relative_path,
            scanned.category.value, "journal", entry.id,
        )

        return IngestResult(
            relative_path=scanned.relative_path,
            category=scanned.category.value,
            ingestion_target=scanned.ingestion_target.value,
            success=True,
            entity_ids=[entry.id],
            entity_count=1,
        )

    async def _ingest_as_literature(
        self, path: Path, scanned: ScannedFile,
        tags: list[str], scan_id: str,
    ) -> IngestResult:
        """Ingest a PDF as a literature entry."""
        from rka.models.literature import LiteratureCreate

        # Try to extract title from the scan's title_suggestion or filename
        title = scanned.title_suggestion or path.stem.replace("-", " ").replace("_", " ")

        # Try extracting metadata from PDF
        capabilities = self._detect_capabilities(False)
        abstract: str | None = None
        authors: list[str] | None = None
        year: int | None = None

        if capabilities.pymupdf_available:
            meta = self._extract_pdf_metadata_raw(path)
            if meta:
                if meta.get("title"):
                    title = meta["title"]
                abstract = meta.get("abstract")
                authors = meta.get("authors")
                year = meta.get("year")

        data = LiteratureCreate(
            title=title,
            authors=authors,
            year=year,
            abstract=abstract,
            pdf_path=scanned.path,  # Original locator, never the disposable parser snapshot.
            status="to_read",
            added_by="import",
            tags=tags,
        )
        lit = await self.lit.create(data, actor="system")

        await self._log_bootstrap(
            scan_id, scanned.file_hash, scanned.relative_path,
            scanned.category.value, "literature", lit.id,
        )

        return IngestResult(
            relative_path=scanned.relative_path,
            category=scanned.category.value,
            ingestion_target=scanned.ingestion_target.value,
            success=True,
            entity_ids=[lit.id],
            entity_count=1,
        )

    # ================================================================
    # Private: Helpers
    # ================================================================

    async def _log_bootstrap(
        self, scan_id: str, file_hash: str, file_path: str,
        category: str, entity_type: str, entity_id: str,
    ) -> None:
        """Record a successful ingestion in the bootstrap_log table."""
        await self.db.execute(
            """INSERT INTO bootstrap_log
               (scan_id, file_hash, file_path, category, entity_type, entity_id, project_id)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            [scan_id, file_hash, file_path, category, entity_type, entity_id, self.project_id],
        )
        await self.db.commit()

    async def _check_duplicates(self, files: list[ScannedFile]) -> None:
        """Mark files that have already been ingested (by hash)."""
        for f in files:
            row = await self.db.fetchone(
                "SELECT 1 FROM bootstrap_log WHERE file_hash = ? AND project_id = ?",
                [f.file_hash, self.project_id],
            )
            if row:
                f.is_duplicate = True

    @staticmethod
    def _build_summary(files: list[ScannedFile]) -> ScanSummary:
        """Build summary statistics from scanned files."""
        cat_counter: Counter = Counter()
        target_counter: Counter = Counter()
        hint_counter: Counter = Counter()
        total_size = 0
        dup_count = 0
        llm_count = 0

        for f in files:
            cat_counter[f.category.value] += 1
            target_counter[f.ingestion_target.value] += 1
            hint_counter[f.content_hint.value] += 1
            total_size += f.size_bytes
            if f.is_duplicate:
                dup_count += 1
            if f.llm_classified:
                llm_count += 1

        return ScanSummary(
            by_category=dict(cat_counter),
            by_target=dict(target_counter),
            by_content_hint=dict(hint_counter),
            total_size_bytes=total_size,
            duplicate_count=dup_count,
            llm_classified_count=llm_count,
        )

    @staticmethod
    def _should_ignore(path: Path, root: Path, ignores: set[str]) -> bool:
        """Check if a file path matches any ignore pattern."""
        return is_ignored(path, root, ignores)

    @staticmethod
    def _hash_file(path: Path, full_hash_limit: int = 10 * 1024 * 1024) -> str:
        """Compute SHA-256 hash of a file."""
        return hash_file(path, full_hash_limit)

    @staticmethod
    def _safe_read_text(
        path: Path,
        capabilities: ScanCapabilities | None = None,
        max_chars: int = 200_000,
    ) -> str | None:
        """Safely read file as text, capped at *max_chars* characters."""
        return safe_read_text(path, capabilities, max_chars)

    @staticmethod
    def _extract_module_docstring(content: str) -> str | None:
        """Extract the module-level docstring from Python/code files."""
        return extract_module_docstring(content)

    @staticmethod
    def _extract_pdf_preview(path: Path, capabilities: ScanCapabilities) -> str | None:
        """Extract text from the first page of a PDF."""
        return extract_pdf_preview(path, capabilities)

    @staticmethod
    def _extract_pdf_metadata_raw(path: Path) -> dict | None:
        """Extract metadata from PDF using pymupdf."""
        return extract_pdf_metadata_raw(path)

    @staticmethod
    def _extract_docx_text(path: Path) -> str | None:
        """Extract text from a DOCX file."""
        return extract_docx_text(path)

    def _detect_capabilities(self, use_llm: bool = True) -> ScanCapabilities:
        """Check which optional features are wired.

        `llm_available` is object-wiring, not reachability — a configured but
        unanswering backend passes it. `_record_llm_failure` corrects it once
        failures look systemic, so the rest of the walk does not repeat a call
        that cannot succeed.
        """
        return detect_capabilities(has_llm=use_llm and self.llm is not None)

    @staticmethod
    def _record_llm_failure(
        state: "_LlmScanHealth",
        capabilities: ScanCapabilities,
        warnings: list[str],
        doing: str,
        filename: str,
        exc: Exception,
    ) -> None:
        """Record one LLM failure, and give up only once it looks systemic.

        Every file used to retry independently and swallow the result at
        `logger.debug`, which is off by default. Against a backend that does
        not answer, each attempt costs the full connect budget, the walk is
        serial, and `max_files` defaults to 5000 — so one request could run
        for a very long time and still return HTTP 200 with an empty
        `warnings` list.

        Giving up on the *first* failure overcorrects, because
        `LLMClient.extract` raises `LLMUnavailableError` for everything: a
        refused connection and a healthy model returning one tag where the
        schema wants two arrive here as the same type with the same message.
        Latching on that would let a single awkward file downgrade the rest
        of a scan against a backend that is working fine.

        So: count consecutive failures, reset on any success, and stop only
        after `_LLM_FAILURE_THRESHOLD` in a row. A dead backend still costs a
        handful of files instead of five thousand; an isolated bad response
        costs that one file and nothing else. Either way it is logged at
        warning and named in the manifest.
        """
        state.consecutive_failures += 1
        logger.warning("LLM failed while %s %s: %s", doing, filename, exc)

        if state.consecutive_failures < _LLM_FAILURE_THRESHOLD:
            warnings.append(f"LLM failed while {doing} {filename}: {exc}")
            return
        if not capabilities.llm_available:
            return

        capabilities.llm_available = False
        warnings.append(
            f"LLM enhancement disabled for the rest of this scan after "
            f"{state.consecutive_failures} consecutive failures, most recently "
            f"while {doing} {filename} ({exc}). Remaining files were classified "
            f"without it."
        )
        logger.warning(
            "LLM enhancement disabled for this scan after %d consecutive failures",
            state.consecutive_failures,
        )

    # ================================================================
    # Public: Host-side scan
    # ================================================================

    async def scan_from_host_data(self, request: HostScanRequest) -> ScanManifest:
        """Build a ScanManifest from pre-scanned host-side file data.

        The host MCP binary has already read files and classified them.
        This method converts the host data to ScannedFile objects, runs
        duplicate detection against the DB, and returns a manifest.
        """
        scan_id = generate_id("scan")
        files: list[ScannedFile] = []

        for hf in request.files:
            scanned = ScannedFile(
                path=f"{request.root_path}/{hf.relative_path}",
                relative_path=hf.relative_path,
                filename=hf.filename,
                extension=hf.extension,
                size_bytes=hf.size_bytes,
                category=FileCategory(hf.category),
                content_hint=ContentHint(hf.content_hint),
                ingestion_target=IngestionTarget(hf.ingestion_target),
                proposed_type=hf.proposed_type,
                proposed_tags=hf.proposed_tags,
                file_hash=hf.file_hash,
                preview=hf.content_preview,
            )
            files.append(scanned)

        # Check duplicates against the DB
        await self._check_duplicates(files)

        # Build summary
        summary = self._build_summary(files)

        return ScanManifest(
            scan_id=scan_id,
            root_path=request.root_path,
            total_files_found=request.total_files_found,
            total_files_scanned=len(files),
            files=files,
            summary=summary,
            warnings=[],
            capabilities=ScanCapabilities(),
        )
