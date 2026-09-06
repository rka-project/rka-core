"""FastAPI dependency injection — DB and service instances via app.state."""

from __future__ import annotations

import logging

from fastapi import Depends, Header, HTTPException, Query, Request

from rka.config import RKAConfig
from rka.infra.database import Database
from rka.infra.llm import LLMClient
from rka.infra.embeddings import EmbeddingService
from rka.services.project import ProjectService
from rka.services.notes import NoteService
from rka.services.decisions import DecisionService
from rka.services.literature import LiteratureService
from rka.services.missions import MissionService
from rka.services.checkpoints import CheckpointService
from rka.services.events import EventService
from rka.services.search import SearchService
from rka.services.context import ContextEngine
from rka.services.audit import AuditService
from rka.services.academic import AcademicImportService
from rka.services.workspace import WorkspaceService
from rka.services.graph import GraphService
from rka.services.summary import SummaryService, QAService
from rka.services.artifacts import ArtifactService
from rka.services.knowledge_pack import KnowledgePackService
from rka.services.claims import ClaimService
from rka.services.clusters import ClusterService
from rka.services.topics import TopicService
from rka.services.research_map import ResearchMapService
from rka.services.review_queue import ReviewQueueService
from rka.services.onboarding import OnboardingService
from rka.services.researcher_tools import ResearcherToolsService
from rka.services.decision_options import DecisionOptionsService
from rka.services.calibration import CalibrationService
from rka.services.hooks_service import HooksService
from rka.services.interpretation import InterpretationService
from rka.services.experiments import ExperimentService
from rka.services.planning import ManuscriptPlanningService
from rka.services.semantic_patch import SemanticPatchService
from rka.services.manuscript_source import ManuscriptSourceService
from rka.services.sources import SourceService

logger = logging.getLogger(__name__)


# ---- Core infrastructure dependencies (read from app.state) ----

def get_config(request: Request) -> RKAConfig:
    return request.app.state.config


def get_db(request: Request) -> Database:
    db = getattr(request.app.state, "db", None)
    if db is None:
        raise RuntimeError("Database not initialized")
    return db


def get_llm(request: Request) -> LLMClient | None:
    return getattr(request.app.state, "llm", None)


def get_embeddings(request: Request) -> EmbeddingService | None:
    return getattr(request.app.state, "embeddings", None)


def get_context_engine(request: Request) -> ContextEngine | None:
    return getattr(request.app.state, "context", None)


# ---- Project scoping ----

def get_project_id(
    x_rka_project: str | None = Header(default=None, alias="X-RKA-Project"),
    project_id: str | None = Query(default=None),
) -> str:
    """Resolve the project id for a scoped operation. Explicit or nothing.

    This used to fall back to ``DEFAULT_PROJECT_ID`` when neither the header
    nor the query parameter was supplied. That default was silent, and silence
    is the problem: a scoped write with no project did not fail, it landed in
    the default project. Thirty entities in this database got there that way —
    journal entries, claims and decisions whose typed links all point into
    other projects, written between March and May 2026.

    Nothing legitimate relies on the fallback. The web client always sends the
    header, and every project-scoped MCP operation carries ``project_id`` as a
    required field. What relied on it was callers that forgot — which is
    exactly the case that must fail loudly.

    Unscoped endpoints (``list_projects``, ``create_project``, ``health``,
    pack import) do not depend on this function and are unaffected.
    """
    resolved = (x_rka_project or project_id or "").strip()
    if not resolved:
        raise HTTPException(
            status_code=422,
            detail=(
                "Project scope is required: pass the X-RKA-Project header or a "
                "project_id query parameter. This endpoint no longer falls back "
                "to a default project, because doing so silently filed data "
                "under the wrong project."
            ),
        )
    return resolved


