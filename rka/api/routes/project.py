"""Project state routes."""

from __future__ import annotations

import os
import sqlite3

import logging

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.responses import FileResponse, JSONResponse
from starlette.background import BackgroundTask

from rka.api.deps import (
    get_project_service,
    get_scoped_knowledge_pack_service,
    get_knowledge_pack_service,
    require_project,
)
from rka.models.capabilities import CapabilityNegotiationError, CoreCapabilities
from rka.models.project import ProjectCreate, ProjectInfo, ProjectState, ProjectStateUpdate
from rka.services.embedding_jobs import BackfillScopeBusy
from rka.services.embedding_index import EmbeddingGenerationMismatch
from rka.services.capabilities import (
    build_core_capabilities,
    validate_capability_requirements,
)
from rka.services.knowledge_pack import KnowledgePackService
from rka.services.project import ProjectService

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/projects", response_model=list[ProjectInfo])
async def list_projects(svc: ProjectService = Depends(get_project_service)):
    return await svc.list_projects()


@router.post("/projects", response_model=ProjectInfo)
async def create_project(
    data: ProjectCreate,
    actor: str = "web_ui",
    svc: ProjectService = Depends(get_project_service),
):
    try:
        return await svc.create_project(data, actor=actor)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except sqlite3.IntegrityError as exc:
        message = str(exc)
        if "projects.name" in message:
            raise HTTPException(
                status_code=409,
                detail=f"Project name '{data.name}' already exists",
            ) from exc
        if "projects.id" in message:
            project_id = data.id or "generated"
            raise HTTPException(
                status_code=409,
                detail=f"Project '{project_id}' already exists",
            ) from exc
        raise


@router.get("/status", response_model=ProjectState)
async def get_status(
    project_id: str = Depends(require_project),
    svc: ProjectService = Depends(get_project_service),
):
    state = await svc.get(project_id=project_id)
    if state is None:
        raise HTTPException(404, "Project not initialized. Run `rka init` first.")
    return state


@router.put("/status", response_model=ProjectState)
async def update_status(
    data: ProjectStateUpdate,
    actor: str = "web_ui",
    project_id: str = Depends(require_project),
    svc: ProjectService = Depends(get_project_service),
):
    return await svc.update(data, actor=actor, project_id=project_id)


@router.get(
    "/capabilities",
    response_model=CoreCapabilities,
    responses={409: {"model": CapabilityNegotiationError}},
)
async def get_capabilities(
    request: Request,
    required_contract: str | None = Query(default=None),
    required_capability: list[str] | None = Query(default=None),
):
    """Return Core/interface versions plus runtime capability availability.

    Affordance C (Mission B / mis_01KR209WY4M6WQFEXRH79KC2ZF). Pure
    runtime introspection — no DB write, no schema change.

    Mission D (v2.4.0) removed the `llm` field per PI directive
    `jrn_01KRNZBS50K250HHHHEC58E4GC`. **BREAKING-IN-MINOR**: any consumer
    that read `response["llm"]` before v2.4.0 must update. LLM service
    code is preserved server-side (`rka/infra/llm.py`,
    `rka/api/routes/llm.py`, `rka_ask`/`rka_generate_summary` MCP tools)
    for a future re-wiring through the orchestrator's Claude Code SDK.
    """
    state = request.app.state
    db = getattr(state, "db", None)

    embeddings = getattr(state, "embeddings", None)
    if embeddings and getattr(db, "vec_available", False):
        if getattr(embeddings, "runtime_available", None) is False:
            emb_block = {
                "available": False,
                "reason_unavailable": (
                    "configured embedding backend temporarily unavailable; "
                    "search is using lexical retrieval"
                ),
            }
        elif (
            getattr(embeddings, "index_search_ready", None) is not None
            and not await embeddings.index_search_ready()
        ):
            emb_block = {
                "available": False,
                "reason_unavailable": (
                    "embedding index is rebuilding or requires repair; "
                    "search is using lexical retrieval"
                ),
            }
        else:
            emb_block = {"available": True, "reason_unavailable": None}
    elif not embeddings:
        reason = getattr(state, "embedding_unavailable_reason", None)
        emb_block = {
            "available": False,
            "reason_unavailable": reason
            or "embeddings disabled (RKA_EMBEDDINGS_ENABLED=false)",
        }
    else:
        emb_block = {
            "available": False,
            "reason_unavailable": "sqlite-vec extension not loaded",
        }

    document = build_core_capabilities(
        embedding_available=emb_block["available"],
        embedding_reason=emb_block["reason_unavailable"],
    )
    error = validate_capability_requirements(
        document,
        required_contract=required_contract,
        required_capabilities=required_capability,
    )
    if error is not None:
        return JSONResponse(status_code=409, content=error.model_dump(mode="json"))
    return document


@router.delete("/projects/{project_id}")
async def delete_project(
    project_id: str,
    confirm: bool = False,
    svc: ProjectService = Depends(get_project_service),
):
    """Delete a project and all its scoped data. Requires confirm=true query parameter."""
    try:
        return await svc.delete_project(project_id, confirm=confirm)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/projects/{project_id}/entity-counts")
async def get_project_entity_counts(
    project_id: str,
    svc: ProjectService = Depends(get_project_service),
):
    """Return entity counts for a project (for deletion confirmation UI)."""
    row = await svc.db.fetchone("SELECT id FROM projects WHERE id = ?", [project_id])
    if not row:
        raise HTTPException(status_code=404, detail=f"Project '{project_id}' not found")
    counts = await svc.get_project_entity_counts(project_id)
    return {"project_id": project_id, "entity_counts": counts, "total_rows": sum(counts.values())}


@router.get("/projects/export")
async def export_project_pack(
    project_id: str = Depends(require_project),
    svc: KnowledgePackService = Depends(get_scoped_knowledge_pack_service),
):
    try:
        path, filename = await svc.export_pack(project_id=project_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    return FileResponse(
        path,
        media_type="application/zip",
        filename=filename,
        background=BackgroundTask(lambda: os.unlink(path) if os.path.exists(path) else None),
    )


@router.post("/projects/import", status_code=202)
async def import_project_pack(
    file: UploadFile = File(...),
    project_id: str | None = Form(default=None),
    project_name: str | None = Form(default=None),
    svc: KnowledgePackService = Depends(get_knowledge_pack_service),
):
    """Atomically import graph, lexical indexes and durable indexing receipt.

    A separate worker builds vectors. Polling distinguishes lexical completion
    from semantic readiness; a 202 never depends on process-local background work.
    """
    if not file.filename:
        raise HTTPException(status_code=400, detail="Knowledge pack file is required")
    try:
        result = await svc.import_pack(
            fileobj=file.file,
            project_id=project_id,
            project_name=project_name,
            defer_indexing=True,
        )
        return JSONResponse(
            status_code=202,
            content=result.model_dump(),
        )
    except (BackfillScopeBusy, EmbeddingGenerationMismatch) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        message = str(exc)
        status_code = 409 if "already exists" in message or "already contain" in message else 400
        raise HTTPException(status_code=status_code, detail=message) from exc
    finally:
        await file.close()


@router.get("/projects/import/status")
async def import_status(request: Request, job_id: str | None = Query(default=None)):
    """Poll an import's index build. Omit `job_id` for the most recent."""
    status = await KnowledgePackService(request.app.state.db).import_status(job_id)
    if status is None:
        raise HTTPException(
            status_code=404,
            detail=f"No import job found for job_id={job_id!r}" if job_id
            else "No durable import receipt found",
        )
    return status
