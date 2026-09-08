"""Project-ownership guards for aggregate update operations."""

from __future__ import annotations

from datetime import datetime

import pytest

from rka.models.decision import DecisionCreate, DecisionUpdate
from rka.models.journal import JournalAttributionCorrection, JournalEntryCreate, JournalEntryUpdate
from rka.models.literature import LiteratureCreate, LiteratureUpdate
from rka.models.project import ProjectCreate
from rka.services import literature as literature_module
from rka.services.decisions import DecisionNotFoundError, DecisionService
from rka.services.literature import LiteratureNotFoundError, LiteratureService
from rka.services.notes import NoteNotFoundError, NoteService
from rka.services.project import ProjectService
from rka.services.summary import SummaryService

PROJECT_ID = "proj_default"


async def _create_project(db, project_id: str, name: str) -> None:
    await ProjectService(db).create_project(
        ProjectCreate(id=project_id, name=name),
        actor="system",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("target_kind", ["missing", "foreign"])
@pytest.mark.parametrize("path", ["ordinary", "attribution_correction"])
async def test_note_update_rejects_unowned_target_without_side_effects(
    db,
    target_kind: str,
    path: str,
) -> None:
    foreign_project = "proj_foreign_note_update"
    await _create_project(db, foreign_project, "Foreign Note Update")
    foreign = await NoteService(db, project_id=foreign_project).create(
        JournalEntryCreate(
            content="foreign note source",
            source="executor",
        )
    )
    target_id = "jrn_missing_update" if target_kind == "missing" else foreign.id
    changes_before = db.conn.total_changes

    with pytest.raises(NoteNotFoundError, match="not found"):
        service = NoteService(db, project_id=PROJECT_ID)
        if path == "ordinary":
            await service.update(
                target_id,
                JournalEntryUpdate(
                    content="must not change the source",
                    tags=["phantom-note-tag"],
                    related_decisions=["dec_phantom_note_target"],
                ),
            )
        else:
            await service.correct_attribution(target_id, JournalAttributionCorrection(
                source="pi", verbatim_input="Must not be written",
                expected_revision=0, request_id="unowned", actor="executor", reason="Scope probe",
            ))

    assert db.conn.total_changes == changes_before
    assert await db.fetchone(
        "SELECT id FROM journal_attribution_revisions WHERE journal_id=?", [target_id],
    ) is None
    assert await db.fetchone(
        "SELECT content, source FROM journal WHERE id = ?",
        [foreign.id],
    ) == {"content": "foreign note source", "source": "executor"}
    assert await db.fetchone(
        """SELECT tag FROM tags
           WHERE project_id = ? AND entity_type = 'journal'
             AND entity_id = ?""",
        [PROJECT_ID, target_id],
    ) is None
    assert await db.fetchone(
        """SELECT id FROM entity_links
           WHERE project_id = ? AND source_type = 'journal'
             AND source_id = ?""",
        [PROJECT_ID, target_id],
    ) is None
    assert await db.fetchone(
        """SELECT id FROM events
           WHERE project_id = ? AND entity_type = 'journal'
             AND entity_id = ?""",
        [PROJECT_ID, target_id],
    ) is None
    assert await db.fetchone(
        """SELECT id FROM audit_log
           WHERE project_id = ? AND entity_type = 'journal'
             AND entity_id = ? AND action = 'update'""",
        [PROJECT_ID, target_id],
    ) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("target_kind", ["missing", "foreign"])
async def test_decision_update_rejects_unowned_target_without_side_effects(
    db,
    target_kind: str,
) -> None:
    foreign_project = "proj_foreign_decision_update"
    await _create_project(db, foreign_project, "Foreign Decision Update")
    foreign = await DecisionService(db, project_id=foreign_project).create(
        DecisionCreate(
            question="foreign decision source",
            phase="design",
            decided_by="brain",
        )
    )
    target_id = "dec_missing_update" if target_kind == "missing" else foreign.id
    changes_before = db.conn.total_changes

    with pytest.raises(DecisionNotFoundError, match="not found"):
        await DecisionService(db, project_id=PROJECT_ID).update(
            target_id,
            DecisionUpdate(
                question="must not change the source",
                status="abandoned",
                tags=["phantom-decision-tag"],
                related_journal=["jrn_phantom_decision_target"],
            ),
            actor="pi",
        )

    assert db.conn.total_changes == changes_before
    assert await db.fetchone(
        "SELECT question, status FROM decisions WHERE id = ?",
        [foreign.id],
    ) == {"question": "foreign decision source", "status": "active"}
    assert await db.fetchone(
        """SELECT tag FROM tags
           WHERE project_id = ? AND entity_type = 'decision'
             AND entity_id = ?""",
        [PROJECT_ID, target_id],
    ) is None
    assert await db.fetchone(
        """SELECT id FROM entity_links
           WHERE project_id = ? AND source_type = 'decision'
             AND source_id = ?""",
        [PROJECT_ID, target_id],
    ) is None
    assert await db.fetchone(
        """SELECT id FROM events
           WHERE project_id = ? AND entity_type = 'decision'
             AND entity_id = ? AND event_type = 'decision_abandoned'""",
        [PROJECT_ID, target_id],
    ) is None
    assert await db.fetchone(
        """SELECT id FROM audit_log
           WHERE project_id = ? AND entity_type = 'decision'
             AND entity_id = ? AND action = 'update'""",
        [PROJECT_ID, target_id],
    ) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("target_kind", ["missing", "foreign"])
async def test_literature_update_rejects_unowned_target_without_side_effects(
    db,
    target_kind: str,
) -> None:
    foreign_project = "proj_foreign_literature_update"
    await _create_project(db, foreign_project, "Foreign Literature Update")
    foreign = await LiteratureService(db, project_id=foreign_project).create(
        LiteratureCreate(title="foreign literature source")
    )
    target_id = "lit_missing_update" if target_kind == "missing" else foreign.id
    changes_before = db.conn.total_changes

    with pytest.raises(LiteratureNotFoundError, match="not found"):
        await LiteratureService(db, project_id=PROJECT_ID).update(
            target_id,
            LiteratureUpdate(
                title="must not change the source",
                status="cited",
                tags=["phantom-literature-tag"],
                related_decisions=["dec_phantom_literature_target"],
            ),
            actor="pi",
        )

    assert db.conn.total_changes == changes_before
    assert await db.fetchone(
        "SELECT title, status FROM literature WHERE id = ?",
        [foreign.id],
    ) == {"title": "foreign literature source", "status": "to_read"}
    assert await db.fetchone(
        """SELECT tag FROM tags
           WHERE project_id = ? AND entity_type = 'literature'
             AND entity_id = ?""",
        [PROJECT_ID, target_id],
    ) is None
    assert await db.fetchone(
        """SELECT id FROM entity_links
           WHERE project_id = ? AND source_type = 'literature'
             AND source_id = ?""",
        [PROJECT_ID, target_id],
    ) is None
    assert await db.fetchone(
        """SELECT id FROM events
           WHERE project_id = ? AND entity_type = 'literature'
             AND entity_id = ? AND event_type = 'literature_cited'""",
        [PROJECT_ID, target_id],
    ) is None
    assert await db.fetchone(
        """SELECT id FROM audit_log
           WHERE project_id = ? AND entity_type = 'literature'
             AND entity_id = ? AND action = 'update'""",
        [PROJECT_ID, target_id],
    ) is None


@pytest.mark.asyncio
async def test_literature_update_preserves_same_second_metadata_order(
    db,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = LiteratureService(db, project_id=PROJECT_ID)
    literature = await service.create(
        LiteratureCreate(title="same-second freshness")
    )
    validation_completed_at = "2026-07-23T10:00:00.000000Z"
    metadata_updated_at = "2026-07-23T10:00:00.000001Z"
    monkeypatch.setattr(
        literature_module,
        "_precise_now",
        lambda: metadata_updated_at,
    )

    updated = await service.update(
        literature.id,
        LiteratureUpdate(title="same-second freshness edited"),
    )

    assert updated.updated_at == metadata_updated_at
    def parse(value: str) -> datetime:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))

    assert parse(updated.updated_at) > parse(validation_completed_at)


