"""v2.6.4 — Navigator contract integration tests.

Pins the v2.6.4 contract changes on top of the v2.6.3 navigator
architecture (see test_v263_navigator.py for the tier-split and
capability-handshake invariants).

What this file pins:
- The MCP instructions string carries the navigator contract (so any
  client reading `initialize` learns about the dynamic surface).
- The Brain + Executor orientation prompts mention the navigator (so
  clients without Skills support still get the v2.6.3+ session-start
  protocol).
- The 3 navigator tools (`rka_load_tools`, `rka_list_tools`,
  `rka_help`) are always-on and self-documented.
- Mixed-load semantics: within a SINGLE batch, the first occurrence of
  a duplicate name loads and the second is `already_active`. This is
  the subtlety the workflow review flagged — pinning the current
  implementation behaviour.
- `rka_help` returns the correct tier / registered status for both
  always-on and unregistered-deferred tools.
- End-to-end: `rka_load_tools` followed by listing the FastMCP tool
  manager exposes the loaded tool with the expected schema shape
  (no `project_id` requirement for the unscoped `rka_list_projects`).

Pattern after tests/test_mcp/test_v263_navigator.py — same
fixtures (FakeSession, FakeCtx, _reset_deferred_registrations
autouse). Keep additions to that file in mind: future tier-shape
changes should land there; this file is for v2.6.4-specific
contract pins.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import TypeAdapter

from rka.mcp.operation_args import ExecuteArgsUnion, QueryArgsUnion

import rka.mcp.server as _live_server
from rka.mcp.server import (_TIER_ALWAYS_ON, _TIER_DEFERRED, RKA_INSTRUCTIONS, brain_orientation, executor_orientation)


# ---------------------------------------------------------------------------
# Fixtures (mirror test_v263_navigator.py)
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
    """Restore the module-import-baseline registration state after each test.

    Same mechanism as in test_v263_navigator.py — without this the order of
    test execution would silently affect later tests that read the always-on
    surface.
    """
    baseline_registered = {
        name: rec["registered"] for name, rec in _live_server._TOOL_REGISTRY.items()
    }
    yield
    for name, was_registered in baseline_registered.items():
        rec = _live_server._TOOL_REGISTRY.get(name)
        if rec is None:
            continue
        if rec["registered"] and not was_registered:
            try:
                _live_server.mcp._tool_manager.remove_tool(name)
            except Exception:
                pass
            rec["registered"] = False


async def _call(_tool: str, **kwargs: Any) -> dict:
    """Test-side dispatch helper — same shape as test_v263_navigator._call."""
    fn = _live_server._TOOL_REGISTRY[_tool]["fn"]
    raw = await fn(**kwargs)
    return json.loads(raw)


# ---------------------------------------------------------------------------
# Test 1+2: RKA_INSTRUCTIONS carries the navigator surface contract
# ---------------------------------------------------------------------------


def test_rka_instructions_mentions_navigator():
    """The MCP `initialize` handshake's `instructions` string must teach
    clients about the navigator — without this, a fresh client only
    sees the always-on tools and has no signal that more exist.
    """
    instructions = (_live_server.mcp.instructions or "").lower()
    assert (
        "navigator" in instructions or "rka_load_tools" in instructions
    ), (
        "RKA_INSTRUCTIONS must mention `navigator` or `rka_load_tools` "
        "so clients reading the initialize handshake learn how to "
        "discover the deferred surface"
    )


def test_rka_instructions_does_not_direct_clients_to_call_set_project_at_session_start():
    """`rka_set_project` is a deprecated no-op since v2.6 — the session
    start protocol must NOT direct clients to call it. The replacement
    is per-call `project_id` kwargs; we assert the legacy directive
    "call `rka_set_project(id)` first" is gone.
    """
    text = RKA_INSTRUCTIONS
    # The pre-v2.6 instructions had a Multi-Project section that said
    # "call `rka_list_projects()` and `rka_set_project(id)` first" —
    # both as a session-start directive. v2.6+ replaces this with the
    # Project Scoping section. Pin BOTH the literal old phrase AND any
    # surviving "call rka_set_project ... first" pattern is gone.
    lowered = text.lower()
    assert "rka_set_project(id) first" not in text
    # Defensive: any phrase that prescribes calling rka_set_project as
    # a first / session-start / initialization step would defeat the
    # v2.6 contract. The current instructions only mention
    # `rka_set_project` to mark it deprecated.
    forbidden_phrases = [
        "rka_set_project first",
        "rka_set_project at session start",
        "rka_set_project on session start",
        "call rka_set_project to set",
    ]
    for phrase in forbidden_phrases:
        assert phrase.lower() not in lowered, (
            f"RKA_INSTRUCTIONS still directs clients to call "
            f"rka_set_project at session start ({phrase!r}); this is "
            f"the deprecated v2.5 contract"
        )


def test_orientation_prompts_use_current_typed_provenance_and_checkpoint_shapes():
    """Fallback prompts must not teach legacy names inside typed dispatch."""
    instruction_text = RKA_INSTRUCTIONS
    brain_text = brain_orientation()
    executor_text = executor_orientation()

    for text in (instruction_text, brain_text, executor_text):
        assert '"operation": "trace_provenance"' not in text

    assert '"operation": "provenance"' in instruction_text
    assert '"filters": {"status": "open"}' in instruction_text
    assert '"filters": {"status": "open"}' in brain_text


def test_executor_orientation_examples_match_current_typed_contracts():
    """The fallback must not strand clients on stale field or operation names."""
    text = executor_orientation()

    for stale in (
        "68 read ops",
        "84 write/lifecycle ops",
        '"operation": "get"',
        '"operation": "add_literature"',
        '"findings": "..."',
        '"title": "...", "description": "..."',
        '"operation": "submit_report", ..., "related_decisions"',
    ):
        assert stale not in text

    expected_fragments = (
        '"operation": "update_mission_status", "project_id": "prj_...", '
        '"mission_id": "mis_..."',
        '"operation": "entity", "project_id": "prj_...", "id": "dec_..."',
        '"provenance": {"related_mission": "mis_..."}',
        '"mission_id": "mis_...", "type": "clarification"',
        '"findings": ["..."]',
        '"operation": "record_literature", "project_id": "prj_..."',
    )
    for fragment in expected_fragments:
        assert fragment in text

    # Pin the examples to the same discriminated unions the live tools expose.
    query_adapter = TypeAdapter(QueryArgsUnion)
    execute_adapter = TypeAdapter(ExecuteArgsUnion)
    query_adapter.validate_python(
        {
            "operation": "entity",
            "project_id": "prj_test",
            "id": "dec_test",
        }
    )
    execute_adapter.validate_python(
        {
            "operation": "update_mission_status",
            "project_id": "prj_test",
            "mission_id": "mis_test",
            "status": "active",
        }
    )
    execute_adapter.validate_python(
        {
            "operation": "record_note",
            "project_id": "prj_test",
            "content": "finding",
            "type": "note",
            "source": "executor",
            "provenance": {"related_mission": "mis_test"},
        }
    )
    execute_adapter.validate_python(
        {
            "operation": "submit_checkpoint",
            "project_id": "prj_test",
            "mission_id": "mis_test",
            "type": "clarification",
            "description": "Need input.",
            "blocking": True,
        }
    )
    execute_adapter.validate_python(
        {
            "operation": "submit_report",
            "project_id": "prj_test",
            "mission_id": "mis_test",
            "summary": "done",
            "findings": ["finding"],
        }
    )
    execute_adapter.validate_python(
        {
            "operation": "record_literature",
            "project_id": "prj_test",
            "title": "Paper",
        }
    )


# ---------------------------------------------------------------------------
# Test 3+4: brain_orientation + executor_orientation mention the navigator
# ---------------------------------------------------------------------------


def test_brain_orientation_mentions_navigator():
    """The Brain orientation prompt (fallback for clients without Skills
    support) must teach Brain about the navigator — otherwise Brain
    starts blind to 80+ of its tools."""
    text = brain_orientation().lower()
    assert "rka_load_tools" in text or "navigator" in text, (
        "brain_orientation must mention `rka_load_tools` or `navigator` "
        "so the Brain prompt teaches the v2.6.3 surface model"
    )


def test_executor_orientation_mentions_navigator():
    """The Executor orientation prompt (fallback for clients without
    Skills support) must teach Executor about the navigator —
    otherwise Executor starts blind to 80+ of its tools."""
    text = executor_orientation().lower()
    assert "rka_load_tools" in text or "navigator" in text, (
        "executor_orientation must mention `rka_load_tools` or "
        "`navigator` so the Executor prompt teaches the v2.6.3 "
        "surface model"
    )


# ---------------------------------------------------------------------------
# Test 5: Navigator tools are always-on and self-documented
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name", ["rka_load_tools", "rka_list_tools", "rka_help"]
)
def test_navigator_tools_always_on_with_summary(name):
    """Each of the 3 navigator tools must (a) live in the always-on
    tier so clients see them at startup, (b) be category=navigator so
    `rka_list_tools(category='navigator')` surfaces all three, and
    (c) have a non-empty docstring summary so `rka_help(name=...)`
    and `rka_list_tools` aren't empty.
    """
    rec = _live_server._TOOL_REGISTRY[name]
    assert rec["tier"] == _TIER_ALWAYS_ON, (
        f"{name} must be always-on so clients can discover and load "
        f"the rest of the catalog"
    )
    assert rec["category"] == "navigator", (
        f"{name} must be category=navigator so rka_list_tools surfaces "
        f"all three under one bucket"
    )
    assert rec["summary"], f"{name} has no docstring summary"
    assert rec["registered"] is True, (
        f"{name} must be registered at startup (always-on tier "
        f"contract)"
    )


# ---------------------------------------------------------------------------
# Test 6: End-to-end — rka_load_tools followed by tool call works
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_load_then_list_projects_round_trip(ctx):
    """End-to-end navigator flow:
    1. Client calls `rka_load_tools(names=["rka_list_projects"])`.
    2. Server registers the tool + fires list_changed.
    3. The FastMCP tool manager now lists `rka_list_projects`.
    4. The tool's parameters dict does NOT require `project_id` (it's
       an unscoped discovery tool, by v2.6 contract).
    """
    result = await _call(
        "rka_load_tools", names=["rka_list_projects"], ctx=ctx
    )
    assert result["loaded"] == ["rka_list_projects"]
    assert result["already_active"] == []
    assert result["unknown"] == []
    assert ctx.session.notifications == 1

    # FastMCP tool manager now exposes rka_list_projects
    mgr_tools = {t.name: t for t in _live_server.mcp._tool_manager.list_tools()}
    assert "rka_list_projects" in mgr_tools

    # Schema shape — rka_list_projects is one of the few unscoped tools
    # (no project_id required), used for discovery. Pin that the
    # deferred-then-loaded path renders the correct inputSchema.
    params = mgr_tools["rka_list_projects"].parameters
    properties = params.get("properties", {})
    required = params.get("required", []) or []
    assert "project_id" not in properties, (
        "rka_list_projects must remain unscoped — the dispatcher uses "
        "it to discover available project_ids BEFORE any project is "
        "pinned"
    )
    assert "project_id" not in required


# ---------------------------------------------------------------------------
# Test 7: rka_list_tools(tier="deferred") is non-empty and contains expected
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_tools_deferred_tier_contains_expected():
    """Sanity-pin the deferred surface — the inverse of the always-on
    sanity check in test_v263_navigator.test_always_on_tier_membership.
    """
    result = await _call("rka_list_tools", tier="deferred")
    flat = [
        t["name"]
        for tools in result["categories"].values()
        for t in tools
    ]
    # Every result must be tier=deferred
    for tools in result["categories"].values():
        for t in tools:
            assert t["tier"] == _TIER_DEFERRED

    # Spot-check the most-common deferred writes — if any of these
    # ever drift to always-on, the always-on tier membership test in
    # test_v263_navigator will surface it too, but pinning here
    # double-locks the regression surface.
    assert "rka_add_decision" in flat
    assert "rka_create_mission" in flat
    assert "rka_add_literature" in flat
    # And the non-emptiness floor — deferred layer should be the bulk
    # of the surface.
    assert len(flat) >= 70, (
        f"deferred tier has only {len(flat)} tools — expected at "
        f"least 70; tier-split may have regressed"
    )


# ---------------------------------------------------------------------------
# Test 8: Duplicate-in-batch semantic
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_load_tools_duplicate_in_same_batch_first_loads_rest_already_active(ctx):
    """The workflow review flagged the duplicate-in-batch case:
    `rka_load_tools(names=["X", "X"])` — what's the contract?

    Current implementation (server.py rka_load_tools, ~line 5218-5236):
    iterate in order, register-or-skip, mutate `_TOOL_REGISTRY[name]
    ['registered']` between iterations. So the SECOND occurrence sees
    `registered=True` (set by the first) and routes to `already_active`.

    Pin this semantic — it's the most sensible behaviour (no
    register-twice, no notification storm) and matches what the
    workflow review converged on. If the contract ever needs to
    change (e.g. report duplicates as a separate `duplicates` bucket),
    update this test alongside the implementation so the new contract
    is explicit.
    """
    result = await _call(
        "rka_load_tools",
        names=["rka_add_literature", "rka_add_literature"],
        ctx=ctx,
    )
    assert result["loaded"] == ["rka_add_literature"], (
        f"first occurrence of duplicate should be in `loaded`; got "
        f"loaded={result['loaded']!r}"
    )
    assert result["already_active"] == ["rka_add_literature"], (
        f"second (duplicate) occurrence should be in `already_active`; "
        f"got already_active={result['already_active']!r}"
    )
    assert result["unknown"] == []
    # ONE notification — the first occurrence triggered registration,
    # the second was a no-op. Pinning this matters because the
    # tool-list-changed notification is expensive at the client side
    # (forces a full tools/list refetch).
    assert ctx.session.notifications == 1


# ---------------------------------------------------------------------------
# Test 9+10: rka_help reports correct tier + registered for both layers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_help_on_always_on_tool_reports_always_on_and_registered():
    """`rka_help` on an always-on tool reports tier=always_on +
    registered=True. This is the simple half of the navigator's
    help-introspection contract.
    """
    result = await _call("rka_help", name="rka_add_note")
    assert result["name"] == "rka_add_note"
    assert result["tier"] == _TIER_ALWAYS_ON
    assert result["registered"] is True
    assert result["summary"]  # non-empty
    assert result["docstring"]  # full prose


@pytest.mark.asyncio
async def test_help_on_unloaded_deferred_tool_reports_deferred_and_unregistered():
    """`rka_help` on a deferred tool that has NOT been loaded yet
    reports tier=deferred + registered=False. The PI / Brain / Executor
    can introspect a tool's signature BEFORE deciding to load it —
    this is the navigator's discovery affordance.

    Note: the autouse `_reset_deferred_registrations` fixture restores
    the baseline after each test, so even if another test in the same
    run loaded `rka_create_mission`, this test sees it as unregistered
    again.
    """
    # Sanity: confirm the tool we're about to inspect is in fact
    # deferred and unregistered at the start of THIS test.
    assert _live_server._TOOL_REGISTRY["rka_create_mission"]["tier"] == _TIER_DEFERRED
    assert _live_server._TOOL_REGISTRY["rka_create_mission"]["registered"] is False

    result = await _call("rka_help", name="rka_create_mission")
    assert result["name"] == "rka_create_mission"
    assert result["tier"] == _TIER_DEFERRED
    assert result["registered"] is False
    assert result["summary"]
    assert result["docstring"]
