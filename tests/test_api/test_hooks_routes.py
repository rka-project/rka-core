"""Hook system REST endpoint tests (Mission 2 Phase B)."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import pytest_asyncio

from rka.api.app import create_app
from rka.config import RKAConfig


HEADERS = {"X-RKA-Project": "proj_default"}


@pytest_asyncio.fixture
async def api_client(tmp_path: Path):
    config = RKAConfig(
        project_dir=tmp_path,
        data_dir=tmp_path / "data",
        db_path=Path("hooks_routes.db"),
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


def _bn_hook(event: str = "session_start", template: dict | None = None) -> dict:
    return {
        "event": event,
        "handler_type": "brain_notify",
        "handler_config": {
            "severity": "info",
            "content_template": template or {"msg": "hello"},
        },
        "name": f"bn-{event}",
    }


@pytest.mark.asyncio
async def test_post_hook_returns_201_and_id(api_client: httpx.AsyncClient):
    r = await api_client.post("/api/hooks", json=_bn_hook(), headers=HEADERS)
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["id"].startswith("hk_")
    assert data["event"] == "session_start"
    assert data["enabled"] is True


@pytest.mark.asyncio
async def test_post_hook_invalid_event_rejected(api_client: httpx.AsyncClient):
    bad = _bn_hook()
    bad["event"] = "post_lunchtime"
    r = await api_client.post("/api/hooks", json=bad, headers=HEADERS)
    assert r.status_code == 422  # Pydantic Literal mismatch


@pytest.mark.asyncio
async def test_post_hook_invalid_handler_type_rejected(api_client: httpx.AsyncClient):
    bad = _bn_hook()
    bad["handler_type"] = "shell"  # deferred to v1.1
    r = await api_client.post("/api/hooks", json=bad, headers=HEADERS)
    assert r.status_code == 422


@pytest.mark.asyncio
@pytest.mark.parametrize("handler_type", ["sql", "mcp_tool"])
@pytest.mark.parametrize("actor", ["pi", "system", "executor"])
async def test_unsupported_handler_registration_has_structured_error(api_client, handler_type, actor):
    body = _bn_hook()
    body.update(handler_type=handler_type, created_by=actor)
    response = await api_client.post("/api/hooks", json=body, headers=HEADERS)
    assert response.status_code == 422
    assert response.json()["error"] == "unsupported_hook_handler"
    assert response.json()["handler_type"] == handler_type
    assert (await api_client.get("/api/hooks", headers=HEADERS)).json() == []


@pytest.mark.asyncio
async def test_get_hooks_list_with_filters(api_client: httpx.AsyncClient):
    await api_client.post("/api/hooks", json=_bn_hook("session_start"), headers=HEADERS)
    await api_client.post("/api/hooks", json=_bn_hook("post_journal_create"), headers=HEADERS)
    r = await api_client.get("/api/hooks?event=session_start", headers=HEADERS)
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 1
    assert rows[0]["event"] == "session_start"


@pytest.mark.asyncio
async def test_disable_then_enable_round_trip(api_client: httpx.AsyncClient):
    create = await api_client.post("/api/hooks", json=_bn_hook(), headers=HEADERS)
    hook_id = create.json()["id"]
    r1 = await api_client.put(f"/api/hooks/{hook_id}/disable", headers=HEADERS)
    assert r1.status_code == 200
    assert r1.json()["enabled"] is False
    r2 = await api_client.put(f"/api/hooks/{hook_id}/enable", headers=HEADERS)
    assert r2.status_code == 200
    assert r2.json()["enabled"] is True


@pytest.mark.asyncio
async def test_delete_hook_then_404(api_client: httpx.AsyncClient):
    create = await api_client.post("/api/hooks", json=_bn_hook(), headers=HEADERS)
    hook_id = create.json()["id"]
    r = await api_client.delete(f"/api/hooks/{hook_id}", headers=HEADERS)
    assert r.status_code == 204
    follow = await api_client.get(f"/api/hooks/{hook_id}", headers=HEADERS)
    assert follow.status_code == 404


@pytest.mark.asyncio
async def test_fire_endpoint_executes_brain_notify(api_client: httpx.AsyncClient):
    await api_client.post(
        "/api/hooks",
        json=_bn_hook("session_start", template={"reminder": "run maintenance"}),
        headers=HEADERS,
    )
    r = await api_client.post(
        "/api/hooks/fire",
        json={"event": "session_start", "payload": {}},
        headers=HEADERS,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["event"] == "session_start"
    assert len(body["fired_executions"]) == 1
    # Notification visible via REST.
    notify_resp = await api_client.get("/api/notifications", headers=HEADERS)
    assert notify_resp.status_code == 200
    notes = notify_resp.json()
    assert len(notes) == 1
    assert notes[0]["content"] == {"reminder": "run maintenance"}


@pytest.mark.asyncio
async def test_executions_list_query(api_client: httpx.AsyncClient):
    await api_client.post("/api/hooks", json=_bn_hook("periodic"), headers=HEADERS)
    await api_client.post(
        "/api/hooks/fire",
        json={"event": "periodic", "payload": {}},
        headers=HEADERS,
    )
    r = await api_client.get("/api/hooks/executions/list", headers=HEADERS)
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 1
    assert rows[0]["status"] == "success"


@pytest.mark.asyncio
async def test_clear_notifications(api_client: httpx.AsyncClient):
    await api_client.post("/api/hooks", json=_bn_hook(), headers=HEADERS)
    await api_client.post(
        "/api/hooks/fire",
        json={"event": "session_start", "payload": {}},
        headers=HEADERS,
    )
    notes = (await api_client.get("/api/notifications", headers=HEADERS)).json()
    bnt_ids = [n["id"] for n in notes]
    r = await api_client.post(
        "/api/notifications/clear",
        json={"ids": bnt_ids},
        headers=HEADERS,
    )
    assert r.status_code == 200
    assert r.json()["cleared"] == len(bnt_ids)
    # Default list excludes cleared.
    after = (await api_client.get("/api/notifications", headers=HEADERS)).json()
    assert after == []
    # include_cleared brings them back.
    with_cleared = (
        await api_client.get(
            "/api/notifications?include_cleared=true", headers=HEADERS,
        )
    ).json()
    assert len(with_cleared) == len(bnt_ids)
    assert all(n["cleared_at"] is not None for n in with_cleared)
