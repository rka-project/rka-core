"""v2.7.0a3 — 3-tool dispatch surface tests (Cloudflare / Notion pattern).

Pins the load-bearing v2.7.0a3 architecture: in the default mode
(RKA_LEGACY_TOOLS env var unset / "0"), the always-on surface is
collapsed to the 3-tool dispatch surface — rka_query (reads) +
rka_execute (writes) + rka_describe (schema lookup) — plus
rka_load_tools + rka_help (the navigator escape hatch / deprecated
alias respectively, kept always-on per Decision 5).

The 91 legacy tools and the 8 v2.7.0a2 verbs are demoted to
tier='deferred' so they don't compete with the 3-tool surface for
ToolSearch ranking. They remain CALLABLE via rka_load_tools (default
mode) or globally restored via RKA_LEGACY_TOOLS=1.

These tests REIMPORT ``rka.mcp.server`` after toggling the env flag.
The flag is read at module-import time in the @tool() decorator's
``_resolve_tier`` helper, so importlib.reload is required to test the
non-conftest mode.

Note: the directory-level conftest at ``tests/test_mcp/conftest.py``
sets RKA_LEGACY_TOOLS=1 for the rest of the v2.6.x / v2.7.0a2 test
suite. These tests momentarily override it to "0" via
``importlib.reload``.
"""

from __future__ import annotations

import importlib
import json
import os

import pytest


def _reimport_server(legacy_flag: str | None):
    """Reimport rka.mcp.server with the given RKA_LEGACY_TOOLS value.

    Pass ``"0"`` (or any non-``"1"`` string) for default mode (3-tool
    surface); pass ``"1"`` for legacy-enabled mode. Pass ``None`` to
    unset the env var entirely (which reads as ``"0"`` per Decision 4
    semantics).

    Returns the freshly-reloaded ``rka.mcp.server`` module. Subsequent
    calls in the same test reuse the module — tests should always
    perform their assertions via the returned object, not via a
    top-level import that may have been pinned by a prior reload.
    """
    if legacy_flag is None:
        os.environ.pop("RKA_LEGACY_TOOLS", None)
    else:
        os.environ["RKA_LEGACY_TOOLS"] = legacy_flag
    # Reload by spec — the module's @tool() decorators re-run, the
    # FastMCP _tool_manager is replaced via the module-level mcp = FastMCP(...)
    # call.
    import rka.mcp.server as server
    importlib.reload(server)
    return server


# ---------------------------------------------------------------------------
# Fixture: ensure each test cleans up after itself so the rest of the
# session's RKA_LEGACY_TOOLS=1 contract is preserved.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _restore_legacy_env():
    """Around each test: snapshot the env var, then restore it after.
    The test_mcp conftest sets RKA_LEGACY_TOOLS=1 — restore to that on
    teardown so the rest of the suite continues working."""
    prior = os.environ.get("RKA_LEGACY_TOOLS")
    yield
    if prior is None:
        os.environ.pop("RKA_LEGACY_TOOLS", None)
    else:
        os.environ["RKA_LEGACY_TOOLS"] = prior
    # Reimport with the snapshotted value so the module's tier mapping
    # matches the rest of the session's expectations.
    import rka.mcp.server as server
    importlib.reload(server)


# ---------------------------------------------------------------------------
# Default-mode (RKA_LEGACY_TOOLS unset) — 3-tool dispatch surface
# ---------------------------------------------------------------------------


def test_default_surface_collapses_legacy_tools_to_deferred() -> None:
    """In default mode, the always-on tier is the 3 dispatch tools
    (rka_query / rka_execute / rka_describe) plus the always-on
    navigator escape hatch (rka_load_tools) and the deprecated alias
    (rka_help) per Decision 5. The 91 legacy tools + 8 v2.7.0a2 verbs
    + rka_list_tools are demoted to deferred — invisible at
    tools/list but callable via rka_load_tools.
    """
    server = _reimport_server("0")
    always_on = sorted(
        n for n, r in server._TOOL_REGISTRY.items()
        if r["tier"] == server._TIER_ALWAYS_ON
    )
    # Decision 4 + Decision 5, plus two startup package-only reads.
    expected = {
        "rka_query",
        "rka_execute",
        "rka_describe",
        "rka_load_tools",  # Decision 5 — escape hatch
        "rka_help",        # Decision 5 — deprecated alias for rka_describe
        "rka_list_skills",
        "rka_read_skill",
    }
    assert set(always_on) == expected, (
        f"v2.7.0a3 default always-on surface drift: got {always_on}; "
        f"expected {sorted(expected)}"
    )