def get_transport_actor(
    x_rka_actor: str | None = Header(default=None, alias="X-RKA-Actor"),
) -> str:
    """Attribute the request transport for audit records.

    Direct REST/UI calls default to ``web_ui``. The bundled MCP proxy sends
    ``executor`` explicitly. This is provenance metadata and a local
    interaction guard, not a network authentication boundary.
    """
    actor = (x_rka_actor or "web_ui").strip()
    if actor not in {"executor", "web_ui"}:
        raise HTTPException(
            status_code=422,
            detail="X-RKA-Actor must be 'executor' or 'web_ui'",
        )
    return actor


async def require_project(
    project_id: str = Depends(get_project_id),
    db: Database = Depends(get_db),
) -> str:
    """Validate project exists before executing project-scoped operations."""
    row = await db.fetchone("SELECT id FROM projects WHERE id = ?", [project_id])
    if not row:
        raise HTTPException(status_code=404, detail=f"Project '{project_id}' not found")
    return project_id


# ---- Service factories ----
# These construct per-request service instances using app.state singletons.
# The `_scoped` variants validate the project exists first.

def get_project_service(db: Database = Depends(get_db)) -> ProjectService:
    return ProjectService(db)


def get_search_service(
    request: Request,
    project_id: str = Depends(get_project_id),
    db: Database = Depends(get_db),
    embeddings: EmbeddingService | None = Depends(get_embeddings),
) -> SearchService:
    search = getattr(request.app.state, "search", None)
    if search is not None:
        return search.with_project(project_id)
    return SearchService(db=db, embeddings=embeddings, project_id=project_id)


def get_scoped_search_service(
    request: Request,
    project_id: str = Depends(require_project),
    db: Database = Depends(get_db),
    embeddings: EmbeddingService | None = Depends(get_embeddings),
) -> SearchService:
    search = getattr(request.app.state, "search", None)
    if search is not None:
        return search.with_project(project_id)
    return SearchService(db=db, embeddings=embeddings, project_id=project_id)


def get_note_service(
    project_id: str = Depends(get_project_id),
    db: Database = Depends(get_db),
    llm: LLMClient | None = Depends(get_llm),
    embeddings: EmbeddingService | None = Depends(get_embeddings),
) -> NoteService:
    return NoteService(db, llm=llm, embeddings=embeddings, project_id=project_id)


def get_scoped_note_service(
    project_id: str = Depends(require_project),
    db: Database = Depends(get_db),
    llm: LLMClient | None = Depends(get_llm),
    embeddings: EmbeddingService | None = Depends(get_embeddings),
) -> NoteService:
    return NoteService(db, llm=llm, embeddings=embeddings, project_id=project_id)


def get_decision_service(
    project_id: str = Depends(get_project_id),
    db: Database = Depends(get_db),
    llm: LLMClient | None = Depends(get_llm),
    embeddings: EmbeddingService | None = Depends(get_embeddings),
) -> DecisionService:
    return DecisionService(db, llm=llm, embeddings=embeddings, project_id=project_id)


def get_scoped_decision_service(
    project_id: str = Depends(require_project),
    db: Database = Depends(get_db),
    llm: LLMClient | None = Depends(get_llm),
    embeddings: EmbeddingService | None = Depends(get_embeddings),
) -> DecisionService:
    return DecisionService(db, llm=llm, embeddings=embeddings, project_id=project_id)


def get_scoped_decision_options_service(
    project_id: str = Depends(require_project),
    db: Database = Depends(get_db),
) -> DecisionOptionsService:
    return DecisionOptionsService(db, project_id=project_id)


def get_scoped_calibration_service(
    project_id: str = Depends(require_project),
    db: Database = Depends(get_db),
) -> CalibrationService:
    return CalibrationService(db, project_id=project_id)


def get_scoped_hooks_service(
    project_id: str = Depends(require_project),
    db: Database = Depends(get_db),
) -> HooksService:
    return HooksService(db, project_id=project_id)


def get_literature_service(
    project_id: str = Depends(get_project_id),
    db: Database = Depends(get_db),
    llm: LLMClient | None = Depends(get_llm),
    embeddings: EmbeddingService | None = Depends(get_embeddings),
) -> LiteratureService:
    return LiteratureService(db, llm=llm, embeddings=embeddings, project_id=project_id)


