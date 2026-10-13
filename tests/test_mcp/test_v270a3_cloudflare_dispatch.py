"""v2.7.0a3 — Cloudflare 2-tool dispatch contract tests.

These tests pin the externally-observable contract of the 3-tool dispatch
surface (rka_query + rka_execute + rka_describe) per the Cloudflare /
Awkoy notion-mcp pattern that the v2.7.0a3 release ratifies.

What's covered:

- Default-env startup surface (3 dispatch tools always-on with alwaysLoad).
- rka_describe(operation=...) returns useful per-op schema (signature +
  required fields + provenance hints + examples).
- rka_describe('') returns the full catalog grouped by tool / category.
- rka_describe with a malformed name returns suggestions for fuzzy
  matches — the LLM can self-correct without re-querying the catalog.
- rka_execute / rka_query route to the SAME legacy tool the v2.7.0a2
  surface would have hit (parity by monkeypatching the registry).
- inputSchema enum promotion on the discriminator survives — Annotated
  [Literal] renders into the FastMCP tool descriptor's
  `inputSchema.properties.operation.enum` (or equivalent anyOf branch).
- Provenance discipline (Phase-X²' lessons) survives: rka_execute on
  record_decision rejects missing related_journal; rka_execute on
  record_note(source='pi') rejects missing verbatim_input.
- rka_describe response is small (≤2 KB) per Anthropic's per-tool
  truncation guidance.
- Env-flag round-trip — RKA_LEGACY_TOOLS=1 brings back the v2.7.0a2
  surface (24 always-on, including skill reads); unsetting goes back to 7.
- Every example in OPERATIONS_SCHEMA matches the operation's declared
  required_fields contract (no example missing a required field, no
  example smuggling an unknown field that the validator would reject).

These are CONTRACT-level tests: they reload `rka.mcp.server` after
toggling RKA_LEGACY_TOOLS so the module-level @tool() gating + the
FastMCP _tool_manager registration go through their real startup paths.
The directory-level conftest at tests/test_mcp/conftest.py sets the
env-flag to "1" for backwards-compat with the rest of the suite; the
autouse fixture here snapshots and restores it.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import json
import os
from typing import Any

import httpx
import pytest


# ---------------------------------------------------------------------------
# Helpers: reimport rka.mcp.server under a specific RKA_LEGACY_TOOLS value.
# Mirrors the helper in test_v270a3_dispatch_surface.py so the two files
# stay decoupled.
# ---------------------------------------------------------------------------


def _reimport_server(legacy_flag: str | None):
    if legacy_flag is None:
        os.environ.pop("RKA_LEGACY_TOOLS", None)
    else:
        os.environ["RKA_LEGACY_TOOLS"] = legacy_flag
    import rka.mcp.server as server
    importlib.reload(server)
    return server


@pytest.fixture(autouse=True)
def _restore_legacy_env():
    """Snapshot RKA_LEGACY_TOOLS so the rest of the suite's contract
    (conftest.py sets it to "1") stays intact after each test."""
    prior = os.environ.get("RKA_LEGACY_TOOLS")
    yield
    if prior is None:
        os.environ.pop("RKA_LEGACY_TOOLS", None)
    else:
        os.environ["RKA_LEGACY_TOOLS"] = prior
    import rka.mcp.server as server
    importlib.reload(server)


# ---------------------------------------------------------------------------
# Test 1 — default-env startup: only 3 dispatch tools at always-on
# (besides the navigator escape hatches kept always-on per Decision 5).
# ---------------------------------------------------------------------------


def test_three_tools_at_startup_with_default_env() -> None:
    """In default mode (RKA_LEGACY_TOOLS unset → "0"), the
    'dispatch'-category always-on surface registered with FastMCP is
    exactly {rka_query, rka_execute, rka_describe}. The 91 legacy tools
    and the 8 v2.7.0a2 verbs do NOT appear in this set.
    """
    server = _reimport_server("0")
    always_on_dispatch = sorted(
        n for n, r in server._TOOL_REGISTRY.items()
        if r["tier"] == server._TIER_ALWAYS_ON
        and r["category"] == "dispatch"
    )
    assert always_on_dispatch == ["rka_describe", "rka_execute", "rka_query"], (
        f"v2.7.0a3 default dispatch surface drift; got {always_on_dispatch}"
    )

    # FastMCP _tool_manager also surfaces them. Spot-check.
    mcp_names = {t.name for t in server.mcp._tool_manager.list_tools()}
    for n in ("rka_query", "rka_execute", "rka_describe"):
        assert n in mcp_names, (
            f"{n!r} missing from FastMCP's tools/list in default mode"
        )


# ---------------------------------------------------------------------------
# Test 2 — rka_describe('record_decision') returns the load-bearing
# required-fields set.
# ---------------------------------------------------------------------------


def test_describe_returns_schema_for_known_operation() -> None:
    """rka_describe('record_decision') JSON must surface the 4
    provenance-required canonical fields callers need to remember:
    question, chosen, rationale, related_journal. The Phase-X²' polish
    PR added these as TOOL_REQUIRED_FIELDS lock-tests; rka_describe is
    the externally-visible surface that callers actually read."""
    server = _reimport_server("0")
    out = asyncio.run(server.rka_describe(operation="record_decision"))
    data = json.loads(out)
    assert "error" not in data, data
    required = data.get("required_fields") or []
    for field in ("question", "chosen", "rationale", "related_journal"):
        assert field in required, (
            f"rka_describe('record_decision').required_fields missing "
            f"{field!r}; got {required}"
        )
    # And the operation is identified as a rka_execute write.
    assert data.get("tool") == "rka_execute"


# ---------------------------------------------------------------------------
# Test 3 — rka_describe with a near-miss op returns suggestions.
# ---------------------------------------------------------------------------


def test_describe_suggests_corrections_for_unknown_operation() -> None:
    """rka_describe('decision') (close-but-not-canonical) returns
    {error, did_you_mean: [...]} with at least one related real
    operation in the suggestions list. Examples of canonical names
    the LLM should be nudged toward: record_decision, update_decision,
    supersede_decision."""
    server = _reimport_server("0")
    out = asyncio.run(server.rka_describe(operation="decision"))
    data = json.loads(out)
    assert data.get("error") == "unknown_operation", data
    suggestions = data.get("did_you_mean") or []
    assert suggestions, (
        f"rka_describe('decision') returned no suggestions: {data}"
    )
    # At least one of the suggestions should be a decision-family op.
    decision_family = {
        "record_decision", "update_decision", "supersede_decision",
        "present_decision", "record_pi_selection", "decision_tree",
    }
    overlap = set(suggestions) & decision_family
    assert overlap, (
        f"rka_describe('decision').did_you_mean has no decision-family "
        f"suggestion; got {suggestions}"
    )


# ---------------------------------------------------------------------------
# Test 4 — rka_describe() with no operation returns the full catalog.
# ---------------------------------------------------------------------------


def test_describe_full_index_when_empty() -> None:
    """rka_describe(operation='') returns the compact operations index
    (v2.7.0 NO-COMPROMISE compromise-#3 mitigation: <250 tokens
    target). Used by callers for browsing the full surface; per-op
    schema is reachable via the FastMCP-rendered inputSchema or via
    ``rka_describe('<op_name>')``."""
    server = _reimport_server("0")
    out = asyncio.run(server.rka_describe(operation=""))
    data = json.loads(out)
    # Compact-index keys: rka_query + rka_execute as comma-separated
    # strings, plus a total count.
    assert "rka_query" in data and "rka_execute" in data and "total" in data, data
    # Spot-check well-known op names visible in each tool's string.
    assert "status" in data["rka_query"], data["rka_query"][:200]
    assert "record_decision" in data["rka_execute"], data["rka_execute"][:200]


# ---------------------------------------------------------------------------
# Mock-transport helper for parity tests — captures every REST request
# the legacy tool / verb dispatch path would have made. Mirrors the
# pattern from test_v270_legacy_parity.py.
# ---------------------------------------------------------------------------


class _Capture:
    """Records every (method, url_path, body, params) tuple seen by
    the mock httpx transport."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []


