"""Typed MCP -> REST -> service journal reliability contracts."""

from __future__ import annotations

from pathlib import Path
import json

import httpx
import pytest
import pytest_asyncio
from pydantic import ValidationError

from rka.api.app import create_app
from rka.config import RKAConfig
from rka.mcp.operation_args import (
    BulkUpdateArgs, CorrectNoteAttributionArgs, QueryEntityArgs, QueryNoteAttributionHistoryArgs,
    IngestDocumentArgs, RecordNoteArgs, UpdateNoteArgs,
)
from rka.mcp.verb_dispatch import dispatch_execute, dispatch_execute_typed, dispatch_query_typed


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["rest", "typed", "legacy", "raw_dispatch", "legacy_record", "batch"])
async def test_raw_capture_roundtrip_on_all_create_surfaces(mcp_env, surface):
    import rka.mcp.server as server

    original = "  PI 原文\r\n"
    data = {"content": original, "source": "pi", "capture_mode": "raw_capture"}
    headers = {"X-RKA-Project": "proj_default"}
    if surface == "rest":
        result = await mcp_env.post("/api/notes", json=data, headers=headers)
        assert result.status_code == 201, result.text
    elif surface == "typed":
        assert "Created" in await dispatch_execute_typed(RecordNoteArgs(project_id="proj_default", **data))
    elif surface == "legacy":
        assert "Created" in await server.rka_add_note(project_id="proj_default", **data)
    elif surface == "legacy_record":
        assert "Created" in await server.rka_record_note(project_id="proj_default", **data)
    elif surface == "batch":
        result = await mcp_env.post("/api/import/batch", headers=headers,
                                   json={"entries": [{"entity_type": "note", "data": data}]})
        assert result.status_code == 200, result.text
    else:
        assert "Created" in await dispatch_execute("record_note", project_id="proj_default", **data)
    result = await mcp_env.get("/api/notes", headers=headers)
    note, = result.json()
    assert note["verbatim_input"] == original and note["capture_mode"] == "raw_capture"
    updated = await mcp_env.put(f"/api/notes/{note['id']}", headers=headers, json={"content": "Edited"})
    assert updated.json()["verbatim_input"] == original
    rejected = await mcp_env.put(f"/api/notes/{note['id']}", headers=headers,
                                 json={"capture_mode": "unknown"})
    assert rejected.status_code == 422


@pytest.mark.asyncio
async def test_typed_document_ingest_pi_raw_capture_and_mode_correction(mcp_env):
    raw = "## Heading\r\n  exact PI text  \n"
    await dispatch_execute_typed(IngestDocumentArgs(project_id="proj_default", content=raw, source="pi"))
    headers = {"X-RKA-Project": "proj_default"}
    note, = (await mcp_env.get("/api/notes", headers=headers)).json()
    assert note["capture_mode"] == "raw_capture" and note["verbatim_input"] == raw
    args = CorrectNoteAttributionArgs(
        project_id="proj_default", id=note["id"], source="pi", verbatim_input=raw,
        capture_mode="agent_restatement", expected_revision=0, request_id="mode-1", actor="executor",
        reason="Correct the declared capture method",
    )
    first = json.loads(await dispatch_execute_typed(args))
    assert first["before_capture_mode"] == "raw_capture"
    assert first["after_capture_mode"] == "agent_restatement"
    assert json.loads(await dispatch_execute_typed(args)) == first


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["rest", "typed", "legacy", "raw_dispatch", "legacy_record", "batch"])
async def test_pi_restatement_without_original_is_rejected_on_all_surfaces(mcp_env, surface):
    from rka.mcp import server

    data = {"content": "Agent paraphrase", "source": "pi", "capture_mode": "agent_restatement"}
    headers = {"X-RKA-Project": "proj_default"}
    if surface == "rest":
        result = await mcp_env.post("/api/notes", json=data, headers=headers)
        assert result.status_code == 422
    elif surface == "typed":
        with pytest.raises(ValidationError, match="verbatim_input"):
            RecordNoteArgs(project_id="proj_default", **data)
    elif surface == "legacy":
        with pytest.raises(Exception, match="API error 422"):
            await server.rka_add_note(project_id="proj_default", **data)
    elif surface == "legacy_record":
        assert "error" in json.loads(await server.rka_record_note(project_id="proj_default", **data))
    elif surface == "batch":
        result = await mcp_env.post("/api/import/batch", headers=headers,
                                   json={"entries": [{"entity_type": "note", "data": data}]})
        assert result.json()["errors"] and result.json()["imported"] == []
    else:
        result = await dispatch_execute("record_note", project_id="proj_default", **data)
        assert json.loads(result)["error"] == "missing_provenance"
    assert (await mcp_env.get("/api/notes", headers=headers)).json() == []


