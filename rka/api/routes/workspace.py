"""Workspace bootstrap routes — scan, ingest, review."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException

from rka.models.workspace import (
    WorkspaceScanRequest,
    WorkspaceIngestRequest,
    ScanManifest,
    WorkspaceIngestResponse,
    BootstrapReview,
    HostScanRequest,
    IngestFileRequest,
)
from rka.services.workspace import WorkspaceService
from rka.api.deps import (
    get_scoped_workspace_service,
    get_scoped_academic_service,
    get_scoped_literature_service,
    get_scoped_note_service,
)
from rka.services.academic import AcademicImportService
from rka.services.literature import LiteratureService
from rka.services.notes import NoteService
from rka.infra.file_access import require_bounded_text

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/workspace/scan", response_model=ScanManifest)
async def scan_workspace(
    data: WorkspaceScanRequest,
    svc: WorkspaceService = Depends(get_scoped_workspace_service),
):
    """Scan a workspace folder and classify files for ingestion.

    Returns a manifest with classified files and their proposed
    ingestion targets. The manifest is ephemeral (not stored in DB).
    """
    try:
        return await svc.scan(
            folder_path=data.folder_path,
            ignore_patterns=data.ignore_patterns,
            include_preview=data.include_preview,
            max_file_size_mb=data.max_file_size_mb,
            use_llm=data.use_llm,
            max_files=data.max_files,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.post("/workspace/ingest", response_model=WorkspaceIngestResponse)
async def ingest_workspace(
    data: WorkspaceIngestRequest,
    svc: WorkspaceService = Depends(get_scoped_workspace_service),
):
    """Ingest files from a scan manifest into the knowledge base.

    Each file is dispatched to the appropriate service based on its
    classification in the manifest.
    """
    return await svc.ingest(data)


@router.get("/workspace/review/{scan_id}", response_model=BootstrapReview)
async def review_bootstrap(
    scan_id: str,
    svc: WorkspaceService = Depends(get_scoped_workspace_service),
):
    """Review a completed bootstrap for Brain handoff.

    Returns entry counts by type/tag, entries needing attention,
    and suggested next actions for reorganization.
    """
    return await svc.review(scan_id)


# ---- Host-side scan / ingest endpoints ----


@router.post("/workspace/scan/from-host", response_model=ScanManifest)
async def scan_from_host(
    data: HostScanRequest,
    svc: WorkspaceService = Depends(get_scoped_workspace_service),
):
    """Accept pre-scanned file metadata from the host MCP binary.

    The host has already read files and classified them using the
    shared ``classify`` module.  This endpoint converts the payload
    to a ``ScanManifest``, runs duplicate detection against the DB,
    and returns the manifest.  No filesystem I/O happens here.
    """
    try:
        return await svc.scan_from_host_data(data)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.post("/workspace/ingest/with-content")
async def ingest_with_content(
    data: IngestFileRequest,
    workspace_svc: WorkspaceService = Depends(get_scoped_workspace_service),
    academic_svc: AcademicImportService = Depends(get_scoped_academic_service),
    lit_svc: LiteratureService = Depends(get_scoped_literature_service),
    note_svc: NoteService = Depends(get_scoped_note_service),
):
    """Ingest a single file whose content was pre-read on the host.

    Routes based on ``content_type``:
    - ``"text"`` / ``"code"`` -- creates journal entries via
      ``academic.ingest_document`` or ``note_service.create``
    - ``"bibtex"`` -- creates literature entries via
      ``academic.import_bibtex``
    - ``"pdf_metadata"`` -- creates a literature entry from the
      metadata dict

    No filesystem I/O happens here -- the content arrives in the
    request body.
    """
    require_bounded_text(data.content)
    try:
        if data.content_type in ("text", "code"):
            if data.proposed_type in ("finding", "methodology", "observation",
                                       "summary", "idea", "pi_instruction"):
                result = await academic_svc.ingest_document(
                    content=data.content,
                    source=data.source,
                    default_type=data.proposed_type,
                    phase=data.phase,
                    tags=data.tags,
                )
                entity_ids = [e["id"] for e in result.get("created", [])]
                return {
                    "scan_id": data.scan_id,
                    "relative_path": data.relative_path,
                    "success": True,
                    "entity_ids": entity_ids,
                    "entity_count": len(entity_ids),
                }
            else:
                # Fallback: create a single journal entry
                from rka.models.journal import JournalEntryCreate
                entry_data = JournalEntryCreate(
                    content=data.content,
                    capture_mode="raw_capture",
                    type=data.proposed_type,
                    source=data.source,
                    phase=data.phase,
                    tags=data.tags,
                )
                entry = await note_svc.create(entry_data, actor=data.source)
                return {
                    "scan_id": data.scan_id,
                    "relative_path": data.relative_path,
                    "success": True,
                    "entity_ids": [entry.id],
                    "entity_count": 1,
                }

        elif data.content_type == "bibtex":
            result = await academic_svc.import_bibtex(
                bibtex_content=data.content,
                added_by=data.source,
            )
            entity_ids = [e["id"] for e in result.get("imported", [])]
            return {
                "scan_id": data.scan_id,
                "relative_path": data.relative_path,
                "success": True,
                "entity_ids": entity_ids,
                "entity_count": len(entity_ids),
            }

        elif data.content_type == "pdf_metadata":
            from rka.models.literature import LiteratureCreate
            meta = data.metadata
            lit_data = LiteratureCreate(
                title=meta.get("title", data.filename),
                authors=meta.get("authors"),
                year=meta.get("year"),
                abstract=meta.get("abstract"),
                status="to_read",
                added_by=data.source,
                tags=data.tags,
            )
            lit = await lit_svc.create(lit_data, actor="system")
            return {
                "scan_id": data.scan_id,
                "relative_path": data.relative_path,
                "success": True,
                "entity_ids": [lit.id],
                "entity_count": 1,
            }

        else:
            raise HTTPException(
                400,
                f"Unsupported content_type: {data.content_type!r}. "
                f"Expected one of: text, code, bibtex, pdf_metadata.",
            )
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("ingest_with_content failed for %s", data.relative_path)
        return {
            "scan_id": data.scan_id,
            "relative_path": data.relative_path,
            "success": False,
            "error": str(exc),
            "entity_ids": [],
            "entity_count": 0,
        }