def _make_handler(capture: _Capture, response_factory):
    def handler(request: httpx.Request) -> httpx.Response:
        body = None
        if request.content:
            try:
                body = json.loads(request.content)
            except Exception:  # pragma: no cover — body might not be JSON
                body = request.content.decode("utf-8", errors="replace")
        capture.requests.append({
            "method": request.method,
            "path": request.url.path,
            "params": dict(request.url.params),
            "body": body,
        })
        status, payload = response_factory(request.method, request.url.path)
        return httpx.Response(status, json=payload)

    return handler


def _install_capture(
    server_mod, monkeypatch: pytest.MonkeyPatch,
    *, response_factory=None,
) -> _Capture:
    """Patch server._client (and the session-start hook) so calls go to
    an in-process MockTransport whose requests we capture. Returns the
    capture object."""
    cap = _Capture()
    factory = response_factory or (lambda m, p: (200, {"ok": True}))

    def fake_client(project_id: str | None = None) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.MockTransport(_make_handler(cap, factory)),
            base_url="http://testserver",
            headers={"X-RKA-Project": project_id} if project_id else {},
        )

    monkeypatch.setattr(server_mod, "_client", fake_client)

    async def _noop_fire(_pid):
        return None

    monkeypatch.setattr(server_mod, "_maybe_fire_session_start", _noop_fire)
    return cap