@pytest.mark.asyncio
async def test_raw_update_dispatch_does_not_silently_drop_capture_mode(mcp_env):
    headers = {"X-RKA-Project": "proj_default"}
    response = await mcp_env.post("/api/notes", headers=headers, json={"content": "Keep body"})
    assert response.status_code == 201, response.text
    note = response.json()
    result = await dispatch_execute("update_note", project_id="proj_default", id=note["id"],
                                    capture_mode="raw_capture", content="Must not write")
    assert json.loads(result)["error"] == "invalid_attribution"
    assert (await mcp_env.get(f"/api/notes/{note['id']}", headers=headers)).json()["content"] == "Keep body"


@pytest_asyncio.fixture
async def mcp_env(tmp_path: Path, monkeypatch):
    import rka.mcp.server as mcp_server

    app = create_app(
        RKAConfig(
            project_dir=tmp_path,
            db_path=Path("core-reliability-mcp.db"),
            llm_enabled=False,
            embeddings_enabled=False,
        )
    )
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)

        def client(project_id: str | None = None) -> httpx.AsyncClient:
            headers = {"X-RKA-Project": project_id} if project_id else {}
            return httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://testserver",
                headers=headers,
            )

        monkeypatch.setattr(mcp_server, "_client", client)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as rest:
            yield rest


@pytest.mark.asyncio
async def test_i4_typed_and_deferred_resolution_roundtrip(mcp_env):
    from rka.mcp.operation_args import ResolveStaleArgs
    from rka.mcp.operations_schema import OPERATIONS_SCHEMA
    import rka.mcp.server as server
    headers = {"X-RKA-Project": "proj_default"}
    note = (await mcp_env.post("/api/notes", json={"content": "source"}, headers=headers)).json()
    claim = (await mcp_env.post("/api/claims", json={"content": "finding", "source_entry_id": note["id"], "claim_type": "evidence"}, headers=headers)).json()
    flagged = await mcp_env.post("/api/freshness/flag-stale", json={"entity_id": claim["id"], "reason": "new evidence"}, headers=headers)
    assert flagged.status_code == 200
    args = ResolveStaleArgs(project_id="proj_default", entity_id=claim["id"], verdict="historical", resolution="reviewed", resolved_by="pi", journal_id=note["id"])
    receipt = json.loads(await dispatch_execute_typed(args))
    assert receipt["staleness_verdict"] == "historical"
    assert not receipt["currentness"]["is_current"]
    assert await server.rka_resolve_stale(**args.model_dump(exclude={"operation"})) == receipt
    stored = (await mcp_env.get(f"/api/claims/{claim['id']}", headers=headers)).json()
    assert stored["staleness_resolution_journal_id"] == note["id"]
    rejected = await mcp_env.put(f"/api/claims/{claim['id']}", json={"stale": False}, headers=headers)
    assert rejected.status_code == 422
    assert "resolve_stale" in OPERATIONS_SCHEMA


@pytest.mark.asyncio
async def test_i5_typed_dependency_declaration_roundtrip(mcp_env):
    from rka.mcp.operation_args import RecordDirectiveDependencyArgs
    headers = {"X-RKA-Project": "proj_default"}
    note = (await mcp_env.post("/api/notes", json={"content": "conditional directive", "type": "directive"}, headers=headers)).json()
    decision = (await mcp_env.post("/api/decisions", json={"question": "choice", "chosen": "selected", "rationale": "reason", "decided_by": "brain", "phase": "implementation"}, headers=headers)).json()
    args = RecordDirectiveDependencyArgs(project_id="proj_default", directive_id=note["id"], decision_id=decision["id"], declared_by="pi", reason="only while this decision applies")
    first = json.loads(await dispatch_execute_typed(args))
    assert json.loads(await dispatch_execute_typed(args)) == first
    rows = (await mcp_env.get(f"/api/notes/{note['id']}/dependencies", headers=headers)).json()
    assert rows == [first]


