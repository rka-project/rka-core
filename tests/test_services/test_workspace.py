"""Tests for WorkspaceService — scan, classify, ingest."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
import pytest_asyncio

from rka.infra.database import Database
from rka.infra.file_access import FileAccessPolicy, FileAccessError
from rka.models.workspace import (
    ContentHint,
    FileCategory,
    IngestionTarget,
    WorkspaceIngestRequest,
)
from rka.services.workspace import WorkspaceService


# ---- Fixtures ----


def test_journal_capture_reader_keeps_crlf_and_marks_truncated_text(ws_svc, tmp_path):
    path = tmp_path / "capture.txt"
    path.write_bytes("  原文\r\n".encode("utf-8"))
    assert ws_svc._read_journal_text(path) == ("  原文\r\n", "raw_capture")
    path.write_bytes(b"x" * 200_001)
    content, mode = ws_svc._read_journal_text(path)
    assert mode == "unknown" and content.endswith("[…truncated]")


@pytest.mark.asyncio
async def test_bootstrap_text_captures_original_but_generated_previews_stay_unknown(services, workspace):
    ws, notes, *_ = services
    manifest = await ws.scan(workspace, use_llm=False)
    result = await ws.ingest(WorkspaceIngestRequest(manifest=manifest, source="pi"))
    assert result is not None
    entries = await notes.list(limit=200)
    captured = [entry for entry in entries if entry.capture_mode == "raw_capture"]
    assert captured and all(entry.verbatim_input for entry in captured)
    generated = [entry for entry in entries if entry.content.startswith(("**Code:", "**Data file:"))]
    assert generated and all(entry.capture_mode == "unknown" and entry.verbatim_input is None
                             for entry in generated)


@pytest_asyncio.fixture
async def services(db: Database, tmp_path):
    """Build the full service stack needed by WorkspaceService."""
    from rka.services.notes import NoteService
    from rka.services.literature import LiteratureService
    from rka.services.academic import AcademicImportService

    note_svc = NoteService(db=db, llm=None, embeddings=None)
    lit_svc = LiteratureService(db=db, llm=None, embeddings=None)
    academic_svc = AcademicImportService(lit_svc, note_service=note_svc)
    ws_svc = WorkspaceService(
        db=db,
        academic_service=academic_svc,
        note_service=note_svc,
        literature_service=lit_svc,
        llm=None,
        file_policy=FileAccessPolicy([tmp_path]),
    )
    return ws_svc, note_svc, lit_svc, academic_svc


@pytest_asyncio.fixture
async def ws_svc(services) -> WorkspaceService:
    """Shortcut for just the WorkspaceService."""
    return services[0]


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """Create a realistic workspace folder with mixed research files."""
    ws = tmp_path / "research_workspace"
    ws.mkdir()

    # Markdown file with headings (structured document → ingest_document)
    (ws / "meeting_notes.md").write_text(textwrap.dedent("""\
        # Meeting Notes — 2025-01-15

        Attendees: Alice, Bob, Charlie

        ## Agenda

        - Review progress on experiment A
        - Plan next sprint

        ## Action Items

        - Alice: finish data collection
        - Bob: update the model
    """))

    # Paper-like markdown (paper_manuscript)
    (ws / "draft_paper.md").write_text(textwrap.dedent("""\
        # Abstract

        We present a novel approach to knowledge graph construction.

        # Introduction

        Research knowledge management is challenging...

        # Methodology

        Our system uses a three-phase pipeline...

        # Results

        We evaluated on 500 documents...

        # Conclusion

        The proposed system achieves state-of-the-art results.

        # References

        [1] Smith et al., 2023
    """))

    # Brainstorm file (short, mostly bullets)
    (ws / "ideas.txt").write_text(textwrap.dedent("""\
        - Try graph neural networks for citation prediction
        - Compare with transformer-based approaches
        - Use DBLP dataset for evaluation
        - Consider multi-hop reasoning
        - Explore knowledge distillation
    """))

    # Plain text with action items
    (ws / "todo.txt").write_text(textwrap.dedent("""\
        TODO: Review paper submissions
        TODO: Update literature database
        Action Items:
        - Submit IRB application by Friday
        - Set up new compute cluster
        Next Steps:
        - Run ablation study
    """))

    # Python code file
    (ws / "analysis.py").write_text(textwrap.dedent('''\
        """Statistical analysis utilities for experiment results."""

        import numpy as np

        def compute_effect_size(group_a, group_b):
            """Cohen's d effect size."""
            pooled_std = np.sqrt((np.std(group_a)**2 + np.std(group_b)**2) / 2)
            return (np.mean(group_a) - np.mean(group_b)) / pooled_std
    '''))

    # CSV data file
    (ws / "results.csv").write_text("model,accuracy,f1\nbert,0.92,0.89\ngpt,0.95,0.93\n")

    # BibTeX file
    (ws / "refs.bib").write_text(textwrap.dedent("""\
        @article{smith2023knowledge,
          title={Knowledge Graph Construction for Research},
          author={Smith, John and Doe, Jane},
          journal={Journal of AI Research},
          year={2023},
          volume={42},
          pages={1--25}
        }
    """))

    # Unknown extension file (should be skipped)
    (ws / "data.xyz").write_bytes(b"binary data here")

    # .git directory (should be ignored)
    git_dir = ws / ".git"
    git_dir.mkdir()
    (git_dir / "HEAD").write_text("ref: refs/heads/main")

    # __pycache__ directory (should be ignored)
    cache_dir = ws / "__pycache__"
    cache_dir.mkdir()
    (cache_dir / "analysis.cpython-311.pyc").write_bytes(b"\x00")

    return ws


# ---- TestFileClassification ----


class TestFileClassification:
    """Test _detect_content_hint() regex heuristics."""

    def test_meeting_notes(self):
        content = "Meeting Notes — 2025-01-15\nAttendees: Alice, Bob\n\nAgenda:\n- Item 1"
        assert WorkspaceService._detect_content_hint(content) == ContentHint.meeting_notes

    def test_meeting_notes_minutes(self):
        content = "Minutes of the weekly standup\n\nPresent: Team A"
        assert WorkspaceService._detect_content_hint(content) == ContentHint.meeting_notes

    def test_paper_manuscript(self):
        content = (
            "# Abstract\nWe present...\n"
            "# Introduction\nThe problem is...\n"
            "# Methodology\nOur approach...\n"
            "# Results\nWe found...\n"
            "# Conclusion\nIn summary...\n"
        )
        assert WorkspaceService._detect_content_hint(content) == ContentHint.paper_manuscript

    def test_paper_needs_three_sections(self):
        """Two academic keywords should NOT trigger paper_manuscript."""
        content = "# Abstract\nSomething\n# Conclusion\nDone"
        # Only 2 sections — should NOT be paper_manuscript
        result = WorkspaceService._detect_content_hint(content)
        assert result != ContentHint.paper_manuscript

    def test_action_items(self):
        content = "TODO: Fix the regression in model A\nTODO: Review PR #42"
        assert WorkspaceService._detect_content_hint(content) == ContentHint.action_items

    def test_action_items_next_steps(self):
        content = "Next Steps:\n- Deploy to staging\n- Run integration tests"
        assert WorkspaceService._detect_content_hint(content) == ContentHint.action_items

    def test_brainstorm(self):
        content = "- Idea one\n- Idea two\n- Idea three\n- Idea four\n- Idea five"
        assert WorkspaceService._detect_content_hint(content) == ContentHint.brainstorm

    def test_brainstorm_needs_short_and_bulleted(self):
        """Long files with bullets should NOT be brainstorm."""
        lines = ["- Item " + str(i) for i in range(50)]
        content = "\n".join(lines)
        result = WorkspaceService._detect_content_hint(content)
        assert result != ContentHint.brainstorm

    def test_code_documentation(self):
        content = "## API Reference\n\nSome text.\n\n```python\ndef foo(): pass\n```"
        assert WorkspaceService._detect_content_hint(content) == ContentHint.code_documentation

    def test_structured_document(self):
        content = "## Section One\n\nText.\n\n## Section Two\n\nMore text."
        assert WorkspaceService._detect_content_hint(content) == ContentHint.structured_document

    def test_general(self):
        content = "Just a plain paragraph of text about some research topic."
        assert WorkspaceService._detect_content_hint(content) == ContentHint.general

    def test_priority_meeting_over_paper(self):
        """Meeting notes keyword should win even if paper sections exist."""
        content = (
            "Meeting Notes — 2025-01-15\nAttendees: Alice\n"
            "Abstract, Introduction, Methodology, Results, Conclusion"
        )
        assert WorkspaceService._detect_content_hint(content) == ContentHint.meeting_notes


# ---- TestScan ----


class TestScan:
    """Test workspace scanning."""

    @pytest.mark.asyncio
    async def test_scan_counts(self, ws_svc: WorkspaceService, workspace: Path):
        """Scan should find the correct number of files."""
        manifest = await ws_svc.scan(str(workspace), use_llm=False)

        # Should have found all files in the workspace (including ignored ones in total_found)
        assert manifest.total_files_found > 0
        # Scanned files should NOT include .git or __pycache__ contents
        scanned_names = {f.filename for f in manifest.files}
        assert "HEAD" not in scanned_names
        assert "analysis.cpython-311.pyc" not in scanned_names

    @pytest.mark.asyncio
    async def test_scan_categories(self, ws_svc: WorkspaceService, workspace: Path):
        """Each file should be classified to the correct category."""
        manifest = await ws_svc.scan(str(workspace), use_llm=False)
        file_map = {f.filename: f for f in manifest.files}

        assert file_map["meeting_notes.md"].category == FileCategory.markdown
        assert file_map["draft_paper.md"].category == FileCategory.markdown
        assert file_map["ideas.txt"].category == FileCategory.text
        assert file_map["todo.txt"].category == FileCategory.text
        assert file_map["analysis.py"].category == FileCategory.code
        assert file_map["results.csv"].category == FileCategory.data
        assert file_map["refs.bib"].category == FileCategory.bibtex
        assert file_map["data.xyz"].category == FileCategory.unknown

    @pytest.mark.asyncio
    async def test_scan_ingestion_targets(self, ws_svc: WorkspaceService, workspace: Path):
        """Files should be mapped to the correct ingestion target."""
        manifest = await ws_svc.scan(str(workspace), use_llm=False)
        file_map = {f.filename: f for f in manifest.files}

        assert file_map["meeting_notes.md"].ingestion_target == IngestionTarget.ingest_document
        assert file_map["refs.bib"].ingestion_target == IngestionTarget.import_bibtex
        assert file_map["analysis.py"].ingestion_target == IngestionTarget.journal_entry
        assert file_map["results.csv"].ingestion_target == IngestionTarget.journal_entry
        assert file_map["data.xyz"].ingestion_target == IngestionTarget.skip

    @pytest.mark.asyncio
    async def test_scan_content_hints(self, ws_svc: WorkspaceService, workspace: Path):
        """Text files should get correct content hints from heuristics."""
        manifest = await ws_svc.scan(str(workspace), use_llm=False)
        file_map = {f.filename: f for f in manifest.files}

        assert file_map["meeting_notes.md"].content_hint == ContentHint.meeting_notes
        assert file_map["draft_paper.md"].content_hint == ContentHint.paper_manuscript
        assert file_map["todo.txt"].content_hint == ContentHint.action_items

    @pytest.mark.asyncio
    async def test_scan_git_ignored(self, ws_svc: WorkspaceService, workspace: Path):
        """Files in .git and __pycache__ should be excluded."""
        manifest = await ws_svc.scan(str(workspace), use_llm=False)
        relative_paths = {f.relative_path for f in manifest.files}

        # Should not contain any .git or __pycache__ entries
        for rp in relative_paths:
            assert ".git" not in rp
            assert "__pycache__" not in rp

    @pytest.mark.asyncio
    async def test_scan_custom_ignore(self, ws_svc: WorkspaceService, workspace: Path):
        """Custom ignore patterns should exclude matching files."""
        manifest = await ws_svc.scan(
            str(workspace), ignore_patterns=["*.csv"], use_llm=False,
        )
        filenames = {f.filename for f in manifest.files}
        assert "results.csv" not in filenames

    @pytest.mark.asyncio
    async def test_scan_generates_id(self, ws_svc: WorkspaceService, workspace: Path):
        """Scan should generate a proper scan ID."""
        manifest = await ws_svc.scan(str(workspace), use_llm=False)
        assert manifest.scan_id.startswith("scn_")

    @pytest.mark.asyncio
    async def test_scan_includes_preview(self, ws_svc: WorkspaceService, workspace: Path):
        """Text files should have preview content."""
        manifest = await ws_svc.scan(str(workspace), include_preview=True, use_llm=False)
        file_map = {f.filename: f for f in manifest.files}

        md_file = file_map["meeting_notes.md"]
        assert md_file.preview is not None
        assert "Meeting Notes" in md_file.preview

    @pytest.mark.asyncio
    async def test_scan_no_preview(self, ws_svc: WorkspaceService, workspace: Path):
        """When include_preview=False, no preview should be set."""
        manifest = await ws_svc.scan(str(workspace), include_preview=False, use_llm=False)
        file_map = {f.filename: f for f in manifest.files}
        assert file_map["meeting_notes.md"].preview is None

    @pytest.mark.asyncio
    async def test_scan_summary(self, ws_svc: WorkspaceService, workspace: Path):
        """Scan summary should have correct category counts."""
        manifest = await ws_svc.scan(str(workspace), use_llm=False)
        summary = manifest.summary

        assert summary.by_category.get("markdown", 0) == 2
        assert summary.by_category.get("text", 0) == 2
        assert summary.by_category.get("code", 0) == 1
        assert summary.by_category.get("bibtex", 0) == 1
        assert summary.by_category.get("data", 0) == 1
        assert summary.duplicate_count == 0

    @pytest.mark.asyncio
    async def test_scan_file_hash(self, ws_svc: WorkspaceService, workspace: Path):
        """Each scanned file should have a SHA-256 hash."""
        manifest = await ws_svc.scan(str(workspace), use_llm=False)
        for f in manifest.files:
            assert f.file_hash
            assert len(f.file_hash) == 64  # SHA-256 hex length

    @pytest.mark.asyncio
    async def test_scan_duplicate_detection(
        self, ws_svc: WorkspaceService, workspace: Path, db: Database,
    ):
        """Re-scanning after ingest should detect duplicates."""
        # First scan + ingest
        manifest = await ws_svc.scan(str(workspace), use_llm=False)
        request = WorkspaceIngestRequest(manifest=manifest, source="pi")
        await ws_svc.ingest(request)

        # Re-scan — previously ingested files should be marked as duplicates
        manifest2 = await ws_svc.scan(str(workspace), use_llm=False)
        dup_files = [f for f in manifest2.files if f.is_duplicate]

        # At least the successfully ingested files should be marked
        assert len(dup_files) > 0

    @pytest.mark.asyncio
    async def test_scan_max_files_cap(self, ws_svc: WorkspaceService, tmp_path: Path):
        """Scan should respect max_files and emit a warning."""
        ws = tmp_path / "many_files"
        ws.mkdir()
        for i in range(20):
            (ws / f"note_{i:03d}.md").write_text(f"## Note {i}\n\nContent {i}.")

        manifest = await ws_svc.scan(str(ws), use_llm=False, max_files=5)
        assert len(manifest.files) == 5
        assert manifest.total_files_found == 5  # Stop enumeration; this is a partial count.
        assert any("File cap reached" in w for w in manifest.warnings)

    @pytest.mark.asyncio
    async def test_scan_not_a_directory(self, ws_svc: WorkspaceService, tmp_path: Path):
        """Scanning a non-existent path should raise ValueError."""
        with pytest.raises(FileAccessError, match="File access denied"):
            await ws_svc.scan(str(tmp_path / "no_such_folder"), use_llm=False)


# ---- TestIngest ----


class TestIngest:
    """Test workspace ingestion into the knowledge base."""

    @pytest.mark.asyncio
    async def test_dry_run_creates_nothing(
        self, ws_svc: WorkspaceService, workspace: Path, db: Database,
    ):
        """Dry run should NOT create any entities."""
        manifest = await ws_svc.scan(str(workspace), use_llm=False)
        request = WorkspaceIngestRequest(manifest=manifest, source="pi", dry_run=True)
        result = await ws_svc.ingest(request)

        assert result.total_created > 0  # Should report what *would* be created
        assert result.total_errors == 0

        # But nothing should exist in the journal table
        rows = await db.fetchall("SELECT count(*) as cnt FROM journal")
        assert rows[0]["cnt"] == 0

        # And nothing in bootstrap_log
        log_rows = await db.fetchall("SELECT count(*) as cnt FROM bootstrap_log")
        assert log_rows[0]["cnt"] == 0

    @pytest.mark.asyncio
    async def test_ingest_markdown(
        self, ws_svc: WorkspaceService, workspace: Path, db: Database,
    ):
        """Ingesting markdown should create journal entries via ingest_document."""
        manifest = await ws_svc.scan(str(workspace), use_llm=False)

        # Only ingest the meeting notes
        manifest.files = [f for f in manifest.files if f.filename == "meeting_notes.md"]
        request = WorkspaceIngestRequest(manifest=manifest, source="pi")
        result = await ws_svc.ingest(request)

        assert result.total_errors == 0
        assert result.total_created >= 1

        # Check that journal entries were created
        rows = await db.fetchall("SELECT count(*) as cnt FROM journal")
        assert rows[0]["cnt"] >= 1

    @pytest.mark.asyncio
    async def test_ingest_code(
        self, ws_svc: WorkspaceService, workspace: Path, db: Database,
    ):
        """Ingesting a code file should create a single journal entry."""
        manifest = await ws_svc.scan(str(workspace), use_llm=False)
        manifest.files = [f for f in manifest.files if f.filename == "analysis.py"]
        request = WorkspaceIngestRequest(manifest=manifest, source="pi")
        result = await ws_svc.ingest(request)

        assert result.total_errors == 0
        assert result.total_created == 1

        # Migration 009 mapped legacy 'methodology' -> 'log' for the v2 type vocabulary.
        rows = await db.fetchall("SELECT * FROM journal")
        assert len(rows) == 1
        assert rows[0]["type"] == "log"
        assert "analysis.py" in rows[0]["content"]

    @pytest.mark.asyncio
    async def test_ingest_data_file(
        self, ws_svc: WorkspaceService, workspace: Path, db: Database,
    ):
        """Ingesting a data file should create a single journal entry."""
        manifest = await ws_svc.scan(str(workspace), use_llm=False)
        manifest.files = [f for f in manifest.files if f.filename == "results.csv"]
        request = WorkspaceIngestRequest(manifest=manifest, source="pi")
        result = await ws_svc.ingest(request)

        assert result.total_errors == 0
        assert result.total_created == 1

        # Migration 009 mapped legacy 'observation' -> 'note' for the v2 type vocabulary.
        rows = await db.fetchall("SELECT * FROM journal")
        assert len(rows) == 1
        assert rows[0]["type"] == "note"

    @pytest.mark.asyncio
    async def test_ingest_skips_unknown(
        self, ws_svc: WorkspaceService, workspace: Path, db: Database,
    ):
        """Unknown file types should be skipped during ingestion."""
        manifest = await ws_svc.scan(str(workspace), use_llm=False)
        manifest.files = [f for f in manifest.files if f.filename == "data.xyz"]
        request = WorkspaceIngestRequest(manifest=manifest, source="pi")
        result = await ws_svc.ingest(request)

        assert result.total_skipped == 1
        assert result.total_created == 0

    @pytest.mark.asyncio
    async def test_ingest_skip_files(
        self, ws_svc: WorkspaceService, workspace: Path, db: Database,
    ):
        """Explicitly skipped files should not be ingested."""
        manifest = await ws_svc.scan(str(workspace), use_llm=False)

        # Find the relative path for analysis.py
        py_file = [f for f in manifest.files if f.filename == "analysis.py"][0]

        request = WorkspaceIngestRequest(
            manifest=manifest,
            source="pi",
            skip_files=[py_file.relative_path],
        )
        result = await ws_svc.ingest(request)

        # analysis.py should have been skipped
        ingested_paths = {r.relative_path for r in result.results}
        assert py_file.relative_path not in ingested_paths

    @pytest.mark.asyncio
    async def test_ingest_override_tags(
        self, ws_svc: WorkspaceService, workspace: Path, db: Database,
    ):
        """Override tags should be applied to all ingested entries."""
        manifest = await ws_svc.scan(str(workspace), use_llm=False)
        manifest.files = [f for f in manifest.files if f.filename == "analysis.py"]
        request = WorkspaceIngestRequest(
            manifest=manifest, source="pi",
            override_tags=["bootstrap", "experiment-1"],
        )
        result = await ws_svc.ingest(request)
        assert result.total_created == 1

        # Check that tags were applied
        tag_rows = await db.fetchall("SELECT tag FROM tags")
        tags = {r["tag"] for r in tag_rows}
        assert "bootstrap" in tags
        assert "experiment-1" in tags

    @pytest.mark.asyncio
    async def test_ingest_logs_to_bootstrap_log(
        self, ws_svc: WorkspaceService, workspace: Path, db: Database,
    ):
        """Ingested files should be logged in bootstrap_log."""
        manifest = await ws_svc.scan(str(workspace), use_llm=False)
        manifest.files = [f for f in manifest.files if f.filename == "analysis.py"]
        request = WorkspaceIngestRequest(manifest=manifest, source="pi")
        await ws_svc.ingest(request)

        log_rows = await db.fetchall("SELECT * FROM bootstrap_log")
        assert len(log_rows) >= 1
        assert log_rows[0]["scan_id"] == manifest.scan_id
        assert log_rows[0]["category"] == "code"
        assert log_rows[0]["entity_type"] == "journal"

    @pytest.mark.asyncio
    async def test_ingest_skips_duplicates(
        self, ws_svc: WorkspaceService, workspace: Path, db: Database,
    ):
        """Duplicate files should be skipped during ingestion."""
        # First ingest
        manifest = await ws_svc.scan(str(workspace), use_llm=False)
        manifest.files = [f for f in manifest.files if f.filename == "analysis.py"]
        request = WorkspaceIngestRequest(manifest=manifest, source="pi")
        await ws_svc.ingest(request)

        # Second scan (file now shows as duplicate)
        manifest2 = await ws_svc.scan(str(workspace), use_llm=False)
        manifest2.files = [f for f in manifest2.files if f.filename == "analysis.py"]
        assert manifest2.files[0].is_duplicate is True

        # Second ingest — should skip the duplicate
        request2 = WorkspaceIngestRequest(manifest=manifest2, source="pi")
        result2 = await ws_svc.ingest(request2)
        assert result2.total_skipped == 1
        assert result2.total_created == 0


# ---- TestHelpers ----


class TestHelpers:
    """Test static helper methods."""

    def test_hash_file(self, tmp_path: Path):
        """File hashing should produce consistent SHA-256."""
        f = tmp_path / "test.txt"
        f.write_text("hello world")
        h1 = WorkspaceService._hash_file(f)
        h2 = WorkspaceService._hash_file(f)
        assert h1 == h2
        assert len(h1) == 64

    def test_hash_file_different_content(self, tmp_path: Path):
        """Different content should produce different hashes."""
        f1 = tmp_path / "a.txt"
        f2 = tmp_path / "b.txt"
        f1.write_text("hello")
        f2.write_text("world")
        assert WorkspaceService._hash_file(f1) != WorkspaceService._hash_file(f2)

    def test_hash_file_large_uses_fast_composite(self, tmp_path: Path):
        """Files above the threshold should use fast composite hash."""
        f = tmp_path / "big.bin"
        # Write a 100 KB file but set threshold to 50 KB
        data = b"A" * 100_000
        f.write_bytes(data)
        h_fast = WorkspaceService._hash_file(f, full_hash_limit=50_000)
        h_full = WorkspaceService._hash_file(f, full_hash_limit=200_000)
        # Fast and full hashes should differ (different algorithm)
        assert h_fast != h_full
        # Fast hash should still be deterministic
        assert h_fast == WorkspaceService._hash_file(f, full_hash_limit=50_000)
        assert len(h_fast) == 64

    def test_should_ignore_git(self, tmp_path: Path):
        """Files inside .git should be ignored."""
        root = tmp_path
        git_file = root / ".git" / "HEAD"
        git_file.parent.mkdir()
        git_file.touch()
        assert WorkspaceService._should_ignore(git_file, root, _DEFAULT_IGNORES_SET()) is True

    def test_should_ignore_pyc(self, tmp_path: Path):
        """*.pyc files should be ignored."""
        root = tmp_path
        pyc_file = root / "module.pyc"
        pyc_file.touch()
        assert WorkspaceService._should_ignore(pyc_file, root, _DEFAULT_IGNORES_SET()) is True

    def test_should_not_ignore_normal(self, tmp_path: Path):
        """Normal files should not be ignored."""
        root = tmp_path
        normal = root / "notes.md"
        normal.touch()
        assert WorkspaceService._should_ignore(normal, root, _DEFAULT_IGNORES_SET()) is False

    def test_safe_read_text(self, tmp_path: Path):
        """Should read UTF-8 text files."""
        f = tmp_path / "test.txt"
        f.write_text("Hello, world!", encoding="utf-8")
        assert WorkspaceService._safe_read_text(f) == "Hello, world!"

    def test_safe_read_text_latin1(self, tmp_path: Path):
        """Should fallback to latin-1 for non-UTF-8 files."""
        f = tmp_path / "test.txt"
        f.write_bytes("Héllo wörld".encode("latin-1"))
        result = WorkspaceService._safe_read_text(f)
        assert result is not None
        assert "rld" in result

    def test_safe_read_text_truncates_large_file(self, tmp_path: Path):
        """Large text files should be truncated at max_chars."""
        f = tmp_path / "huge.txt"
        f.write_text("x" * 1000)
        result = WorkspaceService._safe_read_text(f, max_chars=100)
        assert result is not None
        assert len(result) < 200  # 100 chars + truncation marker
        assert result.endswith("[…truncated]")

    def test_safe_read_text_small_file_not_truncated(self, tmp_path: Path):
        """Small files should be returned in full, no truncation marker."""
        f = tmp_path / "small.txt"
        f.write_text("short content")
        result = WorkspaceService._safe_read_text(f, max_chars=1000)
        assert result == "short content"
        assert "[…truncated]" not in result

    def test_extract_module_docstring(self):
        """Should extract Python module docstrings."""
        code = '"""This is the module docstring."""\n\nimport os\n'
        result = WorkspaceService._extract_module_docstring(code)
        assert result == "This is the module docstring."

    def test_extract_module_docstring_with_comments(self):
        """Should extract docstring even after comments."""
        code = '# -*- coding: utf-8 -*-\n"""Module doc."""\n'
        result = WorkspaceService._extract_module_docstring(code)
        assert result == "Module doc."

    def test_extract_module_docstring_none(self):
        """Should return None when there's no docstring."""
        code = "import os\nprint('hello')\n"
        result = WorkspaceService._extract_module_docstring(code)
        assert result is None

    def test_hint_to_type_mapping(self):
        """Each content hint should map to an appropriate journal type."""
        assert WorkspaceService._hint_to_type(ContentHint.meeting_notes) == "summary"
        assert WorkspaceService._hint_to_type(ContentHint.paper_manuscript) == "finding"
        assert WorkspaceService._hint_to_type(ContentHint.brainstorm) == "idea"
        assert WorkspaceService._hint_to_type(ContentHint.action_items) == "pi_instruction"
        assert WorkspaceService._hint_to_type(ContentHint.code_documentation) == "methodology"
        assert WorkspaceService._hint_to_type(ContentHint.general) == "finding"


# ---- Helpers ----


def _DEFAULT_IGNORES_SET() -> set[str]:
    """Return the default ignore set for testing _should_ignore."""
    from rka.services.workspace import _DEFAULT_IGNORES
    return _DEFAULT_IGNORES