def test_default_surface_count_is_seven() -> None:
    """The default always-on count is exactly 7 (3 dispatch + 2
    navigator + 2 package-only reads). Well below the ~30-50 quality knee and
    dramatically below the v2.7.0a2 baseline of 20."""
    server = _reimport_server("0")
    always_on = [
        n for n, r in server._TOOL_REGISTRY.items()
        if r["tier"] == server._TIER_ALWAYS_ON
    ]
    assert len(always_on) == 7, (
        f"v2.7.0a3 default always-on count = {len(always_on)}, "
        f"expected 7: {sorted(always_on)}"
    )


def test_legacy_91_tools_still_callable_in_default_mode() -> None:
    """Bookkeeper invariant: even in default mode the legacy 91 tools
    remain in the registry (just at tier='deferred'). They're callable
    via rka_load_tools."""
    server = _reimport_server("0")
    # Spot-check load-bearing legacy tools that callers (CLAUDE.md,
    # skills, orchestrator) reference directly.
    legacy_must_exist = (
        "rka_add_note", "rka_add_decision", "rka_create_mission",
        "rka_submit_checkpoint", "rka_submit_report",
        "rka_link_literature_to_zotero", "rka_ingest_document",
        "rka_get_status", "rka_get_context", "rka_get_research_map",
    )
    for name in legacy_must_exist:
        assert name in server._TOOL_REGISTRY, (
            f"legacy tool {name!r} dropped from registry in default mode; "
            f"v2.7.0a3 must keep them callable via rka_load_tools."
        )
        # And demoted to deferred tier.
        assert server._TOOL_REGISTRY[name]["tier"] == server._TIER_DEFERRED, (
            f"legacy tool {name!r} expected at tier='deferred' in default "
            f"mode; got {server._TOOL_REGISTRY[name]['tier']!r}"
        )


def test_v270a2_verbs_demoted_in_default_mode() -> None:
    """The 8 v2.7.0a2 verbs (rka_record_note, rka_record_decision,
    rka_record_literature, rka_mission, rka_checkpoint, rka_review,
    rka_session) are demoted to deferred in default mode per
    Decision 4. They remain callable via rka_load_tools but don't
    compete with the 3-tool surface for ToolSearch ranking.

    Exception: rka_query is promoted to category='dispatch' as one of
    the 3 always-on dispatch tools (NOT demoted)."""
    server = _reimport_server("0")
    demoted_v270a2_verbs = (
        "rka_record_note", "rka_record_decision", "rka_record_literature",
        "rka_mission", "rka_checkpoint", "rka_review", "rka_session",
    )
    for name in demoted_v270a2_verbs:
        rec = server._TOOL_REGISTRY[name]
        assert rec["tier"] == server._TIER_DEFERRED, (
            f"v2.7.0a2 verb {name!r} expected deferred in default mode; "
            f"got tier={rec['tier']!r}"
        )
    # rka_query stays always-on (it IS the read half of the 3-tool surface).
    assert (
        server._TOOL_REGISTRY["rka_query"]["tier"]
        == server._TIER_ALWAYS_ON
    )


# ---------------------------------------------------------------------------
# Legacy-enabled mode (RKA_LEGACY_TOOLS=1) — v2.7.0a2 baseline preserved
# ---------------------------------------------------------------------------


def test_legacy_env_flag_restores_v2_7_0a2_surface() -> None:
    """Set RKA_LEGACY_TOOLS=1 and reimport: the always-on surface
    grows back to 24 tools (the historical 22 plus two package-only
    skill reads). This is the
    power-user / migration escape hatch from Decision 4.
    """
    server = _reimport_server("1")
    always_on = sorted(
        n for n, r in server._TOOL_REGISTRY.items()
        if r["tier"] == server._TIER_ALWAYS_ON
    )
    assert len(always_on) == 24, (
        f"RKA_LEGACY_TOOLS=1 always-on count = {len(always_on)}; "
        f"expected 24 (legacy baseline plus 2 package-only reads): {always_on}"
    )
    # rka_get_status (legacy minimal-session-start tool) is back.
    assert "rka_get_status" in always_on
    # All 8 v2.7.0a2 verbs are back.
    for verb in (
        "rka_query", "rka_record_note", "rka_record_decision",
        "rka_record_literature", "rka_mission", "rka_checkpoint",
        "rka_review", "rka_session",
    ):
        assert verb in always_on, (
            f"RKA_LEGACY_TOOLS=1 missing v2.7.0a2 verb {verb!r}"
        )
    # And the new dispatch tools are also present.
    assert "rka_execute" in always_on
    assert "rka_describe" in always_on