async def test_i4_list_and_research_map_render_inactive_dispositions(mcp_env):
    import rka.mcp.server as server

    headers = {"X-RKA-Project": "proj_default"}
    note = (await mcp_env.post("/api/notes", headers=headers, json={"content": "currency source"})).json()
    claim = (await mcp_env.post("/api/claims", headers=headers, json={"source_entry_id": note["id"], "content": "retained evidence", "claim_type": "evidence"})).json()
    cluster = (await mcp_env.post("/api/clusters", headers=headers, json={"label": "retained cluster"})).json()
    for entity in (claim, cluster):
        response = await mcp_env.post("/api/freshness/flag-stale", headers=headers, json={"entity_id": entity["id"], "reason": "review"})
        assert response.status_code == 200, response.text
        response = await mcp_env.post("/api/freshness/resolve-stale", headers=headers, json={"entity_id": entity["id"], "verdict": "historical", "resolution": "retained, not current", "resolved_by": "pi"})
        assert response.status_code == 200, response.text
    for rendered in (
        await server.rka_get_claims(project_id="proj_default"),
        await server.rka_list_clusters(project_id="proj_default"),
        await server.rka_get_research_map(project_id="proj_default"),
    ):
        assert "NOT CURRENT" in rendered and "historical" in rendered
    graph = (await mcp_env.get("/api/research-map", headers=headers)).json()
    assert graph["unassigned_clusters"][0]["currentness"]["is_current"] is False


async def test_i7_public_checkpoint_and_report_conflicts_do_not_duplicate(mcp_env):
    headers = {"X-RKA-Project": "proj_default"}
    response = await mcp_env.post("/api/missions", headers=headers, json={"objective": "public lifecycle", "phase": "implementation"})
    assert response.status_code == 201, response.text
    mission = response.json()
    response = await mcp_env.post("/api/checkpoints", headers=headers, json={"mission_id": mission["id"], "type": "decision", "description": "approve"})
    assert response.status_code == 201, response.text
    checkpoint = response.json()
    url = f"/api/checkpoints/{checkpoint['id']}/resolve"
    resolution = {"resolution": "accepted", "resolved_by": "pi", "rationale": "reviewed", "create_decision": True}
    first = await mcp_env.put(url, headers=headers, json=resolution)
    assert first.status_code == 200, first.text
    retry = await mcp_env.put(url, headers=headers, json=resolution)
    assert retry.json() == first.json()
    conflict = await mcp_env.put(url, headers=headers, json={**resolution, "resolution": "changed"})
    assert conflict.status_code == 409
    assert len((await mcp_env.get("/api/decisions", headers=headers)).json()) == 1
    report_url = f"/api/missions/{mission['id']}/report"
    report = {"summary": "done", "findings": ["one finding"]}
    first = await mcp_env.post(report_url, headers=headers, json=report)
    assert first.status_code == 200, first.text
    assert (await mcp_env.post(report_url, headers=headers, json=report)).json() == first.json()
    assert (await mcp_env.post(report_url, headers=headers, json={**report, "summary": "overwrite"})).status_code == 409
    assert len((await mcp_env.get("/api/notes", headers=headers)).json()) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["rest", "typed", "legacy", "bulk"])
