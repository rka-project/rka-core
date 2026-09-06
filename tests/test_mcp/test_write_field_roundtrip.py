"""I2: typed/raw dispatch -> legacy HTTP adapter -> REST -> SQLite -> REST GET."""

import ast
import inspect

import httpx
import pytest
import pytest_asyncio
from pydantic import TypeAdapter

from rka.api.app import create_app
from rka.config import RKAConfig
from rka.mcp.operation_args import ExecuteArgsUnion


@pytest_asyncio.fixture
async def field_api(db, tmp_path, monkeypatch):
    from rka.mcp import server

    config = RKAConfig(data_dir=tmp_path / "data", llm_enabled=False, embeddings_enabled=False)
    app = create_app(config)
    app.state.db = db
    app.state.config = config
    app.state.llm = None
    app.state.embeddings = None

    def client(project_id=None):
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://isolated.test",
            headers={"X-RKA-Project": project_id or "proj_default"},
        )

    monkeypatch.setattr(server, "_client", client)
    async with client() as api:
        note = await api.post(
            "/api/notes", json={"content": "Synthetic supporting evidence", "source": "executor"}
        )
        decision = await api.post(
            "/api/decisions",
            json={
                "question": "Anchor choice",
                "phase": "test",
                "decided_by": "executor",
                "chosen": "A",
                "rationale": "Synthetic rationale",
            },
        )
        literature = await api.post(
            "/api/literature", json={"title": "Anchor reference", "added_by": "executor"}
        )
        for response in (note, decision, literature):
            assert response.status_code == 201, response.text
    return client, note.json()["id"], decision.json()["id"], literature.json()["id"]


async def execute(body, mode="typed"):
    from rka.mcp import verb_dispatch

    if mode == "typed":
        return await verb_dispatch.dispatch_execute_typed(
            TypeAdapter(ExecuteArgsUnion).validate_python(body)
        )
    body = dict(body)
    operation = body.pop("operation")
    if mode == "legacy":
        from rka.mcp.server import _rka_execute_legacy_impl

        return await _rka_execute_legacy_impl(operation, **body)
    return await verb_dispatch.dispatch_execute(operation, **body)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["typed", "raw", "legacy"])
async def test_registered_source_retains_nested_provenance(field_api, mode):
    client, _, _, _ = field_api
    provenance = {"collector": "executor", "pages": [1, 3], "note": None, "synthetic": True}
    result = await execute(
        {
            "operation": "register_source",
            "project_id": "proj_default",
            "source_kind": "pasted_text",
            "registered_by": "executor",
            "pasted_text": "Synthetic source bytes",
            "provenance": provenance,
            "ownership_kind": "researcher",
        },
        mode,
    )
    async with client() as api:
        rows = (await api.get("/api/sources")).json()
        assert len(rows) == 1, result
        detail = (await api.get(f"/api/sources/{rows[0]['id']}")).json()
    assert detail["provenance"] == provenance
    assert detail["registered_by"] == "executor"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["typed", "raw", "legacy"])
