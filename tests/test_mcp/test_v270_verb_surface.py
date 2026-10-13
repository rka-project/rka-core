"""v2.7.0 PR-1 — 8-verb surface contract tests.

Pins the v2.7.0 design B + Grafts A/C contract for the always-on tier:

- All 8 verbs are registered at tier='always_on' with category='verb'.
- Always-on count = 12 legacy + 8 verbs = 20 (still under client cap).
- Each verb has a non-empty docstring rendered as the FastMCP tool
  `description` and used by LLM clients as the tool blurb.
- Each verb description LEADS WITH a role tag — [BRAIN], [EXECUTOR],
  [PI], or [ANY] (or a slash combination like [BRAIN/EXECUTOR/PI])
  per the v2.7.0 design Graft C requirement.

These contracts let PR-2 demote the 91 legacy tools without
accidentally also demoting any verb (count check), let the
documentation tooling rely on role tags being present (Graft C), and
let the future verb-surface evolution catch any silent drop of a
verb (membership check).
"""

from __future__ import annotations

import re

import pytest

from rka.mcp.server import (
    _TIER_ALWAYS_ON,
    _TOOL_REGISTRY,
    mcp,
)


# The 8 v2.7.0 always-on verbs — single source of truth for this file.
V270_VERBS = (
    "rka_query",
    "rka_record_note",
    "rka_record_decision",
    "rka_record_literature",
    "rka_mission",
    "rka_checkpoint",
    "rka_review",
    "rka_session",
)


# Pre-v2.7.0 always-on tools (12) that PR-1 keeps unchanged.
LEGACY_ALWAYS_ON = (
    # Minimal Session Start (5)
    "rka_get_status",
    "rka_get_context",
    "rka_get_pending_maintenance",
    "rka_get_checkpoints",
    "rka_get_research_map",
    # Universal retrieval (2)
    "rka_search",
    "rka_get",
    # Most-frequent writes (2)
    "rka_add_note",
    "rka_resolve_checkpoint",
    # Navigator (3)
    "rka_load_tools",
    "rka_list_tools",
    "rka_help",
)


# Acceptable role-tag prefixes. Multi-role combinations like
# [BRAIN/EXECUTOR/PI] and [BRAIN][PI] are both legal — we accept any
# combination so long as the description STARTS with a bracketed role
# tag.
_ROLE_TAG_RE = re.compile(
    r"^\s*\[(?:BRAIN|EXECUTOR|PI|ANY)"
    r"(?:[/,\s\]\[BRAINEXECUTORPIANY]*)\]"
)


def _verb_record(name: str) -> dict:
    rec = _TOOL_REGISTRY.get(name)
    assert rec is not None, f"verb {name!r} missing from _TOOL_REGISTRY"
    return rec


def _mcp_tool(name: str):
    tools = {t.name: t for t in mcp._tool_manager.list_tools()}
    return tools.get(name)


# ---------------------------------------------------------------------------
# Registration + tier
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("verb", V270_VERBS)
def test_verb_registered_as_always_on(verb: str) -> None:
    """Every v2.7.0 verb is registered with tier='always_on' so the LLM
    sees it without needing rka_load_tools."""
    rec = _verb_record(verb)
    assert rec["tier"] == _TIER_ALWAYS_ON, (
        f"verb {verb!r} tier={rec['tier']!r} — must be always_on for "
        f"PR-1 (verb surface needs zero-friction discovery)"
    )
    assert rec["registered"] is True, (
        f"verb {verb!r} not yet handed to mcp._tool_manager — "
        f"always_on tier should auto-register at import"
    )


@pytest.mark.parametrize("verb", V270_VERBS)
def test_verb_category_is_verb(verb: str) -> None:
    """Every v2.7.0a2 verb has category='verb' so rka_list_tools(category='verb')
    returns the 7 remaining verbs (rka_query promoted to category='dispatch'
    in v2.7.0a3)."""
    rec = _verb_record(verb)
    # v2.7.0a3: rka_query is repurposed as a 'dispatch' tool (the read
    # half of the 3-tool dispatch surface). Other 7 verbs stay 'verb'.
    if verb == "rka_query":
        assert rec["category"] == "dispatch", (
            f"verb {verb!r} category={rec['category']!r}, expected "
            f"'dispatch' in v2.7.0a3+ (rka_query is the read half of the "
            f"3-tool surface)"
        )
    else:
        assert rec["category"] == "verb", (
            f"verb {verb!r} category={rec['category']!r}, expected 'verb' "
            f"for clean rka_list_tools(category='verb') filtering"
        )