async def test_journal_update_audit_is_readable_after_every_public_path(mcp_env, surface):
    import rka.mcp.server as mcp_server

    headers = {"X-RKA-Project": "proj_default"}
    created = await mcp_env.post(
        "/api/notes", headers=headers,
        json={"content": "Synthetic original", "source": "brain", "tags": ["before"]},
    )
    assert created.status_code == 201, created.text
    note_id = created.json()["id"]
    data = {"content": "Synthetic revision", "tags": ["after"]}
    if surface == "rest":
        response = await mcp_env.put(
            f"/api/notes/{note_id}", json=data,
            headers={**headers, "X-RKA-Actor": "pi"},
        )
        assert response.status_code == 200, response.text
    elif surface == "typed":
        result = await dispatch_execute_typed(UpdateNoteArgs(
            operation="update_note", project_id="proj_default", id=note_id, **data,
        ))
        assert "Updated" in result
    elif surface == "legacy":
        result = await mcp_server.rka_update_note(id=note_id, project_id="proj_default", **data)
        assert "Updated" in result
    else:
        result = await dispatch_execute_typed(BulkUpdateArgs(
            operation="bulk_update", project_id="proj_default",
            updates=[{"entity_type": "journal", "id": note_id, **data}],
        ))
        assert result.startswith("Updated 1/1")
    audit = await mcp_env.get(
        "/api/audit", headers=headers,
        params={"entity_type": "journal", "entity_id": note_id, "action": "update"},
    )
    assert audit.status_code == 200, audit.text
    row, = audit.json()
    assert row["details"]["before"] == {"content": "Synthetic original", "tags": ["before"]}
    assert row["details"]["after"] == data
    # This batch does not invent an authenticated actor from source or headers.
    assert row["actor"] == "system"
    assert row["details"]["actor_basis"] == "legacy_default"


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["rest", "typed", "legacy"])
async def test_attribution_correction_round_trip_and_conflict(mcp_env, surface):
    import rka.mcp.server as mcp_server

    headers = {"X-RKA-Project": "proj_default"}
    note = (await mcp_env.post("/api/notes", headers=headers, json={"content": "Synthetic"})).json()
    note_id = note["id"]
    assert note["attribution_revision"] == 0
    entity = json.loads(await dispatch_query_typed(QueryEntityArgs(
        project_id="proj_default", id=note_id,
    )))
    assert entity["attribution_revision"] == 0
    body = {"expected_revision": 0, "request_id": "public-1", "actor": "executor",
            "reason": "Correct source", "source": "pi", "verbatim_input": "Exact synthetic original"}

    async def call(data):
        if surface == "rest":
            response = await mcp_env.post(f"/api/notes/{note_id}/attribution-corrections", headers=headers, json=data)
            response.raise_for_status()
            return response.json()
        if surface == "typed":
            return json.loads(await dispatch_execute_typed(CorrectNoteAttributionArgs(
                project_id="proj_default", id=note_id, **data,
            )))
        return json.loads(await mcp_server.rka_correct_note_attribution(
            project_id="proj_default", id=note_id, **data,
        ))

    first = await call(body)
    assert first["revision"] == 1 and first["actor_basis"] == "caller_asserted"
    assert first["before_verbatim_input"] is None
    assert await call(body) == first
    if surface == "rest":
        with pytest.raises(httpx.HTTPStatusError) as error:
            await call({**body, "request_id": "stale"})
        assert error.value.response.status_code == 409
    else:
        # Existing MCP adapters render API detail as an ordinary Exception.
        with pytest.raises(Exception, match="API error 409: attribution revision conflict"):
            await call({**body, "request_id": "stale"})
    second = await call({**body, "expected_revision": 1, "request_id": "public-2",
                         "source": "brain", "verbatim_input": None})
    assert second["revision"] == 2 and second["after_verbatim_input"] is None
    assert await call(body) == first
    history = json.loads(await dispatch_query_typed(QueryNoteAttributionHistoryArgs(
        project_id="proj_default", id=note_id, after_revision=1,
    )))
    assert history == [second]
    note = (await mcp_env.get(f"/api/notes/{note_id}", headers=headers)).json()
    assert note["attribution_revision"] == 2 and note["source"] == "brain"
    assert note["verbatim_input"] is None
    entity = json.loads(await dispatch_query_typed(QueryEntityArgs(
        project_id="proj_default", id=note_id,
    )))
    assert entity["attribution_revision"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["rest", "typed", "legacy", "bulk", "legacy_bulk"])
async def test_public_updates_cannot_bypass_attribution_correction(mcp_env, surface):
    import rka.mcp.server as mcp_server

    headers = {"X-RKA-Project": "proj_default"}
    note = (await mcp_env.post("/api/notes", headers=headers, json={"content": "Synthetic"})).json()
    data = {"source": "pi", "verbatim_input": "Forged assertion", "content": "Must not be written"}
    if surface == "rest":
        response = await mcp_env.put(f"/api/notes/{note['id']}", headers=headers, json=data)
        assert response.status_code == 422
    elif surface == "typed":
        with pytest.raises(ValidationError, match="correct_note_attribution"):
            UpdateNoteArgs(project_id="proj_default", id=note["id"], **data)
    elif surface == "legacy":
        with pytest.raises(Exception, match="API error 422:.*correct_note_attribution"):
            await mcp_server.rka_update_note(id=note["id"], project_id="proj_default", **data)
    else:
        updates = [
            {"entity_type": "journal", "id": note["id"], "content": "Would be partial"},
            {"entity_type": "journal", "id": note["id"], **data},
        ]
        if surface == "bulk":
            with pytest.raises(ValidationError, match="correct_note_attribution"):
                BulkUpdateArgs(project_id="proj_default", updates=updates)
        else:
            result = await mcp_server.rka_bulk_update(project_id="proj_default", updates=updates)
            assert "correct_note_attribution" in result
    readback = (await mcp_env.get(f"/api/notes/{note['id']}", headers=headers)).json()
    assert readback == note