@pytest.mark.asyncio
@pytest.mark.parametrize("target_kind", ["missing", "foreign"])
async def test_summary_bless_rejects_unowned_target_without_audit(
    db,
    target_kind: str,
) -> None:
    foreign_project = "proj_foreign_summary_bless"
    await _create_project(db, foreign_project, "Foreign Summary Bless")
    foreign_id = "sum_foreign_bless"
    await db.execute(
        """INSERT INTO exploration_summaries
           (id, scope_type, granularity, content, produced_by, project_id)
           VALUES (?, 'project', 'paragraph', 'foreign summary', 'llm', ?)""",
        [foreign_id, foreign_project],
    )
    await db.commit()
    target_id = "sum_missing_bless" if target_kind == "missing" else foreign_id
    changes_before = db.conn.total_changes

    result = await SummaryService(db, project_id=PROJECT_ID).bless(
        target_id,
        actor="pi",
    )

    assert result is None
    assert db.conn.total_changes == changes_before
    assert await db.fetchone(
        "SELECT blessed, content FROM exploration_summaries WHERE id = ?",
        [foreign_id],
    ) == {"blessed": 0, "content": "foreign summary"}
    assert await db.fetchone(
        """SELECT id FROM audit_log
           WHERE project_id = ? AND entity_type = 'summary'
             AND entity_id = ? AND action = 'update'""",
        [PROJECT_ID, target_id],
    ) is None