# ---------------------------------------------------------------------------
# alwaysLoad meta hint on the 3 dispatch tools (both modes)
# ---------------------------------------------------------------------------


def test_three_tools_carry_alwaysload_meta_in_default_mode() -> None:
    """The v2.7.0a2 alwaysLoad meta hint contract carries forward to
    v2.7.0a3: the 3 dispatch tools (and the 2 retained navigator
    tools) must each register with ``_meta={"anthropic/alwaysLoad":
    True}`` so Claude Code's ToolSearch ranking is bypassed.
    """
    server = _reimport_server("0")
    mcp_tools = {t.name: t for t in server.mcp._tool_manager.list_tools()}
    expected_pinned = (
        "rka_query", "rka_execute", "rka_describe",
        "rka_load_tools", "rka_help",
    )
    for name in expected_pinned:
        t = mcp_tools.get(name)
        assert t is not None, (
            f"{name!r} not registered with FastMCP in default mode"
        )
        meta = getattr(t, "meta", None) or {}
        assert meta.get("anthropic/alwaysLoad") is True, (
            f"{name!r} missing alwaysLoad meta hint in default mode; "
            f"got: {meta!r}"
        )


def test_three_tools_carry_alwaysload_meta_in_legacy_mode() -> None:
    """Same contract in legacy-enabled mode: the 3 v2.7.0a3 dispatch
    tools carry the alwaysLoad hint regardless of env flag."""
    server = _reimport_server("1")
    mcp_tools = {t.name: t for t in server.mcp._tool_manager.list_tools()}
    for name in ("rka_query", "rka_execute", "rka_describe"):
        t = mcp_tools.get(name)
        assert t is not None, f"{name!r} not registered with FastMCP"
        meta = getattr(t, "meta", None) or {}
        assert meta.get("anthropic/alwaysLoad") is True, (
            f"{name!r} missing alwaysLoad meta hint in legacy mode"
        )


# ---------------------------------------------------------------------------
# project_id scoping contract on the 3 dispatch tools
# ---------------------------------------------------------------------------


def test_query_execute_describe_are_scoped_correctly() -> None:
    """v2.7.0 NO-COMPROMISE typed-args contract:
      - rka_query takes a single ``args: QueryArgsUnion`` parameter.
        project_id lives INSIDE each per-operation Pydantic model
        (required for scoped reads, absent for unscoped list_projects /
        health).
      - rka_execute takes a single ``args: ExecuteArgsUnion`` parameter.
        project_id lives INSIDE each per-operation model (required for
        scoped writes, absent for unscoped create_project / reset_session).
      - rka_describe is UNSCOPED — no project_id at all (schema lookup).

    The v2.7.0a3 ``operation/project_id/**kw`` shape was deliberately
    collapsed in v2.7.0 Phase 3 — the discriminated union renders the
    full per-branch enum + required-fields surface to the LLM via
    FastMCP's JSON Schema oneOf rendering, so project_id requirement
    is now enforced at the schema layer per branch rather than as a
    verb-signature kwarg.
    """
    import inspect
    server = _reimport_server("0")
    # rka_describe — no project_id parameter
    sig_describe = inspect.signature(server.rka_describe)
    assert "project_id" not in sig_describe.parameters, (
        f"rka_describe must not carry project_id (UNSCOPED schema "
        f"lookup): {sig_describe}"
    )
    # rka_execute — typed-args surface: single ``args`` parameter.
    sig_execute = inspect.signature(server.rka_execute)
    assert "args" in sig_execute.parameters, (
        f"rka_execute must declare typed args parameter: {sig_execute}"
    )
    args_execute = sig_execute.parameters["args"]
    assert args_execute.kind in (
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
        inspect.Parameter.KEYWORD_ONLY,
    ), (
        f"rka_execute.args must be POSITIONAL_OR_KEYWORD; "
        f"got {args_execute.kind!r}"
    )
    # rka_query — typed-args surface: single ``args`` parameter.
    sig_query = inspect.signature(server.rka_query)
    assert "args" in sig_query.parameters, (
        f"rka_query must declare typed args parameter: {sig_query}"
    )
    # Verify project_id is REQUIRED on a scoped Execute branch (the
    # contract now lives at the model layer).
    from rka.mcp.operation_args import RecordDecisionArgs
    pid_field = RecordDecisionArgs.model_fields["project_id"]
    assert pid_field.is_required(), (
        f"RecordDecisionArgs.project_id must be required (kwarg-only at "
        f"the model layer); got: {pid_field}"
    )