@pytest.mark.asyncio
async def test_history_and_correction_reject_wrong_project_and_bad_requests(mcp_env):
    headers = {"X-RKA-Project": "proj_default"}
    note = (await mcp_env.post("/api/notes", headers=headers, json={"content": "Synthetic"})).json()
    body = {"expected_revision": 0, "request_id": "public-1", "actor": "executor",
            "reason": "Correction", "source": "brain", "verbatim_input": None}
    for key in body:
        missing = {k: v for k, v in body.items() if k != key}
        result = await mcp_env.post(f"/api/notes/{note['id']}/attribution-corrections", headers=headers, json=missing)
        assert result.status_code == 422, key
    assert (await mcp_env.get(f"/api/notes/{note['id']}/attribution-history", headers=headers, params={"limit": 201})).status_code == 422
    project = await mcp_env.post("/api/projects", json={"name": "Unrelated synthetic project"})
    assert project.status_code == 200, project.text
    outsider = {"X-RKA-Project": project.json()["id"]}
    assert (await mcp_env.get(f"/api/notes/{note['id']}/attribution-history", headers=outsider)).status_code == 404
    assert (await mcp_env.post(f"/api/notes/{note['id']}/attribution-corrections", headers=outsider, json=body)).status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["dispatcher", "legacy_wrapper"])
async def test_raw_dispatch_requires_explicit_nullable_original(mcp_env, surface):
    from rka.mcp.server import _rka_execute_legacy_impl

    invoke = dispatch_execute if surface == "dispatcher" else _rka_execute_legacy_impl
    headers = {"X-RKA-Project": "proj_default"}
    note = (await mcp_env.post("/api/notes", headers=headers, json={
        "content": "Synthetic", "verbatim_input": "Existing original",
    })).json()
    data = {"expected_revision": 0, "request_id": "raw-1", "actor": "executor",
            "reason": "Correct source and remove wrongly attributed original", "source": "brain"}
    missing = json.loads(await invoke(
        "correct_note_attribution", project_id="proj_default", id=note["id"], **data,
    ))
    assert missing["error"] == "missing_field"
    assert "verbatim_input" in missing["message"]
    readback = (await mcp_env.get(f"/api/notes/{note['id']}", headers=headers)).json()
    assert readback == note
    corrected = json.loads(await invoke(
        "correct_note_attribution", project_id="proj_default", id=note["id"],
        verbatim_input=None, **data,
    ))
    assert corrected["before_verbatim_input"] == "Existing original"
    assert corrected["after_verbatim_input"] is None


@pytest.mark.asyncio
async def test_mcp_summary_only_note_update_round_trip(
    mcp_env: httpx.AsyncClient,
) -> None:
    created = await mcp_env.post(
        "/api/notes",
        headers={"X-RKA-Project": "proj_default"},
        json={"content": "Body stays unchanged.", "summary": "Old summary."},
    )
    assert created.status_code == 201, created.text
    note_id = created.json()["id"]

    result = await dispatch_execute_typed(
        UpdateNoteArgs(
            operation="update_note",
            project_id="proj_default",
            id=note_id,
            summary="New summary.",
        )
    )
    assert "Updated" in result

    read_back = await mcp_env.get(
        f"/api/notes/{note_id}",
        headers={"X-RKA-Project": "proj_default"},
    )
    assert read_back.status_code == 200
    assert read_back.json()["summary"] == "New summary."
    assert read_back.json()["content"] == "Body stays unchanged."


