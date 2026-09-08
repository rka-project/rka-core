"""Acceptance boundaries for I4-I7, using only synthetic project data."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from rka.models.checkpoint import CheckpointCreate, CheckpointResolve
from rka.models.claim import ClaimCreate, EvidenceClusterCreate
from rka.models.decision import DecisionCreate, DecisionUpdate
from rka.models.journal import JournalEntryCreate
from rka.models.mission import MissionCreate, MissionReportCreate, MissionUpdate
from rka.models.project import ProjectCreate
from rka.models.review_queue import ReviewItemCreate
from rka.services.admin_repair import repair_orphan_supersedes
from rka.services.checkpoints import CheckpointService
from rka.services.claims import ClaimService
from rka.services.clusters import ClusterService
from rka.services.decisions import DecisionService
from rka.services.lifecycle import DirectiveDependencyService, find_affected_entries
from rka.services.maintenance import MaintenanceService
from rka.services.missions import MissionService
from rka.services.notes import NoteService
from rka.services.project import ProjectService
from rka.services.researcher_tools import ResearcherToolsService
from rka.services.review_queue import ReviewQueueService
from rka.services.search import SearchService


def decision(label):
    return DecisionCreate(
        question=label,
        chosen="choice",
        rationale="test",
        phase="implementation",
        decided_by="brain",
    )


async def claim(db, project_id="proj_default"):
    note = await NoteService(db, project_id=project_id).create(
        JournalEntryCreate(content="equivalent currency finding", source="executor")
    )
    value = await ClaimService(db, project_id=project_id).create(
        ClaimCreate(
            source_entry_id=note.id, content="equivalent currency finding", claim_type="evidence"
        )
    )
    return note, value


@pytest.mark.parametrize("kind", ["claim", "cluster"])
@pytest.mark.parametrize("verdict", ["current", "dismissed", "historical"])
async def test_aging_uses_review_time_and_excludes_inactive(db, kind, verdict):
    if kind == "claim":
        _, entity = await claim(db)
        table = "claims"
    else:
        entity = await ClusterService(db).create(EvidenceClusterCreate(label="aging"))
        table = "evidence_clusters"
    svc = ResearcherToolsService(db)
    await svc.flag_stale(entity.id, "review", propagate=False)
    await svc.resolve_stale(entity.id, verdict, "reviewed now", "brain")
    category = f"aging_{kind}s"
    assert entity.id not in (await svc.check_freshness())["categories"][category]["ids"]
    await db.execute(
        f"UPDATE {table} SET staleness_reviewed_at = '2000-01-01T00:00:00Z' WHERE id = ?",
        [entity.id],
    )
    assert (entity.id in (await svc.check_freshness())["categories"][category]["ids"]) == (
        verdict != "historical"
    )


async def test_resolution_scope_and_queue_atomicity(db, monkeypatch):
    project = await ProjectService(db).create_project(ProjectCreate(name="foreign freshness"))
    foreign_note, foreign_claim = await claim(db, project.id)
    note, local = await claim(db)
    svc = ResearcherToolsService(db)
    await svc.flag_stale(local.id, "review", propagate=False)
    for entity_id, journal_id in ((foreign_claim.id, note.id), (local.id, foreign_note.id)):
        with pytest.raises(ValueError):
            await svc.resolve_stale(entity_id, "current", "review", "brain", journal_id)
    queue = ReviewQueueService(db)
    review = await queue.flag_for_review(
        ReviewItemCreate(item_type="claim", item_id=local.id, flag="stale_dependency")
    )
    unrelated = await queue.flag_for_review(
        ReviewItemCreate(item_type="claim", item_id=local.id, flag="potential_contradiction")
    )
    audit = svc.audit
    monkeypatch.setattr(svc, "audit", AsyncMock(side_effect=RuntimeError("audit failure")))
    with pytest.raises(RuntimeError):
        await svc.resolve_stale(local.id, "current", "review", "brain", note.id)
    assert (await queue.get(review.id)).status == "pending"
    assert (await ClaimService(db).get(local.id)).staleness_reviewed_at is None
    monkeypatch.setattr(svc, "audit", audit)
    await svc.resolve_stale(local.id, "current", "review", "brain", note.id)
    assert (await queue.get(review.id)).status == "resolved"
    assert (await queue.get(unrelated.id)).status == "pending"
    await svc.flag_stale(local.id, "new signal", propagate=False)
    assert local.id in (await svc.check_freshness())["categories"]["stale_claims"]["ids"]
    assert (await queue.get(review.id)).status == "resolved"  # historical receipt is immutable


async def test_search_current_precedes_equivalent_inactive(db):
    _, older = await claim(db)
    _, newer = await claim(db)
    svc = ResearcherToolsService(db)
    await svc.flag_stale(newer.id, "archive", propagate=False)
    await svc.resolve_stale(newer.id, "historical", "retained for provenance", "pi")
    hits = await SearchService(db).search("equivalent currency finding", entity_types=["claim"])
    ids = [h.entity_id for h in hits]
    assert ids.index(older.id) < ids.index(newer.id)


async def test_retired_cluster_no_synthesis_backlog_but_current_hard_flag_remains(db):
    cluster = await ClusterService(db).create(EvidenceClusterCreate(label="archive cluster"))
    await db.execute(
        "UPDATE evidence_clusters SET needs_reprocessing = 1, claim_count = 1 WHERE id = ?",
        [cluster.id],
    )
    svc = ResearcherToolsService(db)
    await svc.resolve_stale(cluster.id, "current", "requires rebuilding", "brain")
    maintenance = MaintenanceService(db)
    assert (
        cluster.id
        in (await maintenance.get_pending_maintenance())["categories"][
            "clusters_needing_synthesis"
        ]["ids"]
    )
    await svc.flag_stale(cluster.id, "no longer needed", propagate=False)
    await svc.resolve_stale(cluster.id, "retired", "archive", "pi")
    assert (
        cluster.id
        not in (await maintenance.get_pending_maintenance())["categories"][
            "clusters_needing_synthesis"
        ]["ids"]
    )


async def test_competing_supersede_and_generic_reopen_rejected(db):
    svc = DecisionService(db)
    old = await svc.create(decision("A"))
    results = await asyncio.gather(
        svc.supersede_decision(old.id, decision("B")),
        svc.supersede_decision(old.id, decision("C")),
        return_exceptions=True,
    )
    assert sum(isinstance(r, ValueError) for r in results) == 1
    assert len(await svc.list()) == 2
    with pytest.raises(ValueError, match="cannot be reopened"):
        await svc.update(old.id, DecisionUpdate(status="active"))
    head = await svc.get((await svc.get(old.id)).superseded_by)
    with pytest.raises(ValueError, match="successor must be active"):
        await svc.supersede_decision(
            head.id, decision("D").model_copy(update={"status": "abandoned"})
        )
    assert len(await svc.list()) == 2


async def test_admin_dry_run_apply_and_retry_use_shared_dependencies(db):
    svc, notes, deps = DecisionService(db), NoteService(db), DirectiveDependencyService(db)
    old, new = await svc.create(decision("old")), await svc.create(decision("new"))
    note = await notes.create(
        JournalEntryCreate(type="directive", content="conditional", source="executor")
    )
    await deps.record(note.id, old.id, "pi", "depends on old")
    derived = await ClaimService(db).create(
        ClaimCreate(source_entry_id=note.id, content="derived", claim_type="evidence")
    )
    await db.execute("UPDATE decisions SET status = 'superseded' WHERE id = ?", [old.id])
    assert note.id in await find_affected_entries(db, "proj_default", old.id)
    mapping = {old.id: new.id}
    preview = await repair_orphan_supersedes(db, "proj_default", mapping, dry_run=True)
    assert not preview[0].applied and not preview[0].rolled_back
    assert (await notes.get(note.id)).status == "active"
    applied = await repair_orphan_supersedes(db, "proj_default", mapping, dry_run=False)
    assert applied[0].applied and not applied[0].rolled_back
    assert (await notes.get(note.id)).status == "superseded"
    review = await ResearcherToolsService(db).resolve_stale(
        derived.id, "historical", "old evidence", "pi"
    )
    await repair_orphan_supersedes(db, "proj_default", mapping, dry_run=False)
    assert (await ClaimService(db).get(derived.id)).staleness_reviewed_at == review[
        "staleness_reviewed_at"
    ]


@pytest.mark.parametrize("aggregate", ["checkpoint", "report"])
async def test_aggregate_audit_failure_does_not_leave_child_or_terminal_state(
    db, monkeypatch, aggregate
):
    missions = MissionService(db)
    mission = await missions.create(
        MissionCreate(objective="failure injection", phase="implementation")
    )
    if aggregate == "checkpoint":
        svc = CheckpointService(db)
        checkpoint = await svc.create(
            CheckpointCreate(mission_id=mission.id, type="decision", description="choose")
        )
        monkeypatch.setattr(svc, "audit", AsyncMock(side_effect=RuntimeError("audit failure")))
        with pytest.raises(RuntimeError):
            await svc.resolve(
                checkpoint.id,
                CheckpointResolve(resolution="accept", resolved_by="pi", create_decision=True),
                DecisionService(db),
            )
        assert (await svc.get(checkpoint.id)).status == "open"
        assert not await DecisionService(db).list()
    else:
        monkeypatch.setattr(missions, "audit", AsyncMock(side_effect=RuntimeError("audit failure")))
        with pytest.raises(RuntimeError):
            await missions.submit_report(
                mission.id, MissionReportCreate(summary="done", findings=["finding"])
            )
        assert (await missions.get(mission.id)).report is None
        assert not await db.fetchone(
            "SELECT id FROM journal WHERE related_mission = ?", [mission.id]
        )


async def test_parent_two_node_cycle_rejected(db):
    svc = MissionService(db)
    a = await svc.create(MissionCreate(objective="A", phase="implementation"))
    b = await svc.create(MissionCreate(objective="B", phase="implementation"))
    await svc.update(a.id, MissionUpdate(parent_mission_id=b.id))
    with pytest.raises(ValueError, match="cycle"):
        await svc.update(b.id, MissionUpdate(parent_mission_id=a.id))
    assert (await svc.get(b.id)).parent_mission_id is None


async def test_read_call_chains_ignore_legacy_cross_project_rows(db):
    from rka.services.research_map import ResearchMapService

    foreign = await ProjectService(db).create_project(ProjectCreate(name="foreign read boundary"))
    rq = await DecisionService(db).create(
        decision("local RQ").model_copy(update={"kind": "research_question"})
    )
    # Simulate legacy malformed associations, without bypassing production.
    await db.execute(
        "INSERT INTO tags (tag, entity_type, entity_id, project_id) VALUES ('rq:foreign-secret', 'decision', ?, ?)",
        [rq.id, foreign.id],
    )
    result = await ResearchMapService(db).get_research_questions()
    assert next(row["status"] for row in result if row["id"] == rq.id) == "active"
    _, source = await claim(db)
    cluster = await ClusterService(db).create(EvidenceClusterCreate(label="local cluster"))
    await db.execute(
        """INSERT INTO claim_edges (id, source_claim_id, cluster_id, relation, project_id)
        VALUES ('edge_foreign', ?, ?, 'member_of', ?)""",
        [source.id, cluster.id, foreign.id],
    )
    llm = AsyncMock()
    synthesized = await ClusterService(db, llm=llm).process_theme_synthesize_job(cluster.id)
    assert synthesized == {"outcome": "noop", "reason": "no_claims"}
    llm.synthesize_theme.assert_not_awaited()
    assert await ResearchMapService(db).get_claims_for_cluster(cluster.id) == []


async def test_foreign_directive_untouched_and_edge_direction_is_not_dependency(db):
    foreign = await ProjectService(db).create_project(ProjectCreate(name="foreign lifecycle"))
    old = await DecisionService(db).create(decision("old"))
    foreign_note = await NoteService(db, project_id=foreign.id).create(
        JournalEntryCreate(
            type="directive", content="foreign principle", source="pi", capture_mode="raw_capture"
        )
    )
    # Historical malformed JSON must not pull another project's journal into
    # the local cascade, even when the ID happens to match the old decision.
    await db.execute(
        "UPDATE journal SET related_decisions = json_array(?) WHERE id = ?",
        [old.id, foreign_note.id],
    )
    local = await NoteService(db).create(
        JournalEntryCreate(content="independent supporting evidence", source="executor")
    )
    await NoteService(db).add_link("decision", old.id, "justified_by", "journal", local.id)
    assert foreign_note.id not in await find_affected_entries(db, "proj_default", old.id)
    assert local.id not in await find_affected_entries(db, "proj_default", old.id)
    await DecisionService(db).supersede_decision(old.id, decision("new"))
    assert (await NoteService(db, project_id=foreign.id).get(foreign_note.id)).status == "active"
    assert (await NoteService(db).get(local.id)).status == "active"