def test_exactly_eight_verbs_under_verb_category() -> None:
    """v2.7.0a3 — rka_query promoted to 'dispatch' category. The other
    7 v2.7.0a2 verbs remain under category='verb'."""
    verbs = sorted(
        n for n, r in _TOOL_REGISTRY.items()
        if r.get("category") == "verb"
    )
    expected = sorted(set(V270_VERBS) - {"rka_query"})
    assert verbs == expected, (
        f"v2.7.0a3 verb-category drift: got {verbs}, expected {expected} "
        f"(rka_query moved to category='dispatch' in v2.7.0a3)"
    )


# ---------------------------------------------------------------------------
# Always-on count — legacy-enabled surface plus startup skill reads
# ---------------------------------------------------------------------------


def test_legacy_always_on_count_includes_startup_skill_reads() -> None:
    """The PR-1 + v2.7.0a3 always-on tier (under RKA_LEGACY_TOOLS=1)
    is 12 legacy + 8 verbs + 2 v2.7.0a3 dispatch tools
    (rka_execute + rka_describe) + 2 package-only reads = 24. The conftest sets the env var
    so this counts the LEGACY-ENABLED mode. v2.7.0a3 default mode
    (env unset) collapses to 7 tools and is tested separately in
    test_v270a3_dispatch_surface.py.
    """
    always_on = sorted(
        n for n, r in _TOOL_REGISTRY.items()
        if r["tier"] == _TIER_ALWAYS_ON
    )
    assert len(always_on) == 24, (
        f"always-on tier count = {len(always_on)} "
        f"(expected legacy 22 + 2 package-only skill reads = 24). "
        f"Members: {always_on}"
    )


def test_always_on_membership_exact() -> None:
    """The exact always-on member set under RKA_LEGACY_TOOLS=1 is the
    12 PR-1-preserved legacy tools plus the 8 v2.7.0a2 verbs plus
    the 2 v2.7.0a3 dispatch tools (rka_execute + rka_describe). Any
    drift here means a tool was silently moved between tiers."""
    v270a3_dispatch = {"rka_execute", "rka_describe"}
    expected = set(LEGACY_ALWAYS_ON) | set(V270_VERBS) | v270a3_dispatch | {
        "rka_list_skills", "rka_read_skill"
    }
    actual = {
        n for n, r in _TOOL_REGISTRY.items() if r["tier"] == _TIER_ALWAYS_ON
    }
    assert actual == expected, (
        f"always-on membership drift — "
        f"added {sorted(actual - expected)} / "
        f"removed {sorted(expected - actual)}"
    )


def test_legacy_91_tools_still_callable() -> None:
    """Bookkeeper invariant for PR-1: every legacy tool is still in
    the registry (regardless of tier). PR-2 will demote tier but
    must never drop a tool."""
    # We don't pin the exact 91 list (the deferred set drifts when
    # internal tools move); the contract is that the *total* registry
    # is well over 91 (12 + 79 deferred + 8 verbs + 3 navigator = 102+).
    assert len(_TOOL_REGISTRY) >= 91, (
        f"registry has {len(_TOOL_REGISTRY)} entries; expected ≥ 91. "
        f"Some legacy @tool was silently dropped — bookkeeper invariant "
        f"violated."
    )
    # Spot-check load-bearing legacy tools that callers (CLAUDE.md,
    # skills, orchestrator) reference directly.
    for n in (
        "rka_add_note", "rka_add_decision", "rka_create_mission",
        "rka_submit_checkpoint", "rka_submit_report",
        "rka_link_literature_to_zotero", "rka_ingest_document",
    ):
        assert n in _TOOL_REGISTRY, (
            f"legacy tool {n!r} missing — PR-1 must NOT remove legacy tools"
        )


# ---------------------------------------------------------------------------
# Description (docstring) shape — Graft C role tag + Anthropic 500-char floor
# ---------------------------------------------------------------------------


def _description(verb: str) -> str:
    """The FastMCP-rendered description (what LLM clients see)."""
    t = _mcp_tool(verb)
    assert t is not None, f"verb {verb!r} not on mcp._tool_manager"
    return t.description or ""


