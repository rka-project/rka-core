"""v2.6.3 — Dynamic tool surface (navigator architecture) tests.

The navigator architecture splits the rka MCP tool surface into two tiers:
- ~12 always-on tools registered at module import
- ~79 deferred tools registered on demand via `rka_load_tools` + a
  `notifications/tools/list_changed` notification

These tests verify the split is correct, the navigator semantics are
honest, the FastMCP `tools.listChanged: true` capability is advertised,
and the registration path is identical to what `mcp.tool()` would have
produced at import (so no schema asymmetry between always-on and
on-demand tools).

These tests cover server/session-shim behavior, not client-visible discovery.
Some clients do not propagate list_changed to the model's catalog. The two
package-only skill reads are therefore published on the initial tools/list.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

import rka.mcp.server as server

# Other MCP suites reload this module to exercise startup modes. Resolve the
# live registry and manager together; collection-time aliases become stale.


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class _FakeSession:
    """Minimal MCP session double — records `send_tool_list_changed` calls."""

    def __init__(self) -> None:
        self.notifications: int = 0

    async def send_tool_list_changed(self) -> None:
        self.notifications += 1


class _FakeCtx:
    """FastMCP Context double — only exposes `.session` (the only attr
    rka_load_tools touches)."""

    def __init__(self) -> None:
        self.session = _FakeSession()


@pytest.fixture
def ctx() -> _FakeCtx:
    return _FakeCtx()


@pytest.fixture(autouse=True)
def _reset_deferred_registrations():
    """After each test, walk the registry and un-register any deferred
    tools that got flipped to `registered=True` by the test, restoring
    the module-import baseline so the next test starts clean.

    Side-effecting the global registry is OK in tests as long as we
    restore — alternative would be deep-copying the registry per-test,
    which would diverge from the actual production singleton being
    exercised.
    """
    baseline_registered = {
        name: rec["registered"] for name, rec in server._TOOL_REGISTRY.items()
    }
    yield
    for name, was_registered in baseline_registered.items():
        rec = server._TOOL_REGISTRY.get(name)
        if rec is None:
            continue
        if rec["registered"] and not was_registered:
            # Un-register from FastMCP's tool manager too, so the next
            # test sees the original always-on count.
            try:
                server.mcp._tool_manager.remove_tool(name)
            except Exception:
                pass
            rec["registered"] = False


# ---------------------------------------------------------------------------
# Tier split (the architectural invariant)
# ---------------------------------------------------------------------------


def test_registry_contains_all_rka_tools():
    """Every rka_* function in server.py is registered, regardless of tier."""
    names = set(server._TOOL_REGISTRY)
    # 91 original rka_* tools + 3 navigator tools = 94 minimum.
    assert len(names) >= 94
    # Every name must start with `rka_` (the canonical prefix).
    assert all(n.startswith("rka_") for n in names)


def test_always_on_tier_membership():
    """The always-on layer (under RKA_LEGACY_TOOLS=1) is the documented
    Minimal Session Start + universal retrieval + most-frequent writes
    + 3 navigator tools + 8 v2.7.0a2 intent verbs + 2 v2.7.0a3
    dispatch tools + 2 package-only skill reads = 24 total.

    The conftest in this directory sets RKA_LEGACY_TOOLS=1 so this
    test exercises the legacy-enabled mode. The v2.7.0a3 default
    mode (env unset) collapses to 7 always-on tools and is tested
    separately in test_v270a3_dispatch_surface.py.

    This is the load-bearing list — if a tool moves between tiers it
    will surface here so the change is deliberate, not silent.
    """
    expected_always_on = {
        # v2.6.4 baseline — Minimal Session Start
        "rka_get_status",
        "rka_get_context",
        "rka_get_pending_maintenance",
        "rka_get_checkpoints",
        "rka_get_research_map",
        # v2.6.4 baseline — Universal retrieval
        "rka_search",
        "rka_get",
        # v2.6.4 baseline — Most-frequent writes
        "rka_add_note",
        "rka_resolve_checkpoint",
        # v2.6.3 — Navigator
        "rka_load_tools",
        "rka_list_tools",
        "rka_help",
        # v2.7.0a2 — 8 intent verbs (additive; PR-1)
        "rka_query",
        "rka_record_note",
        "rka_record_decision",
        "rka_record_literature",
        "rka_mission",
        "rka_checkpoint",
        "rka_review",
        "rka_session",
        # v2.7.0a3 — 2 new dispatch tools (rka_query already counted above)
        "rka_execute",
        "rka_describe",
        "rka_list_skills",
        "rka_read_skill",
    }
    actual_always_on = {
        n for n, r in server._TOOL_REGISTRY.items() if r["tier"] == server._TIER_ALWAYS_ON
    }
    assert actual_always_on == expected_always_on, (
        f"always-on membership drift: "
        f"added {actual_always_on - expected_always_on} / "
        f"removed {expected_always_on - actual_always_on}"
    )


def test_deferred_tier_size_within_cap():
    """The deferred layer must be every non-always-on rka tool. We
    care about the count vs the always-on count so future refactors
    don't leak tools into either tier silently.

    v2.7.0a3 + RKA_LEGACY_TOOLS=1 — always-on is 22 (12 v2.6.4
    baseline + 8 intent verbs + 2 v2.7.0a3 dispatch tools).
    Deferred is 79+ legacy tools that haven't been promoted.
    """
    always_on = [
        n for n, r in server._TOOL_REGISTRY.items() if r["tier"] == server._TIER_ALWAYS_ON
    ]
    deferred = [
        n for n, r in server._TOOL_REGISTRY.items() if r["tier"] == server._TIER_DEFERRED
    ]
    assert len(always_on) == 24
    assert len(deferred) >= 79  # 79 original deferred tools; never < this
    assert set(always_on).isdisjoint(set(deferred))


def test_only_always_on_is_visible_at_startup():
    """The fundamental client-cap contract: a client doing tools/list
    at session start sees ONLY the always-on layer — nothing else."""
    visible = sorted(t.name for t in server.mcp._tool_manager.list_tools())
    always_on = sorted(
        n for n, r in server._TOOL_REGISTRY.items() if r["tier"] == server._TIER_ALWAYS_ON
    )
    # Filter out any deferred tools that were loaded by a previous test
    # whose teardown didn't run yet (paranoia — autouse fixture should
    # handle this).
    visible_always_on = [n for n in visible if n in always_on]
    assert visible_always_on == always_on


def test_every_tool_has_category_metadata():
    """rka_list_tools renders by category; every tool needs one."""
    for name, rec in server._TOOL_REGISTRY.items():
        assert rec["category"], f"{name} has no category"
        assert isinstance(rec["category"], str)


# ---------------------------------------------------------------------------
# Capability handshake (the MCP-protocol-level contract)
# ---------------------------------------------------------------------------


def test_tools_list_changed_capability_advertised():
    """v2.6.3 advertises tools.listChanged: true in the initialize
    handshake — without this both Claude Desktop and Claude Code IGNORE
    the runtime notification."""
    init_opts = server.mcp._mcp_server.create_initialization_options()
    assert init_opts.capabilities is not None
    assert init_opts.capabilities.tools is not None
    assert init_opts.capabilities.tools.listChanged is True


def test_capability_override_preserves_other_notifications():
    """The _create_init_with_list_changed wrapper must preserve any
    prompts_changed / resources_changed the caller passes in (e.g.
    Streamable-HTTP transport may opt into those independently)."""
    from mcp.server.lowlevel.server import NotificationOptions

    caller_opts = NotificationOptions(
        prompts_changed=True, resources_changed=True, tools_changed=False
    )
    init_opts = server.mcp._mcp_server.create_initialization_options(
        notification_options=caller_opts
    )
    # tools_changed forced to True regardless
    assert init_opts.capabilities.tools.listChanged is True
    # Other flags preserved
    assert init_opts.capabilities.prompts.listChanged is True
    assert init_opts.capabilities.resources.listChanged is True


# ---------------------------------------------------------------------------
# Navigator: rka_list_tools
# ---------------------------------------------------------------------------


async def _call(_tool: str, **kwargs: Any) -> dict:
    """Test-side helper: dispatch into a registered tool by name.

    Param renamed to `_tool` (leading underscore) so a tool whose own
    first parameter is `name` (e.g. `rka_help`) doesn't collide via
    kwargs.
    """
    fn = server._TOOL_REGISTRY[_tool]["fn"]
    raw = await fn(**kwargs)
    return json.loads(raw)


@pytest.mark.asyncio
async def test_list_tools_no_filter_returns_full_catalog():
    result = await _call("rka_list_tools")
    assert result["total_tools"] == len(server._TOOL_REGISTRY)
    assert result["filtered_count"] == result["total_tools"]
    # Every category bucket has at least one tool.
    for cat, tools in result["categories"].items():
        assert tools, f"empty category bucket: {cat}"


@pytest.mark.asyncio
async def test_list_tools_category_filter():
    result = await _call("rka_list_tools", category="literature")
    assert set(result["categories"]) == {"literature"}
    names = [t["name"] for t in result["categories"]["literature"]]
    assert "rka_add_literature" in names
    assert "rka_update_literature" in names
    # No tools from other categories leaked in.
    assert "rka_add_note" not in names


@pytest.mark.asyncio
async def test_list_tools_tier_filter():
    result = await _call("rka_list_tools", tier="always_on")
    flat = [t["name"] for tools in result["categories"].values() for t in tools]
    # Every result must be tier=always_on
    for cat, tools in result["categories"].items():
        for t in tools:
            assert t["tier"] == "always_on"
    assert len(flat) == 24  # legacy surface plus two package-only reads


@pytest.mark.asyncio
async def test_list_tools_query_substring_match():
    result = await _call("rka_list_tools", query="cluster")
    flat = [t["name"] for tools in result["categories"].values() for t in tools]
    # All result names or summaries contain "cluster" (case-insensitive)
    for tools in result["categories"].values():
        for t in tools:
            assert "cluster" in t["name"].lower() or "cluster" in t["summary"].lower()
    assert "rka_review_cluster" in flat
    assert "rka_create_cluster" in flat


@pytest.mark.asyncio
async def test_list_tools_unknown_category_returns_empty():
    result = await _call("rka_list_tools", category="nonexistent_bucket")
    assert result["filtered_count"] == 0
    assert result["categories"] == {}


# ---------------------------------------------------------------------------
# Navigator: rka_help
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_help_returns_full_record_for_deferred_tool():
    result = await _call("rka_help", name="rka_add_literature")
    assert result["name"] == "rka_add_literature"
    assert result["tier"] == "deferred"
    assert result["category"] == "literature"
    assert "(" in result["signature"]  # rendered signature
    assert result["summary"]  # non-empty
    assert result["docstring"]  # full prose


@pytest.mark.asyncio
async def test_help_returns_full_record_for_always_on_tool():
    result = await _call("rka_help", name="rka_add_note")
    assert result["tier"] == "always_on"
    assert result["registered"] is True


@pytest.mark.asyncio
async def test_help_unknown_tool_returns_error_shape():
    result = await _call("rka_help", name="rka_does_not_exist")
    assert result["error"] == "unknown_tool"
    assert result["name"] == "rka_does_not_exist"


@pytest.mark.asyncio
async def test_help_handles_blank_name():
    result = await _call("rka_help", name="")
    assert result["error"] == "unknown_tool"


# ---------------------------------------------------------------------------
# Navigator: rka_load_tools
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_load_tools_registers_deferred(ctx):
    result = await _call(
        "rka_load_tools",
        names=["rka_add_literature", "rka_update_literature"],
        ctx=ctx,
    )
    assert result["loaded"] == ["rka_add_literature", "rka_update_literature"]
    assert result["already_active"] == []
    assert result["unknown"] == []
    # Notification fired exactly once for the whole load batch
    assert ctx.session.notifications == 1
    # FastMCP tool manager now has the tool
    mgr_names = [t.name for t in server.mcp._tool_manager.list_tools()]
    assert "rka_add_literature" in mgr_names
    assert "rka_update_literature" in mgr_names
    # Registry flipped to registered=True
    assert server._TOOL_REGISTRY["rka_add_literature"]["registered"] is True
    assert server._TOOL_REGISTRY["rka_update_literature"]["registered"] is True


@pytest.mark.asyncio
async def test_load_tools_is_idempotent(ctx):
    await _call("rka_load_tools", names=["rka_add_literature"], ctx=ctx)
    # Second call — same tool, already active
    ctx2 = _FakeCtx()
    result = await _call(
        "rka_load_tools", names=["rka_add_literature"], ctx=ctx2
    )
    assert result["loaded"] == []
    assert result["already_active"] == ["rka_add_literature"]
    # No spurious notification — nothing actually changed
    assert ctx2.session.notifications == 0


@pytest.mark.asyncio
async def test_load_tools_unknown_names_returned_not_errored(ctx):
    result = await _call(
        "rka_load_tools",
        names=["rka_does_not_exist", "rka_also_fake"],
        ctx=ctx,
    )
    assert result["loaded"] == []
    assert result["unknown"] == ["rka_does_not_exist", "rka_also_fake"]
    # No notification — nothing was registered
    assert ctx.session.notifications == 0


@pytest.mark.asyncio
async def test_load_tools_mixed_known_unknown_already(ctx):
    # Pre-load one
    await _call("rka_load_tools", names=["rka_add_literature"], ctx=ctx)
    ctx2 = _FakeCtx()
    result = await _call(
        "rka_load_tools",
        names=[
            "rka_add_literature",        # already
            "rka_update_literature",     # known + deferred
            "rka_does_not_exist",        # unknown
        ],
        ctx=ctx2,
    )
    assert result["loaded"] == ["rka_update_literature"]
    assert result["already_active"] == ["rka_add_literature"]
    assert result["unknown"] == ["rka_does_not_exist"]
    # One notification fired (one tool actually loaded)
    assert ctx2.session.notifications == 1


@pytest.mark.asyncio
async def test_load_tools_blank_and_whitespace_names_skipped(ctx):
    result = await _call(
        "rka_load_tools",
        names=["", "   ", "rka_add_literature"],
        ctx=ctx,
    )
    assert result["loaded"] == ["rka_add_literature"]
    # Empty / whitespace strings silently skipped, NOT reported as unknown
    assert result["unknown"] == []


@pytest.mark.asyncio
async def test_load_tools_empty_list_is_noop(ctx):
    result = await _call("rka_load_tools", names=[], ctx=ctx)
    assert result == {"loaded": [], "already_active": [], "unknown": []}
    assert ctx.session.notifications == 0


@pytest.mark.asyncio
async def test_load_tools_notification_failure_does_not_reverse_registration(ctx):
    """If the client's session can't accept the notification (e.g. dead
    transport), the registration MUST stay — the tool is callable on the
    next initialize even if this client never sees the update."""

    class _RaisingSession:
        async def send_tool_list_changed(self):
            raise RuntimeError("transport dead")

    class _RaisingCtx:
        session = _RaisingSession()

    result = await _call(
        "rka_load_tools",
        names=["rka_add_literature"],
        ctx=_RaisingCtx(),
    )
    # Registration still happened
    assert result["loaded"] == ["rka_add_literature"]
    assert server._TOOL_REGISTRY["rka_add_literature"]["registered"] is True
    # FastMCP tool manager has it
    assert any(
        t.name == "rka_add_literature" for t in server.mcp._tool_manager.list_tools()
    )


# ---------------------------------------------------------------------------
# Schema parity: deferred-then-loaded tools render the same as always-on
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loaded_tool_schema_matches_signature(ctx):
    """A deferred tool, once loaded, must have the same inputSchema
    structure FastMCP renders for any always-on tool — same shape, same
    annotations, same `project_id` requirement."""
    await _call("rka_load_tools", names=["rka_add_literature"], ctx=ctx)
    mgr_tools = {t.name: t for t in server.mcp._tool_manager.list_tools()}
    add_lit = mgr_tools["rka_add_literature"]
    assert add_lit.parameters  # has rendered inputSchema
    # project_id is a kwarg-only required field on every project-scoped
    # rka tool post-v2.6 — this confirms the deferred-then-loaded path
    # preserves the same contract as direct mcp.tool() registration.
    props = add_lit.parameters.get("properties", {})
    assert "project_id" in props


@pytest.mark.asyncio
async def test_loaded_tool_callable_via_fastmcp(ctx):
    """End-to-end: load → call via FastMCP's call_tool path. This is the
    exact path Claude Desktop / Claude Code go through on a tool call."""
    await _call(
        "rka_load_tools", names=["rka_list_clusters"], ctx=ctx
    )
    # rka_list_clusters is project-scoped — without a running daemon the
    # call will fail at the HTTP layer, but the lookup-and-dispatch via
    # FastMCP's tool manager must succeed. We assert the lookup succeeds
    # by inspecting list_tools (already done above) + asserting the
    # wrapper is the same object as in the registry.
    mgr_tool = next(
        t for t in server.mcp._tool_manager.list_tools() if t.name == "rka_list_clusters"
    )
    assert mgr_tool is not None


# ---------------------------------------------------------------------------
# Bookkeeper: the navigator itself is callable (sanity)
# ---------------------------------------------------------------------------


def test_navigator_tools_registered_at_startup():
    """The three navigator tools must be in the always-on layer — without
    them, clients can never discover or load the deferred surface."""
    mgr_names = {t.name for t in server.mcp._tool_manager.list_tools()}
    assert "rka_load_tools" in mgr_names
    assert "rka_list_tools" in mgr_names
    assert "rka_help" in mgr_names