def get_scoped_literature_service(
    project_id: str = Depends(require_project),
    db: Database = Depends(get_db),
    llm: LLMClient | None = Depends(get_llm),
    embeddings: EmbeddingService | None = Depends(get_embeddings),
) -> LiteratureService:
    return LiteratureService(db, llm=llm, embeddings=embeddings, project_id=project_id)


def get_mission_service(
    project_id: str = Depends(get_project_id),
    db: Database = Depends(get_db),
    llm: LLMClient | None = Depends(get_llm),
    embeddings: EmbeddingService | None = Depends(get_embeddings),
) -> MissionService:
    return MissionService(db, llm=llm, embeddings=embeddings, project_id=project_id)


def get_scoped_mission_service(
    project_id: str = Depends(require_project),
    db: Database = Depends(get_db),
    llm: LLMClient | None = Depends(get_llm),
    embeddings: EmbeddingService | None = Depends(get_embeddings),
) -> MissionService:
    return MissionService(db, llm=llm, embeddings=embeddings, project_id=project_id)


def get_checkpoint_service(
    project_id: str = Depends(get_project_id),
    db: Database = Depends(get_db),
) -> CheckpointService:
    return CheckpointService(db, project_id=project_id)


def get_scoped_checkpoint_service(
    project_id: str = Depends(require_project),
    db: Database = Depends(get_db),
) -> CheckpointService:
    return CheckpointService(db, project_id=project_id)


def get_event_service(
    project_id: str = Depends(get_project_id),
    db: Database = Depends(get_db),
) -> EventService:
    return EventService(db, project_id=project_id)


def get_scoped_event_service(
    project_id: str = Depends(require_project),
    db: Database = Depends(get_db),
) -> EventService:
    return EventService(db, project_id=project_id)


def get_audit_service(
    project_id: str = Depends(get_project_id),
    db: Database = Depends(get_db),
) -> AuditService:
    return AuditService(db, project_id=project_id)


def get_scoped_audit_service(
    project_id: str = Depends(require_project),
    db: Database = Depends(get_db),
) -> AuditService:
    return AuditService(db, project_id=project_id)


def get_academic_service(
    config: RKAConfig = Depends(get_config),
    project_id: str = Depends(get_project_id),
    db: Database = Depends(get_db),
    llm: LLMClient | None = Depends(get_llm),
    embeddings: EmbeddingService | None = Depends(get_embeddings),
) -> AcademicImportService:
    lit_svc = LiteratureService(db, llm=llm, embeddings=embeddings, project_id=project_id)
    note_svc = NoteService(db, llm=llm, embeddings=embeddings, project_id=project_id)
    return AcademicImportService(lit_svc, note_service=note_svc, file_policy=config.file_access_policy)


def get_scoped_academic_service(
    config: RKAConfig = Depends(get_config),
    project_id: str = Depends(require_project),
    db: Database = Depends(get_db),
    llm: LLMClient | None = Depends(get_llm),
    embeddings: EmbeddingService | None = Depends(get_embeddings),
) -> AcademicImportService:
    lit_svc = LiteratureService(db, llm=llm, embeddings=embeddings, project_id=project_id)
    note_svc = NoteService(db, llm=llm, embeddings=embeddings, project_id=project_id)
    return AcademicImportService(lit_svc, note_service=note_svc, file_policy=config.file_access_policy)


def get_workspace_service(
    config: RKAConfig = Depends(get_config),
    project_id: str = Depends(get_project_id),
    db: Database = Depends(get_db),
    llm: LLMClient | None = Depends(get_llm),
    embeddings: EmbeddingService | None = Depends(get_embeddings),
) -> WorkspaceService:
    lit_svc = LiteratureService(db, llm=llm, embeddings=embeddings, project_id=project_id)
    note_svc = NoteService(db, llm=llm, embeddings=embeddings, project_id=project_id)
    academic_svc = AcademicImportService(lit_svc, note_service=note_svc, file_policy=config.file_access_policy)
    return WorkspaceService(
        db=db,
        academic_service=academic_svc,
        note_service=note_svc,
        literature_service=lit_svc,
        file_policy=config.file_access_policy,
        llm=llm,
    )


