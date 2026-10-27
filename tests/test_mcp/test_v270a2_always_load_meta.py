"""v2.7.0a2 — alwaysLoad meta hint + T2-vocabulary + Decisions 2/3 tests.

Pins the LOAD-BEARING fix of v2.7.0a2: every always-on tool carries the
Anthropic-namespaced ``anthropic/alwaysLoad`` hint in its MCP descriptor's
``_meta`` field so Claude Code's per-turn ToolSearch is bypassed. The T2
empirical falsification (rka_query did not surface for "what's blocked /
what needs my attention / render the research map") is the exact failure
mode this hint prevents.

These tests guard the four moving parts of v2.7.0a2:

1. Every always-on tool (12 baseline + 8 v2.7.0 verbs = 20) has
   ``_meta == {"anthropic/alwaysLoad": True}`` on its FastMCP descriptor.
2. Deferred tools — if/when they reach FastMCP — must NOT carry the
   alwaysLoad hint (pinning them defeats ToolSearch ranking by design).
3. The hint survives wire-protocol serialization (the ``_meta`` field
   with leading underscore — what tools/list emits to MCP clients).
4. rka_query's description carries the T2 falsification vocabulary
   ("blocked", "research map", "checkpoints", "maintenance") so the
   verb survives ToolSearch ranking even on older clients that don't
   honor alwaysLoad.

Decisions 2 and 3 from the v2.7.0a2 Phase 1 lock-in:
  - Decision 2 / Option C — rka_record_literature accepts BOTH
    ``add_to_library`` (gate) and ``import_top_n`` (slice ceiling).
  - Decision 3 / Option A — rka_record_literature renames ``year`` →
    ``year_min`` with a one-release deprecation alias; setting both
    is an error.

Empirical context: see CLAUDE.md "Phase-X²' polish — schema-divergence
validation chain" + the 2026-06-02 T1–T5 PI conversation results.
"""

from __future__ import annotations

import asyncio
import inspect

import pytest

import rka.mcp.server as _live_server
from rka.mcp.server import (_TIER_ALWAYS_ON, _TIER_DEFERRED, rka_query, rka_record_literature)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _list_mcp_tools() -> dict:
    """Synchronous wrapper for the FastMCP tool manager's sync listing."""
    return {t.name: t for t in _live_server.mcp._tool_manager.list_tools()}


def _list_mcp_tools_via_async_handler() -> list:
    """Drive the public async ``mcp.list_tools()`` handler that emits the
    wire-protocol tool descriptors. This is the path a real tools/list
    request takes through FastMCP."""
    return asyncio.run(_live_server.mcp.list_tools())


# ---------------------------------------------------------------------------
# Test 1: every always-on tool has the alwaysLoad meta hint
# ---------------------------------------------------------------------------


def test_every_always_on_tool_has_always_load_meta() -> None:
    """LOAD-BEARING: every tier='always_on' tool registered with FastMCP
    must carry ``_meta={"anthropic/alwaysLoad": True}``.

    A future PR that adds a new always-on tool but forgets always_load=True
    (e.g. by overriding it to False) will fail this test loudly. The hint
    is the contract that bypasses Claude Code's per-turn ToolSearch
    ranking — without it the T2 falsification (rka_query lost to legacy
    tools whose names matched prompt vocab) recurs.
    """
    mcp_tools = _list_mcp_tools()
    always_on_names = sorted(
        n for n, r in _live_server._TOOL_REGISTRY.items()
        if r["tier"] == _TIER_ALWAYS_ON
    )
    # Sanity-check the population matches Decision 1's pin under
    # RKA_LEGACY_TOOLS=1 (the conftest default): 12 baseline + 8
    # v2.7.0a2 verbs + 2 v2.7.0a3 dispatch + 2 package-only reads = 24.
    assert len(always_on_names) == 24, (
        f"always-on tier count = {len(always_on_names)}, expected 24 "
        f"(legacy 22 + 2 package-only skill reads). "
        f"Drift means a tool moved tiers — re-audit alwaysLoad on every "
        f"change."
    )

    missing_hint: list[str] = []
    wrong_hint: list[tuple[str, object]] = []
    not_registered: list[str] = []
    for name in always_on_names:
        t = mcp_tools.get(name)
        if t is None:
            not_registered.append(name)
            continue
        meta = getattr(t, "meta", None)
        if meta is None:
            missing_hint.append(name)
            continue
        val = meta.get("anthropic/alwaysLoad")
        if val is not True:
            wrong_hint.append((name, val))

    assert not not_registered, (
        f"always-on tools missing from mcp._tool_manager: {not_registered}. "
        f"The @tool() decorator's `if tier == _TIER_ALWAYS_ON:` branch "
        f"must register every always-on tool at import."
    )
    assert not missing_hint, (
        f"always-on tools missing the alwaysLoad meta hint: {missing_hint}. "
        f"Each should register with `meta={{'anthropic/alwaysLoad': True}}`."
    )
    assert not wrong_hint, (
        f"always-on tools have wrong alwaysLoad value (expected True): "
        f"{wrong_hint}"
    )