@pytest.mark.asyncio
async def test_mcp_journal_lifecycle_and_pinning_update_round_trip(
    mcp_env: httpx.AsyncClient,
) -> None:
    created = await mcp_env.post(
        "/api/notes",
        headers={"X-RKA-Project": "proj_default"},
        json={
            "content": "Lifecycle probe.",
            "source": "brain",
            "confidence": "verified",
            "status": "active",
            "pinned": False,
        },
    )
    assert created.status_code == 201, created.text
    note_id = created.json()["id"]

    result = await dispatch_execute_typed(
        UpdateNoteArgs(
            operation="update_note",
            project_id="proj_default",
            id=note_id,
            status="superseded",
            pinned=True,
        )
    )
    assert "fields=status,pinned" in result

    read_back = await mcp_env.get(
        f"/api/notes/{note_id}",
        headers={"X-RKA-Project": "proj_default"},
    )
    assert read_back.status_code == 200
    assert read_back.json()["status"] == "superseded"
    assert read_back.json()["pinned"] is True
    assert read_back.json()["source"] == "brain"
    assert read_back.json()["confidence"] == "verified"


@pytest.mark.asyncio
async def test_flat_and_legacy_nested_bulk_update_round_trip(
    mcp_env: httpx.AsyncClient,
) -> None:
    created = await mcp_env.post(
        "/api/notes",
        headers={"X-RKA-Project": "proj_default"},
        json={
            "content": "Bulk lifecycle probe.",
            "importance": "critical",
            "status": "active",
            "pinned": False,
        },
    )
    assert created.status_code == 201, created.text
    note_id = created.json()["id"]

    flat_result = await dispatch_execute_typed(
        BulkUpdateArgs(
            operation="bulk_update",
            project_id="proj_default",
            updates=[
                {
                    "entity_type": "journal",
                    "id": note_id,
                    "importance": "low",
                    "status": "superseded",
                    "tags": ["bulk-verified"],
                }
            ],
        )
    )
    assert flat_result.startswith("Updated 1/1")
    assert "fields=importance,status,tags" in flat_result

    read_back = await mcp_env.get(
        f"/api/notes/{note_id}",
        headers={"X-RKA-Project": "proj_default"},
    )
    assert read_back.status_code == 200
    assert read_back.json()["importance"] == "low"
    assert read_back.json()["status"] == "superseded"
    assert read_back.json()["tags"] == ["bulk-verified"]

    nested_result = await dispatch_execute_typed(
        BulkUpdateArgs(
            operation="bulk_update",
            project_id="proj_default",
            updates=[
                {
                    "entity_type": "note",
                    "id": note_id,
                    "data": {"status": "active", "pinned": True},
                }
            ],
        )
    )
    assert nested_result.startswith("Updated 1/1")

    final_read_back = await mcp_env.get(
        f"/api/notes/{note_id}",
        headers={"X-RKA-Project": "proj_default"},
    )
    assert final_read_back.status_code == 200
    assert final_read_back.json()["status"] == "active"
    assert final_read_back.json()["pinned"] is True


@pytest.mark.asyncio
async def test_bulk_update_validates_and_updates_every_supported_entity_type(
    mcp_env: httpx.AsyncClient,
) -> None:
    decision = await mcp_env.post(
        "/api/decisions",
        headers={"X-RKA-Project": "proj_default"},
        json={
            "question": "Original decision question?",
            "phase": "design",
            "decided_by": "brain",
        },
    )
    assert decision.status_code == 201, decision.text
    literature = await mcp_env.post(
        "/api/literature",
        headers={"X-RKA-Project": "proj_default"},
        json={"title": "Bulk update paper", "status": "to_read"},
    )
    assert literature.status_code == 201, literature.text

    result = await dispatch_execute_typed(
        BulkUpdateArgs(
            operation="bulk_update",
            project_id="proj_default",
            updates=[
                {
                    "entity_type": "decision",
                    "id": decision.json()["id"],
                    "rationale": "Updated through validated bulk dispatch.",
                    "tags": ["bulk-decision"],
                },
                {
                    "entity_type": "literature",
                    "id": literature.json()["id"],
                    "status": "read",
                    "tags": ["bulk-literature"],
                },
            ],
        )
    )
    assert result.startswith("Updated 2/2")

    decision_readback = await mcp_env.get(
        f"/api/decisions/{decision.json()['id']}",
        headers={"X-RKA-Project": "proj_default"},
    )
    assert decision_readback.status_code == 200
    assert decision_readback.json()["rationale"] == (
        "Updated through validated bulk dispatch."
    )
    assert decision_readback.json()["tags"] == ["bulk-decision"]

    literature_readback = await mcp_env.get(
        f"/api/literature/{literature.json()['id']}",
        headers={"X-RKA-Project": "proj_default"},
    )
    assert literature_readback.status_code == 200
    assert literature_readback.json()["status"] == "read"
    assert literature_readback.json()["tags"] == ["bulk-literature"]