def get_scoped_workspace_service(
    config: RKAConfig = Depends(get_config),
    project_id: str = Depends(require_project),
    db: Database = Depends(get_db),
    llm: LLMClient | None = Depends(get_llm),
    embeddings: EmbeddingService | None = Depends(get_embeddings),
) -> WorkspaceService:
    lit_svc = LiteratureService(db, llm=llm, embeddings=embeddings, project_id=project_id)
    note_svc = NoteService(db, llm=llm, embeddings=embeddings, project_id=project_id)
    academic_svc = AcademicImportService(lit_svc, note_service=note_svc, file_policy=config.file_access_policy)
    return WorkspaceService(
        db=db,
        academic_service=academic_svc,
        note_service=note_svc,
        literature_service=lit_svc,
        file_policy=config.file_access_policy,
        llm=llm,
    )


def get_graph_service(db: Database = Depends(get_db)) -> GraphService:
    return GraphService(db)


def get_summary_service(
    project_id: str = Depends(get_project_id),
    db: Database = Depends(get_db),
    llm: LLMClient | None = Depends(get_llm),
    embeddings: EmbeddingService | None = Depends(get_embeddings),
) -> SummaryService:
    return SummaryService(db, llm=llm, embeddings=embeddings, project_id=project_id)


def get_scoped_summary_service(
    project_id: str = Depends(require_project),
    db: Database = Depends(get_db),
    llm: LLMClient | None = Depends(get_llm),
    embeddings: EmbeddingService | None = Depends(get_embeddings),
) -> SummaryService:
    return SummaryService(db, llm=llm, embeddings=embeddings, project_id=project_id)


def get_qa_service(
    project_id: str = Depends(get_project_id),
    db: Database = Depends(get_db),
    llm: LLMClient | None = Depends(get_llm),
    embeddings: EmbeddingService | None = Depends(get_embeddings),
) -> QAService:
    return QAService(db, llm=llm, embeddings=embeddings, project_id=project_id)


def get_scoped_qa_service(
    project_id: str = Depends(require_project),
    db: Database = Depends(get_db),
    llm: LLMClient | None = Depends(get_llm),
    embeddings: EmbeddingService | None = Depends(get_embeddings),
) -> QAService:
    return QAService(db, llm=llm, embeddings=embeddings, project_id=project_id)


def get_artifact_service(
    config: RKAConfig = Depends(get_config),
    project_id: str = Depends(get_project_id),
    db: Database = Depends(get_db),
    llm: LLMClient | None = Depends(get_llm),
    embeddings: EmbeddingService | None = Depends(get_embeddings),
) -> ArtifactService:
    return ArtifactService(db, llm=llm, embeddings=embeddings, project_id=project_id, file_policy=config.file_access_policy)


def get_scoped_artifact_service(
    config: RKAConfig = Depends(get_config),
    project_id: str = Depends(require_project),
    db: Database = Depends(get_db),
    llm: LLMClient | None = Depends(get_llm),
    embeddings: EmbeddingService | None = Depends(get_embeddings),
) -> ArtifactService:
    return ArtifactService(db, llm=llm, embeddings=embeddings, project_id=project_id, file_policy=config.file_access_policy)


def get_scoped_source_service(
    project_id: str = Depends(require_project),
    db: Database = Depends(get_db),
    config: RKAConfig = Depends(get_config),
) -> SourceService:
    return SourceService(
        db,
        project_id=project_id,
        max_bytes=config.registered_source_max_bytes,
        file_policy=config.file_access_policy,
    )