@pytest.mark.parametrize("verb", V270_VERBS)
def test_verb_description_non_empty(verb: str) -> None:
    """Every verb has a non-empty rendered description so LLM clients
    don't see an empty blurb in the tool catalog."""
    desc = _description(verb)
    assert desc.strip(), f"verb {verb!r} has empty FastMCP description"


@pytest.mark.parametrize("verb", V270_VERBS)
def test_verb_description_starts_with_role_tag(verb: str) -> None:
    """v2.7.0 Graft C — every verb description LEADS with a bracketed
    role tag: [BRAIN], [EXECUTOR], [PI], [ANY], or a multi-role
    combination such as [BRAIN/EXECUTOR/PI] or [BRAIN][PI]. This is
    the routing signal for the orchestrator's actor-aware tool surface
    and for human review of the tool catalog."""
    desc = _description(verb)
    # Look at the first non-empty line stripped of leading whitespace.
    first_line = ""
    for line in desc.splitlines():
        if line.strip():
            first_line = line.strip()
            break
    assert _ROLE_TAG_RE.match(first_line), (
        f"verb {verb!r} description first line {first_line[:80]!r} "
        f"does not start with a [BRAIN]/[EXECUTOR]/[PI]/[ANY] role tag "
        f"(Graft C). Full description prefix: {desc[:200]!r}"
    )


@pytest.mark.parametrize("verb", V270_VERBS)
def test_verb_description_starts_with_first_line_role(verb: str) -> None:
    """Cross-check: the role tag is the very first non-whitespace token.
    Catches a future drift where someone moves the role tag to a later
    line."""
    desc = _description(verb)
    stripped = desc.lstrip()
    assert stripped.startswith("["), (
        f"verb {verb!r} description does not open with '[' — "
        f"got prefix: {desc[:60]!r}"
    )


# Anthropic's MCP guidance recommends concise tool descriptions. Our
# descriptions are docstring-based and unavoidably longer because they
# include Args: blocks, but we keep the first prose paragraph (the
# part LLM clients actually pre-load) tight.
@pytest.mark.parametrize("verb", V270_VERBS)
def test_verb_summary_within_500_chars(verb: str) -> None:
    """The summary (first paragraph of the docstring) stays within
    ~500 chars so the LLM-facing blurb is concise. The full Args:
    block is rendered separately by FastMCP from the signature."""
    desc = _description(verb)
    # First blank-line-delimited paragraph.
    paragraphs = [p for p in desc.split("\n\n") if p.strip()]
    summary = paragraphs[0] if paragraphs else ""
    assert len(summary) <= 500, (
        f"verb {verb!r} summary paragraph is {len(summary)} chars; "
        f"keep the lead paragraph ≤500 for LLM-client tool-catalog "
        f"economy. Summary: {summary[:120]!r}..."
    )


# ---------------------------------------------------------------------------
# Registry sanity
# ---------------------------------------------------------------------------


def test_verb_registry_summary_populated() -> None:
    """The summary field used by rka_list_tools is non-empty for every
    verb so the navigator surface renders cleanly."""
    for verb in V270_VERBS:
        rec = _verb_record(verb)
        assert rec["summary"].strip(), (
            f"verb {verb!r} registry summary is empty — "
            f"rka_list_tools(category='verb') would show an empty bullet"
        )


def test_verb_signature_recorded() -> None:
    """Each verb's signature string is captured in the registry so
    rka_help(name=<verb>) can render it. The signature opens with '('
    (then includes a trailing return annotation like ' -> str')."""
    for verb in V270_VERBS:
        rec = _verb_record(verb)
        sig = rec["signature"]
        assert sig.startswith("("), (
            f"verb {verb!r} signature looks malformed: {sig!r}"
        )
        # v2.7.0 (Phase 3): rka_query now takes a typed
        # ``QueryArgsUnion`` Pydantic union as its sole parameter;
        # project_id lives INSIDE each branch's per-operation model.
        # The signature contract is checked at the model layer
        # (drift-test) rather than the verb signature string.
        if verb == "rka_query":
            assert "args" in sig, (
                f"verb {verb!r} signature missing typed-args param: {sig}"
            )
            continue
        # Every other project-scoped verb has project_id in the signature.
        # Only rka_session is unscoped; even so it carries an optional
        # project_id kwarg for its export/digest/generate_claude_md
        # actions.
        assert "project_id" in sig, (
            f"verb {verb!r} signature missing project_id: {sig}"
        )