# ---------------------------------------------------------------------------
# Test 2: deferred tools must NOT carry the alwaysLoad hint
# ---------------------------------------------------------------------------


def test_deferred_tools_do_not_have_always_load_meta() -> None:
    """Deferred tools are designed to be subject to ToolSearch ranking
    when rka_load_tools brings them into the live toolset. Pinning them
    with alwaysLoad would defeat that design — the navigator surface
    would no longer be the discovery path.

    For deferred tools that aren't yet registered with FastMCP at import,
    the registry's own ``always_load`` field must be False (the decorator
    infers this from tier when always_load is None).
    """
    deferred_names = sorted(
        n for n, r in _live_server._TOOL_REGISTRY.items()
        if r["tier"] == _TIER_DEFERRED
    )
    assert deferred_names, (
        "no deferred tools found — has the v2.7.0a2 tier scheme drifted?"
    )

    # Contract 1: registry's always_load field is False for deferred.
    bad_registry: list[tuple[str, object]] = []
    for name in deferred_names:
        val = _live_server._TOOL_REGISTRY[name].get("always_load")
        if val is not False:
            bad_registry.append((name, val))
    assert not bad_registry, (
        f"deferred tools should have always_load=False in registry, "
        f"got: {bad_registry[:5]} ({len(bad_registry)} total). "
        f"Pinning deferred tools would defeat ToolSearch ranking."
    )

    # Contract 2: if a deferred tool somehow surfaced on FastMCP
    # (because rka_load_tools was called at import time, or because a
    # test imported a deferred tool's wrapper directly), it must not
    # carry the alwaysLoad hint.
    mcp_tools = _list_mcp_tools()
    leaked: list[str] = []
    for name in deferred_names:
        t = mcp_tools.get(name)
        if t is None:
            continue  # not registered — fine, that's the deferred default
        meta = getattr(t, "meta", None) or {}
        if meta.get("anthropic/alwaysLoad") is True:
            leaked.append(name)
    assert not leaked, (
        f"deferred tools carrying alwaysLoad meta hint: {leaked}. "
        f"This would defeat ToolSearch ranking for the navigator surface."
    )


# ---------------------------------------------------------------------------
# Test 3: alwaysLoad survives the tools/list wire-protocol response
# ---------------------------------------------------------------------------


def test_always_load_survives_tools_list_response() -> None:
    """Wire-protocol contract: the ``_meta`` field with leading underscore
    (per the MCP spec — pydantic field alias) must be present on every
    always-on tool's serialized tool descriptor.

    This is what tools/list returns to MCP clients. If the hint is in
    the python ``Tool.meta`` attribute but not in the serialized
    descriptor (e.g. because of a missing by_alias=True), Claude Code
    wouldn't see it on the wire and the pin would be silently broken.
    """
    descriptors = _list_mcp_tools_via_async_handler()
    assert descriptors, "mcp.list_tools() returned empty — server broken?"

    always_on_names = {
        n for n, r in _live_server._TOOL_REGISTRY.items() if r["tier"] == _TIER_ALWAYS_ON
    }

    seen_on_wire: dict[str, dict] = {}
    for d in descriptors:
        if d.name not in always_on_names:
            continue
        wire = d.model_dump(by_alias=True, exclude_none=True)
        # MCP spec field is `_meta` (leading underscore); pydantic alias.
        assert "_meta" in wire, (
            f"tool {d.name!r} wire descriptor missing `_meta` field — "
            f"model_dump(by_alias=True) is not exposing the alias. "
            f"Without `_meta` on the wire, Claude Code can't read the "
            f"alwaysLoad hint and the pin is broken."
        )
        meta = wire["_meta"]
        assert meta.get("anthropic/alwaysLoad") is True, (
            f"tool {d.name!r} wire-serialized _meta missing or wrong: "
            f"{meta!r}"
        )
        seen_on_wire[d.name] = meta

    missing_from_wire = sorted(always_on_names - set(seen_on_wire))
    assert not missing_from_wire, (
        f"always-on tools not present in mcp.list_tools() output: "
        f"{missing_from_wire}. The async handler should expose every "
        f"registered tool."
    )


# ---------------------------------------------------------------------------
# Test 4: rka_query description carries T2-falsified trigger vocabulary
# ---------------------------------------------------------------------------


