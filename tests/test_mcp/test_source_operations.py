"""Typed MCP source registration/admission contract and dispatch."""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

import httpx
import pytest
from pydantic import TypeAdapter, ValidationError

from rka.mcp import server, verb_dispatch
from rka.mcp.operation_args import ExecuteArgsUnion, QueryArgsUnion
from rka.mcp.operations_schema import OPERATIONS_SCHEMA


def test_source_operations_are_typed_and_described() -> None:
    query = TypeAdapter(QueryArgsUnion).validate_python(
        {"operation": "sources", "project_id": "prj_test", "id": "src_test"}
    )
    assert query.operation == "sources"

    register = TypeAdapter(ExecuteArgsUnion).validate_python(
        {
            "operation": "register_source",
            "project_id": "prj_test",
            "source_kind": "pasted_text",
            "pasted_text": "exact bytes",
            "registered_by": "pi",
        }
    )
    assert register.source_kind == "pasted_text"
    with pytest.raises(ValidationError, match="Input should be True"):
        TypeAdapter(ExecuteArgsUnion).validate_python(
            {
                "operation": "admit_source_interpretation",
                "project_id": "prj_test",
                "source_id": "src_test",
                "candidate_id": "icd_test",
                "expected_revision": 1,
                "target_type": "journal",
                "target_id": "jrn_test",
                "actor": "pi",
                "reason": "not checked",
                "grounding_verified": False,
            }
        )

    assert OPERATIONS_SCHEMA["register_source"]["notes"].startswith("Never fetches")
    assert OPERATIONS_SCHEMA["admit_source_interpretation"]["optional_fields"] == []
    assert "sources" in verb_dispatch._QUERY_DISPATCH
    assert "register_source" in verb_dispatch.EXECUTE_OPERATIONS
    assert "admit_source_interpretation" in verb_dispatch.EXECUTE_OPERATIONS


@pytest.mark.asyncio
async def test_source_typed_dispatch_routes_exact_fields(monkeypatch) -> None:
    calls: list[tuple[str, dict]] = []

    def fake_legacy(name: str):
        async def invoke(**kwargs):
            calls.append((name, kwargs))
            return json.dumps({"ok": True})

        return invoke

    monkeypatch.setattr(verb_dispatch, "_legacy", fake_legacy)
    query = TypeAdapter(QueryArgsUnion).validate_python(
        {
            "operation": "sources",
            "project_id": "prj_test",
            "filters": {"source_kind": "repository"},
            "limit": 7,
        }
    )
    await verb_dispatch.dispatch_query_typed(query)
    register = TypeAdapter(ExecuteArgsUnion).validate_python(
        {
            "operation": "register_source",
            "project_id": "prj_test",
            "source_kind": "repository",
            "stable_locator": "https://example.test/repo/tree/abc",
            "registered_by": "executor",
            "ownership_kind": "third_party",
        }
    )
    await verb_dispatch.dispatch_execute_typed(register)
    admission = TypeAdapter(ExecuteArgsUnion).validate_python(
        {
            "operation": "admit_source_interpretation",
            "project_id": "prj_test",
            "source_id": "src_test",
            "candidate_id": "icd_test",
            "expected_revision": 1,
            "target_type": "claim",
            "target_id": "clm_test",
            "actor": "pi",
            "reason": "Verified exact bytes.",
            "grounding_verified": True,
        }
    )
    await verb_dispatch.dispatch_execute_typed(admission)

    assert calls[0] == (
        "rka_get_sources",
        {
            "source_id": None,
            "source_kind": "repository",
            "ownership_kind": None,
            "limit": 7,
            "project_id": "prj_test",
        },
    )
    assert calls[1][0] == "rka_register_source"
    assert calls[1][1]["stable_locator"].endswith("/abc")
    assert calls[2][0] == "rka_admit_source_interpretation"
    assert calls[2][1]["grounding_verified"] is True


@pytest.mark.asyncio
async def test_mcp_reads_host_file_and_transports_exact_bytes(
    monkeypatch,
    tmp_path: Path,
) -> None:
    payload = b"host-only\x00binary\xff"
    source_path = tmp_path / "capture.bin"
    source_path.write_bytes(payload)
    monkeypatch.setenv("RKA_HOST_FILE_ROOTS", json.dumps([str(tmp_path)]))
    captured: dict = {}

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, path: str, *, json: dict):
            if path == "/api/sources":
                captured["path"] = path
                captured["json"] = json
            return httpx.Response(
                201,
                request=httpx.Request("POST", f"http://core{path}"),
                json={
                    "source": {"id": "src_test", "title": "capture.bin"},
                    "duplicate": False,
                },
            )

    monkeypatch.setattr(server, "_client", lambda _project_id: FakeClient())
    result = await server.rka_register_source(
        source_kind="file",
        filepath=str(source_path),
        registered_by="executor",
        project_id="prj_test",
    )

    assert json.loads(result)["source"]["id"] == "src_test"
    assert captured["path"] == "/api/sources"
    request = captured["json"]
    assert "filepath" not in request
    assert request["filename"] == "capture.bin"
    assert base64.b64decode(request["content_base64"]) == payload
    assert request["expected_content_hash"] == hashlib.sha256(payload).hexdigest()


def test_mcp_host_file_transfer_rejects_symlink_and_oversize(
    monkeypatch,
    tmp_path: Path,
) -> None:
    source_path = tmp_path / "capture.bin"
    source_path.write_bytes(b"12345")
    monkeypatch.setenv("RKA_HOST_FILE_ROOTS", json.dumps([str(tmp_path)]))
    symlink = tmp_path / "capture-link.bin"
    symlink.symlink_to(source_path)

    with pytest.raises(PermissionError, match="symlink"):
        server._read_registered_source_file(str(symlink))

    monkeypatch.setenv("RKA_REGISTERED_SOURCE_MAX_BYTES", "4")
    with pytest.raises(PermissionError, match="maximum size"):
        server._read_registered_source_file(str(source_path))
