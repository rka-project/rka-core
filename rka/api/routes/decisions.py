"""Decision tree routes.

Also hosts the decision_options (v2.2 rich multi-choice) endpoints and the
calibration_outcomes endpoints since they attach naturally to
``/decisions/{decision_id}/...``. Standalone option + calibration paths
live under ``/decision_options/{option_id}/...`` and ``/calibration/...``.
Splitting calibration to its own file becomes worthwhile once this file
exceeds ~350 lines.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from rka.models.decision import (
    Decision,
    DecisionCreate,
    DecisionSupersedeBody,
    DecisionTreeNode,
    DecisionUpdate,
)
from rka.models.decision_option import (
    DecisionOption,
    DecisionOptionCreate,
    DominatedByPayload,
    PiSelectionPayload,
)
from rka.models.calibration import (
    CalibrationMetrics,
    CalibrationOutcome,
    CalibrationOutcomeCreate,
)
from rka.services.decisions import DecisionService
from rka.services.decision_options import DecisionOptionsService
from rka.services.calibration import CalibrationService
from rka.infra.database import Database
from rka.services.academic import AcademicImportService
from rka.api.deps import (
    get_db,
    require_project,
    get_scoped_academic_service,
    get_scoped_decision_service,
    get_scoped_decision_options_service,
    get_scoped_calibration_service,
)

router = APIRouter()


@router.post("/decisions", response_model=Decision, status_code=201)
async def create_decision(
    data: DecisionCreate,
    svc: DecisionService = Depends(get_scoped_decision_service),
):
    return await svc.create(data)


@router.get("/decisions", response_model=list[Decision])
async def list_decisions(
    phase: str | None = None,
    status: str | None = None,
    parent_id: str | None = None,
    limit: int = Query(50, le=200),
    offset: int = 0,
    svc: DecisionService = Depends(get_scoped_decision_service),
):
    return await svc.list(phase=phase, status=status, parent_id=parent_id, limit=limit, offset=offset)


@router.get("/decisions/tree", response_model=list[DecisionTreeNode])
async def get_decision_tree(
    phase: str | None = None,
    active_only: bool = False,
    svc: DecisionService = Depends(get_scoped_decision_service),
):
    return await svc.get_tree(phase=phase, active_only=active_only)


# ============================================================
# Orphaned supersede chains (repair surface)
# ============================================================
# A decision marked `superseded` with no `superseded_by` tells a reader that
# it is dead but not what replaced it — the exact shape that sends a session
# hunting for a successor it cannot reach. `rka admin repair-supersedes`
# could already fix this, but only from a shell: an agent session or the web
# UI had no path to it. These are thin adapters over the same service.


class SupersessionLink(BaseModel):
    """One (superseded, replacement) pair to reconnect."""

    old_decision_id: str
    new_decision_id: str
    # Mutation is opt-in, mirroring `rka admin repair-supersedes --apply`.
    # A wrong pointer is worse than a missing one: it sends a reader
    # confidently to an unrelated decision. Default to a preview.
    apply: bool = False
    actor: str = "pi"


@router.get("/decisions/orphan-supersedes")
async def list_orphan_supersedes_route(
    project_id: str = Depends(require_project),
    db: Database = Depends(get_db),
) -> list[dict]:
    """Decisions marked superseded with no pointer to their replacement."""
    from rka.services.admin_repair import list_orphan_supersedes

    return await list_orphan_supersedes(db, project_id)


@router.post("/decisions/link-supersession")
async def link_supersession_route(
    body: SupersessionLink,
    project_id: str = Depends(require_project),
    db: Database = Depends(get_db),
) -> dict:
    """Reconnect an existing decision pair as (superseded -> replacement).

    Distinct from `POST /decisions/{id}/supersede`, which *creates* the
    replacement. Here both rows already exist and only the chain between
    them is missing, so this replays the full supersede sequence without
    creating anything: scope_version bump, superseded_by, the `supersedes`
    entity_link, the staleness cascade over claims and clusters sourced
    from the old decision's journal entries, a re_distill_review row, and
    the decision_superseded event. Idempotent — already-satisfied steps
    report ALREADY.
    """
    from rka.services.admin_repair import repair_orphan_supersedes

    reports = await repair_orphan_supersedes(
        db,
        project_id,
        {body.old_decision_id: body.new_decision_id},
        dry_run=not body.apply,
        actor=body.actor,
    )
    if not reports:
        raise HTTPException(422, "no pair supplied")
    report = reports[0]
    return {
        "applied": bool(body.apply) and report.applied,
        "dry_run": not body.apply,
        "old_decision_id": body.old_decision_id,
        "new_decision_id": body.new_decision_id,
        "rolled_back": report.rolled_back,
        # Surfaced so a refused pair says why. Without it a caller sees only
        # `applied: false` plus a FAILED step and has to guess which
        # pre-flight check rejected the pair.
        "failure_reason": report.failure_reason,
        "steps": [
            {"step": s.name, "state": s.state, "detail": s.detail}
            for s in report.steps
        ],
    }


# NOTE: these literal paths MUST stay above `/decisions/{dec_id}` — FastAPI
# matches in registration order, so a parameterised route declared first
# swallows "orphan-supersedes" as a decision id (404 "Decision
# orphan-supersedes not found"). `/decisions/tree` sits above it for the
# same reason.
@router.get("/decisions/mermaid")
async def export_decisions_mermaid(
    phase: str | None = None,
    active_only: bool = False,
    svc: AcademicImportService = Depends(get_scoped_academic_service),
):
    """Export the decision tree as a Mermaid flowchart diagram.

    The handler belongs to the academic-import service but the route must be
    declared here: `academic.py`'s router is included after this one, so
    `/decisions/mermaid` declared there is matched by `/decisions/{dec_id}`
    first and answered with "Decision mermaid not found".
    """
    mermaid = await svc.export_decisions_mermaid(phase=phase, active_only=active_only)
    return {"mermaid": mermaid}


@router.get("/decisions/{dec_id}", response_model=Decision)
async def get_decision(dec_id: str, svc: DecisionService = Depends(get_scoped_decision_service)):
    dec = await svc.get(dec_id)
    if dec is None:
        raise HTTPException(404, f"Decision {dec_id} not found")
    return dec


@router.put("/decisions/{dec_id}", response_model=Decision)
async def update_decision(
    dec_id: str,
    data: DecisionUpdate,
    actor: str = "web_ui",
    svc: DecisionService = Depends(get_scoped_decision_service),
):
    dec = await svc.get(dec_id)
    if dec is None:
        raise HTTPException(404, f"Decision {dec_id} not found")
    try:
        return await svc.update(dec_id, data, actor=actor)
    except ValueError as e:
        # Orphan-supersede guard: status='superseded' without a successor.
        raise HTTPException(422, str(e))


@router.post("/decisions/{dec_id}/supersede", response_model=Decision, status_code=201)
async def supersede_decision(
    dec_id: str,
    new_data: DecisionSupersedeBody,
    svc: DecisionService = Depends(get_scoped_decision_service),
):
    """Atomically supersede a decision and trigger re-distillation.

    v2.7.0.6 — body binds to `DecisionSupersedeBody` (phase optional)
    instead of `DecisionCreate` (phase required). When phase is omitted
    or empty, the service layer inherits it from the OLD decision.
    """
    try:
        return await svc.supersede_decision(dec_id, new_data)
    except ValueError as e:
        # 422 for inheritance-guard failures (both phases empty);
        # 404 for missing-decision; service raises ValueError for both.
        # Heuristic on the message to distinguish.
        msg = str(e)
        if "not found" in msg:
            raise HTTPException(404, msg)
        raise HTTPException(422, msg)


# ============================================================
# Decision options (v2.2 multi-choice UX substrate)
# ============================================================

async def _require_decision(dec_id: str, svc: DecisionService) -> None:
    if await svc.get(dec_id) is None:
        raise HTTPException(404, f"Decision {dec_id} not found")


@router.post(
    "/decisions/{dec_id}/options",
    response_model=DecisionOption,
    status_code=201,
)
async def create_decision_option(
    dec_id: str,
    option: DecisionOptionCreate,
    dec_svc: DecisionService = Depends(get_scoped_decision_service),
    opt_svc: DecisionOptionsService = Depends(get_scoped_decision_options_service),
):
    await _require_decision(dec_id, dec_svc)
    try:
        return await opt_svc.create(dec_id, option)
    except Exception as exc:  # FK / schema errors
        raise HTTPException(400, f"Failed to create option: {exc}")


@router.post(
    "/decisions/{dec_id}/options/bulk",
    response_model=list[DecisionOption],
    status_code=201,
)
async def create_decision_options_bulk(
    dec_id: str,
    options: list[DecisionOptionCreate],
    dec_svc: DecisionService = Depends(get_scoped_decision_service),
    opt_svc: DecisionOptionsService = Depends(get_scoped_decision_options_service),
):
    await _require_decision(dec_id, dec_svc)
    try:
        return await opt_svc.create_bulk(dec_id, options)
    except Exception as exc:
        raise HTTPException(400, f"Failed to bulk-create options: {exc}")


@router.get(
    "/decisions/{dec_id}/options",
    response_model=list[DecisionOption],
)
async def list_decision_options(
    dec_id: str,
    opt_svc: DecisionOptionsService = Depends(get_scoped_decision_options_service),
):
    return await opt_svc.list_for_decision(dec_id)


@router.get(
    "/decision_options/{option_id}",
    response_model=DecisionOption,
)
async def get_decision_option(
    option_id: str,
    opt_svc: DecisionOptionsService = Depends(get_scoped_decision_options_service),
):
    option = await opt_svc.get(option_id)
    if option is None:
        raise HTTPException(404, f"Decision option {option_id} not found")
    return option


@router.put("/decision_options/{option_id}/dominated_by")
async def set_decision_option_dominated_by(
    option_id: str,
    payload: DominatedByPayload,
    opt_svc: DecisionOptionsService = Depends(get_scoped_decision_options_service),
):
    if await opt_svc.get(option_id) is None:
        raise HTTPException(404, f"Decision option {option_id} not found")
    try:
        await opt_svc.set_dominated_by(option_id, payload.dominator_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"id": option_id, "dominated_by": payload.dominator_id}


@router.put("/decision_options/{option_id}/recommend")
async def recommend_decision_option(
    option_id: str,
    opt_svc: DecisionOptionsService = Depends(get_scoped_decision_options_service),
):
    try:
        await opt_svc.mark_recommended(option_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    return {"id": option_id, "is_recommended": True}


@router.put("/decisions/{dec_id}/pi_selection")
async def record_pi_selection(
    dec_id: str,
    payload: PiSelectionPayload,
    dec_svc: DecisionService = Depends(get_scoped_decision_service),
    opt_svc: DecisionOptionsService = Depends(get_scoped_decision_options_service),
):
    await _require_decision(dec_id, dec_svc)
    try:
        await opt_svc.record_pi_selection(
            dec_id,
            payload.selected_option_id,
            payload.override_rationale,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {
        "decision_id": dec_id,
        "selected_option_id": payload.selected_option_id,
        "override_rationale": payload.override_rationale,
    }


# ============================================================
# Calibration outcomes (Mission 1B-iii)
# ============================================================


async def _require_decision_for_outcome(dec_id: str, svc: DecisionService):
    """Fetch the decision, raise 404 if missing, 400 if no PI selection recorded.

    Outcomes only make sense for decisions the PI has resolved — either by
    selecting an option or invoking an escape hatch. Without that, there's
    nothing to measure success against.
    """
    dec = await svc.get(dec_id)
    if dec is None:
        raise HTTPException(404, f"Decision {dec_id} not found")
    if not dec.pi_selected_option_id and not dec.pi_override_rationale:
        raise HTTPException(
            400,
            f"Decision {dec_id} has no recorded PI selection (neither "
            "pi_selected_option_id nor pi_override_rationale set); cannot record outcome.",
        )
    return dec


@router.post(
    "/decisions/{dec_id}/outcomes",
    response_model=CalibrationOutcome,
    status_code=201,
)
async def record_outcome(
    dec_id: str,
    data: CalibrationOutcomeCreate,
    dec_svc: DecisionService = Depends(get_scoped_decision_service),
    cal_svc: CalibrationService = Depends(get_scoped_calibration_service),
):
    await _require_decision_for_outcome(dec_id, dec_svc)
    try:
        return await cal_svc.record(dec_id, data)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get(
    "/decisions/{dec_id}/outcomes",
    response_model=list[CalibrationOutcome],
)
async def list_outcomes_for_decision(
    dec_id: str,
    cal_svc: CalibrationService = Depends(get_scoped_calibration_service),
):
    return await cal_svc.list_for_decision(dec_id)


@router.get(
    "/calibration/outcomes",
    response_model=list[CalibrationOutcome],
)
async def list_all_outcomes(
    outcome: str | None = Query(
        None, description="Filter by outcome: succeeded | failed | mixed | unresolved"
    ),
    since: str | None = Query(
        None, description="ISO-8601 timestamp; filter to outcomes recorded at/after this time"
    ),
    cal_svc: CalibrationService = Depends(get_scoped_calibration_service),
):
    return await cal_svc.list_all(since=since, outcome_filter=outcome)


@router.get(
    "/calibration/metrics",
    response_model=CalibrationMetrics,
)
async def calibration_metrics(
    cal_svc: CalibrationService = Depends(get_scoped_calibration_service),
):
    """Compute Brier + ECE over eligible decisions in the project."""
    return await cal_svc.compute_metrics()