# ---------------------------------------------------------------------------
# rka_describe behavior — schema lookup returns useful per-op detail
# ---------------------------------------------------------------------------


def test_describe_returns_useful_schema_for_record_decision() -> None:
    """rka_describe(operation='record_decision') must return a payload
    that mentions the four load-bearing fields callers need to know
    about: question, chosen, rationale, related_journal. These are
    the provenance-discipline-required fields surfaced via the
    Phase-X²' hardening; they should be discoverable here too.
    """
    import asyncio
    server = _reimport_server("0")
    out = asyncio.run(server.rka_describe(operation="record_decision"))
    # rka_describe returns JSON. Parse it.
    data = json.loads(out)
    # No error.
    assert "error" not in data, (
        f"rka_describe('record_decision') returned an error: {data}"
    )
    # Operation echoed back.
    assert data.get("operation") == "record_decision"
    # Required fields list contains the canonical names.
    blob = json.dumps(data).lower()
    for required in ("question", "chosen", "rationale", "related_journal"):
        assert required in blob, (
            f"rka_describe('record_decision') response missing "
            f"reference to {required!r}: {blob[:300]}"
        )


def test_describe_returns_useful_schema_for_read_op() -> None:
    """rka_describe(operation='status') must route through the read
    path and identify rka_query as the dispatching tool."""
    import asyncio
    server = _reimport_server("0")
    out = asyncio.run(server.rka_describe(operation="status"))
    data = json.loads(out)
    assert "error" not in data, data
    # The describe payload uses `tool` to name the parent dispatcher.
    # For read operations it's rka_query; for write operations
    # rka_execute. Both shapes are returned by the operations_schema
    # module's DESCRIBE_TABLE entries.
    assert data.get("tool") == "rka_query", (
        f"rka_describe(status) should identify rka_query as the "
        f"dispatching tool; got tool={data.get('tool')!r}"
    )
    # Schema includes signature + required_fields for the LLM.
    assert "signature" in data
    assert "required_fields" in data


def test_describe_unknown_operation_returns_structured_error() -> None:
    """Unknown operations don't crash — they return a structured
    {error, hint} JSON so the LLM caller can self-correct."""
    import asyncio
    server = _reimport_server("0")
    out = asyncio.run(server.rka_describe(operation="not_a_real_op"))
    data = json.loads(out)
    assert data.get("error") == "unknown_operation", (
        f"rka_describe(<unknown>) should return unknown_operation; "
        f"got: {data}"
    )


def test_describe_no_arg_returns_full_catalog() -> None:
    """rka_describe() with no operation returns the compact catalog for browsing.

    v2.7.0 NO-COMPROMISE compromise-#3 mitigation: the catalog is now
    a compact ``{rka_query: "op1, op2, ...", rka_execute: "op1, ...",
    total: N}`` shape (<250 tokens) — the full per-op schema is
    reachable via the FastMCP-rendered ``inputSchema`` of rka_query /
    rka_execute (each branch has per-op required + enum arrays), or
    via ``rka_describe('<op_name>')``.
    """
    import asyncio
    server = _reimport_server("0")
    out = asyncio.run(server.rka_describe())
    data = json.loads(out)
    # The catalog response carries compact rka_query + rka_execute
    # name lists + a total count.
    assert "rka_query" in data, data
    assert "rka_execute" in data, data
    assert "total" in data, data
    # Sanity: non-trivial number of operations.
    assert data["total"] >= 70, (
        f"rka_describe() returned only {data['total']} operations; "
        f"expected ≥ 70 across rka_query + rka_execute."
    )
    # Sample operation names visible in each tool's string.
    assert "status" in data["rka_query"], (
        f"rka_query catalog missing 'status': {data['rka_query'][:200]}"
    )
    assert "record_decision" in data["rka_execute"], (
        f"rka_execute catalog missing 'record_decision': "
        f"{data['rka_execute'][:200]}"
    )


