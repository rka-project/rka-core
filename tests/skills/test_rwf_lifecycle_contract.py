"""RWF recipes against the existing Core contract, using synthetic tmp_path DBs.

No REST/MCP connection, embedding provider, installed plugin, or real project
data is used. Parse literal example arguments; never execute Markdown as code.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
import pytest_asyncio

from rka.models.decision import DecisionCreate
from rka.models.journal import JournalEntryCreate, JournalEntryUpdate
from rka.models.mission import MissionCreate, MissionReportCreate, MissionTask, MissionUpdate
from rka.services.decisions import DecisionService
from rka.services.missions import MissionService
from rka.services.notes import NoteService

SKILLS = Path(__file__).resolve().parents[2] / "rka" / "skills"
TEMPLATES = SKILLS / "brain" / "templates"


def _recipe(name: str) -> list[dict]:
    calls = []
    for block in re.findall(r"```python\n(.*?)```", (TEMPLATES / name).read_text(encoding="utf-8"), re.S):
        for node in ast.walk(ast.parse(block)):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id == "rka_execute":
                    [argument] = [kw.value for kw in node.keywords if kw.arg == "args"]
                    calls.append(ast.literal_eval(argument))
    assert calls, f"{name} has no executable save recipe"
    return calls


def _operation(name: str, operation: str) -> dict:
    [call] = [c for c in _recipe(name) if c["operation"] == operation]
    return call


def test_gap_confirmation_recipe_changes_header_not_confidence():
    call = _operation("gap-note.md", "update_note")
    assert "confidence" not in call, "PI approval is not source verification"
    assert "status: confirmed" in call["content"]
    assert "review_anchor:" in call["content"]
    assert call["related_decisions"], "confirmation keeps the decision backlink"


@pytest.mark.parametrize("path", [
    "brain/templates/stop-pivot-note.md",
    "brain/workflows.md",
    "executor/SKILL.md",
    "executor/workflows.md",
])
def test_incomplete_closeout_guides_warn_against_completion_reports(path):
    text = (SKILLS / path).read_text(encoding="utf-8")
    assert "do not call `submit_report`" in " ".join(text.lower().split())
    assert "Inconclusive" in text


def test_stop_save_recipe_never_submits_a_completion_report():
    calls = _recipe("stop-pivot-note.md")
    assert not any(c["operation"] == "submit_report" for c in calls)
    assert _operation("stop-pivot-note.md", "record_note")["provenance"]["related_mission"]
    assert _operation("stop-pivot-note.md", "update_mission_status")["status"] == "partial"


@pytest_asyncio.fixture
async def workflow(db):
    # tests/conftest.py creates a fresh SQLite database under tmp_path.
    notes = NoteService(db, project_id="proj_default")
    missions = MissionService(db, project_id="proj_default")
    decision = await DecisionService(db, project_id="proj_default").create(
        DecisionCreate(question="Synthetic RWF contract check", phase="validation", decided_by="brain")
    )
    mission = await missions.create(MissionCreate(
        phase="validation", objective="Run the synthetic protocol",
        motivated_by_decision=decision.id,
        tasks=[MissionTask(description="Collect the required observations", status="in_progress")],
    ))
    return notes, missions, decision.id, mission.id


@pytest.mark.asyncio
@pytest.mark.parametrize("confidence", ["hypothesis", "tested", "verified"])
async def test_gap_confirmation_preserves_stored_confidence(workflow, confidence):
    notes, _, decision_id, _ = workflow
    original = (
        "template: gap-note@0.1.0\nstatus: draft\n"
        "review_anchor: pending-pi-confirmation\n"
        "Synthetic source boundary: abstract only; full text not examined."
    )
    note = await notes.create(JournalEntryCreate(
        content=original, source="brain", confidence=confidence,
    ))
    args = _operation("gap-note.md", "update_note")
    payload = {k: v for k, v in args.items() if k not in {"operation", "project_id", "id"}}
    payload["related_decisions"] = [decision_id]
    if "content" in payload:
        # Fill the recipe's full-note placeholder, preserving the source caveat.
        payload["content"] = original.replace("status: draft", "status: confirmed").replace(
            "pending-pi-confirmation", decision_id,
        )
    await notes.update(note.id, JournalEntryUpdate(**payload), actor="brain")
    saved = await notes.get(note.id)
    assert saved.confidence == confidence
    assert "status: confirmed" in saved.content
    assert "full text not examined" in saved.content
    assert saved.related_decisions == [decision_id]
    assert saved.source == "brain", "PI approval does not reattribute the agent's note"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["partial", "blocked", "cancelled"])
async def test_stop_journal_preserves_unfinished_mission(workflow, status):
    notes, missions, decision_id, mission_id = workflow
    args = _operation("stop-pivot-note.md", "record_note")
    payload = {k: v for k, v in args.items() if k not in {"operation", "project_id", "provenance"}}
    payload.update(
        request_id=f"synthetic-stop-{status}",
        content="Synthetic stop_reason: implementation_failure. Required observations missing.",
        tags=["rwf-stop-pivot", "rwf-v0", "implementation_failure"],
        related_mission=mission_id, related_decisions=[decision_id],
    )
    note = await notes.create(JournalEntryCreate(**payload))
    await missions.update(mission_id, MissionUpdate(status=status))
    saved = await missions.get(mission_id)
    saved_note = await notes.get(note.id)
    assert saved.status == status
    assert saved.report is None
    # Core timestamps partial closeout too; a timestamp is not proof of complete.
    assert (saved.completed_at is not None) == (status == "partial")
    assert saved.tasks[0].status == "in_progress"
    assert saved_note.related_mission == mission_id
    assert saved_note.status == "active", "keep the stop evidence retrievable"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["partial", "blocked", "cancelled"])
async def test_report_contract_explains_why_unfinished_recipe_must_not_submit(workflow, status):
    _, missions, _, mission_id = workflow
    await missions.update(mission_id, MissionUpdate(status=status))
    report = MissionReportCreate(summary="Synthetic incomplete execution")
    if status == "partial":
        saved = await missions.submit_report(mission_id, report)
        assert saved.status == "complete"
        assert saved.consistency_warnings, "unfinished tasks are not silently reconciled"
    else:
        with pytest.raises(ValueError, match="cannot accept a completion report"):
            await missions.submit_report(mission_id, report)
        saved = await missions.get(mission_id)
        assert saved.status == status
        assert saved.report is None


@pytest.mark.asyncio
async def test_completed_protocol_can_report_an_inconclusive_result(workflow):
    _, missions, _, mission_id = workflow
    await missions.update(mission_id, MissionUpdate(
        status="active",
        tasks=[MissionTask(description="Collect the required observations", status="complete")],
    ))
    report = MissionReportCreate(
        summary="Synthetic Inconclusive: all planned runs completed; effect inside uncertainty band.",
        findings=["No support or refutation established; all required work was performed."],
    )
    await missions.submit_report(mission_id, report)
    saved = await missions.get(mission_id)
    assert saved.status == "complete"
    assert saved.report.summary == report.summary
    assert saved.consistency_warnings == []