def test_rka_query_description_contains_t2_trigger_phrases() -> None:
    """Empirical T2 failure (2026-06-02): rka_query did NOT surface for
    the PI prompt "what's blocked, what needs my attention, render the
    research map." None of those phrases were in the verb's description,
    so ToolSearch ranked the legacy ``rka_get_*`` tools higher (they
    won on NAME match).

    v2.7.0a2 adds the T2 vocabulary to the description so the verb
    survives ToolSearch ranking on older clients that don't honor
    alwaysLoad. The phrases below were chosen from the T2 prompt and
    related navigation verbs from the legacy surface.
    """
    desc = (rka_query.__doc__ or "").lower()
    required_phrases = (
        "blocked",
        "research map",
        "checkpoints",
        "maintenance",
    )
    missing = [p for p in required_phrases if p not in desc]
    assert not missing, (
        f"rka_query description missing T2-falsification vocabulary: "
        f"{missing}. The 2026-06-02 T2 PI conversation showed the verb "
        f"loses to legacy tools when prompt vocab doesn't match the "
        f"description. Add these phrases to the docstring's 'Trigger "
        f"phrases:' line."
    )


# ---------------------------------------------------------------------------
# Test 5: rka_record_literature Decision 2 compliance (Option C)
# ---------------------------------------------------------------------------


def test_rka_record_literature_decision_2_compliance() -> None:
    """Decision 2 / Option C — composed behavior:
      - ``add_to_library: bool = False`` remains the all-or-nothing gate
        (False = display only, True = import all returned results).
      - ``import_top_n: int | None = None`` is the OPTIONAL slice ceiling
        applied only when add_to_library=True (None = import all).

    The cockpit's T4 critique surfaced this as design-vs-impl drift; the
    fix preserves backwards compat by adding import_top_n alongside
    add_to_library rather than replacing it.

    Cosmetic fix #5 batched here: docstring no longer claims an exact
    mode count (the prior "Five+ modes" wording was false-precision —
    actual sub-modes total 8 via bullets vs 4 in the design doc).
    """
    sig = inspect.signature(rka_record_literature)
    params = sig.parameters

    # Contract 1: add_to_library survives with bool default False.
    assert "add_to_library" in params, (
        "rka_record_literature missing add_to_library kwarg — "
        "Decision 2 Option C requires keeping it as the gate."
    )
    p = params["add_to_library"]
    assert p.default is False, (
        f"add_to_library default = {p.default!r}, expected False "
        f"(display-only by default)."
    )
    # rka/mcp/server.py uses `from __future__ import annotations`, so
    # parameter annotations stringify; accept either the runtime type or
    # its name token.
    assert p.annotation in (bool, "bool"), (
        f"add_to_library annotation = {p.annotation!r}, expected bool."
    )

    # Contract 2: import_top_n added as int | None = None.
    assert "import_top_n" in params, (
        "rka_record_literature missing import_top_n kwarg — "
        "Decision 2 Option C requires adding the slice ceiling."
    )
    p = params["import_top_n"]
    assert p.default is None, (
        f"import_top_n default = {p.default!r}, expected None "
        f"(no ceiling => import all returned)."
    )
    # Annotation should be Optional[int] / int | None.
    ann_str = str(p.annotation)
    assert "int" in ann_str and ("None" in ann_str or "Optional" in ann_str), (
        f"import_top_n annotation = {ann_str!r}, expected int | None"
    )

    # Contract 3: both are kwarg-only (the verb signature uses '*' to
    # enforce keyword-only invocation).
    assert params["import_top_n"].kind == inspect.Parameter.KEYWORD_ONLY, (
        f"import_top_n kind = {params['import_top_n'].kind!r}, "
        f"expected KEYWORD_ONLY"
    )
    assert params["add_to_library"].kind == inspect.Parameter.KEYWORD_ONLY, (
        f"add_to_library kind = {params['add_to_library'].kind!r}, "
        f"expected KEYWORD_ONLY"
    )

    # Contract 4: cosmetic — docstring no longer asserts "Five+ modes"
    # / "5+ modes" / "Five modes" (the false-precision drift).
    doc = (rka_record_literature.__doc__ or "").lower()
    assert "five+ modes" not in doc and "5+ modes" not in doc, (
        "rka_record_literature docstring still claims 'Five+ modes' — "
        "Decision 2 fix #5 wants this normalized away to avoid "
        "false-precision drift."
    )

    # Contract 5: import_top_n is documented in the Args block so callers
    # discover the slice-ceiling semantic.
    assert "import_top_n" in doc, (
        "rka_record_literature docstring does not mention import_top_n — "
        "Decision 2 Option C requires the docstring to describe the new "
        "kwarg so the cockpit can discover it via schema introspection."
    )


# ---------------------------------------------------------------------------
# Test 6: rka_record_literature Decision 3 compliance (Option A)
# ---------------------------------------------------------------------------