def get_knowledge_pack_service(
    db: Database = Depends(get_db),
    llm: LLMClient | None = Depends(get_llm),
    embeddings: EmbeddingService | None = Depends(get_embeddings),
) -> KnowledgePackService:
    """Pack service for the one operation that has no request project: import.

    Importing a pack *creates* the project — its target comes from the upload
    or the form, never from the request scope. This factory used to resolve a
    request project anyway and hand it over unused, which was harmless while
    an unsupplied scope silently became `proj_default`. Once that default was
    removed, importing a pack started failing with "Project scope is required"
    — an error about a value the operation does not read.

    Use `get_scoped_knowledge_pack_service` for export, which is scoped.
    """
    return KnowledgePackService(db, llm=llm, embeddings=embeddings)


def get_scoped_knowledge_pack_service(
    project_id: str = Depends(require_project),
    db: Database = Depends(get_db),
    llm: LLMClient | None = Depends(get_llm),
    embeddings: EmbeddingService | None = Depends(get_embeddings),
) -> KnowledgePackService:
    return KnowledgePackService(db, llm=llm, embeddings=embeddings, project_id=project_id)


# ---- v2.0 service factories ----

def get_scoped_claim_service(
    project_id: str = Depends(require_project),
    db: Database = Depends(get_db),
    llm: LLMClient | None = Depends(get_llm),
    embeddings: EmbeddingService | None = Depends(get_embeddings),
) -> ClaimService:
    return ClaimService(db, llm=llm, embeddings=embeddings, project_id=project_id)


def get_scoped_interpretation_service(
    project_id: str = Depends(require_project),
    db: Database = Depends(get_db),
    embeddings: EmbeddingService | None = Depends(get_embeddings),
) -> InterpretationService:
    return InterpretationService(db, embeddings=embeddings, project_id=project_id)


def get_scoped_experiment_service(
    project_id: str = Depends(require_project),
    db: Database = Depends(get_db),
    embeddings: EmbeddingService | None = Depends(get_embeddings),
) -> ExperimentService:
    return ExperimentService(db, embeddings=embeddings, project_id=project_id)


def get_scoped_manuscript_planning_service(
    project_id: str = Depends(require_project),
    db: Database = Depends(get_db),
) -> ManuscriptPlanningService:
    return ManuscriptPlanningService(db, project_id=project_id)


def get_scoped_semantic_patch_service(
    project_id: str = Depends(require_project),
    db: Database = Depends(get_db),
) -> SemanticPatchService:
    return SemanticPatchService(db, project_id=project_id)


def get_scoped_manuscript_source_service(
    config: RKAConfig = Depends(get_config),
    project_id: str = Depends(require_project),
    db: Database = Depends(get_db),
) -> ManuscriptSourceService:
    return ManuscriptSourceService(db, config=config, project_id=project_id)


def get_scoped_cluster_service(
    project_id: str = Depends(require_project),
    db: Database = Depends(get_db),
    llm: LLMClient | None = Depends(get_llm),
    embeddings: EmbeddingService | None = Depends(get_embeddings),
) -> ClusterService:
    return ClusterService(db, llm=llm, embeddings=embeddings, project_id=project_id)


def get_scoped_topic_service(
    project_id: str = Depends(require_project),
    db: Database = Depends(get_db),
) -> TopicService:
    return TopicService(db, project_id=project_id)


def get_scoped_research_map_service(
    project_id: str = Depends(require_project),
    db: Database = Depends(get_db),
) -> ResearchMapService:
    return ResearchMapService(db, project_id=project_id)


def get_scoped_review_queue_service(
    project_id: str = Depends(require_project),
    db: Database = Depends(get_db),
) -> ReviewQueueService:
    return ReviewQueueService(db, project_id=project_id)


def get_scoped_onboarding_service(
    project_id: str = Depends(require_project),
    db: Database = Depends(get_db),
) -> OnboardingService:
    return OnboardingService(db, project_id=project_id)


def get_scoped_researcher_tools_service(
    project_id: str = Depends(require_project),
    db: Database = Depends(get_db),
) -> ResearcherToolsService:
    return ResearcherToolsService(db, project_id=project_id)
