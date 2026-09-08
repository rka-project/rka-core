"""I5/I7: project boundaries, first-writer lifecycle, and atomic retries."""

import asyncio

import pytest

from rka.models.calibration import CalibrationOutcomeCreate
from rka.models.checkpoint import CheckpointCreate, CheckpointResolve
from rka.models.claim import ClaimCreate
from rka.models.decision import DecisionCreate
from rka.models.journal import JournalEntryCreate, JournalEntryUpdate
from rka.models.mission import MissionCreate, MissionUpdate, MissionReportCreate
from rka.models.project import ProjectCreate
from rka.models.review_queue import ReviewItemCreate, ReviewItemResolve
from rka.services.calibration import CalibrationService
from rka.services.checkpoints import CheckpointService
from rka.services.claims import ClaimService
from rka.services.decisions import DecisionService
from rka.services.lifecycle import DirectiveDependencyService
from rka.services.missions import MissionService
from rka.services.notes import NoteService
from rka.services.project import ProjectService
from rka.services.review_queue import ReviewQueueService


def decision(label):
    return DecisionCreate(
        question=label,
        chosen="choice " + label,
        rationale="test",
        phase="implementation",
        decided_by="brain",
    )


async def test_supersede_dependencies_chain_retries_and_candidates(db):
    decisions, notes, deps = DecisionService(db), NoteService(db), DirectiveDependencyService(db)
    old = await decisions.create(decision("A"))
    dependent = await notes.create(
        JournalEntryCreate(type="directive", source="executor", content="conditional directive")
    )
    independent = await notes.create(
        JournalEntryCreate(
            type="directive",
            source="pi",
            capture_mode="raw_capture",
            content="independent PI principle",
            related_decisions=[old.id],
        )
    )
    bound = await deps.record(dependent.id, old.id, "pi", "conditional on A")
    assert await deps.record(dependent.id, old.id, "pi", "conditional on A") == bound
    claim = await ClaimService(db).create(
        ClaimCreate(
            source_entry_id=dependent.id, content="dependent evidence", claim_type="evidence"
        )
    )
    new, retry = await asyncio.gather(
        decisions.supersede_decision(old.id, decision("B")),
        decisions.supersede_decision(old.id, decision("B")),
    )
    assert new.id == retry.id
    assert (await notes.get(dependent.id)).status == "superseded"
    assert (await notes.get(independent.id)).status == "active"
    assert (await ClaimService(db).get(claim.id)).stale
    assert independent.id in (await deps.review_candidates())["ids"]
    third = await decisions.supersede_decision(new.id, decision("C"))
    assert (await decisions.supersede_decision(old.id, decision("B"))).id == new.id
    assert (await decisions.get(new.id)).superseded_by == third.id
    with pytest.raises(ValueError, match="different intent"):
        await decisions.supersede_decision(old.id, decision("D"))
    assert len(await decisions.list()) == 3


async def test_supersede_failure_rolls_back_new_and_old(db, monkeypatch):
    svc = DecisionService(db)
    old = await svc.create(decision("old"))
    original_audit = svc.audit

    async def fail(action, kind, entity_id, actor, details=None, **kwargs):
        if details and details.get("action") == "supersede_decision":
            raise RuntimeError("receipt failure")
        return await original_audit(action, kind, entity_id, actor, details, **kwargs)

    monkeypatch.setattr(svc, "audit", fail)
    with pytest.raises(RuntimeError):
        await svc.supersede_decision(old.id, decision("new"))
    assert (await svc.get(old.id)).status == "active"
    assert len(await svc.list()) == 1
    assert not await db.fetchone("SELECT id FROM entity_links WHERE link_type = 'supersedes'")


async def test_note_update_invalidates_derived_claim(db):
    note = await NoteService(db).create(JournalEntryCreate(content="source", source="executor"))
    claim = await ClaimService(db).create(
        ClaimCreate(source_entry_id=note.id, content="derived", claim_type="evidence")
    )
    await NoteService(db).update(
        note.id, JournalEntryUpdate(content="changed source"), actor="brain"
    )
    assert (await ClaimService(db).get(claim.id)).stale