@pytest.mark.parametrize("supersede", [False, True])
async def test_decision_create_and_replacement_retain_fields(field_api, db, mode, supersede):
    client, note_id, old_id, lit_id = field_api
    payload = {
        "operation": "record_decision",
        "project_id": "proj_default",
        "question": "New synthetic choice",
        "chosen": "B",
        "rationale": "Evidence supports B",
        "decided_by": "executor",
        "kind": "design_choice",
        "phase": "test",
        "related_journal": [note_id],
        "related_literature": [lit_id],
        "tags": ["field-parity"],
        "assumptions": ["synthetic input"],
        "status": "revisit",
        "options": [{"label": "B", "description": "Candidate B", "explored": True}],
    }
    if supersede:
        payload["supersedes_decision_id"] = old_id
    result = await execute(payload, mode)
    row = await db.fetchone("SELECT id FROM decisions WHERE question = ?", [payload["question"]])
    assert row is not None, result
    async with client() as api:
        stored = (await api.get(f"/api/decisions/{row['id']}")).json()
    for key in (
        "tags",
        "status",
        "assumptions",
        "related_journal",
        "related_literature",
        "phase",
        "decided_by",
        "kind",
        "options",
    ):
        assert stored[key] == payload[key], (key, stored)
    if supersede:
        old = await db.fetchone(
            "SELECT status, superseded_by FROM decisions WHERE id = ?", [old_id]
        )
        assert old["status"] == "superseded" and old["superseded_by"] == row["id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["typed", "raw", "legacy"])
async def test_literature_create_retains_status_tags_and_links(field_api, db, mode):
    client, _, decision_id, _ = field_api
    payload = {
        "operation": "record_literature",
        "project_id": "proj_default",
        "title": "New synthetic reference",
        "authors": ["Jane Doe"],
        "year": 2026,
        "venue": "Synthetic Venue",
        "doi": "10.0000/field-parity",
        "url": "https://example.invalid/synthetic",
        "abstract": "Synthetic abstract",
        "status": "cited",
        "tags": ["important"],
        "related_decisions": [decision_id],
    }
    result = await execute(payload, mode)
    row = await db.fetchone("SELECT id FROM literature WHERE doi = ?", [payload["doi"]])
    assert row is not None, result
    async with client() as api:
        stored = (await api.get(f"/api/literature/{row['id']}")).json()
    for key in payload.keys() - {"operation", "project_id"}:
        assert stored[key] == payload[key], (key, stored)
    link = await db.fetchone(
        "SELECT created_by FROM entity_links WHERE source_id=? AND target_id=? AND link_type='informed_by'",
        [row["id"], decision_id],
    )
    assert link["created_by"] == "brain"


@pytest.mark.asyncio
async def test_literature_omission_null_and_empty_lists(field_api, db):
    client, _, decision_id, _ = field_api
    async with client() as api:
        response = await api.post(
            "/api/literature",
            json={
                "title": "Mutable reference",
                "status": "read",
                "tags": ["keep"],
                "related_decisions": [decision_id],
            },
        )
    lit_id = response.json()["id"]
    await execute(
        {
            "operation": "update_literature",
            "project_id": "proj_default",
            "id": lit_id,
            "notes": "Updated note",
            "tags": None,
        }
    )
    async with client() as api:
        stored = (await api.get(f"/api/literature/{lit_id}")).json()
    assert stored["tags"] == ["keep"] and stored["status"] == "read"
    await execute(
        {
            "operation": "update_literature",
            "project_id": "proj_default",
            "id": lit_id,
            "tags": [],
            "related_decisions": [],
        }
    )
    async with client() as api:
        stored = (await api.get(f"/api/literature/{lit_id}")).json()
    assert stored["tags"] == [] and stored["related_decisions"] == []
    assert not await db.fetchall(
        "SELECT * FROM entity_links WHERE source_id=? AND link_type='informed_by'", [lit_id]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["typed", "raw", "legacy"])
@pytest.mark.parametrize("confidence", [None, 0.0, 0.87])
async def test_interpretation_hint_confidence_roundtrip(field_api, mode, confidence):
    client, note_id, _, _ = field_api
    candidates = []
    async with client() as api:
        for statement in ("Synthetic observation A", "Synthetic observation B"):
            response = await api.post(
                "/api/interpretations",
                json={
                    "source_type": "journal",
                    "source_id": note_id,
                    "locator_kind": "record",
                    "locator_value": note_id,
                    "statement": statement,
                    "epistemic_kind": "observation",
                    "created_by": "executor",
                    "extraction_tool": "isolated-test",
                },
            )
            assert response.status_code == 201, response.text
            candidates.append(response.json())
    payload = {
        "operation": "add_interpretation_hint",
        "project_id": "proj_default",
        "id": candidates[0]["id"],
        "related_candidate_id": candidates[1]["id"],
        "kind": "duplicate",
        "rationale": "Synthetic comparison",
        "created_by": "brain",
        "expected_revision": candidates[0]["revision"],
    }
    if confidence is not None:
        payload["confidence"] = confidence
    result = await execute(payload, mode)
    async with client() as api:
        stored = (await api.get(f"/api/interpretations/{candidates[0]['id']}")).json()
    assert len(stored["hints"]) == 1, result
    assert stored["hints"][0]["confidence"] == (0.5 if confidence is None else confidence)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["typed", "raw", "legacy"])
async def test_note_omission_preserves_but_explicit_defaults_update(field_api, mode):
    client, _, _, _ = field_api
    async with client() as api:
        response = await api.post(
            "/api/notes",
            json={
                "content": "Synthetic finding",
                "source": "brain",
                "confidence": "verified",
                "importance": "high",
            },
        )
        assert response.status_code == 201, response.text
        note_id = response.json()["id"]
    base = {"operation": "update_note", "project_id": "proj_default", "id": note_id}
    await execute(
        {
            **base,
            "content": "Revised synthetic finding",
            "source": None,
            "confidence": None,
            "importance": None,
        },
        mode,
    )
    async with client() as api:
        stored = (await api.get(f"/api/notes/{note_id}")).json()
    assert (stored["source"], stored["confidence"], stored["importance"]) == (
        "brain",
        "verified",
        "high",
    )
    await execute({**base, "confidence": "hypothesis", "importance": "normal"}, mode)
    async with client() as api:
        stored = (await api.get(f"/api/notes/{note_id}")).json()
    assert (stored["source"], stored["confidence"], stored["importance"]) == (
        "brain",
        "hypothesis",
        "normal",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["typed", "raw", "legacy"])
async def test_note_creation_still_applies_creation_defaults(field_api, db, mode):
    client, _, _, _ = field_api
    result = await execute(
        {
            "operation": "record_note",
            "project_id": "proj_default",
            "content": "Synthetic note with omitted creation defaults",
        },
        mode,
    )
    row = await db.fetchone(
        "SELECT id FROM journal WHERE content = ?",
        ["Synthetic note with omitted creation defaults"],
    )
    assert row is not None, result
    async with client() as api:
        stored = (await api.get(f"/api/notes/{row['id']}")).json()
    assert (stored["source"], stored["confidence"], stored["importance"]) == (
        "executor",
        "hypothesis",
        "normal",
    )


def test_lifted_common_fields_are_not_read_from_residual_kwargs():
    from rka.mcp.verb_dispatch import dispatch_execute

    common = {"source", "confidence", "importance", "verbatim_input", "provenance", "tags", "phase"}
    tree = ast.parse(inspect.getsource(dispatch_execute))
    bad = [
        node.args[0].value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "kw"
        and node.func.attr == "get"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and node.args[0].value in common
    ]
    assert bad == [], f"Lifted arguments are no longer in **kw: {bad}"