@pytest.mark.asyncio
async def test_legacy_bulk_preflight_prevents_partial_write(
    mcp_env: httpx.AsyncClient,
) -> None:
    import rka.mcp.server as mcp_server

    created = await mcp_env.post(
        "/api/notes",
        headers={"X-RKA-Project": "proj_default"},
        json={"content": "Must remain active.", "status": "active"},
    )
    assert created.status_code == 201, created.text
    note_id = created.json()["id"]

    result = await mcp_server.rka_bulk_update(
        updates=[
            {
                "entity_type": "journal",
                "id": note_id,
                "status": "superseded",
            },
            {
                "entity_type": "journal",
                "id": "jrn_invalid",
                "unknown": "field",
            },
        ],
        project_id="proj_default",
    )
    assert result.startswith("Updated 0/2 (1 errors)")

    read_back = await mcp_env.get(
        f"/api/notes/{note_id}",
        headers={"X-RKA-Project": "proj_default"},
    )
    assert read_back.status_code == 200
    assert read_back.json()["status"] == "active"


@pytest.mark.parametrize(
    "item",
    [
        {"id": "jrn_probe", "status": "superseded"},
        {"entity_type": "journal", "id": "jrn_probe"},
        {
            "entity_type": "journal",
            "id": "jrn_probe",
            "data": {"status": "superseded"},
            "pinned": True,
        },
        {"entity_type": "journal", "id": "jrn_probe", "unknown": "value"},
    ],
)
def test_bulk_update_rejects_ambiguous_or_empty_items(item: dict) -> None:
    with pytest.raises(ValidationError):
        BulkUpdateArgs(
            operation="bulk_update",
            project_id="proj_default",
            updates=[item],
        )


@pytest.mark.asyncio
async def test_bulk_update_does_not_report_success_on_readback_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import rka.mcp.server as mcp_server

    class MismatchResponse:
        status_code = 200
        text = ""

        @staticmethod
        def json() -> dict:
            return {"id": "jrn_probe", "status": "active"}

    class MismatchClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def put(self, _endpoint: str, *, json: dict):
            assert json == {"status": "superseded"}
            return MismatchResponse()

    async def no_session_hook(_project_id):
        return None

    monkeypatch.setattr(mcp_server, "_client", lambda _project_id: MismatchClient())
    monkeypatch.setattr(mcp_server, "_maybe_fire_session_start", no_session_hook)

    result = await mcp_server.rka_bulk_update(
        updates=[
            {
                "entity_type": "journal",
                "id": "jrn_probe",
                "status": "superseded",
            }
        ],
        project_id="proj_default",
    )

    assert result.startswith("Updated 0/1 (1 errors)")
    assert "write response mismatch for fields=status" in result


@pytest.mark.asyncio
async def test_mcp_rejects_foreign_provenance_without_partial_write(
    mcp_env: httpx.AsyncClient,
) -> None:
    project = await mcp_env.post(
        "/api/projects",
        json={"id": "prj_foreign", "name": "Foreign"},
    )
    assert project.status_code == 200, project.text
    decision = await mcp_env.post(
        "/api/decisions",
        headers={"X-RKA-Project": "prj_foreign"},
        json={
            "question": "Foreign decision?",
            "phase": "design",
            "decided_by": "brain",
        },
    )
    assert decision.status_code == 201, decision.text

    with pytest.raises(Exception, match="API error 422.*proj_default"):
        await dispatch_execute_typed(
            RecordNoteArgs(
                operation="record_note",
                project_id="proj_default",
                content="MCP write that must roll back.",
                provenance={"related_decisions": [decision.json()["id"]]},
            )
        )

    listed = await mcp_env.get(
        "/api/notes",
        headers={"X-RKA-Project": "proj_default"},
    )
    assert listed.status_code == 200
    assert all(
        note["content"] != "MCP write that must roll back."
        for note in listed.json()
    )
