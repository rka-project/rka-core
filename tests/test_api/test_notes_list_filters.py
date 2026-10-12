"""GET /api/notes narrows by tags and source (rka-core#170).

Before this fix the route had no ``tags`` parameter at all, so a caller who
sent one got the unfiltered listing and nothing told them the filter was
ignored. These tests pin the contract: every listed tag must be present, the
match is project-scoped, ``source`` narrows on the asserted author role, and a
request without either filter behaves as before.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import pytest_asyncio

from rka.api.app import create_app
from rka.config import RKAConfig

PROJECT_ID = "proj_default"
HEADERS = {"X-RKA-Project": PROJECT_ID}


@pytest_asyncio.fixture
async def api_client(tmp_path: Path):
    config = RKAConfig(
        project_dir=tmp_path,
        db_path=Path("notes_list_filters.db"),
        llm_enabled=False,
        embeddings_enabled=False,
    )
    app = create_app(config)
    lifespan = app.router.lifespan_context(app)
    await lifespan.__aenter__()
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver", headers=HEADERS
        ) as client:
            yield client
    finally:
        await lifespan.__aexit__(None, None, None)


async def _seed(api_client: httpx.AsyncClient) -> dict[str, str]:
    """Three notes: one with tags a+b from the brain, one with tag a from the
    executor, one PI directive with no tags."""
    created: dict[str, str] = {}
    specs = {
        "both": {"content": "carries a and b", "type": "note", "source": "brain",
                 "tags": ["rwf-stop-pivot", "rwf-v0"]},
        "only_a": {"content": "carries a", "type": "note", "source": "executor",
                   "tags": ["rwf-stop-pivot"]},
        "pi_plain": {"content": "PI said so", "type": "directive", "source": "pi",
                     "verbatim_input": "PI said so", "capture_mode": "agent_restatement"},
    }
    for key, body in specs.items():
        resp = await api_client.post("/api/notes", json=body)
        assert resp.status_code == 201, resp.text
        created[key] = resp.json()["id"]
    return created


async def _ids(api_client: httpx.AsyncClient, **params) -> set[str]:
    resp = await api_client.get("/api/notes", params=params)
    assert resp.status_code == 200, resp.text
    return {row["id"] for row in resp.json()}


@pytest.mark.asyncio
async def test_single_tag_returns_every_entry_carrying_it(api_client):
    ids = await _seed(api_client)
    assert await _ids(api_client, tags=["rwf-stop-pivot"]) == {ids["both"], ids["only_a"]}


@pytest.mark.asyncio
async def test_two_tags_require_both(api_client):
    ids = await _seed(api_client)
    assert await _ids(api_client, tags=["rwf-stop-pivot", "rwf-v0"]) == {ids["both"]}


@pytest.mark.asyncio
async def test_unknown_tag_returns_nothing_rather_than_everything(api_client):
    await _seed(api_client)
    assert await _ids(api_client, tags=["no-such-tag"]) == set()


@pytest.mark.asyncio
async def test_tag_match_is_case_insensitive_like_storage(api_client):
    ids = await _seed(api_client)
    assert await _ids(api_client, tags=["RWF-V0"]) == {ids["both"]}


@pytest.mark.asyncio
async def test_source_filter_narrows_to_asserted_role(api_client):
    ids = await _seed(api_client)
    assert await _ids(api_client, source="pi") == {ids["pi_plain"]}


@pytest.mark.asyncio
async def test_tags_and_source_compose(api_client):
    ids = await _seed(api_client)
    assert await _ids(api_client, tags=["rwf-stop-pivot"], source="executor") == {ids["only_a"]}
    assert await _ids(api_client, tags=["rwf-stop-pivot"], source="pi") == set()


@pytest.mark.asyncio
async def test_no_filter_still_lists_everything(api_client):
    ids = await _seed(api_client)
    assert await _ids(api_client) == set(ids.values())
