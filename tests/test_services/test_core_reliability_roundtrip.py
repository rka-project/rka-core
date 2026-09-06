"""Core journal/provenance reliability contracts from roadmap issue #123."""

from __future__ import annotations

from pathlib import Path

import pytest

from rka.infra.database import Database
from rka.models.decision import DecisionCreate, DecisionUpdate
from rka.models.journal import JournalEntryCreate, JournalEntryUpdate
from rka.models.literature import LiteratureCreate
from rka.models.mission import MissionCreate
from rka.services.artifacts import ArtifactService
from rka.services.base import BaseService
from rka.services.decisions import DecisionService
from rka.services.literature import LiteratureService
from rka.services.missions import MissionService
from rka.services.notes import NoteService


async def _ensure_project(db: Database, project_id: str) -> None:
    await db.execute(
        "INSERT OR IGNORE INTO projects (id, name, description, created_by) "
        "VALUES (?, ?, ?, ?)",
        [project_id, project_id, "Core reliability fixture", "system"],
    )


@pytest.mark.asyncio
async def test_foreign_relation_is_rejected_without_partial_journal_write(
    db: Database,
) -> None:
    await _ensure_project(db, "prj_alpha")
    await _ensure_project(db, "prj_beta")
    foreign = await DecisionService(
        db, project_id="prj_beta"
    ).create(
        DecisionCreate(
            question="Foreign decision?",
            phase="design",
            decided_by="brain",
        )
    )

    alpha = NoteService(db, project_id="prj_alpha")
    with pytest.raises(ValueError, match="project prj_alpha"):
        await alpha.create(
            JournalEntryCreate(
                content="This write must roll back.",
                related_decisions=[foreign.id],
            )
        )

    assert await db.fetchone(
        "SELECT id FROM journal WHERE project_id = ? AND content = ?",
        ["prj_alpha", "This write must roll back."],
    ) is None
    assert await db.fetchone(
        "SELECT id FROM entity_links WHERE project_id = ? AND target_id = ?",
        ["prj_alpha", foreign.id],
    ) is None
    assert await db.fetchone(
        "SELECT id FROM events WHERE project_id = ? AND summary LIKE ?",
        ["prj_alpha", "%This write must roll back.%"],
    ) is None


@pytest.mark.asyncio
async def test_repeated_relation_update_is_idempotent_and_foreign_failure_is_atomic(
    db: Database,
) -> None:
    await _ensure_project(db, "prj_alpha")
    await _ensure_project(db, "prj_beta")
    local = await DecisionService(db, project_id="prj_alpha").create(
        DecisionCreate(
            question="Local decision?",
            phase="design",
            decided_by="brain",
        )
    )
    foreign = await DecisionService(db, project_id="prj_beta").create(
        DecisionCreate(
            question="Foreign decision?",
            phase="design",
            decided_by="brain",
        )
    )
    notes = NoteService(db, project_id="prj_alpha")
    note = await notes.create(
        JournalEntryCreate(content="Stable note.", related_decisions=[local.id])
    )

    before = await db.fetchone(
        "SELECT id FROM entity_links WHERE project_id = ? AND source_id = ? "
        "AND link_type = 'references' AND target_id = ?",
        ["prj_alpha", note.id, local.id],
    )
    assert before is not None

    await notes.update(
        note.id,
        JournalEntryUpdate(
            related_decisions=[f"  {local.id}  ", "", local.id]
        ),
    )
    after_retry = await db.fetchall(
        "SELECT id FROM entity_links WHERE project_id = ? AND source_id = ? "
        "AND link_type = 'references'",
        ["prj_alpha", note.id],
    )
    assert after_retry == [before]
    normalized = await notes.get(note.id)
    assert normalized is not None
    assert normalized.related_decisions == [local.id]

    with pytest.raises(ValueError, match="project prj_alpha"):
        await notes.update(
            note.id,
            JournalEntryUpdate(related_decisions=[foreign.id]),
        )

    after_failure = await notes.get(note.id)
    assert after_failure is not None
    assert after_failure.related_decisions == [local.id]
    assert await db.fetchall(
        "SELECT id FROM entity_links WHERE project_id = ? AND source_id = ? "
        "AND link_type = 'references'",
        ["prj_alpha", note.id],
    ) == [before]