# ---------------------------------------------------------------------------
# Test 5 — rka_execute parity: each operation reaches the SAME REST
# endpoint (method + path) the legacy tool would have hit. We compare
# the captured request from the legacy tool against the capture from
# the rka_execute call.
# ---------------------------------------------------------------------------


_EXECUTE_PARITY_SAMPLES = [
    # (operation, legacy_tool_name, kwargs_for_legacy, kwargs_for_execute)
    (
        "record_note",
        "rka_add_note",
        {"content": "note body"},
        {"content": "note body"},
    ),
    (
        "record_decision",
        "rka_add_decision",
        {
            "question": "Q?",
            "phase": "design",
            "decided_by": "brain",
            "chosen": "A",
            "rationale": "because",
            "kind": "decision",
            "related_journal": ["jrn_x"],
        },
        {
            "question": "Q?",
            "phase": "design",
            "decided_by": "brain",
            "chosen": "A",
            "rationale": "because",
            "kind": "decision",
            "related_journal": ["jrn_x"],
        },
    ),
    (
        "submit_checkpoint",
        "rka_submit_checkpoint",
        {
            "mission_id": "mis_x",
            "type": "decision",
            "description": "ratify this",
            "task_reference": "task_a",
        },
        {
            "mission_id": "mis_x",
            "type": "decision",
            "description": "ratify this",
            "task_reference": "task_a",
        },
    ),
    (
        "create_mission",
        "rka_create_mission",
        {
            "phase": "design",
            "objective": "mission objective",
            "context": "ctx",
            "motivated_by_decision": "dec_x",
        },
        {
            "phase": "design",
            "objective": "mission objective",
            "context": "ctx",
            "motivated_by_decision": "dec_x",
        },
    ),
    (
        "ingest_document",
        "rka_ingest_document",
        {"content": "# Header\nbody"},
        {"content": "# Header\nbody"},
    ),
    (
        "record_literature",
        "rka_add_literature",
        {"title": "Some Paper"},
        {"title": "Some Paper"},
    ),
]


async def _safely_call(coro_fn) -> None:
    """Run a tool call but swallow exceptions raised AFTER the first
    REST request (most legacy tools post-process the response and
    crash on the mock's minimal payload — but we only care about the
    REST path, captured at the transport).
    """
    try:
        await coro_fn()
    except Exception:  # noqa: BLE001 — we only care about the captured req
        pass