async def test_project_write_boundaries_and_parent_cycle(db):
    project = await ProjectService(db).create_project(ProjectCreate(name="foreign I7"))
    foreign_missions = MissionService(db, project_id=project.id)
    foreign = await foreign_missions.create(
        MissionCreate(objective="other", phase="implementation")
    )
    foreign_decision = await DecisionService(db, project_id=project.id).create(decision("foreign"))
    local_svc = MissionService(db)
    local = await local_svc.create(MissionCreate(objective="local", phase="implementation"))
    with pytest.raises(ValueError, match="project"):
        await local_svc.update(local.id, MissionUpdate(parent_mission_id=foreign.id))
    with pytest.raises(ValueError, match="cycle"):
        await local_svc.update(local.id, MissionUpdate(parent_mission_id=local.id))
    with pytest.raises(ValueError):
        await ReviewQueueService(db).flag_for_review(
            ReviewItemCreate(item_type="mission", item_id=foreign.id, flag="stale_dependency")
        )
    with pytest.raises(ValueError):
        await CalibrationService(db).record(
            foreign_decision.id, CalibrationOutcomeCreate(outcome="succeeded")
        )
    with pytest.raises(ValueError):
        await DirectiveDependencyService(db).record("jrn_missing", foreign_decision.id, "pi", "bad")
    assert (await local_svc.get(local.id)).parent_mission_id is None
    assert not await db.fetchone("SELECT id FROM calibration_outcomes")
    assert not await db.fetchone("SELECT id FROM review_queue")


async def test_checkpoint_resolve_exact_retry_conflict_and_race(db):
    mission = await MissionService(db).create(
        MissionCreate(objective="checkpoint", phase="implementation")
    )
    svc, decisions = CheckpointService(db), DecisionService(db)
    checkpoint = await svc.create(
        CheckpointCreate(mission_id=mission.id, type="decision", description="choose one")
    )
    data = CheckpointResolve(
        resolution="approved", resolved_by="pi", rationale="reviewed", create_decision=True
    )
    first, retry = await asyncio.gather(
        svc.resolve(checkpoint.id, data, decisions), svc.resolve(checkpoint.id, data, decisions)
    )
    assert first.linked_decision_id == retry.linked_decision_id
    assert len(await decisions.list()) == 1
    with pytest.raises(ValueError, match="already closed"):
        await svc.resolve(
            checkpoint.id, data.model_copy(update={"resolution": "rejected"}), decisions
        )
    assert (await svc.get(checkpoint.id)).linked_decision_id == first.linked_decision_id


async def test_report_first_writer_and_terminal_guards(db):
    svc = MissionService(db)
    mission = await svc.create(MissionCreate(objective="report", phase="implementation"))
    data = MissionReportCreate(summary="done", findings=["one finding"])
    first, retry = await asyncio.gather(
        svc.submit_report(mission.id, data), svc.submit_report(mission.id, data)
    )
    assert first.report == retry.report
    count = await db.fetchone(
        "SELECT COUNT(*) AS n FROM journal WHERE related_mission = ?", [mission.id]
    )
    assert count["n"] == 1
    with pytest.raises(ValueError, match="overwritten"):
        await svc.submit_report(mission.id, data.model_copy(update={"summary": "different"}))
    with pytest.raises(ValueError, match="immutable"):
        await svc.update(mission.id, MissionUpdate(status="active"))
    cancelled = await svc.create(MissionCreate(objective="cancelled", phase="implementation"))
    await svc.update(cancelled.id, MissionUpdate(status="cancelled"))
    with pytest.raises(ValueError, match="cancelled"):
        await svc.submit_report(cancelled.id, data)
    assert (await svc.get(cancelled.id)).report is None


async def test_review_resolution_no_overwrite(db):
    note = await NoteService(db).create(
        JournalEntryCreate(content="review source", source="executor")
    )
    svc = ReviewQueueService(db)
    review = await svc.flag_for_review(
        ReviewItemCreate(item_type="journal", item_id=note.id, flag="stale_dependency")
    )
    data = ReviewItemResolve(resolved_by="brain", resolution="accepted")
    first = await svc.resolve(review.id, data)
    assert await svc.resolve(review.id, data) == first
    with pytest.raises(ValueError, match="already closed"):
        await svc.resolve(review.id, data.model_copy(update={"resolution": "changed"}))
