"""The MCP journal read forwards source and tags to REST (rka-core#170).

``rka_describe("journal")`` has advertised ``filters.source`` and
``filters.tags`` since the typed surface shipped, but the typed dispatch
dropped both before the HTTP call and the legacy tool had no parameter for
them. A caller got the unfiltered listing with no error. These tests capture
the outbound request and assert the filters reach ``GET /api/notes`` through
both entry points, and that omitting them sends nothing new.
"""

from __future__ import annotations

import httpx
import pytest

from rka.mcp import server
from rka.mcp.verb_dispatch import dispatch_query

PROJECT = "prj_filters"


def _capture(monkeypatch) -> list[httpx.Request]:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=[])

    monkeypatch.setattr(
        server, "_client",
        lambda project_id=None: httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://testserver",
            headers={"X-RKA-Project": project_id} if project_id else {},
        ),
    )
    return requests


def _notes_request(requests: list[httpx.Request]) -> httpx.Request:
    """The one GET /api/notes among whatever side requests the adapter also makes."""
    [request] = [r for r in requests if r.url.path == "/api/notes"]
    return request


@pytest.mark.asyncio
async def test_legacy_tool_forwards_source_and_repeated_tags(monkeypatch):
    requests = _capture(monkeypatch)
    await server.rka_get_journal(project_id=PROJECT, source="pi", tags=["rwf-stop-pivot", "rwf-v0"])
    request = _notes_request(requests)
    assert request.url.params["source"] == "pi"
    assert request.url.params.get_list("tags") == ["rwf-stop-pivot", "rwf-v0"]


@pytest.mark.asyncio
async def test_typed_journal_dispatch_forwards_filters(monkeypatch):
    requests = _capture(monkeypatch)
    await dispatch_query(
        "journal", project_id=PROJECT,
        filters={"source": "executor", "tags": ["rwf-hypothesis-test"], "type": "note"},
    )
    request = _notes_request(requests)
    assert request.url.params["source"] == "executor"
    assert request.url.params.get_list("tags") == ["rwf-hypothesis-test"]
    assert request.url.params["type"] == "note"


@pytest.mark.asyncio
async def test_omitted_filters_send_no_source_or_tags(monkeypatch):
    requests = _capture(monkeypatch)
    await dispatch_query("journal", project_id=PROJECT, filters={"confidence": "verified"})
    request = _notes_request(requests)
    assert "source" not in request.url.params
    assert "tags" not in request.url.params
    assert request.url.params["confidence"] == "verified"