# ---------------------------------------------------------------------------
# rka_execute behavior — unknown operation + dispatch routing
# ---------------------------------------------------------------------------


def test_execute_unknown_operation_returns_structured_error() -> None:
    """rka_execute with an out-of-enum operation is rejected at the
    typed-args layer.

    v2.7.0 NO-COMPROMISE: previously the dispatcher's runtime
    ``operation not in EXECUTE_OPERATIONS`` guard caught this; in the
    typed-args surface, FastMCP/Pydantic rejects the call at the
    discriminator-union layer BEFORE the verb body even runs (this is
    exactly the v2.7.0 pre-mortem compromise #1 mitigation). The
    legacy dispatch path (RKA_LEGACY_TOOLS=1) still emits the
    structured 'invalid_operation' JSON; we exercise that path here
    via the preserved ``_rka_execute_legacy_impl`` callable.
    """
    import asyncio
    server = _reimport_server("0")
    # Call the legacy impl directly — the typed surface would refuse
    # at parse time with a Pydantic ValidationError which is the
    # correct v2.7.0 behaviour (closes compromise #1).
    out = asyncio.run(server._rka_execute_legacy_impl(
        operation="not_a_real_op", project_id="prj_test",
    ))
    data = json.loads(out)
    assert data.get("error") in {"invalid_operation", "unknown_operation"}, (
        f"_rka_execute_legacy_impl(<unknown>) should return an "
        f"invalid_operation / unknown_operation error; got: {data}"
    )
    blob = json.dumps(data).lower()
    assert "not_a_real_op" in blob, (
        f"legacy-impl error response missing offending op name: {data}"
    )

    # Independently verify the typed-args layer rejects the bad
    # operation with a Pydantic ValidationError — this is what an MCP
    # client sees when it calls the canonical verb.
    import pydantic
    from rka.mcp.operation_args import ExecuteArgsUnion
    from pydantic import TypeAdapter
    adapter = TypeAdapter(ExecuteArgsUnion)
    with pytest.raises(pydantic.ValidationError):
        adapter.validate_python({
            "operation": "not_a_real_op",
            "project_id": "prj_test",
        })


def test_execute_operation_enum_includes_load_bearing_ops() -> None:
    """The rka_execute operation enum must include every load-bearing
    write operation that callers (CLAUDE.md, orchestrator, skills)
    reference. Drift here means a write surface was silently
    dropped from the dispatch table."""
    server = _reimport_server("0")
    # Pull the canonical write-ops tuple from verb_dispatch (single
    # source of truth, declared at rka/mcp/verb_dispatch.py:1679).
    from rka.mcp.verb_dispatch import EXECUTE_OPERATIONS
    must_have_ops = (
        "record_note", "record_decision", "record_literature",
        "create_mission", "submit_checkpoint", "submit_report",
        "ingest_document", "import_bibtex", "supersede_decision",
        "review_cluster", "extract_claims", "resolve_contradiction",
        "create_project", "reset_session",
    )
    for op in must_have_ops:
        assert op in EXECUTE_OPERATIONS, (
            f"rka_execute dispatch table missing operation {op!r}; "
            f"callers won't be able to invoke it through the 3-tool "
            f"surface."
        )


# ---------------------------------------------------------------------------
# rka_load_tools escape hatch — legacy tools callable in default mode
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_load_tools_can_promote_legacy_in_default_mode() -> None:
    """In default mode the legacy 91 tools are at tier='deferred', but
    rka_load_tools (kept always-on per Decision 5) can still bring
    them online for callers that need the bare legacy surface."""
    server = _reimport_server("0")

    class _FakeSession:
        def __init__(self):
            self.notifications = 0
        async def send_tool_list_changed(self):
            self.notifications += 1

    class _FakeCtx:
        session = _FakeSession()

    ctx = _FakeCtx()
    fn = server._TOOL_REGISTRY["rka_load_tools"]["fn"]
    raw = await fn(names=["rka_get_status"], ctx=ctx)
    result = json.loads(raw)
    assert result["loaded"] == ["rka_get_status"]
    assert result["unknown"] == []
    # FastMCP now has it.
    mgr_names = {t.name for t in server.mcp._tool_manager.list_tools()}
    assert "rka_get_status" in mgr_names