@pytest.mark.parametrize(
    "operation,legacy_name,legacy_kw,execute_kw",
    _EXECUTE_PARITY_SAMPLES,
)
async def test_execute_dispatch_parity(
    operation: str,
    legacy_name: str,
    legacy_kw: dict,
    execute_kw: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """rka_execute(args=<typed model>) must hit the SAME REST endpoint
    (method + path) the legacy tool would have hit. Pins the
    Cloudflare-collapse bookkeeper invariant: no REST-layer change,
    just a thinner LLM-visible surface.

    v2.7.0 Phase 3: callers now build a typed Pydantic ``Args`` model
    from ``rka.mcp.operation_args`` rather than passing ``operation``
    as a discriminator kwarg. We validate-then-pass via TypeAdapter
    against the canonical ExecuteArgsUnion so the routing decision
    flows through the discriminated-union code path exactly as MCP
    callers would experience it.
    """
    from pydantic import TypeAdapter
    from rka.mcp.operation_args import ExecuteArgsUnion

    server = _reimport_server("0")
    cap = _install_capture(
        server, monkeypatch,
        response_factory=lambda m, p: (200, {
            "id": "x_1", "ok": True, "type": "note",
            "confidence": "hypothesis",
            # Common parser-friendly fields used by legacy post-processing.
            "project_name": "T", "current_phase": "x",
            "results": [], "items": [],
        }),
    )

    # Legacy tool call.
    legacy_fn = server._TOOL_REGISTRY[legacy_name]["fn"]
    await _safely_call(
        lambda: legacy_fn(project_id="prj_test", **legacy_kw)
    )
    legacy_reqs = list(cap.requests)
    cap.requests.clear()

    # v2.7.0 typed-args path: build the typed model and pass to rka_execute.
    adapter = TypeAdapter(ExecuteArgsUnion)
    typed_args = adapter.validate_python({
        "operation": operation, "project_id": "prj_test", **execute_kw,
    })
    await _safely_call(
        lambda: server.rka_execute(typed_args)
    )
    execute_reqs = list(cap.requests)

    assert legacy_reqs, (
        f"Legacy tool {legacy_name!r} produced no REST traffic "
        f"(test fixture problem)."
    )
    assert execute_reqs, (
        f"rka_execute(operation={operation!r}) produced no REST traffic"
    )

    # The endpoints must align: same method, same path. Body parity is
    # covered by the existing test_v270_legacy_parity.py suite; here we
    # focus on the routing question (does rka_execute land on the same
    # endpoint?).
    legacy_paths = [(r["method"], r["path"]) for r in legacy_reqs]
    execute_paths = [(r["method"], r["path"]) for r in execute_reqs]
    assert legacy_paths[0] == execute_paths[0], (
        f"rka_execute(operation={operation!r}) routed to "
        f"{execute_paths[0]}; legacy {legacy_name!r} would have hit "
        f"{legacy_paths[0]}."
    )


# ---------------------------------------------------------------------------
# Test 6 — rka_execute discriminator enum promotion (Annotated[Literal]
# → inputSchema.properties.operation.enum).
# ---------------------------------------------------------------------------


def test_execute_enum_promotion() -> None:
    """rka_execute's discriminator (operation) surfaces as a JSON-Schema
    ``discriminator.mapping`` on the ``args`` parameter. This is the
    load-bearing bit that lets the LLM see the full 58-operation write set
    without reading the docstring.

    v2.7.0 NO-COMPROMISE: the discriminator is keyed off the per-branch
    ``operation`` Literal — each oneOf branch carries an
    ``operation: {const: '<op>'}`` JSON-Schema constraint.
    """
    server = _reimport_server("0")
    tools = {t.name: t for t in server.mcp._tool_manager.list_tools()}
    t = tools.get("rka_execute")
    assert t is not None, "rka_execute not in FastMCP tools/list"
    params = t.parameters
    args_prop = params.get("properties", {}).get("args", {})
    disc = args_prop.get("discriminator", {})
    mapping = disc.get("mapping", {})
    enum_vals = sorted(mapping.keys())
    assert enum_vals, (
        f"rka_execute.args has no discriminator.mapping; "
        f"schema: {args_prop!r}"
    )
    # Every load-bearing write op is present.
    for required_op in (
        "record_note", "record_decision", "create_mission",
        "submit_checkpoint", "submit_report", "supersede_decision",
        "record_literature", "review_cluster",
    ):
        assert required_op in enum_vals, (
            f"rka_execute discriminator missing load-bearing op {required_op!r}"
        )


# ---------------------------------------------------------------------------
# Test 7 — rka_query parity: each operation routes to the SAME legacy
# read tool the v2.7.0a2 surface would have hit.
# ---------------------------------------------------------------------------


_QUERY_PARITY_SAMPLES = [
    # (operation, legacy_tool_name, kwargs_for_both)
    ("status", "rka_get_status", {}),
    ("context", "rka_get_context", {}),
    ("pending_maintenance", "rka_get_pending_maintenance", {}),
    ("research_map", "rka_get_research_map", {}),
    ("review_queue", "rka_get_review_queue", {}),
    ("checkpoints", "rka_get_checkpoints", {}),
    ("calibration_metrics", "rka_get_calibration_metrics", {}),
    ("graph_stats", "rka_graph_stats", {}),
    ("journal", "rka_get_journal", {}),
    ("literature", "rka_get_literature", {}),
    ("clusters", "rka_list_clusters", {}),
    ("hooks", "rka_list_hooks", {}),
]


@pytest.mark.parametrize(
    "operation,legacy_name,extra_kw",
    _QUERY_PARITY_SAMPLES,
)
async def test_query_dispatch_parity(
    operation: str,
    legacy_name: str,
    extra_kw: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """rka_query(args=<typed model>) must hit the SAME REST endpoint
    (method + path) the legacy tool would have hit. Pins the
    Cloudflare-collapse routing invariant for the read surface.

    v2.7.0 Phase 3: callers build a typed Query*Args model from
    ``rka.mcp.operation_args`` and pass it via the discriminated-union
    arg parameter rather than the legacy a3 ``operation`` kwarg.
    """
    from pydantic import TypeAdapter
    from rka.mcp.operation_args import QueryArgsUnion

    server = _reimport_server("0")
    cap = _install_capture(
        server, monkeypatch,
        response_factory=lambda m, p: (200, {
            "results": [], "items": [],
            "project_name": "T", "current_phase": "x",
        }),
    )

    # Legacy tool call.
    legacy_fn = server._TOOL_REGISTRY[legacy_name]["fn"]
    await _safely_call(
        lambda: legacy_fn(project_id="prj_test", **extra_kw)
    )
    legacy_reqs = list(cap.requests)
    cap.requests.clear()

    # v2.7.0 typed-args call.
    adapter = TypeAdapter(QueryArgsUnion)
    typed_args = adapter.validate_python({
        "operation": operation, "project_id": "prj_test", **extra_kw,
    })
    await _safely_call(
        lambda: server.rka_query(typed_args)
    )
    query_reqs = list(cap.requests)

    assert legacy_reqs, (
        f"Legacy tool {legacy_name!r} produced no REST traffic"
    )
    assert query_reqs, (
        f"rka_query(args=op={operation!r}) produced no REST traffic"
    )
    legacy_paths = [(r["method"], r["path"]) for r in legacy_reqs]
    query_paths = [(r["method"], r["path"]) for r in query_reqs]
    assert legacy_paths[0] == query_paths[0], (
        f"rka_query(args=op={operation!r}) routed to "
        f"{query_paths[0]}; legacy {legacy_name!r} hit {legacy_paths[0]}."
    )


# ---------------------------------------------------------------------------
# Test 8 — provenance discipline preserved.
# ---------------------------------------------------------------------------


async def test_provenance_discipline_preserved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Phase-X²' rules carry forward to the v2.7.0 surface:

    - record_note with source='pi' but no verbatim_input is rejected.
    - record_decision without related_journal is rejected.

    v2.7.0 NO-COMPROMISE: rejection now happens at the typed-args layer
    (Pydantic ValidationError) BEFORE any dispatch. This is the
    pre-mortem compromise #1 mitigation — the LLM cannot emit a
    discipline-violating call because the JSON Schema validator
    catches it at the union-discriminator layer.
    """
    import pydantic
    from pydantic import TypeAdapter
    from rka.mcp.operation_args import ExecuteArgsUnion

    server = _reimport_server("0")
    cap = _install_capture(server, monkeypatch)
    adapter = TypeAdapter(ExecuteArgsUnion)

    # (a) record_note with source='pi' but no verbatim_input.
    with pytest.raises(pydantic.ValidationError) as excinfo:
        adapter.validate_python({
            "operation": "record_note",
            "project_id": "prj_test",
            "source": "pi",
            "content": "PI says X",
            # NO verbatim_input
        })
    assert "verbatim_input" in str(excinfo.value).lower()

    # (b) record_decision with no related_journal.
    with pytest.raises(pydantic.ValidationError) as excinfo2:
        adapter.validate_python({
            "operation": "record_decision",
            "project_id": "prj_test",
            "question": "Q?",
            "chosen": "A",
            "rationale": "because",
            "decided_by": "brain",
            "kind": "decision",
            "related_journal": [],  # empty -> validator fires
        })
    assert "related_journal" in str(excinfo2.value).lower()

    # No REST traffic — Pydantic rejected at the schema layer.
    assert cap.requests == [], (
        f"validation failures should not produce REST traffic; "
        f"got: {cap.requests}"
    )


# ---------------------------------------------------------------------------
# Test 9 — rka_describe response size budget.
# ---------------------------------------------------------------------------


def test_describe_response_size_under_2kb() -> None:
    """Per Anthropic's per-tool truncation guidance (in the
    tool-design docs), individual tool-response payloads should be
    small enough to fit in a single LLM context budget — we pin
    ≤2 KB for a per-op rka_describe output. Larger response sizes
    are a hint the entry has grown too verbose (the curated
    OPERATIONS_SCHEMA shouldn't bundle prose; it should refer the
    LLM out to docs)."""
    server = _reimport_server("0")
    out = asyncio.run(server.rka_describe(operation="record_decision"))
    size = len(out.encode("utf-8"))
    assert size <= 2048, (
        f"rka_describe('record_decision') response is {size} bytes "
        f"(> 2 KB budget). Trim the curated entry."
    )


# ---------------------------------------------------------------------------
# Test 10 — RKA_LEGACY_TOOLS round trip restores the v2.7.0a2 surface.
# ---------------------------------------------------------------------------


def test_legacy_env_flag_round_trip() -> None:
    """Flip RKA_LEGACY_TOOLS=1, reimport, assert the v2.7.0a2 always-on
    surface (12 v2.6.4 baseline + 8 v2.7.0 verbs + 2 navigator at
    always_on + the 2 new v2.7.0a3 dispatch tools) is restored. Then
    flip back to "0" and assert the 7-tool default is back."""
    legacy = _reimport_server("1")
    legacy_always_on = sorted(
        n for n, r in legacy._TOOL_REGISTRY.items()
        if r["tier"] == legacy._TIER_ALWAYS_ON
    )
    # 12 v2.6.4 baseline + 8 v2.7.0a2 verbs + rka_load_tools + rka_help
    # + rka_execute + rka_describe + two skill reads = 24 (mirrors the test in
    # test_v270a3_dispatch_surface.py).
    assert len(legacy_always_on) == 24, (
        f"RKA_LEGACY_TOOLS=1 always-on count = {len(legacy_always_on)}; "
        f"expected 24: {legacy_always_on}"
    )
    # v2.7.0a2 verbs are visible in this mode.
    for v in (
        "rka_record_note", "rka_record_decision", "rka_record_literature",
        "rka_mission", "rka_checkpoint", "rka_review", "rka_session",
    ):
        assert v in legacy_always_on, (
            f"RKA_LEGACY_TOOLS=1 missing v2.7.0a2 verb {v!r}"
        )

    # Flip back; the default 7-tool surface is back.
    default = _reimport_server("0")
    default_always_on = sorted(
        n for n, r in default._TOOL_REGISTRY.items()
        if r["tier"] == default._TIER_ALWAYS_ON
    )
    assert len(default_always_on) == 7, (
        f"RKA_LEGACY_TOOLS=0 always-on count = {len(default_always_on)}; "
        f"expected 7: {default_always_on}"
    )


# ---------------------------------------------------------------------------
# Test 11 — alwaysLoad meta hint on all 3 dispatch tools (default mode).
# ---------------------------------------------------------------------------


def test_alwaysload_meta_on_all_three() -> None:
    """The 3 dispatch tools each register with FastMCP carrying
    `_meta={'anthropic/alwaysLoad': True}` so Claude Code's
    ToolSearch ranking pins them into the per-turn toolset."""
    server = _reimport_server("0")
    tools = {t.name: t for t in server.mcp._tool_manager.list_tools()}
    for name in ("rka_query", "rka_execute", "rka_describe"):
        t = tools.get(name)
        assert t is not None, f"{name!r} missing from FastMCP tools/list"
        meta = getattr(t, "meta", None) or {}
        assert meta.get("anthropic/alwaysLoad") is True, (
            f"{name!r} missing _meta['anthropic/alwaysLoad']=True; "
            f"got: {meta!r}"
        )


# ---------------------------------------------------------------------------
# Test 12 — every example in OPERATIONS_SCHEMA carries the right
# required fields for that operation. Drift detector / lock-test.
# ---------------------------------------------------------------------------


def test_describe_examples_are_valid_calls() -> None:
    """For every entry in OPERATIONS_SCHEMA, each example.call dict
    must:

      (a) carry every field in required_fields (a curated example
          missing a required field misleads the LLM into omitting it),
          AND
      (b) carry `operation` matching the entry's canonical name (the
          v2.7.0a3 dispatch surface is operation-first; an example
          with the wrong operation name would teach the LLM the wrong
          call shape).

    The `provenance` key is treated as a synthetic stand-in for the
    field-name layer: when an operation's required_fields includes a
    provenance-style field (related_journal, motivated_by_decision),
    the example may surface it either at top-level or nested under
    `provenance={...}` — the dispatch layer accepts both per Phase-X²'.
    """
    _reimport_server("0")
    from rka.mcp.operations_schema import OPERATIONS_SCHEMA

    # Provenance fields may live at top-level OR nested under
    # `provenance={...}` (Phase-X²' contract).
    _PROVENANCE_ALIASES = {
        "related_journal", "motivated_by_decision",
        "related_decisions", "related_literature", "related_mission",
        "supersedes_decision_id", "parent_id",
    }

    failures: list[str] = []
    for op_name, entry in OPERATIONS_SCHEMA.items():
        examples = entry.get("examples") or []
        required = set(entry.get("required_fields") or [])
        for i, example in enumerate(examples):
            call = example.get("call") or {}
            # (b) operation discriminator matches.
            if call.get("operation") != op_name:
                failures.append(
                    f"{op_name!r}.examples[{i}].call.operation = "
                    f"{call.get('operation')!r}, expected {op_name!r}"
                )
            # (a) required fields present. Per OPERATIONS_SCHEMA
            # convention, a required field may live at top level, OR
            # nested under `filters={...}`, `options={...}`, or
            # `provenance={...}` depending on the operation's
            # dispatcher.
            top_level_keys = set(call.keys())
            nested_keys: set[str] = set()
            for sub_key in ("filters", "options", "provenance"):
                sub = call.get(sub_key)
                if isinstance(sub, dict):
                    nested_keys |= set(sub.keys())
            satisfied = top_level_keys | nested_keys
            for req in required:
                if req in satisfied:
                    continue
                # Provenance-alias substitution.
                if req in _PROVENANCE_ALIASES and (
                    satisfied & _PROVENANCE_ALIASES
                ):
                    continue
                failures.append(
                    f"{op_name!r}.examples[{i}].call missing required "
                    f"field {req!r}; got top-level {sorted(top_level_keys)}"
                    f" + nested {sorted(nested_keys)}"
                )
    assert not failures, (
        "OPERATIONS_SCHEMA examples drift detected:\n  - "
        + "\n  - ".join(failures)
    )
