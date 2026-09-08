"""E2.2 locks the supported public REST/MCP contract."""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

from rka.api.app import create_app
from rka.config import RKAConfig
from rka.contracts import (
    AGENTIC_MCP_OPERATIONS,
    AGENTIC_REST_OPERATIONS,
    CORE_LEGACY_MCP_OPERATIONS,
    CORE_LEGACY_REST_OPERATIONS,
    MCP_TRANSPORT_TOOLS,
)
from rka.mcp.operations_schema import (
    OPERATIONS_SCHEMA,
    WRITER_COMPATIBILITY_OPERATIONS,
)
from tests.contract_fixture.client import PublicCoreClient, STABLE_REST_OPERATIONS


ROOT = Path(__file__).resolve().parents[1]
REST_SNAPSHOT = ROOT / "contracts" / "rka-rest-v1.openapi.json"
MCP_SNAPSHOT = ROOT / "contracts" / "rka-mcp-v1.json"
FIXTURE_CLIENT = ROOT / "tests" / "contract_fixture" / "client.py"


def _snapshot(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_checked_in_contract_snapshots_match_runtime() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/update_contract_snapshots.py"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_rest_v1_surface_and_dispositions_are_locked() -> None:
    snapshot = _snapshot(REST_SNAPSHOT)
    assert snapshot["openapi"]
    assert snapshot["info"] == {
        "title": "RKA Core stable REST contract",
        "version": "v1",
    }
    assert snapshot["x-rka-operation-counts"] == {
        "ownership": {
            "agentic-unsupported": 10,
            "core": 168,
            "core-legacy": 6,
            "writer-compatibility": 53,
        },
        "core_maturity": {"preview": 28, "stable": 140},
    }
    assert len(AGENTIC_REST_OPERATIONS) == 10
    assert len(CORE_LEGACY_REST_OPERATIONS) == 6
    methods = {"get", "put", "post", "delete", "options", "head", "patch", "trace"}
    assert sum(1 for item in snapshot["paths"].values() for key in item if key in methods) == 140
    assert "post" in snapshot["paths"]["/api/notes/{note_id}/attribution-corrections"]
    assert "get" in snapshot["paths"]["/api/notes/{note_id}/attribution-history"]
    assert "post" in snapshot["paths"]["/api/config/embedding/backfill/{job_id}/cancel"]
    snapshotted_operations = {
        (method.upper(), path)
        for path, item in snapshot["paths"].items()
        for method in item
        if method in methods
    }
    assert STABLE_REST_OPERATIONS <= snapshotted_operations


def test_rest_v1_inline_responses_are_valid_openapi_objects() -> None:
    snapshot = _snapshot(REST_SNAPSHOT)
    methods = {"get", "put", "post", "delete", "options", "head", "patch", "trace"}
    for path, path_item in snapshot["paths"].items():
        for method, operation in path_item.items():
            if method not in methods:
                continue
            for status, response in operation.get("responses", {}).items():
                if "$ref" not in response:
                    assert response.get("description") == "Response", (
                        path,
                        method,
                        status,
                    )


def test_mcp_v1_surface_and_dispositions_are_locked() -> None:
    snapshot = _snapshot(MCP_SNAPSHOT)
    assert snapshot["operation_counts"] == {
        "ownership": {
            "agentic-unsupported": 5,
            "core": 108,
            "core-legacy": 1,
            "writer-compatibility": 43,
        },
        "core_maturity": {"preview": 22, "stable": 86},
    }
    assert len(snapshot["operations"]) == 86
    assert {"correct_note_attribution", "note_attribution_history"} <= set(snapshot["operations"])
    assert tuple(snapshot["transport_tools"]) == tuple(sorted(MCP_TRANSPORT_TOOLS))
    assert len(OPERATIONS_SCHEMA) == 157
    assert len(WRITER_COMPATIBILITY_OPERATIONS) == 43
    assert len(AGENTIC_MCP_OPERATIONS) == 5
    assert len(CORE_LEGACY_MCP_OPERATIONS) == 1
    for tool_name in ("rka_query", "rka_execute"):
        args = snapshot["transport_tools"][tool_name]["input_schema"]["properties"]["args"]
        assert set(args["discriminator"]["mapping"]) == {
            name for name, entry in snapshot["operations"].items() if entry["tool"] == tool_name
        }
    for tool in snapshot["transport_tools"].values():
        assert tool["output_schema"]["properties"]["result"]["type"] == "string"


def test_contract_normalization_preserves_fields_named_like_schema_metadata() -> None:
    rest = _snapshot(REST_SNAPSHOT)
    assert "ScanManifest" in rest["components"]["schemas"]
    assert "ScanManifest-Input" not in rest["components"]["schemas"]
    assert "ScanManifest-Output" not in rest["components"]["schemas"]
    literature_create = rest["components"]["schemas"]["LiteratureCreate"]
    assert "title" in literature_create["properties"]
    assert "title" in literature_create["required"]

    mcp = _snapshot(MCP_SNAPSHOT)
    record_literature = mcp["operations"]["record_literature"]["input_schema"]
    assert "title" in record_literature["properties"]


def test_stable_scoped_mcp_operations_require_project_id() -> None:
    snapshot = _snapshot(MCP_SNAPSHOT)
    unscoped = {"capabilities", "create_project", "health", "list_projects"}
    for operation, entry in snapshot["operations"].items():
        required = set(entry["input_schema"].get("required", []))
        if operation in unscoped:
            assert "project_id" not in required
        else:
            assert "project_id" in required, operation


def test_fixture_client_has_no_core_imports() -> None:
    tree = ast.parse(FIXTURE_CLIENT.read_text(encoding="utf-8"))
    forbidden: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            forbidden.extend(
                alias.name
                for alias in node.names
                if alias.name == "rka" or alias.name.startswith("rka.")
            )
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == "rka" or module.startswith("rka."):
                forbidden.append(module)
    assert forbidden == []


@pytest_asyncio.fixture
async def contract_http(tmp_path: Path):
    app = create_app(
        RKAConfig(
            project_dir=tmp_path,
            db_path=Path("public-contract.db"),
            data_dir=tmp_path / "data",
            llm_enabled=False,
            embeddings_enabled=False,
        )
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://contract.test",
        ) as http,
    ):
        yield http


