"""Real MCP adapters and REST policy share one in-process synthetic database."""

from __future__ import annotations

import json

import httpx
import pytest
import pytest_asyncio
from pydantic import TypeAdapter

from rka.api.app import create_app
from rka.config import RKAConfig
from rka.mcp import server, verb_dispatch
from rka.mcp.operation_args import ExecuteArgsUnion


@pytest_asyncio.fixture
async def boundary_client(db, tmp_path, monkeypatch):
    app = create_app(
        RKAConfig(
            project_dir=tmp_path,
            data_dir=tmp_path / "data",
            db_path=tmp_path / "unused.db",
            embeddings_enabled=False,
            llm_enabled=False,
        )
    )
    app.state.db = db

    def client(project_id=None):
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://isolated.test",
            headers={"X-RKA-Project": project_id or "proj_default"},
        )

    monkeypatch.setattr(server, "_client", client)
    async with client() as api:
        yield api


@pytest.mark.asyncio
@pytest.mark.parametrize("typed", [True, False])
@pytest.mark.parametrize("handler_type", ["sql", "mcp_tool"])
async def test_mcp_cannot_bypass_registration_policy(boundary_client, handler_type, typed):
    payload = {
        "project_id": "proj_default",
        "event": "periodic",
        "handler_type": handler_type,
        "handler_config": {"statement": "DELETE FROM journal", "tool": "rka_add_note"},
        "name": "blocked",
        "created_by": "system",
    }
    # The legacy MCP adapter intentionally converts HTTP failures to a tool error.
    with pytest.raises(
        Exception, match=f"API error 422: Hook handler '{handler_type}' is unsupported"
    ):
        if typed:
            args = TypeAdapter(ExecuteArgsUnion).validate_python(
                {"operation": "hook_add", **payload}
            )
            await verb_dispatch.dispatch_execute_typed(args)
        else:
            await verb_dispatch._legacy("rka_add_hook")(**payload)
    assert (await boundary_client.get("/api/hooks")).json() == []


@pytest.mark.asyncio
async def test_supported_typed_hook_persists_and_fires(boundary_client):
    args = TypeAdapter(ExecuteArgsUnion).validate_python(
        {
            "operation": "hook_add",
            "project_id": "proj_default",
            "event": "periodic",
            "handler_type": "brain_notify",
            "handler_config": {"content_template": {"test": "ok"}},
            "name": "supported",
        }
    )
    result = json.loads(await verb_dispatch.dispatch_execute_typed(args))
    assert result["handler_type"] == "brain_notify"
    response = await boundary_client.post(
        "/api/hooks/fire", json={"event": "periodic", "payload": {}}
    )
    assert response.status_code == 200
    assert (await boundary_client.get("/api/notifications")).json()[0]["content"] == {"test": "ok"}


@pytest.mark.asyncio
@pytest.mark.parametrize("handler_type", ["sql", "mcp_tool"])
async def test_rest_legacy_hook_can_be_inspected_and_disabled_but_not_enabled(
    db, boundary_client, handler_type
):
    await db.execute(
        """INSERT INTO hooks
           (id, event, project_id, handler_type, handler_config, enabled, name, created_by)
           VALUES ('hk_legacy', 'periodic', 'proj_default', ?, '{}', 1, 'old', 'pi')""",
        [handler_type],
    )
    await db.commit()
    assert (await boundary_client.get("/api/hooks/hk_legacy")).json()[
        "handler_type"
    ] == handler_type
    assert (await boundary_client.put("/api/hooks/hk_legacy/disable")).json()["enabled"] is False
    response = await boundary_client.put("/api/hooks/hk_legacy/enable")
    assert response.status_code == 422
    assert response.json()["error"] == "unsupported_hook_handler"
    assert (await boundary_client.get("/api/hooks/hk_legacy")).json()["enabled"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("typed", [True, False])
@pytest.mark.parametrize("handler_type", ["sql", "mcp_tool"])
async def test_mcp_cannot_reenable_legacy_hook(db, boundary_client, handler_type, typed):
    await db.execute(
        """INSERT INTO hooks
           (id, event, project_id, handler_type, handler_config, enabled, name, created_by)
           VALUES ('hk_legacy', 'periodic', 'proj_default', ?, '{}', 0, 'old', 'pi')""",
        [handler_type],
    )
    await db.commit()
    payload = {"project_id": "proj_default", "hook_id": "hk_legacy"}
    with pytest.raises(
        Exception, match=f"API error 422: Hook handler '{handler_type}' is unsupported"
    ):
        if typed:
            args = TypeAdapter(ExecuteArgsUnion).validate_python(
                {"operation": "hook_enable", **payload}
            )
            await verb_dispatch.dispatch_execute_typed(args)
        else:
            await verb_dispatch._legacy("rka_enable_hook")(**payload)
    assert (await boundary_client.get("/api/hooks/hk_legacy")).json()["enabled"] is False