def test_rka_record_literature_decision_3_compliance() -> None:
    """Decision 3 / Option A — soft deprecation:
      - ``year_min: int | None = None`` is the new canonical kwarg
        (semantics: filter min on search modes; paper's pub year on
        default-add mode).
      - ``year: int | None = None`` survives as a DEPRECATED ALIAS for
        one release. Setting both raises ``conflicting_args``. Setting
        only ``year`` back-fills ``year_min`` internally.

    This matches the T4 cockpit critique: ``year=2023`` semantics were
    ambiguous (exact match? since? until?). ``year_min=2023`` makes the
    "since" intent explicit — which is the dominant research-search
    intent that backends already use (rka_search_semantic_scholar
    accepts year_min, services/literature.py uses year_min/year_max).
    """
    sig = inspect.signature(rka_record_literature)
    params = sig.parameters

    # Contract 1: year_min added.
    assert "year_min" in params, (
        "rka_record_literature missing year_min kwarg — "
        "Decision 3 Option A requires the new canonical name."
    )
    p = params["year_min"]
    assert p.default is None, (
        f"year_min default = {p.default!r}, expected None"
    )
    ann_str = str(p.annotation)
    assert "int" in ann_str and ("None" in ann_str or "Optional" in ann_str), (
        f"year_min annotation = {ann_str!r}, expected int | None"
    )
    assert p.kind == inspect.Parameter.KEYWORD_ONLY, (
        f"year_min kind = {p.kind!r}, expected KEYWORD_ONLY"
    )

    # Contract 2: year survives as the deprecated alias (still int | None,
    # default None) so old callers keep working.
    assert "year" in params, (
        "rka_record_literature missing year kwarg — "
        "Decision 3 Option A keeps year as a deprecated alias for one "
        "release; removal scheduled v2.8."
    )
    p = params["year"]
    assert p.default is None, (
        f"year default = {p.default!r}, expected None"
    )
    ann_str = str(p.annotation)
    assert "int" in ann_str and ("None" in ann_str or "Optional" in ann_str), (
        f"year annotation = {ann_str!r}, expected int | None"
    )

    # Contract 3: docstring announces the deprecation explicitly so
    # callers see the migration target.
    doc = (rka_record_literature.__doc__ or "").lower()
    assert "year_min" in doc, (
        "rka_record_literature docstring does not mention year_min — "
        "Decision 3 Option A requires the docstring to describe the new "
        "canonical name."
    )
    assert "deprecated" in doc or "deprecation" in doc, (
        "rka_record_literature docstring does not announce the year "
        "deprecation — callers must see the migration target."
    )
    # Decision 3 explicitly schedules removal in v2.8.
    assert "v2.8" in doc or "2.8" in doc, (
        "rka_record_literature docstring does not announce the v2.8 "
        "removal target — Decision 3 Option A scheduled it."
    )

    # Contract 4: behavioral — setting BOTH year and year_min must error
    # out with a conflicting_args-shaped response. We invoke the verb
    # directly with both set; the dispatcher returns an error JSON
    # without round-tripping to RKA (so this is safe to run offline).
    out = asyncio.run(
        rka_record_literature(
            project_id="prj_test_v270a2",
            title="Test paper",
            year=2023,
            year_min=2023,
        )
    )
    assert "conflicting_args" in out, (
        f"rka_record_literature(year=..., year_min=...) should return "
        f"conflicting_args error; got: {out[:200]!r}"
    )


# ---------------------------------------------------------------------------
# Audit surface — make sure the registry exposes always_load for inspection
# ---------------------------------------------------------------------------


def test_registry_exposes_always_load_for_audit() -> None:
    """Decision 1 implementation scope item 3: propagate ``always_load``
    into ``_TOOL_REGISTRY[name]`` for introspection by rka_list_tools so
    PR reviewers can verify which tools are pinned without dumping the
    FastMCP internals.

    Every registry entry must carry a boolean ``always_load`` flag whose
    value matches the FastMCP descriptor's meta hint.
    """
    mcp_tools = _list_mcp_tools()
    mismatches: list[tuple[str, bool, object]] = []
    for name, rec in _live_server._TOOL_REGISTRY.items():
        assert "always_load" in rec, (
            f"registry entry {name!r} missing always_load field — "
            f"Decision 1 implementation scope #3 unfulfilled."
        )
        flag = rec["always_load"]
        assert isinstance(flag, bool), (
            f"registry entry {name!r} always_load = {flag!r}, expected bool"
        )
        # Cross-check against the FastMCP descriptor if registered.
        t = mcp_tools.get(name)
        if t is None:
            continue
        meta = getattr(t, "meta", None) or {}
        wire_flag = meta.get("anthropic/alwaysLoad", False)
        if bool(wire_flag) != flag:
            mismatches.append((name, flag, wire_flag))
    assert not mismatches, (
        f"registry/FastMCP always_load mismatch: {mismatches}"
    )