@pytest.mark.asyncio
async def test_public_only_client_completes_stable_research_workflow(
    contract_http: httpx.AsyncClient,
) -> None:
    http = contract_http
    client = PublicCoreClient(http)
    capabilities = await client.capabilities()
    assert capabilities["core"]["contract"] == "rka-core/v1"

    project_id = "prj_contract_fixture"
    project = await client.create_project(
        project_id=project_id,
        name="Public contract fixture",
        description="Disposable E2.2 downstream-client test.",
    )
    assert project["id"] == project_id
    scoped = client.for_project(project_id)

    note = await scoped.create_note(content="The disposable run observed a 12 percent improvement.")
    assert (await scoped.get_note(note["id"]))["content"] == note["content"]
    rq = await scoped.create_research_question(
        question="Does the fixture method improve the measured outcome?",
        related_journal=[note["id"]],
    )
    claim = await scoped.create_claim(
        source_entry_id=note["id"],
        content="The fixture method improved the measured outcome by 12 percent.",
    )

    cluster = await scoped.create_cluster(
        research_question_id=rq["id"], label="Observed improvement"
    )
    first_edge = await scoped.assign_claim_to_cluster(
        claim_id=claim["id"], cluster_id=cluster["id"]
    )
    repeated_edge = await scoped.assign_claim_to_cluster(
        claim_id=claim["id"], cluster_id=cluster["id"]
    )
    assert repeated_edge["id"] == first_edge["id"]

    evidence = await scoped.assemble_evidence(rq["id"])
    assert "12 percent" in evidence["content"]
    research_map = await scoped.research_map()
    assert any(item["id"] == rq["id"] for item in research_map["research_questions"])
    changes = await scoped.changes_since()
    assert changes["next_cursor"] > 0


@pytest.mark.asyncio
async def test_preview_claim_scope_declares_revision_hash_and_conflict(
    contract_http: httpx.AsyncClient,
) -> None:
    project_id = "prj_contract_scope_fixture"
    client = PublicCoreClient(contract_http)
    await client.create_project(project_id=project_id, name="Scope fixture")
    scoped = client.for_project(project_id)
    note = await scoped.create_note(content="A bounded observation.")
    claim = await scoped.create_claim(
        source_entry_id=note["id"], content="The bounded observation holds."
    )
    headers = {"X-RKA-Project": project_id}
    initial = await contract_http.get(f"/api/claims/{claim['id']}/scope", headers=headers)
    assert initial.status_code == 200
    assert initial.json()["current_revision"] == 0

    payload = {
        "expected_revision": 0,
        "actor": "brain",
        "reason": "Bound the fixture claim to its recorded test setting.",
        "conditions": [
            {
                "kind": "environment",
                "key": "fixture_setting",
                "operator": "equals",
                "value": "disposable-contract-test",
            }
        ],
        "uncertainty": "none",
        "extension_policy": "exact_only",
        "prohibited_extensions": ["unrecorded settings"],
        "falsifier_status": "applicable",
        "falsifier": "A repeated run fails to reproduce the observation.",
        "review_status": "reviewed",
    }
    written = await contract_http.post(
        f"/api/claims/{claim['id']}/scope", headers=headers, json=payload
    )
    assert written.status_code == 200
    assert written.json()["current_revision"] == 1
    assert written.json()["current"]["claim_content_hash"]

    stale = await contract_http.post(
        f"/api/claims/{claim['id']}/scope", headers=headers, json=payload
    )
    assert stale.status_code == 409