@pytest.mark.asyncio
async def test_story_and_artifact_links_round_trip_after_database_reopen(
    tmp_path: Path,
) -> None:
    """Round-trip the story and retry operations with existing stable keys.

    Artifact registration is content-addressed; edge assignment has a stable
    source/type/target identity. Journal/decision/mission/literature creates
    intentionally remain distinct commands until the public contract defines
    caller-supplied idempotency keys.
    """
    db_path = tmp_path / "roundtrip.db"
    artifact_path = tmp_path / "experiment.txt"
    artifact_path.write_text("measured result\n")

    db = Database(str(db_path))
    await db.connect()
    await db.initialize_schema()
    await db.initialize_phase2_schema()
    await _ensure_project(db, "prj_story")

    notes = NoteService(db, project_id="prj_story")
    decisions = DecisionService(db, project_id="prj_story")
    literature = LiteratureService(db, project_id="prj_story")
    missions = MissionService(db, project_id="prj_story")
    from rka.infra.file_access import FileAccessPolicy
    artifacts = ArtifactService(db, project_id="prj_story", file_policy=FileAccessPolicy([tmp_path]))

    pi_note = await notes.create(
        JournalEntryCreate(
            content="The PI selected a three-class framing.",
            summary="Three-class framing",
            type="directive",
            source="pi",
            verbatim_input="Map the nine cases into three classes.",
            confidence="verified",
            importance="critical",
        )
    )
    decision = await decisions.create(
        DecisionCreate(
            question="How should the nine cases be grouped?",
            chosen="Three semantic classes",
            rationale="The grouping preserves the research distinction.",
            phase="design",
            decided_by="pi",
            related_journal=[pi_note.id],
        )
    )
    paper = await literature.create(
        LiteratureCreate(
            title="A supporting taxonomy",
            added_by="brain",
            related_decisions=[decision.id],
        )
    )
    mission = await missions.create(
        MissionCreate(
            phase="evaluation",
            objective="Evaluate the selected grouping.",
            motivated_by_decision=decision.id,
        )
    )
    await decisions.update(
        decision.id,
        DecisionUpdate(
            related_literature=[paper.id],
            related_missions=[mission.id],
        ),
    )
    result_note = await notes.create(
        JournalEntryCreate(
            content="The experiment supports the three-class framing.",
            source="executor",
            related_decisions=[decision.id],
            related_literature=[paper.id],
            related_mission=mission.id,
            supersedes=pi_note.id,
            confidence="tested",
        )
    )
    artifact = await artifacts.register(str(artifact_path), created_by="executor")
    links = BaseService(db, project_id="prj_story")
    await links.add_link(
        "journal",
        result_note.id,
        "produced",
        "artifact",
        artifact["id"],
        created_by="executor",
    )
    await links.add_link(
        "journal",
        result_note.id,
        "produced",
        "artifact",
        artifact["id"],
        created_by="executor",
    )
    duplicate = await artifacts.register(str(artifact_path), created_by="executor")
    assert duplicate == {"id": artifact["id"], "duplicate": True}

    await db.close()
    reopened = Database(str(db_path))
    await reopened.connect()
    try:
        reopened_notes = NoteService(reopened, project_id="prj_story")
        old = await reopened_notes.get(pi_note.id)
        current = await reopened_notes.get(result_note.id)
        assert old is not None and current is not None
        assert old.source == "pi"
        assert old.verbatim_input == "Map the nine cases into three classes."
        assert old.confidence == "superseded"
        assert old.status == "superseded"
        assert old.superseded_by == current.id
        assert current.related_decisions == [decision.id]
        assert current.related_literature == [paper.id]
        assert current.related_mission == mission.id

        edge_rows = await reopened.fetchall(
            "SELECT source_type, source_id, link_type, target_type, target_id "
            "FROM entity_links WHERE project_id = ?",
            ["prj_story"],
        )
        produced_artifact_rows = [
            row
            for row in edge_rows
            if (
                row["source_type"],
                row["source_id"],
                row["link_type"],
                row["target_type"],
                row["target_id"],
            )
            == (
                "journal",
                result_note.id,
                "produced",
                "artifact",
                artifact["id"],
            )
        ]
        assert len(produced_artifact_rows) == 1
        edges = {
            (
                row["source_type"],
                row["source_id"],
                row["link_type"],
                row["target_type"],
                row["target_id"],
            )
            for row in edge_rows
        }
        assert ("decision", decision.id, "justified_by", "journal", pi_note.id) in edges
        assert ("literature", paper.id, "informed_by", "decision", decision.id) in edges
        assert ("decision", decision.id, "motivated", "mission", mission.id) in edges
        assert ("mission", mission.id, "produced", "journal", result_note.id) in edges
        assert ("journal", result_note.id, "supersedes", "journal", pi_note.id) in edges
        assert (
            "journal",
            result_note.id,
            "produced",
            "artifact",
            artifact["id"],
        ) in edges
        assert await ArtifactService(
            reopened, project_id="prj_story"
        ).get(artifact["id"])
        artifact_count = await reopened.fetchone(
            "SELECT COUNT(*) AS n FROM artifacts WHERE project_id = ? "
            "AND content_hash = (SELECT content_hash FROM artifacts WHERE id = ?)",
            ["prj_story", artifact["id"]],
        )
        assert artifact_count == {"n": 1}
    finally:
        await reopened.close()
