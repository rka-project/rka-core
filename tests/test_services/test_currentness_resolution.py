"""I4: consistent reads, audited dispositions, replay, reflag and rollback."""

import asyncio

import pytest

from rka.models.claim import ClaimCreate, ClaimUpdate, EvidenceClusterCreate
from rka.models.journal import JournalEntryCreate
from rka.services.claims import ClaimService
from rka.services.clusters import ClusterService
from rka.services.currentness import currentness
from rka.services.graph import GraphService
from rka.services.maintenance import MaintenanceService
from rka.services.notes import NoteService
from rka.services.researcher_tools import ResearcherToolsService
from rka.services.search import SearchService


async def seed(db, kind="claim"):
    note = await NoteService(db).create(
        JournalEntryCreate(content="freshness contract source", source="executor")
    )
    if kind == "claim":
        entity = await ClaimService(db).create(
            ClaimCreate(
                source_entry_id=note.id, claim_type="evidence", content="freshness contract finding"
            )
        )
    else:
        entity = await ClusterService(db).create(
            EvidenceClusterCreate(label="freshness contract finding")
        )
    return note, entity


@pytest.mark.parametrize(
    "signals,live,warnings",
    [
        ({}, True, []),
        ({"staleness": "yellow"}, True, ["staleness:yellow"]),
        ({"staleness": "red"}, False, []),
        ({"stale": 1, "staleness_verdict": "current"}, False, []),
        ({"needs_reprocessing": 1, "staleness_verdict": "dismissed"}, False, []),
        ({"status": "superseded", "staleness_verdict": "current"}, False, []),
        ({"staleness_verdict": "retired"}, False, []),
        ({"staleness_verdict": "current"}, True, []),
        ({"staleness_verdict": "historical"}, False, []),
        ({"staleness_verdict": "retracted"}, False, []),
        ({"staleness_verdict": "superseded"}, False, []),
        ({"staleness_verdict": "dismissed"}, True, []),
    ],
)
def test_truth_table(signals, live, warnings):
    assert currentness(signals)["is_current"] is live
    assert currentness(signals)["warnings"] == warnings


@pytest.mark.parametrize("kind", ["claim", "cluster"])
@pytest.mark.parametrize(
    "verdict", ["current", "dismissed", "historical", "retired", "superseded", "retracted"]
)
async def test_resolution_roundtrip_replay_and_reflag(db, kind, verdict):
    note, entity = await seed(db, kind)
    svc = ResearcherToolsService(db)
    await svc.flag_stale(entity.id, "new evidence", "red", False)
    args = (entity.id, verdict, "reviewed evidence", "brain", note.id)
    first, retry = await asyncio.gather(svc.resolve_stale(*args), svc.resolve_stale(*args))
    assert first == retry
    assert first["audit_id"] and first["provenance_link_id"]
    assert first["currentness"]["is_current"] == (verdict in {"current", "dismissed"})
    reader = ClaimService(db) if kind == "claim" else ClusterService(db)
    stored = await reader.get(entity.id)
    assert stored.staleness_verdict == verdict
    assert stored.currentness == first["currentness"]
    nodes = {entity.id: {"id": entity.id, "type": kind}}
    await GraphService(db)._augment_node_currentness(nodes, "proj_default")
    assert nodes[entity.id]["currentness"] == first["currentness"]
    hits = await SearchService(db).search("freshness", entity_types=[kind])
    assert next(h.currentness for h in hits if h.entity_id == entity.id) == first["currentness"]
    assert entity.id not in (await svc.check_freshness())["categories"][f"stale_{kind}s"]["ids"]
    backlog = await MaintenanceService(db).get_pending_maintenance()
    assert entity.id not in backlog["categories"][f"stale_{kind}s"]["ids"]
    with pytest.raises(ValueError, match="reflag"):
        await svc.resolve_stale(entity.id, verdict, "changed intent", "brain", note.id)
    await svc.flag_stale(entity.id, "new signal", "yellow", False)
    assert (await reader.get(entity.id)).staleness_verdict is None
    second = await svc.resolve_stale(*args)
    assert second["audit_id"] != first["audit_id"]
    assert second["provenance_link_id"] == first["provenance_link_id"]


async def test_structural_invalidation_stays_hard_and_reopens(db):
    _, entity = await seed(db)
    svc = ResearcherToolsService(db)
    await ClaimService(db).update(entity.id, ClaimUpdate(stale=True))
    receipt = await svc.resolve_stale(
        entity.id, "current", "source still needs re-distillation", "pi"
    )
    assert not receipt["currentness"]["is_current"]
    assert (await ClaimService(db).get(entity.id)).staleness_reviewed_at
    await ClaimService(db).update(entity.id, ClaimUpdate(stale=True))
    assert (await ClaimService(db).get(entity.id)).staleness_reviewed_at is None
    with pytest.raises(ValueError, match="resolve_stale"):
        await ClaimService(db).update(entity.id, ClaimUpdate(stale=False))


async def test_invalid_scope_and_atomic_failure(db, monkeypatch):
    note, entity = await seed(db)
    svc = ResearcherToolsService(db)
    for target in ("clm_missing", "jrn_missing"):
        with pytest.raises(ValueError):
            await svc.flag_stale(target, "reason")
    await svc.flag_stale(entity.id, "reason", propagate=False)
    for kwargs in ({"journal_id": "jrn_missing"}, {"resolution": " "}, {"resolved_by": "system"}):
        data = dict(
            entity_id=entity.id, verdict="current", resolution="review", resolved_by="brain"
        )
        data.update(kwargs)
        with pytest.raises(ValueError):
            await svc.resolve_stale(**data)

    async def fail(*args, **kwargs):
        raise RuntimeError("audit injection")

    monkeypatch.setattr(svc, "audit", fail)
    with pytest.raises(RuntimeError):
        await svc.resolve_stale(entity.id, "current", "review", "brain", note.id)
    assert (await ClaimService(db).get(entity.id)).staleness == "yellow"
    assert not await db.fetchone(
        "SELECT id FROM entity_links WHERE source_id = ? AND target_id = ?", [note.id, entity.id]
    )
