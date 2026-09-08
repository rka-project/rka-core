"""Tests for the /api/capabilities response shape.

Affordance C (Mission B / mis_01KR209WY4M6WQFEXRH79KC2ZF) introduced
the endpoint. Mission D (v2.4.0 / mis_01KRNYPVB8N3HDMZ9HK9HM3TB0) is a
BREAKING-IN-MINOR change: the `llm` field is removed entirely per PI
directive jrn_01KRNZBS50K250HHHHEC58E4GC.

Verified states (E2.1, additive over Mission D):
  - The embedding block reports availability, reason, and active search mode
  - Core/interface versions and contract discovery are explicit
  - Unsupported requirements return an actionable 409 response
  - `llm` field is ABSENT (not null, not {available: false} — gone)
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import pytest_asyncio

from rka.api.app import create_app
from rka import __version__
from rka.config import RKAConfig
from rka.mcp.operations_schema import (
    DEPRECATED_OPERATIONS,
    OPERATIONS_SCHEMA,
    list_operations_compact,
)


@pytest_asyncio.fixture
async def api_client(tmp_path: Path):
    """API client with embeddings disabled (default test config).

    Mission D removed RKA_LLM_* config knobs; we still pass llm_enabled=False
    to the RKAConfig constructor for back-compat — the field is preserved
    server-side but no longer surfaces via capabilities.
    """
    config = RKAConfig(
        project_dir=tmp_path,
        db_path=Path("capabilities_route.db"),
        llm_enabled=False,
        embeddings_enabled=False,
    )
    app = create_app(config)
    lifespan = app.router.lifespan_context(app)
    await lifespan.__aenter__()
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            yield client
    finally:
        await lifespan.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_capabilities_endpoint_is_versioned_and_additive(api_client: httpx.AsyncClient):
    r = await api_client.get("/api/capabilities")
    assert r.status_code == 200
    body = r.json()
    assert set(body) >= {
        "schema_version",
        "core",
        "interfaces",
        "supported_capabilities",
        "available_capabilities",
        "embedding",
    }
    assert body["schema_version"] == "rka.core-capabilities/v1"
    assert body["core"] == {
        "name": "rka-core",
        "version": __version__,
        "contract": "rka-core/v1",
        "supported_contracts": ["rka-core/v1"],
    }
    assert body["interfaces"]["rest"]["contract"] == "rka-rest/v1"
    assert body["interfaces"]["mcp"]["contract"] == "rka-mcp/v1"
    mcp = body["interfaces"]["mcp"]
    assert mcp["operation_maturity_basis"] == "usage-readiness"
    assert mcp["usage_stable_operation_count"] + mcp["usage_preview_operation_count"] + mcp[
        "deprecated_operation_count"
    ] == len(OPERATIONS_SCHEMA)
    assert mcp["deprecated_operation_count"] == len(DEPRECATED_OPERATIONS)
    listed_by_default = sum(len(items) for items in list_operations_compact().values())
    assert mcp["default_operation_count"] == listed_by_default == 88
    assert mcp["supported_operation_count"] == 110
    assert mcp["supported_usage_stable_operation_count"] == 88
    assert {"resolve_stale", "record_directive_dependency"} <= set(OPERATIONS_SCHEMA)
    assert {"correct_note_attribution", "note_attribution_history"} <= set(OPERATIONS_SCHEMA)
    assert mcp["default_operation_count"] == mcp["supported_usage_stable_operation_count"]
    assert mcp["supported_usage_preview_operation_count"] == 22
    assert mcp["unsupported_operation_count"] == 5
    assert mcp["legacy_operation_count"] == 1


@pytest.mark.asyncio
async def test_capabilities_llm_field_is_absent(api_client: httpx.AsyncClient):
    """Direct assertion for the breaking-change verification — kept as a
    standalone test so a future re-introduction surfaces clearly here."""
    r = await api_client.get("/api/capabilities")
    body = r.json()
    assert "llm" not in body, (
        "the `llm` field was REMOVED in v2.4.0 (Mission D); a re-introduction "
        "would be a backwards-incompatible change deserving its own decision"
    )


@pytest.mark.asyncio
async def test_capabilities_embedding_block_shape_preserved(api_client: httpx.AsyncClient):
    """The embedding block keeps old fields and adds an explicit search mode."""
    r = await api_client.get("/api/capabilities")
    body = r.json()
    emb = body["embedding"]
    assert set(emb.keys()) >= {"available", "reason_unavailable", "search_mode"}
    assert isinstance(emb["available"], bool)
    if emb["available"]:
        assert emb["reason_unavailable"] is None
    else:
        assert isinstance(emb["reason_unavailable"], str)
        assert len(emb["reason_unavailable"]) > 0
        assert emb["search_mode"] == "lexical"


@pytest.mark.asyncio
async def test_capabilities_embedding_disabled_carries_reason(api_client: httpx.AsyncClient):
    """In the test conftest, embeddings are disabled; the reason mentions
    the disabled flag so the consumer side (rka_search degraded one-liner,
    Settings page first-run banner) can rely on the wording."""
    r = await api_client.get("/api/capabilities")
    body = r.json()
    emb = body["embedding"]
    assert emb["available"] is False
    assert "disabled" in emb["reason_unavailable"].lower()
    assert emb["search_mode"] == "lexical"


@pytest.mark.asyncio
async def test_capabilities_tracks_runtime_and_generation_degradation(
    api_client: httpx.AsyncClient,
):
    class _Embeddings:
        runtime_available = False
        ready = True

        async def index_search_ready(self) -> bool:
            return self.ready

    app = api_client._transport.app
    assert app.state.db.vec_available is True
    embeddings = _Embeddings()
    app.state.embeddings = embeddings

    failed = (await api_client.get("/api/capabilities")).json()
    assert failed["embedding"]["available"] is False
    assert failed["embedding"]["search_mode"] == "lexical"
    assert "embedding" not in failed["available_capabilities"]

    embeddings.runtime_available = True
    embeddings.ready = False
    rebuilding = (await api_client.get("/api/capabilities")).json()
    assert rebuilding["embedding"]["available"] is False
    assert rebuilding["embedding"]["search_mode"] == "lexical"

    embeddings.ready = True
    recovered = (await api_client.get("/api/capabilities")).json()
    assert recovered["embedding"] == {
        "available": True,
        "reason_unavailable": None,
        "search_mode": "hybrid",
    }
    assert "embedding" in recovered["available_capabilities"]


@pytest.mark.asyncio
async def test_supported_contract_and_capability_requirements_succeed(
    api_client: httpx.AsyncClient,
):
    r = await api_client.get(
        "/api/capabilities",
        params=[
            ("required_contract", "rka-core/v1"),
            ("required_capability", "rest"),
            ("required_capability", "mcp"),
        ],
    )
    assert r.status_code == 200
    assert r.json()["available_capabilities"] == ["rest", "mcp"]


@pytest.mark.asyncio
async def test_unsupported_contract_is_actionable(api_client: httpx.AsyncClient):
    r = await api_client.get(
        "/api/capabilities",
        params={"required_contract": "rka-core/v99"},
    )
    assert r.status_code == 409
    body = r.json()
    assert body["error"] == "unsupported_core_contract"
    assert body["requested_contract"] == "rka-core/v99"
    assert body["supported_contracts"] == ["rka-core/v1"]
    assert body["issues"][0]["requirement"] == "rka-core/v99"
    assert "rka-core/v1" in body["hint"]


@pytest.mark.asyncio
async def test_unsupported_contract_with_satisfied_capability_keeps_contract_error(
    api_client: httpx.AsyncClient,
):
    r = await api_client.get(
        "/api/capabilities",
        params=[
            ("required_contract", "rka-core/v99"),
            ("required_capability", "rest"),
        ],
    )
    assert r.status_code == 409
    body = r.json()
    assert body["error"] == "unsupported_core_contract"
    assert [issue["requirement"] for issue in body["issues"]] == ["rka-core/v99"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("required_capability", "status", "reason_fragment"),
    [
        ("embedding", "unavailable", "disabled"),
        ("writer", "unsupported", "unknown capability"),
    ],
)
async def test_unsupported_capability_combination_is_actionable(
    api_client: httpx.AsyncClient,
    required_capability: str,
    status: str,
    reason_fragment: str,
):
    r = await api_client.get(
        "/api/capabilities",
        params={"required_capability": required_capability},
    )
    assert r.status_code == 409
    body = r.json()
    assert body["error"] == "unsupported_capability_combination"
    assert body["required_capabilities"] == [required_capability]
    assert body["issues"][0]["status"] == status
    assert reason_fragment in body["issues"][0]["reason"].lower()
