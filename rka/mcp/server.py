"""MCP server — thin HTTP proxy to the RKA REST API.

All tools are prefixed with `rka_` for namespace isolation.
The server keeps lightweight per-session state for session digest.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from typing import Annotated, Literal

from rka.mcp.local_transport import LocalOnlyFastMCP
from pydantic import Field
import httpx

from rka import __version__
from rka.models.mission import MissionTask
from rka.mcp._enums import (
    ChkTypeLit,
    ConfidenceLit,
    DecidedByLit,
    DecisionKindLit,
    EvidenceStatusLit,
    ImportanceLit,
    IngestSourceLit,
    JournalCaptureModeLit,
    MissionStatusLit,
    SourceLit,
)


# ---------------------------------------------------------------------------
# v2.6.2 — Annotated[Literal] enum type aliases for WRITE_TOOLS
# ---------------------------------------------------------------------------
# Promotes the canonical RKA enum values from docstring-only declarations
# (where LLMs would have to read prose to learn the allowed set) into
# FastMCP-rendered `inputSchema.properties.*.enum`. LLM clients that
# consume the rendered schema (Claude Desktop, Claude Code, etc.) now
# see the constrained set directly and refuse out-of-enum proposals
# pre-call. This closes the third rung in the validation-chain ladder:
#
#   - v2.6.0: enum constraints exist in the Pydantic models +
#     SQLite CHECK constraints (server-side, post-roundtrip)
#   - v2.6.0+agentic Phase-X² polish: orchestrator-side
#     TOOL_ARG_ENUMS mirror catches enum mismatches at
#     execute_ratified_actions (pre-dispatch, in-orchestrator)
#   - v2.6.2: Annotated[Literal[...]] on the MCP signatures
#     so the FastMCP rendering puts the enum into the LLM's tool
#     definition itself (pre-proposal, in-LLM)
#   - v2.7.0 PR-0: the underlying plain Literal aliases live in
#     rka/mcp/_enums.py so v2.7.0 verbs and v2.6.x tools share the
#     same source. The Annotated wrappers below preserve each tool's
#     existing parameter description for FastMCP rendering.
#
# Mirror of the canonical sets at orchestrator/orchestrator/rka_enums.py
# and rka/db/schema.sql CHECK constraints. When the canonical sets
# change, update BOTH _enums.py here and rka_enums.py in orchestrator.

# Journal entry confidence (rka/db/schema.sql + rka/models/journal.py).
# Run-5's empirical Brain hallucination was 'confirmed' — NOT a valid
# value. The orchestrator Phase-X² polish catches it pre-dispatch; this
# Annotated promotion catches it pre-LLM-emission via the inputSchema.
ConfidenceLiteral = Annotated[
    ConfidenceLit,
    Field(
        description=(
            "Confidence level for the journal entry. Claim confidence is a "
            "numeric 0.0-1.0 field on the claim API. The Brain "
            "LLM commonly hallucinates 'confirmed' — that value is NOT "
            "in the allowed set. Use 'verified' for cross-checked "
            "findings or 'tested' for empirically probed findings."
        )
    ),
]

# Journal entry importance.
ImportanceLiteral = Annotated[
    ImportanceLit,
    Field(description="Importance level for the journal entry."),
]

# Actor-of-record across journal / decision / literature writes.
SourceLiteral = Annotated[
    SourceLit,
    Field(
        description=(
            "Who created this record. For Brain-authored entries, use "
            "'brain'. For Executor-authored, use 'executor'."
        )
    ),
]

# Decision lifecycle / authorship.
DecidedByLiteral = Annotated[
    DecidedByLit,
    Field(description="Who decided this. PI for ratified, Brain for proposed."),
]

# Decision kind.
DecisionKindLiteral = Annotated[
    DecisionKindLit,
    Field(
        description=(
            "Kind of decision. 'research_question' is reserved for "
            "advanceable RQs; most decisions are 'decision' or "
            "'design_choice'."
        )
    ),
]

# Checkpoint kind.
CheckpointTypeLiteral = Annotated[
    ChkTypeLit,
    Field(
        description=(
            "Type of checkpoint. 'gate' for blocking go/no-go points; "
            "'decision' for forks needing PI adjudication; "
            "'clarification' for ambiguity surfaces; 'inspection' for "
            "hands-off review."
        )
    ),
]

# Mission lifecycle status (rka_update_mission_status).
MissionStatusLiteral = Annotated[
    MissionStatusLit,
    Field(description="Mission lifecycle status."),
]

# Document ingestion source (added_by-equivalent for rka_ingest_document).
IngestSourceLiteral = Annotated[
    IngestSourceLit,
    Field(description="Actor of record for the ingested document."),
]

# Skills are shipped as package data inside rka/skills/
_SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills"
SkillNameLiteral = Annotated[
    Literal["brain", "executor", "pi", "mcp-credentials"],
    Field(description="Packaged RKA skill name."),
]

_SKILL_ENTRYPOINTS: dict[str, dict[str, str]] = {
    "brain": {
        "path": "brain/SKILL.md",
        "prompt": "brain_skill",
        "role": "strategic Brain",
        "recommended_for": "strategy, decisions, provenance, research-map maintenance",
    },
    "executor": {
        "path": "executor/SKILL.md",
        "prompt": "executor_skill",
        "role": "implementation Executor",
        "recommended_for": "mission pickup, experiments, implementation, backbriefs",
    },
    "pi": {
        "path": "pi/SKILL.md",
        "prompt": "pi_skill",
        "role": "human PI",
        "recommended_for": "supervision, checkpoint resolution, reviewing RKA state",
    },
    "mcp-credentials": {
        "path": "mcp-credentials/SKILL.md",
        "prompt": "",
        "role": "credential setup assistant",
        "recommended_for": "MCP credential setup and validation",
    },
}


def _parse_skill_frontmatter(text: str) -> dict[str, str]:
    """Parse the simple YAML-ish frontmatter used by packaged SKILL.md files."""
    if not text.startswith("---"):
        return {}
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}
    out: dict[str, str] = {}
    for line in parts[1].splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        out[key.strip()] = value.strip().strip('"')
    return out


def _safe_skill_path(skill: str, relative_path: str | None = None) -> Path:
    """Resolve a packaged skill file without allowing path traversal."""
    entry = _SKILL_ENTRYPOINTS[skill]
    rel = relative_path or entry["path"]
    if relative_path is not None:
        rel_path = Path(relative_path)
        if rel_path.is_absolute() or ".." in rel_path.parts:
            raise ValueError("skill reference must stay inside the selected skill directory")
        rel = str(Path(skill) / rel_path)
    candidate = (_SKILLS_DIR / rel).resolve()
    root = _SKILLS_DIR.resolve()
    if root not in candidate.parents and candidate != root:
        raise ValueError("skill path escapes packaged skills directory")
    if candidate.name.startswith("._") or any(part.startswith("._") for part in candidate.parts):
        raise ValueError("AppleDouble metadata files are not readable skill assets")
    return candidate


def _read_skill(path: str) -> str:
    """Read a skill file from the packaged skills directory."""
    try:
        return (_SKILLS_DIR / path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return f"Skill file not found: {path}"


def _live_prompt_banner(role: str) -> str:
    """Prepend a stale-vs-live diagnostic banner to MCP prompt returns.

    v2.7.0.3: when an LLM session has a stale uploaded copy of a SKILL.md
    and re-fetches via the MCP prompt, the live response can look
    structurally similar to the uploaded copy. The banner gives the LLM
    a line-1 marker so it can confidently prefer the live source.
    """
    return (
        "# Live MCP prompt — sourced from "
        f"rka/skills/{role}/SKILL.md at HEAD (server v2.7.0.3)\n"
        "# If this text differs from a copy you uploaded earlier,\n"
        "# your local copy is stale — trust this one.\n"
        "\n---\n\n"
    )

# ChatGPT connector deployments: RKA_SKILL_TOOLS=1 promotes the three skill
# adapter tools (rka_list_skills / rka_read_skill / rka_start_session) to
# always_on. ChatGPT custom connectors are tool-oriented and never call
# rka_load_tools spontaneously, so deferred tools are invisible to them.
# Local stdio clients (Claude Code / Claude Desktop / Codex) leave the flag
# unset and keep the default 5-tool dispatch surface unchanged. Like
# RKA_LEGACY_TOOLS, the flag is read at module import time.
_SKILL_TOOLS_ALWAYS_ON = os.environ.get("RKA_SKILL_TOOLS", "0") == "1"

RKA_INSTRUCTIONS = """\
Research Knowledge Agent (RKA) is a structured research knowledge base shared by the
Brain, Executor, and PI. It stores journal entries, decisions, literature, missions,
claims, evidence clusters, and a provenance-linked research map.

Detailed operating guidance is available via MCP prompts:
- `brain_skill` — full Brain workflow guide (strategy, decisions, review)
- `executor_skill` — full Executor workflow guide (missions, reports, backbrief)
- `pi_skill` — PI quick reference

Use these prompts to load role-specific guidance at session start.

## Tool Surface (v2.7.0+) — Typed Dispatch Architecture
RKA ships 5 always-on tools: 3 typed dispatch tools plus 2 escape hatches into
the legacy surface.

`rka_describe("")` returns the live default operation index and reports how
many preview operations it withheld. Preview operations remain callable; they
are kept out of the default browse index so the choice space matches what is
actually in use.

- **Dispatch (always-on):**
- `rka_query(args={"operation": ..., "project_id": ..., ...})` — typed reads
    (status, context, journal, decisions, missions, literature,
    research map, etc.). Returns structured data.
- `rka_execute(args={"operation": ..., "project_id": ..., ...})` — typed
    writes and lifecycle operations (record_note, record_decision, create_mission,
    submit_report, submit_checkpoint, etc.). Returns the created/updated entity.
  - `rka_describe(operation="" | "<op_name>")` — schema lookup. With no
    argument, returns the operations index (<250 tokens). With an operation
    name, returns that operation's full inputSchema (per-branch enums,
    required fields, types).
- **Escape hatches (always-on):**
  - `rka_load_tools(names=[...])` — register legacy v2.6.x tool names into
    the live tool surface. Useful only for back-compat with old transcripts.
  - `rka_help(name=...)` — deprecated alias for `rka_describe`.

The operations use typed Pydantic models with per-branch enum constraints and
required-field enforcement at the FastMCP schema layer — illegal values
(e.g. `confidence="confirmed"`) are rejected at the inputSchema boundary
before the call goes out.

`RKA_LEGACY_TOOLS=1` restores the v2.7.0a2 surface (12 legacy always-on +
8 verbs + 3 dispatch tools). The orchestrator daemon's Brain/Executor
subprocess sets this flag to preserve TWO-TAP gate granularity at
`pi_decision_select`.

## Minimal Session Start
1. `rka_query(args={"operation": "context", "project_id": "prj_..."})` — full
   project state and recent knowledge
2. `rka_query(args={"operation": "status", "project_id": "prj_..."})` — phase,
   focus, blockers
3. `rka_query(args={"operation": "pending_maintenance", "project_id": "prj_..."})`
   — provenance gaps and stale knowledge
4. `rka_query(args={"operation": "checkpoints", "project_id": "prj_...", "filters": {"status": "open"}})`
   — unresolved blockers

To discover available operations call `rka_describe("")`; to inspect one
operation's full schema call `rka_describe("record_decision")`.

## Core Provenance Rules
- Decisions require `related_journal=[...]`
- Missions require `motivated_by_decision=...`
- Notes should set `related_decisions=[...]` and/or `related_mission=...` when applicable
- PI input must use `source="pi"` and `verbatim_input="..."` with exact wording

## High-Value Operations
- Recording: `rka_execute(args={"operation": "record_note", ...})`,
  `record_decision`, `record_literature`
- Execution: `create_mission`, `submit_checkpoint`, `submit_report`
- Research map: `review_cluster`, `resolve_contradiction`,
  `rka_query(args={"operation": "research_map", ...})`
- Retrieval: `rka_query(args={"operation": "search", ...})`, `journal`, or
  `rka_query(args={"operation": "provenance", "project_id": "prj_...", "id": "dec_..."})`

## Project Scoping
Every project-scoped operation requires `project_id` as a kwarg on every
call — there is no "active project" session state. Call
`rka_query(args={"operation": "list_projects"})` to discover available
projects (no project_id required). `rka_set_project` is a deprecated no-op
since v2.6.
"""

if _SKILL_TOOLS_ALWAYS_ON:
    RKA_INSTRUCTIONS += """\

## Skill Tools (ChatGPT connector mode — RKA_SKILL_TOOLS=1)
Role skill guides are exposed as always-on tools in this deployment. At the
START of every session, call:
- `rka_start_session(role="pi" | "brain" | "executor", project_id=None)` —
  returns the selected role's full skill guide plus a session checklist.
Then follow the returned guide. Also available:
- `rka_list_skills()` — list all packaged skill guides (brain, executor, pi,
  mcp-credentials).
- `rka_read_skill(name, reference=None)` — read a skill guide or one of its
  referenced files, e.g. rka_read_skill(name="brain",
  reference="workflows.md").
"""

mcp = LocalOnlyFastMCP("Research Knowledge Agent", instructions=RKA_INSTRUCTIONS)
# FastMCP otherwise leaves this unset and the initialize handshake falls back
# to the MCP SDK version, which makes a stale connector look like the backend's
# product version. E2 discovery reports connector/backend versions separately;
# the handshake should identify this connector build too.
mcp._mcp_server.version = __version__
API_URL = os.environ.get("RKA_API_URL", "http://127.0.0.1:9712")
API_TIMEOUT = httpx.Timeout(connect=30.0, read=120.0, write=120.0, pool=30.0)


@dataclass
class MCPSessionState:
    """Tracks state across tool calls in a single MCP stdio session."""

    tool_calls: int = 0
    session_start: str = field(
        default_factory=lambda: datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    )
    entities_created: list[dict[str, str]] = field(default_factory=list)
    decisions_made: list[str] = field(default_factory=list)
    checkpoints_raised: list[str] = field(default_factory=list)
    # Track which project_ids have already fired session_start in this
    # MCP-process lifetime. Keyed by project_id from the most recent tool
    # call; first time a project is seen, the hook fires; subsequent
    # calls for the same project skip.
    #
    # `project_id` field removed in v2.6 — every tool now takes project_id
    # explicitly, eliminating the silent-fallback-to-proj_default failure
    # mode. The RKA_PROJECT env var was removed at the same time.
    session_start_fired_for: set[str] = field(default_factory=set)

    @property
    def verbosity(self) -> str:
        return "full"


_session = MCPSessionState()


def _tick() -> MCPSessionState:
    _session.tool_calls += 1
    return _session


async def _maybe_fire_session_start(project_id: str | None) -> None:
    """Fire session_start hook once per (MCP-process, project_id) pair.

    Idempotent within the process per project. The first time a project
    is seen in this MCP-process lifetime, the hook fires; subsequent
    calls for the same project skip. All firing failures are silent —
    hooks cannot block tool execution.

    project_id=None (unscoped tools like rka_list_projects) is a no-op.
    """
    if not project_id or project_id in _session.session_start_fired_for:
        return
    _session.session_start_fired_for.add(project_id)
    try:
        async with _client(project_id) as c:
            await c.post(
                "/api/hooks/fire",
                json={
                    "event": "session_start",
                    "payload": {
                        "project_id": project_id,
                        "session_start_iso": _session.session_start,
                        "actor": "brain",
                    },
                },
            )
    except Exception:
        # Re-arm so a later tool call retries — failure mid-fire shouldn't
        # leave the session permanently un-fired.
        _session.session_start_fired_for.discard(project_id)


def _record_entity(entity_type: str, entity_id: str, summary: str) -> None:
    _session.entities_created.append(
        {"type": entity_type, "id": entity_id, "summary": summary[:80]}
    )


# ---------------------------------------------------------------------------
# v2.6.3 — Dynamic tool surface (navigator architecture)
# ---------------------------------------------------------------------------
# Claude Desktop's tool-surface filter caps the visible/usable rka tools at
# roughly 30-50 per server before tool-selection accuracy degrades. RKA has
# ~91 tools; the previous all-eager registration meant a chunk of the
# literature / claim / hook / cluster surface was effectively invisible to
# the PI cockpit at session start (empirically surfaced 2026-06-01 during
# the hyperscaler-auditing mission — PI could not call rka_add_literature
# even though it was on the wire).
#
# v2.6.3 splits the surface into two tiers:
#
#   - ALWAYS-ON  (~12 tools): the documented "Minimal Session Start" +
#     most-frequent writes + the three navigator tools below. Registered
#     at module import via mcp.tool().
#   - DEFERRED   (~79 tools): registered into `_TOOL_REGISTRY` but NOT
#     handed to mcp._tool_manager until the LLM (or PI) calls
#     `rka_load_tools(names=[...])`. After registration the navigator
#     fires `notifications/tools/list_changed` (MCP-protocol-level) so
#     both Claude Desktop AND Claude Code (incl. plugin mode) refresh
#     their surface and the new tools become callable.
#
# Cross-client compatibility:
#   - The notification mechanism is part of the base MCP protocol; both
#     Claude Desktop and Claude Code honor it.
#   - In Claude Code plugin mode the rka MCP appears under the
#     `mcp__plugin_rka_rka__` namespace; the navigator's `names` argument
#     takes the UNPREFIXED canonical form (e.g. `rka_add_literature`).
#     The harness handles namespace translation transparently.
#   - On older clients that don't honor `tools/list_changed`, deferred
#     tools simply remain invisible — same as today's effective behavior
#     for the over-the-cap tools, so this is a strict surface improvement
#     with no regression.
#
# Capability flag: `tools.listChanged: true` is advertised via the
# NotificationOptions wired into run_stdio_async / run_streamable_http_async
# at the bottom of this module.

# Categories used by rka_list_tools for browsing. Free-form strings; not
# enforced by code other than as a filter on the navigator output.
_TIER_ALWAYS_ON = "always_on"
_TIER_DEFERRED = "deferred"

# Effective tier for the ChatGPT skill adapter tools — always_on only when
# RKA_SKILL_TOOLS=1 (see flag definition above RKA_INSTRUCTIONS).
_SKILL_TOOL_TIER = _TIER_ALWAYS_ON if _SKILL_TOOLS_ALWAYS_ON else _TIER_DEFERRED


# ---------------------------------------------------------------------------
# v2.7.0a3 — Legacy-tool gating (Cloudflare 2-tool dispatch pattern)
# ---------------------------------------------------------------------------
# Per v2.7.0a3 Decision 4: when RKA_LEGACY_TOOLS is unset (default),
# legacy tools (categories OTHER than the new 'verb' / 'navigator' / 'dispatch'
# buckets) are demoted to tier='deferred' regardless of their declared tier.
# This collapses the always-on surface to the 3-tool dispatch surface
# (rka_query + rka_execute + rka_describe) plus the 4-tool always-on
# navigator escape hatch (rka_load_tools).
#
# RKA_LEGACY_TOOLS=1 restores the v2.7.0a2 surface (12 legacy baseline at
# always_on + 8 v2.7.0 verbs at always_on + 3 navigator at always_on = 23).
# Note: in 'legacy enabled' mode the v2.7.0a3 dispatch tools also remain
# always-on, so the count is higher than v2.7.0a2's 20 baseline.
#
# The flag is read at module import time — flipping it post-import has no
# effect because @tool() runs once at import. For tests that need to flip
# the flag, reimport rka.mcp.server via importlib.reload.
_LEGACY_TOOLS_ENABLED = os.environ.get("RKA_LEGACY_TOOLS", "0") == "1"

# Categories that are NEVER subject to legacy demotion in v2.7.0a3
# default mode. These cover the v2.7.0a3 dispatch surface ('dispatch')
# and the navigator tools that act as the escape hatch into the legacy
# 91 ('navigator'). The 8 v2.7.0a2 'verb' category tools are demoted
# to tier='deferred' in default mode per Decision 4 (deprecation
# in-place; removal in v2.8.0). Individual navigator tools may also
# be demoted via _DEMOTED_NAVIGATORS below.
_NON_LEGACY_CATEGORIES = frozenset({"dispatch", "navigator"})

# v2.7.0a3 Decision 5 — specific navigator tools demoted to deferred
# even though they're in the 'navigator' category. rka_load_tools is
# preserved at always_on (legitimate escape hatch); rka_list_tools
# loses always-on (rka_describe(category=...) subsumes its browsing).
# rka_help is kept as a deprecated alias for rka_describe.
_DEMOTED_NAVIGATORS = frozenset({"rka_list_tools"})

# Registry of EVERY rka_* tool — both always-on and deferred. Populated by
# the @tool() decorator at module import. Used by:
#   - rka_load_tools to look up a deferred function by name and register it
#   - rka_list_tools to render the full directory
#   - rka_help to render a single tool's signature + docstring
#
# Shape: { name: {fn, tier, category, summary, signature, registered} }
#   - registered: bool — flips True once mcp._tool_manager.add_tool has
#     been called (true at import for always-on; flipped by rka_load_tools
#     for deferred). Idempotent on repeat loads.
_TOOL_REGISTRY: dict[str, dict] = {}


def _summarize_doc(doc: str | None) -> str:
    """Pull the first non-empty line of a docstring as the one-line summary
    used by rka_list_tools."""
    if not doc:
        return ""
    for line in doc.strip().splitlines():
        s = line.strip()
        if s:
            return s
    return ""


def tool(
    *,
    tier: str = _TIER_DEFERRED,
    category: str = "general",
    always_load: bool | None = None,
):
    """Register an MCP tool and increment session state on every invocation.

    The wrapper extracts `project_id` from the call's kwargs (every
    project-scoped tool takes it as a kwarg-only parameter post-v2.6) and
    threads it into the session_start hook firing. Unscoped tools that
    don't carry project_id pass None to the hook which is a no-op there.

    v2.6.3 — `tier` controls whether the tool registers at module import
    (`always_on`) or only after a navigator call to `rka_load_tools`
    (`deferred`, the default). `category` is metadata for rka_list_tools.
    Every tool is recorded in `_TOOL_REGISTRY` regardless of tier so the
    navigator can list/help/load it.

    v2.7.0a2 — `always_load` controls the Anthropic-namespaced
    `anthropic/alwaysLoad` hint that pins a tool into Claude Code's
    per-turn toolset (bypassing ToolSearch's ranking). When None
    (default), tier='always_on' tools get always_load=True, and
    tier='deferred' tools get always_load=False. Setting always_load
    explicitly overrides the tier-inferred default. The hint surfaces
    in the MCP tool descriptor's `_meta` field; Claude Code v2.1.121+
    recognises it. Older clients ignore the opaque metadata.

    v2.7.0a3 — Legacy-tool gating: when ``RKA_LEGACY_TOOLS`` env var is
    unset (the default), any tool whose ``category`` is NOT in the
    ``_NON_LEGACY_CATEGORIES`` set (verb / navigator / dispatch) is
    demoted from tier='always_on' to tier='deferred' regardless of the
    caller's declared tier. ``always_load`` is also coerced to False
    so the demoted tool doesn't carry the alwaysLoad hint. Setting
    ``RKA_LEGACY_TOOLS=1`` restores the v2.7.0a2 baseline (legacy 12 at
    always_on, 79 at deferred).
    """

    # v2.7.0a3 — Apply legacy-tool gating BEFORE always_load defaulting
    # so the alwaysLoad hint follows the resolved tier. The decorator
    # demotes by function name (for specific navigator demotions per
    # Decision 5) in addition to the category-based bulk demotion.
    def _resolve_tier(fn_name: str, declared_tier: str) -> str:
        """v2.7.0a3 tier resolution: demote legacy categories + named
        deprecations when RKA_LEGACY_TOOLS env flag unset."""
        if _LEGACY_TOOLS_ENABLED or declared_tier != _TIER_ALWAYS_ON:
            return declared_tier
        if fn_name in _DEMOTED_NAVIGATORS:
            return _TIER_DEFERRED
        if category == "skills" and _SKILL_TOOLS_ALWAYS_ON:
            # ChatGPT connector mode — skill adapters declared always_on
            # via _SKILL_TOOL_TIER are exempt from legacy demotion.
            return declared_tier
        if category not in _NON_LEGACY_CATEGORIES:
            return _TIER_DEFERRED
        return declared_tier

    def decorator(func):
        import inspect as _inspect

        @wraps(func)
        async def wrapper(*args, **kwargs):
            _tick()
            result = await func(*args, **kwargs)
            # Fire session_start hook AFTER the tool body. The hook is
            # keyed on project_id (kwarg-only on every project-scoped
            # tool); unscoped tools pass None and the hook no-ops.
            await _maybe_fire_session_start(kwargs.get("project_id"))
            return result

        name = func.__name__
        # v2.7.0a3 — Resolve effective tier (env-flag + name + category
        # gating). The original `tier` may be overridden DOWN to deferred
        # for legacy tools when RKA_LEGACY_TOOLS != "1".
        effective_tier = _resolve_tier(name, tier)
        # Resolve always_load default from the EFFECTIVE tier when the
        # caller didn't override. (v2.7.0a2 inferred from declared tier;
        # v2.7.0a3 must infer from the env-flag-resolved effective tier
        # so demoted tools don't carry the alwaysLoad hint into ToolSearch
        # ranking.)
        if always_load is None:
            nonlocal_always_load = effective_tier == _TIER_ALWAYS_ON
        else:
            nonlocal_always_load = always_load
        if effective_tier == _TIER_DEFERRED:
            # Defensive: even if an explicit always_load=True was passed,
            # demoted tools must not pin themselves into ToolSearch's
            # Always-Available pool — that would defeat the v2.7.0a3
            # design.
            nonlocal_always_load = False
        try:
            sig_str = str(_inspect.signature(func))
        except (TypeError, ValueError):  # pragma: no cover
            sig_str = "(...)"
        _TOOL_REGISTRY[name] = {
            "fn": wrapper,
            "tier": effective_tier,
            "category": category,
            "summary": _summarize_doc(func.__doc__),
            "signature": sig_str,
            "docstring": (func.__doc__ or "").strip(),
            "registered": False,
            "always_load": bool(nonlocal_always_load),
        }
        if effective_tier == _TIER_ALWAYS_ON:
            # v2.7.0a2 — pin always-on tools into Claude Code's per-turn
            # toolset via the Anthropic-namespaced `_meta` hint.
            meta = (
                {"anthropic/alwaysLoad": True} if nonlocal_always_load else None
            )
            if meta is not None:
                registered = mcp.tool(meta=meta)(wrapper)
            else:
                registered = mcp.tool()(wrapper)
            _TOOL_REGISTRY[name]["registered"] = True
            return registered
        # Deferred — return the wrapped fn so module-level references still
        # work (tests sometimes import the decorated callable directly).
        return wrapper

    return decorator


def _client(project_id: str | None = None) -> httpx.AsyncClient:
    """Build an httpx client scoped to a project via X-RKA-Project header.

    project_id is REQUIRED for every project-scoped tool (writes + scoped reads);
    pass None ONLY when calling project-list / project-create / health endpoints.
    The MCP layer surfaces missing project_id as a clear error to the LLM/caller
    so the silent-fallback-to-proj_default failure mode is structurally
    impossible.

    Disable HTTP/1.1 keep-alive entirely (`max_keepalive_connections=0`). The
    `async with _client() as c:` pattern below creates a fresh client per tool
    call, but httpx's default `Limits` keep idle connections in a pool that
    can wedge in `CLOSE_WAIT` after the daemon closes its side — particularly
    on macOS Docker Desktop's bridge network. The next tool call's
    `async with _client()` then blocks on a half-dead pool entry until the OS
    times it out (well past Claude Desktop's 4-min tool-call ceiling).
    Empirically observed: after several successful calls, both the host
    `rka` and `rka-orchestrator-mcp` subprocesses began silently hanging on
    further requests; a freshly-spawned subprocess returned the same calls
    instantly. Per-call TCP handshake cost on localhost is ~1ms, negligible
    against the multi-second LLM round-trip the calls are part of.
    """
    headers = {"X-RKA-Actor": "executor"}
    if project_id:
        headers["X-RKA-Project"] = project_id
    return httpx.AsyncClient(
        base_url=API_URL,
        timeout=API_TIMEOUT,
        headers=headers,
        limits=httpx.Limits(max_keepalive_connections=0, max_connections=10),
    )


def _format_validation_detail(detail: list) -> str:
    """Render FastAPI's structured 422 detail (list of per-field errors)
    into a compact human-readable form.

    FastAPI's default 422 body shape is:
      {"detail": [
        {"loc": ["body", "confidence"], "msg": "Input should be ...",
         "type": "literal_error", "input": "confirmed", "ctx": {...}},
        ...
      ]}

    Pre-v2.6.2 this list was stringified via str(...) which produced
    repr-style output that buried the actionable info (field name +
    offending value + expected). v2.6.2 renders each entry as
    `<loc-path>=<input!r> not in <ctx-or-msg>` so the Brain LLM sees
    which field needs fixing.

    Closes the Phase-X²' polish Layer 9 consumer-side gap on the MCP
    binary — mirrors the orchestrator's RestMCPClient enrichment.
    """
    parts: list[str] = []
    for item in detail:
        if not isinstance(item, dict):
            parts.append(str(item))
            continue
        loc = item.get("loc") or []
        # Drop the leading 'body' prefix; it's noise for caller-facing
        # messages and 'body.foo' reads less cleanly than 'foo'.
        if loc and loc[0] in ("body", "query", "path", "header"):
            loc = loc[1:]
        loc_str = ".".join(str(x) for x in loc) if loc else "<root>"
        msg = item.get("msg") or ""
        observed = item.get("input")
        observed_repr = (
            f"={observed!r}" if observed is not None else ""
        )
        ctx = item.get("ctx") or {}
        expected = ctx.get("expected")
        if expected:
            parts.append(f"{loc_str}{observed_repr}: {msg} (allowed: {expected})")
        else:
            parts.append(f"{loc_str}{observed_repr}: {msg}")
    return "; ".join(parts)


def _raise_with_detail(r: httpx.Response) -> None:
    """Raise a structured error from a non-success HTTP response.

    v2.6.2 — enriched rendering of FastAPI 422 validation errors so
    the LLM caller sees `field=<value>: <msg>` per offending field
    instead of a repr'd list. Mirrors the Phase-X² polish
    orchestrator-side enrichment for consistent diagnostic surface.
    Non-422 errors (404 / 409 / 500) continue to use the string
    `detail` field if present, falling back to raw response text.
    """
    if r.is_success:
        return
    detail: object
    try:
        body = r.json()
    except Exception:  # noqa: BLE001 — body might not be JSON
        body = None
    if isinstance(body, dict) and "detail" in body:
        raw = body["detail"]
        if isinstance(raw, list):
            # Structured Pydantic-validation detail (FastAPI 422 path).
            detail = _format_validation_detail(raw)
        else:
            detail = raw
    else:
        detail = r.text
    raise Exception(f"API error {r.status_code}: {detail}")


# ============================================================
# Knowledge Management
# ============================================================

@tool(tier="always_on", category="core")
async def rka_add_note(
    content: str,
    type: str = "note",
    source: SourceLiteral = "executor",
    phase: str | None = None,
    verbatim_input: str | None = None,
    related_decisions: list[str] | None = None,
    related_literature: list[str] | None = None,
    related_mission: str | None = None,
    supersedes: str | None = None,
    confidence: ConfidenceLiteral = "hypothesis",
    importance: ImportanceLiteral = "normal",
    tags: list[str] | None = None,
    summary: str | None = None,
    status: str | None = None,
    pinned: bool | None = None,
    *,
    project_id: str,
    capture_mode: JournalCaptureModeLit = "unknown",
) -> str:
    """PRIMARY FIELD: content. Add a research journal entry.

    Args:
        content: The note content (PRIMARY FIELD — Brain's analysis when recording PI input)
        type: Entry type — note | log | directive (legacy types like finding/insight/methodology are auto-mapped)
        source: Who created this — brain | executor | pi | llm | web_ui | system
        phase: Research phase (uses current if omitted)
        verbatim_input: PI's exact words when recording PI input (preserves intellectual attribution)
        related_decisions: Decision IDs this note relates to
        related_literature: Literature IDs this note references
        related_mission: Mission ID this note belongs to
        supersedes: ID of an older note this one replaces
        confidence: hypothesis | tested | verified | superseded | retracted
        importance: critical | high | normal | low
        tags: Optional tags for categorization (e.g. ["anomaly-detection", "methodology"])
        summary: Optional short summary of the entry
        status: draft | active | superseded | retracted (defaults to active)
        pinned: Whether to pin the entry for quick access
        capture_mode: unknown (default), raw_capture, or agent_restatement. Raw creation
            retains supplied verbatim_input or snapshots content exactly when absent.
    """
    # v2.7.0.7 — summary/status/pinned now forwarded. NoteCreate accepts them
    # but the adapter previously omitted them, so the RecordNoteArgs typed
    # surface advertised fields that were silently dropped before the POST.
    async with _client(project_id) as c:
        body = {
            "content": content, "type": type, "source": source,
            "phase": phase, "verbatim_input": verbatim_input,
            "related_decisions": related_decisions,
            "related_literature": related_literature,
            "related_mission": related_mission, "supersedes": supersedes,
            "confidence": confidence, "importance": importance,
            "tags": tags, "summary": summary, "status": status,
            "pinned": pinned,
            "capture_mode": capture_mode,
        }
        r = await c.post("/api/notes", json={k: v for k, v in body.items() if v is not None})
        _raise_with_detail(r)
        d = r.json()
        _record_entity("journal", d["id"], content)
        return f"Created {d['id']} [{d['type']}] confidence={d['confidence']}"


@tool(category="journal")
async def rka_update_note(
    id: str,
    content: str | None = None,
    summary: str | None = None,
    type: str | None = None,
    confidence: str | None = None,
    importance: str | None = None,
    status: str | None = None,
    pinned: bool | None = None,
    verbatim_input: str | None = None,
    related_decisions: list[str] | None = None,
    related_literature: list[str] | None = None,
    related_mission: str | None = None,
    tags: list[str] | None = None,
    phase: str | None = None,
    source: str | None = None,
    *,
    project_id: str,
) -> str:
    """PRIMARY FIELD: id (plus content for the body update).
    Update an existing journal entry.

    Args:
        id: The note ID to update (PRIMARY FIELD — required addressing key)
        content: New content (the canonical body field for the update)
        summary: New short summary
        type: New type — note | log | directive
        confidence: New confidence level — hypothesis | tested | verified | superseded | retracted
        importance: New importance level — critical | high | normal | low
        status: New lifecycle status — draft | active | superseded | retracted
        pinned: Whether the journal entry is pinned
        verbatim_input: Deprecated for updates; use rka_correct_note_attribution
        related_decisions: Decision IDs this note relates to
        related_literature: Literature IDs this note references
        related_mission: Mission ID this note belongs to
        tags: Tags for categorization
        phase: Research phase (e.g. planning | development | design | experiment)
        source: Deprecated for updates; use rka_correct_note_attribution
    """
    async with _client(project_id) as c:
        body = {
            "content": content, "summary": summary, "type": type,
            "confidence": confidence,
            "importance": importance, "status": status, "pinned": pinned,
            "verbatim_input": verbatim_input,
            "related_decisions": related_decisions,
            "related_literature": related_literature,
            "related_mission": related_mission, "tags": tags,
            "phase": phase, "source": source,
        }
        filtered = {k: v for k, v in body.items() if v is not None}
        r = await c.put(f"/api/notes/{id}", json=filtered)
        _raise_with_detail(r)
        changed = list(filtered.keys())
        return f"Updated {id} fields={','.join(changed)}"


@tool(category="journal")
async def rka_correct_note_attribution(
    id: str, expected_revision: int, request_id: str, actor: str,
    reason: str, source: str, verbatim_input: str | None, *, project_id: str,
    capture_mode: JournalCaptureModeLit | None = None,
) -> str:
    """Correct source/original with an explicit reason and attribution revision.

    Actor is a caller declaration, not authentication. Exact retries return
    the original immutable correction, even if later corrections exist.
    """
    from rka.models.journal import JournalAttributionCorrection

    data = JournalAttributionCorrection(
        expected_revision=expected_revision, request_id=request_id, actor=actor,
        reason=reason, source=source, verbatim_input=verbatim_input,
        capture_mode=capture_mode,
    )
    async with _client(project_id) as c:
        response = await c.post(f"/api/notes/{id}/attribution-corrections", json=data.model_dump())
        _raise_with_detail(response)
        return json.dumps(response.json(), ensure_ascii=False)


@tool(category="journal")
async def rka_get_note_attribution_history(
    id: str, after_revision: int = 0, limit: int = 50, *, project_id: str,
) -> str:
    """Read project-scoped attribution corrections in ascending revision order."""
    async with _client(project_id) as c:
        response = await c.get(
            f"/api/notes/{id}/attribution-history",
            params={"after_revision": after_revision, "limit": limit},
        )
        _raise_with_detail(response)
        return json.dumps(response.json(), ensure_ascii=False)


@tool(category="literature")
async def rka_add_literature(
    title: str,
    authors: list[str] | None = None,
    year: int | None = None,
    venue: str | None = None,
    doi: str | None = None,
    url: str | None = None,
    bibtex: str | None = None,
    abstract: str | None = None,
    key_findings: list[str] | None = None,
    relevance: str | None = None,
    pdf_path: str | None = None,
    added_by: str = "brain",
    *,
    project_id: str,
    status: str | None = None,
    tags: list[str] | None = None,
    related_decisions: list[str] | None = None,
) -> str:
    """Add a literature entry (paper, article, etc.).

    Args:
        title: Paper title
        authors: Author list
        year: Publication year
        venue: Conference or journal name
        doi: Digital Object Identifier
        url: URL to the paper
        bibtex: Raw BibTeX entry
        abstract: Paper abstract
        key_findings: List of key findings
        relevance: How it relates to this project
        pdf_path: Local path to PDF
        added_by: Who added this — brain | executor | pi
        status: Initial reading status (defaults to to_read)
        tags: Free-form tags
        related_decisions: Decision IDs informed by this reference
    """
    async with _client(project_id) as c:
        body = {
            "title": title, "authors": authors, "year": year, "venue": venue,
            "doi": doi, "url": url, "bibtex": bibtex, "abstract": abstract,
            "key_findings": key_findings, "relevance": relevance,
            "pdf_path": pdf_path, "added_by": added_by,
            "status": status, "tags": tags, "related_decisions": related_decisions,
        }
        r = await c.post("/api/literature", json={k: v for k, v in body.items() if v is not None})
        _raise_with_detail(r)
        d = r.json()
        _record_entity("literature", d["id"], d["title"])
        return f"Created {d['id']}: {d['title']}"


@tool(category="literature")
async def rka_update_literature(
    id: str,
    title: str | None = None,
    authors: list[str] | None = None,
    year: int | None = None,
    venue: str | None = None,
    doi: str | None = None,
    url: str | None = None,
    bibtex: str | None = None,
    pdf_path: str | None = None,
    abstract: str | None = None,
    status: str | None = None,
    key_findings: list[str] | None = None,
    methodology_notes: str | None = None,
    relevance: str | None = None,
    relevance_score: float | None = None,
    related_decisions: list[str] | None = None,
    notes: str | None = None,
    tags: list[str] | None = None,
    *,
    project_id: str,
) -> str:
    """Update a literature entry. Only provide fields you want to change.

    Args:
        id: Literature ID
        title: Updated paper title
        authors: Updated author list
        year: Updated publication year
        venue: Updated conference or journal name
        doi: Updated DOI
        url: Updated URL
        bibtex: Updated raw BibTeX entry
        pdf_path: Local path to PDF file
        abstract: Updated paper abstract
        status: to_read | reading | read | cited | excluded
        key_findings: Updated key findings list
        methodology_notes: Notes on methodology used in the paper
        relevance: How it relates to this project
        relevance_score: 0.0-1.0 relevance score
        related_decisions: Decision IDs this literature informs
        notes: Researcher annotations
        tags: Tags for categorization
    """
    async with _client(project_id) as c:
        body = {
            "title": title, "authors": authors, "year": year,
            "venue": venue, "doi": doi, "url": url, "bibtex": bibtex,
            "pdf_path": pdf_path, "abstract": abstract, "status": status,
            "key_findings": key_findings, "methodology_notes": methodology_notes,
            "relevance": relevance, "relevance_score": relevance_score,
            "related_decisions": related_decisions, "notes": notes, "tags": tags,
        }
        r = await c.put(f"/api/literature/{id}", json={k: v for k, v in body.items() if v is not None})
        _raise_with_detail(r)
        return f"Updated {id}"


@tool(category="literature")
async def rka_link_literature_to_zotero(
    id: str,
    *,
    project_id: str,
    zotero_key: str | None = None,
) -> dict:
    """Resolve a literature entry to its Zotero item key for full-text access.

    v2.7.0.2 (Bug 3): when ``zotero_key`` is supplied, the linker
    validates that key exists in the configured library via a direct GET
    on ``/items/<key>`` and persists it without running the five
    fuzzy-match strategies. This is the manual-override path for cases
    the matcher can't anchor on — standalone PDF attachments, working
    papers without bibliographic metadata, sector reports without DOIs.

    Default path (zotero_key omitted): tries five strategies in order:
    DOI -> arXiv ID -> URL -> ISBN -> title+author+year fuzzy match. On
    a confident match, the link is persisted (zotero_item_key +
    zotero_match_method). On weak/multiple matches, returns candidates
    for PI confirmation.

    Returns:
      {"zotero_item_key": "ABC12345", "matched_by": "explicit_key",
       "confidence": 1.0}                                              — explicit key, persisted
      {"zotero_item_key": "ABC12345", "matched_by": "doi"}              — strong match, persisted
      {"zotero_item_key": "ABC12345", "matched_by": "title_author_year",
       "confidence": 0.96}                                              — fuzzy but ratified
      {"zotero_item_key": null, "reason": "no_match"}                   — paper not in library; emit FULL-TEXT REQUEST
      {"zotero_item_key": null, "reason": "multiple_matches_below_threshold",
       "candidates": [...]}                                             — ask PI to pick
      {"zotero_item_key": null, "reason": "explicit_key_not_found: KEY"} — supplied key doesn't exist
      {"zotero_item_key": null, "reason": "zotero_not_configured"}      — creds missing

    Once linked, use the zotero MCP server's `zotero_get_fulltext(item_key=...)`
    tool to read the paper's full text for grounded claim extraction.

    Args:
        id: Literature ID (lit_...).
        project_id: RKA project ID (prj_...) — required per the v2.6
          project-scoping contract. Without it the REST layer's
          `get_scoped_literature_service` falls back to the sentinel
          `proj_default`, which mismatches the literature row's actual
          owning project. `LiteratureService.get(lit_id)` then runs
          `WHERE id=? AND project_id='proj_default'`, returns None, and
          the REST handler raises HTTPException(404, "Literature lit_...
          not found") — a misleading 404 that names the lit_ id even
          though the underlying row is intact. This kwarg threads
          `X-RKA-Project` through `_client(project_id)` so the scoping
          resolves correctly. Empirical regression: introduced in
          commit 6e7a2d6 (Phase-3.4 zotero linker, 2026-05-28) which
          landed AFTER PR #32 (v2.6 contract) without the kwarg.
        zotero_key: Optional explicit Zotero item key (8-char alnum).
          When supplied, bypasses fuzzy matching.
    """
    body = {"zotero_key": zotero_key} if zotero_key else None
    async with _client(project_id) as c:
        r = await c.post(f"/api/literature/{id}/link_zotero", json=body)
        _raise_with_detail(r)
        return r.json()


@tool(category="decision")
async def rka_add_decision(
    question: str,
    phase: str,
    decided_by: DecidedByLiteral,
    options: list[dict] | None = None,
    chosen: str | None = None,
    rationale: str | None = None,
    parent_id: str | None = None,
    related_literature: list[str] | None = None,
    related_journal: list[str] | None = None,
    kind: DecisionKindLiteral = "decision",
    assumptions: list[str] | None = None,
    related_missions: list[str] | None = None,
    tags: list[str] | None = None,
    status: str | None = None,
    *,
    project_id: str,
) -> str:
    """PRIMARY FIELD: question. Add a decision node to the research
    decision tree.

    Args:
        question: The decision question (PRIMARY FIELD — NOT `content`)
        phase: Research phase
        decided_by: pi | brain | executor
        options: List of options [{label, description}]
        chosen: Label of chosen option
        rationale: Why this was chosen
        parent_id: Parent decision ID for tree structure
        related_literature: Literature IDs informing this decision
        related_journal: Journal entry IDs that justify this decision (creates justified_by links)
        kind: research_question | design_choice | decision | operational
        assumptions: List of assumptions this decision rests on (stored as JSON)
        related_missions: Mission IDs related to this decision
        tags: Free-form tags
        status: active | abandoned | superseded | merged | revisit
    """
    # v2.7.0.7 — related_missions/tags/status are now forwarded (previously
    # the create path silently dropped them while the supersede path kept
    # them — an asymmetry that lost provenance + tags on plain creates).
    async with _client(project_id) as c:
        body = {
            "question": question, "phase": phase, "decided_by": decided_by,
            "options": options, "chosen": chosen, "rationale": rationale,
            "parent_id": parent_id, "related_literature": related_literature,
            "related_journal": related_journal, "kind": kind,
            "assumptions": assumptions, "related_missions": related_missions,
            "tags": tags, "status": status,
        }
        r = await c.post("/api/decisions", json={k: v for k, v in body.items() if v is not None})
        _raise_with_detail(r)
        d = r.json()
        _session.decisions_made.append(d["id"])
        return f"Created decision {d['id']}: {d['question'][:80]}"


@tool(category="decision")
async def rka_update_decision(
    id: str,
    status: str | None = None,
    chosen: str | None = None,
    rationale: str | None = None,
    abandonment_reason: str | None = None,
    kind: str | None = None,
    related_journal: list[str] | None = None,
    parent_id: str | None = None,
    related_literature: list[str] | None = None,
    related_missions: list[str] | None = None,
    phase: str | None = None,
    tags: list[str] | None = None,
    assumptions: list[str] | None = None,
    *,
    project_id: str,
) -> str:
    """Update a decision node.

    Args:
        id: Decision ID
        status: active | abandoned | superseded | merged | revisit
        chosen: Updated chosen option
        rationale: Updated rationale
        abandonment_reason: Why this branch was abandoned
        kind: research_question | design_choice | decision | operational
        related_journal: Journal entry IDs that justify this decision
        parent_id: Parent decision ID for tree structure (set to "" to clear)
        related_literature: Literature IDs informing this decision
        related_missions: Mission IDs related to this decision
        phase: Research phase
        tags: Tags for categorization
        assumptions: List of assumptions this decision rests on (stored as JSON)
    """
    async with _client(project_id) as c:
        body = {
            "status": status, "chosen": chosen, "rationale": rationale,
            "abandonment_reason": abandonment_reason, "kind": kind,
            "related_journal": related_journal, "parent_id": parent_id,
            "related_literature": related_literature,
            "related_missions": related_missions, "phase": phase, "tags": tags,
            "assumptions": assumptions,
        }
        filtered = {k: v for k, v in body.items() if v is not None}
        r = await c.put(f"/api/decisions/{id}", json=filtered)
        _raise_with_detail(r)
        changed = list(filtered.keys())
        return f"Updated decision {id} fields={','.join(changed)}"


# ============================================================
# Multi-Choice Decision UX (v2.2) — strip-then-re-inject protocol
# ============================================================
# See skills/brain/decision_ux.md for the full protocol spec. These tools
# implement the persistence + validation + ranking portions; the Brain is
# responsible for Stage 1 option generation with PI preference stripped.


@tool(category="decision")
async def rka_present_decision(
    decision_id: str,
    confirmation_brief: str,
    options: list[dict],
    pi_preference: str | None = None,
    *,
    project_id: str,
) -> str:
    """Present a multi-choice decision to the PI via the v2.2 strip-then-re-inject protocol.

    Implements decision_ux.md's three-stage pipeline as a persistence + validation
    + ranking layer. The Brain generates 5 candidate options externally with
    PI preference stripped from the generation context; this tool then:
      - Stage 1 (validation): substring-checks options for PI-preference leakage
        (trip-wire; semantic leaks must be caught by Brain discipline).
      - Stage 2 (pruning): persists all 5, computes Pareto dominance on
        (confidence_numeric, effort_time, effort_reversibility), sets dominated_by
        on dominated rows. Non-dominated survivors proceed.
      - Stage 3 (ranking): recommendation goes to the survivor with highest
        confidence_numeric (proxy for "least convincing opposing-critique").

    Refuses if the decision already has options or a recorded PI selection —
    use rka_supersede_decision to revisit.

    Args:
        decision_id: Existing decision to attach options to.
        confirmation_brief: The Brain's restatement of PI intent (recorded for audit).
        options: List of 5 DecisionOptionCreate-shaped dicts. Brain-authored.
        pi_preference: PI's stated preference text; used only for stage-1
            strip check + stage-3 audit marker. Pass None if no preference known.

    Returns:
        JSON string summarizing the presentation: presented_option_ids,
        recommended_option_id, presentation_method, and the markdown-rendered
        options block for the Brain to show the PI.
    """
    async with _client(project_id) as c:
        # Guard: refuse if decision doesn't exist, has existing options,
        # or already recorded a PI selection.
        r = await c.get(f"/api/decisions/{decision_id}")
        if r.status_code == 404:
            return json.dumps({"error": "decision_not_found", "decision_id": decision_id})
        _raise_with_detail(r)
        dec = r.json()
        if dec.get("pi_selected_option_id") or dec.get("pi_override_rationale"):
            return json.dumps({
                "error": "decision_already_selected",
                "message": "Decision already presented; use rka_supersede_decision to create a new decision with fresh options.",
            })
        r = await c.get(f"/api/decisions/{decision_id}/options")
        _raise_with_detail(r)
        if r.json():
            return json.dumps({
                "error": "decision_already_presented",
                "message": "Decision already presented; use rka_supersede_decision to create a new decision with fresh options.",
            })

        # Stage 1 — strip check. Substring match is advisory per decision_ux.md;
        # Brain discipline is the semantic guarantee.
        if pi_preference:
            pref_lower = pi_preference.strip().lower()
            if pref_lower:
                for idx, opt in enumerate(options):
                    text_blob = " ".join(str(opt.get(k, "")) for k in (
                        "label", "summary", "justification", "explanation",
                    )).lower()
                    if pref_lower in text_blob:
                        return json.dumps({
                            "error": "pi_preference_leaked_into_generation",
                            "message": f"Option index {idx} contains the PI preference text. "
                                       "Stage 1 failed; regenerate with preference stripped.",
                            "offending_option_index": idx,
                        })

        # Stage 2a — persist all 5 via bulk create.
        r = await c.post(f"/api/decisions/{decision_id}/options/bulk", json=options)
        _raise_with_detail(r)
        created = r.json()
        if len(created) < 2:
            return json.dumps({
                "error": "insufficient_options",
                "message": f"Only {len(created)} option(s) persisted; need at least 2 for Pareto to be meaningful.",
                "presented_option_ids": [o["id"] for o in created],
            })

        # Stage 2b — Pareto dominance. Compute locally, then PUT dominated_by per row.
        from rka.services.pareto import compute_dominance
        dominance = compute_dominance(created)
        for i, dominator_idx in dominance.items():
            if dominator_idx is not None:
                dominator_id = created[dominator_idx]["id"]
                r = await c.put(
                    f"/api/decision_options/{created[i]['id']}/dominated_by",
                    json={"dominator_id": dominator_id},
                )
                _raise_with_detail(r)

        # Stage 2c — select survivors. If >3 non-dominated, sort by confidence
        # desc and keep top 3. If <3, proceed with whatever remains and note.
        survivors = [o for i, o in enumerate(created) if dominance[i] is None]
        if len(survivors) > 3:
            survivors.sort(key=lambda o: float(o.get("confidence_numeric", 0.0)), reverse=True)
            survivors = survivors[:3]
        elif len(survivors) == 1:
            # Fall through: present the single survivor; Brain/PI decide what this means.
            pass

        # Stage 3 — ranking. Proxy for "weakest opposing-critique": highest
        # confidence_numeric among survivors.
        recommended = max(survivors, key=lambda o: float(o.get("confidence_numeric", 0.0)))
        r = await c.put(f"/api/decision_options/{recommended['id']}/recommend")
        _raise_with_detail(r)

        # Persist presentation_method on the decisions row.
        r = await c.put(
            f"/api/decisions/{decision_id}",
            json={"presentation_method": "markdown_fallback"},
        )
        # The PUT endpoint may not accept presentation_method via the normal
        # DecisionUpdate model — attempt, tolerate 422. Future work to wire it cleanly.

        # Render markdown presentation block.
        lines = [
            f"# Decision: {dec.get('question', '(no question recorded)')}",
            "",
            "## Confirmation Brief",
            confirmation_brief,
            "",
        ]
        if pi_preference:
            lines.extend(["## PI Preference (re-injected at ranking)", pi_preference, ""])
        lines.append(f"## Surviving Options ({len(survivors)})")
        lines.append("")
        for o in survivors:
            marker = "⭐ RECOMMENDED" if o["id"] == recommended["id"] else ""
            lines.extend([
                f"### {o['label']} {marker}".strip(),
                f"_Confidence: {o.get('confidence_verbal')}_ "
                f"({o.get('confidence_numeric')}, {o.get('confidence_evidence_strength')} evidence) "
                f"· _Effort: {o.get('effort_time')} / {o.get('effort_reversibility')}_",
                "",
                o.get("summary", ""),
                "",
                f"**Justification**: {o.get('justification')}",
                "",
                f"**Explanation**: {o.get('explanation')}",
                "",
                "**Pros**:",
                *[f"- {p}" for p in o.get("pros") or []],
                "",
                "**Cons**:",
                *[f"- {c}" for c in o.get("cons") or []],
                "",
                "**Known unknowns**:",
                *[f"- {u}" for u in o.get("confidence_known_unknowns") or []],
                "",
            ])
        lines.append(
            "Call `rka_record_pi_selection(decision_id, selected_option_id=...)` "
            "to record the PI's choice, or with `override_rationale` for an "
            "escape-hatch response."
        )

        return json.dumps({
            "decision_id": decision_id,
            "presented_option_ids": [o["id"] for o in survivors],
            "recommended_option_id": recommended["id"],
            "presentation_method": "markdown_fallback",
            "dominated_option_ids": [o["id"] for i, o in enumerate(created) if dominance[i] is not None],
            "presentation_markdown": "\n".join(lines),
        }, indent=2)


@tool(category="decision")
async def rka_record_pi_selection(
    decision_id: str,
    selected_option_id: str | None = None,
    override_rationale: str | None = None,
    *,
    project_id: str,
) -> str:
    """Record the PI's response to a presented decision.

    Pass at least one of selected_option_id (PI chose one of the surviving
    options) or override_rationale (PI invoked an escape hatch). Both may be
    set together when the PI selects an option AND provides a rationale —
    the typical override-of-recommendation case. Neither set is rejected.

    Args:
        decision_id: The decision being responded to.
        selected_option_id: If the PI selected one of the presented options,
            its dop_... ID.
        override_rationale: If the PI used an escape hatch, one of the four
            canonical values per decision_ux.md: "defer", "reframe", "reject_all",
            or "custom: <free text>". May also accompany selected_option_id
            to record the rationale for choosing that option over the
            recommended one.
    """
    async with _client(project_id) as c:
        r = await c.put(
            f"/api/decisions/{decision_id}/pi_selection",
            json={
                "selected_option_id": selected_option_id,
                "override_rationale": override_rationale,
            },
        )
        _raise_with_detail(r)
        return json.dumps(r.json(), indent=2)


@tool(category="decision")
async def rka_record_outcome(
    decision_id: str,
    outcome: str,
    outcome_details: str | None = None,
    recorded_by: str = "pi",
    *,
    project_id: str,
) -> str:
    """Record what actually happened after a decision played out.

    Writes to the calibration_outcomes table (migration 018). The Brain or PI
    should call this once a decision's real-world outcome becomes known —
    days, weeks, or months after rka_present_decision was called. Multiple
    outcomes per decision are allowed (revisions); the most recent is what
    Brier/ECE use.

    Refuses on decisions with no recorded PI selection — there's nothing to
    measure success against.

    Args:
        decision_id: The decision being scored.
        outcome: One of "succeeded" | "failed" | "mixed" | "unresolved".
        outcome_details: Optional free text explaining what happened.
        recorded_by: "pi" by default; use "brain" if the Brain is recording
            on the PI's behalf based on a directive.

    Returns:
        JSON string with the created calibration_outcomes row.
    """
    async with _client(project_id) as c:
        r = await c.post(
            f"/api/decisions/{decision_id}/outcomes",
            json={
                "outcome": outcome,
                "outcome_details": outcome_details,
                "recorded_by": recorded_by,
            },
        )
        if r.status_code == 400:
            return json.dumps({
                "error": "decision_not_resolved",
                "message": r.json().get("detail", "Decision has no recorded PI selection"),
                "decision_id": decision_id,
            })
        if r.status_code == 404:
            return json.dumps({
                "error": "decision_not_found",
                "decision_id": decision_id,
            })
        _raise_with_detail(r)
        return json.dumps(r.json(), indent=2)


@tool(category="decision")
async def rka_get_calibration_metrics(*, project_id: str) -> str:
    """Get calibration metrics for the active project.

    Returns both families of metrics side-by-side:

    **Outcome-based** (Brier / ECE): how well the Brain's ``confidence_numeric``
    values match actual outcomes. Lower Brier / lower ECE = better calibration.
    ``metrics_available=False`` with a warning when N<5 outcomes.

    **Selection-based** (override rates): what proportion of decisions the PI
    engaged with rather than rubber-stamping. Three rates:
    ``override_rate`` (selected != recommended, including escape hatches),
    ``escape_hatch_rate`` (PI used defer/reframe/reject_all/custom), and
    ``near_miss_rate`` (PI picked a different non-dominated survivor —
    engaged disagreement). ``override_metrics_available=False`` when
    ``qualifying_decisions<5``. See ``skills/brain/decision_ux.md`` for the
    pattern taxonomy (rubber-stamp / alternate / escape / mixed).

    Takes no arguments — operates on the active project from the MCP session.
    """
    async with _client(project_id) as c:
        r = await c.get("/api/calibration/metrics")
        _raise_with_detail(r)
        return json.dumps(r.json(), indent=2)


# ============================================================
# Hook System v1 (Mission 2 Phase B)
# ============================================================
# 8 tools: registration (add/list/enable/disable/delete) + audit
# (executions) + notifications queue (list/clear). Per
# Historical SQL and scheduled-only MCP handlers remain readable but are
# unsupported. Core only registers/enables/executes brain_notify handlers.


@tool(category="hooks")
async def rka_add_hook(
    event: str,
    handler_type: str,
    handler_config: dict,
    name: str,
    enabled: bool = True,
    created_by: str = "pi",
    *,
    project_id: str,
) -> str:
    """Register a lifecycle hook for the active project.

    Args:
        event: One of session_start | post_journal_create | post_claim_extract
            | post_record_outcome | periodic.
        handler_type: Only brain_notify is supported. SQL and scheduled-only
            MCP handlers return a tool error from the REST HTTP 422 rejection.
            - brain_notify: writes a row to brain_notifications. config =
              ``{severity, content_template}`` where content_template is a
              dict with ``{key}`` interpolation references.
        handler_config: handler-type-specific config dict.
        name: human label for the hook.
        enabled: defaults to True.
        created_by: pi | brain | executor | system. Defaults to pi.
    """
    async with _client(project_id) as c:
        r = await c.post(
            "/api/hooks",
            json={
                "event": event,
                "handler_type": handler_type,
                "handler_config": handler_config,
                "name": name,
                "enabled": enabled,
                "created_by": created_by,
            },
        )
        _raise_with_detail(r)
        return json.dumps(r.json(), indent=2)


@tool(category="hooks")
async def rka_list_hooks(
    event: str | None = None,
    enabled_only: bool = False,
    *,
    project_id: str,
) -> str:
    """List hooks registered for the active project.

    Args:
        event: Optional event filter.
        enabled_only: If True, only return hooks with enabled=true.
    """
    async with _client(project_id) as c:
        params = {}
        if event:
            params["event"] = event
        if enabled_only:
            params["enabled_only"] = "true"
        r = await c.get("/api/hooks", params=params)
        _raise_with_detail(r)
        return json.dumps(r.json(), indent=2)


@tool(category="hooks")
async def rka_enable_hook(hook_id: str, *, project_id: str) -> str:
    """Enable a previously-disabled hook."""
    async with _client(project_id) as c:
        r = await c.put(f"/api/hooks/{hook_id}/enable")
        _raise_with_detail(r)
        return json.dumps(r.json(), indent=2)


@tool(category="hooks")
async def rka_disable_hook(hook_id: str, *, project_id: str) -> str:
    """Disable a hook without deleting it. Re-enable later via rka_enable_hook."""
    async with _client(project_id) as c:
        r = await c.put(f"/api/hooks/{hook_id}/disable")
        _raise_with_detail(r)
        return json.dumps(r.json(), indent=2)


@tool(category="hooks")
async def rka_delete_hook(hook_id: str, *, project_id: str) -> str:
    """Delete a hook permanently. Cascades to hook_executions via FK."""
    async with _client(project_id) as c:
        r = await c.delete(f"/api/hooks/{hook_id}")
        if r.status_code == 404:
            return json.dumps({"error": "hook_not_found", "hook_id": hook_id})
        _raise_with_detail(r)
        return json.dumps({"deleted": hook_id})


@tool(category="hooks")
async def rka_get_hook_executions(
    hook_id: str | None = None,
    since: str | None = None,
    status: str | None = None,
    limit: int = 100,
    *,
    project_id: str,
) -> str:
    """Query the hook_executions audit log.

    Args:
        hook_id: Optional filter to a single hook.
        since: ISO-8601 timestamp; only return executions at or after this time.
        status: Optional filter — success | error | aborted_depth_limit | skipped_disabled.
        limit: Max rows (cap 500).
    """
    async with _client(project_id) as c:
        params: dict[str, str] = {"limit": str(min(limit, 500))}
        if hook_id:
            params["hook_id"] = hook_id
        if since:
            params["since"] = since
        if status:
            params["status"] = status
        r = await c.get("/api/hooks/executions/list", params=params)
        _raise_with_detail(r)
        return json.dumps(r.json(), indent=2)


@tool(category="hooks")
async def rka_get_brain_notifications(
    since: str | None = None,
    include_cleared: bool = False,
    limit: int = 100,
    *,
    project_id: str,
) -> str:
    """Read the brain_notifications queue. Default: only uncleared rows.

    Args:
        since: ISO-8601; only return notifications created at/after.
        include_cleared: Include already-cleared notifications.
        limit: Max rows (cap 500).
    """
    async with _client(project_id) as c:
        params: dict[str, str] = {
            "limit": str(min(limit, 500)),
            "include_cleared": "true" if include_cleared else "false",
        }
        if since:
            params["since"] = since
        r = await c.get("/api/notifications", params=params)
        _raise_with_detail(r)
        return json.dumps(r.json(), indent=2)


@tool(category="hooks")
async def rka_clear_brain_notifications(ids: list[str], *, project_id: str) -> str:
    """Mark a list of brain_notifications as cleared (read).

    Args:
        ids: List of bnt_... notification IDs.
    """
    async with _client(project_id) as c:
        r = await c.post("/api/notifications/clear", json={"ids": ids})
        _raise_with_detail(r)
        return json.dumps(r.json(), indent=2)


@tool(category="maintenance")
async def rka_bulk_update(
    updates: list[dict],
    *,
    project_id: str,
) -> str:
    """PRIMARY FIELD: updates. Validated best-effort entity updates.

    Canonical items are flat: ``entity_type`` + ``id`` + update fields.
    The historical nested ``data={...}`` shape remains accepted. The full
    batch is validated before the first HTTP request, but the requests are
    independent and can partially succeed; this operation is not atomic.

    Args:
        updates: List of updates (PRIMARY FIELD), e.g. [{"entity_type":
            "journal", "id": "jrn_01...", "status": "superseded"}]
    """
    prepared: list[tuple[int, str, str, str, dict]] = []
    errors: list[str] = []
    for i, upd in enumerate(updates):
        try:
            item = _BulkUpdateItem.model_validate(upd)
            data = item.normalized_data()
        except Exception as exc:
            detail = " ".join(str(exc).split())
            errors.append(f"[{i}] invalid update: {detail[:300]}")
            continue
        endpoint = {
            "note": f"/api/notes/{item.id}",
            "journal": f"/api/notes/{item.id}",
            "decision": f"/api/decisions/{item.id}",
            "literature": f"/api/literature/{item.id}",
        }[item.entity_type]
        prepared.append((i, item.entity_type, item.id, endpoint, data))

    # Structural/data errors abort the whole batch before any write. Runtime
    # HTTP failures can still leave earlier valid requests applied, which is
    # why the public contract explicitly describes this as best-effort.
    results: list[str] = []
    if not errors:
        async with _client(project_id) as c:
            for i, etype, eid, endpoint, data in prepared:
                try:
                    r = await c.put(endpoint, json=data)
                    if r.status_code >= 300:
                        errors.append(
                            f"[{i}] {etype} {eid} -> {r.status_code}: {r.text[:100]}"
                        )
                        continue

                    readback = r.json()
                    mismatched = [
                        key
                        for key, expected in data.items()
                        if readback.get(key) != expected
                    ]
                    if mismatched:
                        errors.append(
                            f"[{i}] {etype} {eid} -> write response mismatch for "
                            f"fields={','.join(mismatched)} (write may have occurred)"
                        )
                        continue
                    results.append(
                        f"[{i}] {etype} {eid} OK fields={','.join(data.keys())}"
                    )
                except Exception as exc:
                    errors.append(
                        f"[{i}] {etype} {eid} -> error: {str(exc)[:100]}"
                    )

    summary = f"Updated {len(results)}/{len(updates)}"
    if errors:
        summary += f" ({len(errors)} errors)"
    lines = [summary, ""]
    if results:
        lines.append("Successes:")
        lines.extend(results[:20])
        if len(results) > 20:
            lines.append(f"  ... and {len(results) - 20} more")
    if errors:
        lines.append("Errors:")
        lines.extend(errors)
    return "\n".join(lines)


# ============================================================
# Mission Lifecycle
# ============================================================

@tool(category="mission")
async def rka_create_mission(
    phase: str,
    objective: str,
    tasks: list[MissionTask] | None = None,
    context: str | None = None,
    acceptance_criteria: str | None = None,
    scope_boundaries: str | None = None,
    checkpoint_triggers: str | None = None,
    depends_on: str | None = None,
    motivated_by_decision: str | None = None,
    tags: list[str] | None = None,
    *,
    project_id: str,
) -> str:
    """PRIMARY FIELD: objective. Create a new mission for the Executor.

    PROVENANCE: Always provide `motivated_by_decision` to link the triggering decision.
    Include relevant decision IDs, journal entry IDs, and literature IDs in the `context`
    field so the Executor can read the full reasoning chain before starting work.

    Note for LLM callers: the primary mission body is `objective` —
    NOT `content`. Other write tools use different vocabularies for
    the body field (rka_add_note: content, rka_submit_checkpoint:
    description, rka_submit_report: summary); the canonical-name
    convention is documented per-tool in the docstring opener.

    Args:
        phase: Research phase
        objective: Clear mission objective
        tasks: Task list [{description, status}]
        context: Background context for the Executor — include decision IDs, journal IDs, and literature IDs that inform this mission
        acceptance_criteria: How to know when done
        scope_boundaries: What NOT to do
        checkpoint_triggers: When to escalate
        depends_on: Mission ID this depends on
        motivated_by_decision: Decision ID that triggered this mission (REQUIRED for provenance — creates motivated link)
        tags: Optional explicit tags for categorization
    """
    async with _client(project_id) as c:
        body = {
            "phase": phase,
            "objective": objective,
            # Coerce each task to a plain dict. Via the v2.7.0+ `rka_execute`
            # dispatch path (verb_dispatch -> _legacy("rka_create_mission")),
            # `tasks` arrive as plain dicts (CreateMissionArgs.tasks: list[dict]),
            # NOT MissionTask models — so a bare `task.model_dump()` raised
            # `'dict' object has no attribute 'model_dump'`. Accept both shapes.
            "tasks": [t.model_dump() if hasattr(t, "model_dump") else t
                      for t in tasks] if tasks else None,
            "context": context, "acceptance_criteria": acceptance_criteria,
            "scope_boundaries": scope_boundaries,
            "checkpoint_triggers": checkpoint_triggers,
            "depends_on": depends_on,
            "motivated_by_decision": motivated_by_decision,
            "tags": tags,
        }
        r = await c.post("/api/missions", json={k: v for k, v in body.items() if v is not None})
        _raise_with_detail(r)
        d = r.json()
        n_tasks = len(d.get("tasks") or [])
        mid = d["id"]
        _record_entity("mission", mid, d["objective"])
        lines = [
            "MISSION CREATED",
            "",
            f"  ID:        {mid}",
            f"  Status:    {d.get('status', 'pending')}",
            f"  Enrich:    {d.get('enrichment_status', 'ready')}",
            f"  Objective: {d['objective'][:500]}",
            f"  Tasks:     {n_tasks}",
            "",
            f"Pass this ID to the Executor: {mid}",
        ]
        return "\n".join(lines)


@tool(category="mission")
async def rka_get_mission(id: str | None = None, *, project_id: str) -> str:
    """Get a mission. Returns the active mission if no ID given.

    Args:
        id: Mission ID (optional — defaults to currently active mission)
    """
    async with _client(project_id) as c:
        if id:
            r = await c.get(f"/api/missions/{id}")
            _raise_with_detail(r)
            return json.dumps(r.json(), indent=2)
        # No ID given: prefer active, fall back to most recent pending
        for status in ("active", "pending"):
            r = await c.get("/api/missions", params={"status": status, "limit": 1})
            _raise_with_detail(r)
            missions = r.json()
            if missions:
                return json.dumps(missions[0], indent=2)
        return "No active or pending mission."


@tool(category="mission")
async def rka_update_mission_status(
    id: str,
    status: MissionStatusLiteral,
    tasks: list[MissionTask] | None = None,
    *,
    project_id: str,
) -> str:
    """PRIMARY FIELD: id (plus status for the transition). Update
    mission status and task progress.

    Args:
        id: Mission ID (PRIMARY FIELD — required addressing key)
        status: pending | active | complete | partial | blocked | cancelled
        tasks: Updated task list with progress
    """
    async with _client(project_id) as c:
        body = {"status": status}
        if tasks:
            # Accept both MissionTask models and plain dicts (the v2.7.0+
            # rka_execute dispatch passes dicts; see rka_create_mission).
            body["tasks"] = [t.model_dump() if hasattr(t, "model_dump") else t
                             for t in tasks]
        r = await c.put(f"/api/missions/{id}", json=body)
        _raise_with_detail(r)
        return f"Mission {id} → {status}"


@tool(category="mission")
async def rka_update_mission(
    id: str,
    phase: str | None = None,
    objective: str | None = None,
    context: str | None = None,
    acceptance_criteria: str | None = None,
    scope_boundaries: str | None = None,
    checkpoint_triggers: str | None = None,
    depends_on: str | None = None,
    parent_mission_id: str | None = None,
    motivated_by_decision: str | None = None,
    tags: list[str] | None = None,
    *,
    project_id: str,
) -> str:
    """Update mission body fields. Wraps the post-Bug-A MissionService.update.

    For status + tasks, use the existing rka_update_mission_status tool —
    those have a separate lifecycle and validation path.

    Affordance D (Mission B / mis_01KR209WY4M6WQFEXRH79KC2ZF) — closes the
    MCP wrapper gap that the 2026-05-02 manifest pass flagged: prior to
    this tool, fields like motivated_by_decision could only be set at
    create time; retroactive updates required direct REST PUT against
    /api/missions/{id}.

    When motivated_by_decision is set, MissionService.update materializes
    the corresponding `motivated` entity_link parallel to create-path
    behavior (see Bug A commit 02d7348).

    Args:
        id: Mission ID
        phase: Research phase
        objective: Updated objective
        context: Background context for the Executor
        acceptance_criteria: How to know when done
        scope_boundaries: What NOT to do
        checkpoint_triggers: When to escalate
        depends_on: Mission ID this depends on
        parent_mission_id: Parent mission ID
        motivated_by_decision: Decision ID that triggered this mission
            (creates motivated entity_link)
        tags: Replacement tag list
    """
    async with _client(project_id) as c:
        body = {
            "phase": phase, "objective": objective, "context": context,
            "acceptance_criteria": acceptance_criteria,
            "scope_boundaries": scope_boundaries,
            "checkpoint_triggers": checkpoint_triggers,
            "depends_on": depends_on,
            "parent_mission_id": parent_mission_id,
            "motivated_by_decision": motivated_by_decision,
            "tags": tags,
        }
        filtered = {k: v for k, v in body.items() if v is not None}
        if not filtered:
            return f"Mission {id}: no fields to update."
        r = await c.put(f"/api/missions/{id}", json=filtered)
        _raise_with_detail(r)
        changed = list(filtered.keys())
        return f"Updated mission {id} fields={','.join(changed)}"


def _report_lines(value: str | list[str] | None) -> list[str] | None:
    """Normalise a report's list-or-lines field to a list of non-empty strings.

    Both shapes arrive and both are legitimate. This tool's own contract is
    newline-separated text, but the typed `submit_report` operation declares
    `list[str]` — matching `MissionReportCreate`, which is also `list[str]` —
    and the dispatcher passes that list straight through.

    Accepting only a string left `findings`, `anomalies` and `questions`
    unreachable from the typed surface: a list died here on `.strip()` with
    `'list' object has no attribute 'strip'`, a string was refused by the
    typed model before the call was ever emitted, and omitting them was the
    only thing that worked. Reports were filed with their findings silently
    dropped.

    Module-level rather than nested, so it can be tested without reaching
    into a closure.
    """
    if value is None:
        return None
    items = value if isinstance(value, list) else str(value).splitlines()
    cleaned = [str(item).strip() for item in items if str(item).strip()]
    return cleaned or None


@tool(category="mission")
async def rka_submit_report(
    mission_id: str,
    summary: str | None = None,
    findings: str | list[str] = "",
    anomalies: str | list[str] = "",
    questions: str | list[str] = "",
    codebase_state: str = "",
    recommended_next: str = "",
    *,
    content: str | None = None,  # v2.6.1 additive alias for summary
    project_id: str,
) -> str:
    """PRIMARY FIELD: summary. Submit an execution report for a
    completed mission.

    The summary is the main report body — put the full narrative there.
    Other fields are optional structured sections (one item per line).

    v2.6.1 — `summary` is now a first-class field on
    MissionReportCreate (was a schema-lie before: the MCP signature
    exposed `summary` but the Pydantic body had no such field; the
    wrapper synthesised it as `tasks_completed=[summary]`).
    Downstream readers will see both fields populated for one
    release as a migration window.

    Args:
        mission_id: Mission ID
        summary: Full report text — methodology, results, what was
            done (PRIMARY FIELD).
        findings: Key findings — a list, or newline-separated text (optional)
        anomalies: Unexpected observations — a list, or newline-separated text (optional)
        questions: Open questions for the PI — a list, or newline-separated text (optional)
        codebase_state: Description of codebase state after mission (optional)
        recommended_next: Suggested next steps as a single string (optional)
        content: v2.6.1 additive alias for `summary` — accepted so
            LLMs that extrapolate the universal "content is the body
            field" pattern from rka_add_note still succeed. Collision
            rule: explicit `summary` wins; supplying both with
            different values raises 400.

    Returns:
        The stored Mission JSON returned by the API. This readback includes
        the persisted report and any ``consistency_warnings`` caused by
        non-terminal task rows.
    """
    # v2.6.1 — additive `content` alias for `summary`.
    if summary is None and content is not None:
        summary = content
    elif (
        summary is not None and content is not None and summary != content
    ):
        raise ValueError(
            "rka_submit_report: pass either `summary` or `content` "
            "(additive alias), not both with different values"
        )
    if not summary:
        raise ValueError(
            "rka_submit_report: `summary` (or its alias `content`) is "
            "required"
        )

    body: dict = {
        # v2.6.1 — persist `summary` as a first-class field. Keep
        # tasks_completed=[summary] as back-compat for one release
        # so downstream readers that haven't migrated still see the
        # value where they expect it.
        "summary": summary,
        "tasks_completed": [summary],
        "findings": _report_lines(findings),
        "anomalies": _report_lines(anomalies),
        "questions": _report_lines(questions),
        "codebase_state": codebase_state.strip() or None,
        "recommended_next": recommended_next.strip() or None,
    }
    body = {k: v for k, v in body.items() if v is not None}

    async with _client(project_id) as c:
        r = await c.post(
            f"/api/missions/{mission_id}/report",
            json=body,
        )
        _raise_with_detail(r)
        return json.dumps(r.json(), indent=2, default=str)


@tool(category="mission")
async def rka_get_report(mission_id: str | None = None, *, project_id: str) -> str:
    """Get mission report. Defaults to latest complete mission.

    Args:
        mission_id: Mission ID (optional)
    """
    async with _client(project_id) as c:
        if mission_id:
            r = await c.get(f"/api/missions/{mission_id}/report")
        else:
            # Get latest complete mission
            r = await c.get("/api/missions", params={"status": "complete", "limit": 1})
            _raise_with_detail(r)
            missions = r.json()
            if not missions:
                return "No completed missions."
            r = await c.get(f"/api/missions/{missions[0]['id']}/report")
        _raise_with_detail(r)
        data = r.json()
        if data is None:
            return "No report found."
        return json.dumps(data, indent=2)


# ============================================================
# Checkpoints
# ============================================================

@tool(category="checkpoint")
async def rka_submit_checkpoint(
    mission_id: str,
    type: CheckpointTypeLiteral,
    description: str | None = None,
    task_reference: str | None = None,
    context: str | None = None,
    options: list[dict] | None = None,
    recommendation: str | None = None,
    blocking: bool = True,
    *,
    content: str | None = None,  # v2.6.1 additive alias for description
    project_id: str,
) -> str:
    """PRIMARY FIELD: description. Submit a checkpoint — escalate a
    decision/question to Brain/PI.

    Args:
        mission_id: Current mission ID
        type: decision | clarification | inspection | gate
        description: What needs resolving (PRIMARY FIELD).
        task_reference: Which task triggered this
        context: Additional context
        options: Possible options [{label, description, consequence}]
        recommendation: Executor's non-binding recommendation
        blocking: Whether this blocks further progress
        content: v2.6.1 additive alias for `description` — accepted
            so LLMs that extrapolate the universal "content is the
            body field" pattern from rka_add_note still succeed.
            Collision rule: explicit `description` wins; supplying
            both raises 400.
    """
    # v2.6.1 — additive `content` alias for `description`. Phase-X²' polish
    # sibling on the orchestrator side has the same alias on the adapter;
    # this server-side acceptance closes the gap so the alias works even
    # for direct MCP callers (not just orchestrator-routed calls).
    if description is None and content is not None:
        description = content
    elif description is not None and content is not None and description != content:
        raise ValueError(
            "rka_submit_checkpoint: pass either `description` or "
            "`content` (additive alias), not both with different values"
        )
    if not description:
        raise ValueError(
            "rka_submit_checkpoint: `description` (or its alias "
            "`content`) is required"
        )

    async with _client(project_id) as c:
        body = {
            "mission_id": mission_id, "type": type, "description": description,
            "task_reference": task_reference, "context": context,
            "options": options, "recommendation": recommendation,
            "blocking": blocking,
        }
        r = await c.post("/api/checkpoints", json={k: v for k, v in body.items() if v is not None})
        _raise_with_detail(r)
        d = r.json()
        _session.checkpoints_raised.append(d["id"])
        return f"Checkpoint {d['id']} created ({type}, {'blocking' if blocking else 'non-blocking'})"


@tool(tier="always_on", category="checkpoint")
async def rka_get_checkpoints(status: str = "open", *, project_id: str) -> str:
    """Get checkpoints. Defaults to open checkpoints.

    Args:
        status: open | resolved | dismissed
    """
    async with _client(project_id) as c:
        r = await c.get("/api/checkpoints", params={"status": status})
        _raise_with_detail(r)
        chks = r.json()
        if not chks:
            return f"No {status} checkpoints."
        lines = []
        for chk in chks:
            flag = "🔴 BLOCKING" if chk.get("blocking") else "🟡"
            desc = chk.get("description", "")
            # Format gate checkpoints with structured metadata
            if chk.get("type") == "gate":
                try:
                    meta = json.loads(desc)
                    gate_label = meta.get("gate_type", "gate").replace("_", " ").title()
                    n_deliverables = len(meta.get("deliverables", []))
                    n_criteria = len(meta.get("pass_criteria", []))
                    gate_status = meta.get("status", "pending")
                    if chk.get("status") == "resolved":
                        try:
                            res = json.loads(chk.get("resolution", "{}"))
                            gate_status = res.get("verdict", "resolved")
                        except (json.JSONDecodeError, TypeError):
                            gate_status = "resolved"
                    lines.append(f"{flag} {chk['id']} [Gate: {gate_label}] status={gate_status} ({n_deliverables} deliverables, {n_criteria} criteria)")
                except (json.JSONDecodeError, TypeError):
                    lines.append(f"{flag} {chk['id']} [gate]: {desc[:300]}")
            else:
                lines.append(f"{flag} {chk['id']} [{chk['type']}]: {desc[:300]}")
        return "\n".join(lines)


@tool(tier="always_on", category="checkpoint")
async def rka_resolve_checkpoint(
    id: str,
    resolution: str,
    resolved_by: str,
    rationale: str | None = None,
    create_decision: bool = False,
    *,
    project_id: str,
) -> str:
    """Resolve a checkpoint.

    Args:
        id: Checkpoint ID
        resolution: The resolution decision
        resolved_by: pi | brain
        rationale: Why this resolution
        create_decision: Also create a linked decision node
    """
    async with _client(project_id) as c:
        body = {
            "resolution": resolution, "resolved_by": resolved_by,
            "rationale": rationale, "create_decision": create_decision,
        }
        r = await c.put(f"/api/checkpoints/{id}/resolve", json=body)
        _raise_with_detail(r)
        return f"Checkpoint {id} resolved by {resolved_by}"


@tool(category="checkpoint")
async def rka_create_gate(
    mission_id: str,
    gate_type: str,
    deliverables: list[str],
    pass_criteria: list[str],
    assumptions_to_verify: list[str] | None = None,
    *,
    project_id: str,
) -> str:
    """Create a validation gate checkpoint for a mission.

    Gates are go/no-go decision points at critical transitions.
    The gate blocks progress until the Brain evaluates it.

    Args:
        mission_id: The mission this gate belongs to
        gate_type: problem_framing | plan_validation | evidence_review | synthesis_validation
        deliverables: List of required deliverables before the gate can be evaluated
        pass_criteria: List of specific, testable conditions that must be met to pass
        assumptions_to_verify: Assumptions that should be checked during evaluation
    """
    valid_types = ("problem_framing", "plan_validation", "evidence_review", "synthesis_validation")
    if gate_type not in valid_types:
        raise Exception(f"Invalid gate_type: {gate_type}. Must be one of {valid_types}")

    description = json.dumps({
        "gate_type": gate_type,
        "deliverables": deliverables,
        "pass_criteria": pass_criteria,
        "assumptions_to_verify": assumptions_to_verify or [],
        "status": "pending",
    })

    async with _client(project_id) as c:
        body = {
            "mission_id": mission_id,
            "type": "gate",
            "description": description,
            "blocking": True,
        }
        r = await c.post("/api/checkpoints", json=body)
        _raise_with_detail(r)
        d = r.json()
        _session.checkpoints_raised.append(d["id"])

    gate_label = gate_type.replace("_", " ").title()
    lines = [
        f"🚦 Gate created: {d['id']} [{gate_label}]",
        f"  Mission: {mission_id}",
        f"  Deliverables: {len(deliverables)} items",
        f"  Pass criteria: {len(pass_criteria)} conditions",
    ]
    if assumptions_to_verify:
        lines.append(f"  Assumptions to verify: {len(assumptions_to_verify)}")
    lines.append("  Status: PENDING — gate blocks until evaluated")
    return "\n".join(lines)


@tool(category="checkpoint")
async def rka_evaluate_gate(
    gate_id: str,
    verdict: str,
    notes: str,
    assumption_status: dict[str, str] | None = None,
    *,
    project_id: str,
) -> str:
    """Evaluate a validation gate and record the verdict.

    If any assumption is 'invalidated', auto-flags the related decision as stale.

    Args:
        gate_id: The gate checkpoint ID (chk_...)
        verdict: go | kill | hold | recycle
        notes: Brain's evaluation rationale
        assumption_status: Map of assumption text → validated | unvalidated | invalidated
    """
    valid_verdicts = ("go", "kill", "hold", "recycle")
    if verdict not in valid_verdicts:
        raise Exception(f"Invalid verdict: {verdict}. Must be one of {valid_verdicts}")

    resolution = json.dumps({
        "verdict": verdict,
        "notes": notes,
        "assumption_status": assumption_status or {},
        "evaluated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    })

    async with _client(project_id) as c:
        r = await c.put(f"/api/checkpoints/{gate_id}/resolve", json={
            "resolution": resolution,
            "resolved_by": "brain",
            "rationale": f"Gate verdict: {verdict}",
        })
        _raise_with_detail(r)

        # If any assumption invalidated, flag the mission's decision as stale
        invalidated = [
            k for k, v in (assumption_status or {}).items() if v == "invalidated"
        ]
        stale_targets = []
        if invalidated:
            # Get the checkpoint to find the mission
            chk_r = await c.get(f"/api/checkpoints/{gate_id}")
            if chk_r.is_success:
                chk = chk_r.json()
                mission_id = chk.get("mission_id")
                if mission_id:
                    mis_r = await c.get(f"/api/missions/{mission_id}")
                    if mis_r.is_success:
                        mis = mis_r.json()
                        dec_id = mis.get("motivated_by_decision")
                        if dec_id:
                            for assumption in invalidated:
                                flag_r = await c.post("/api/freshness/flag-stale", json={
                                    "entity_id": dec_id,
                                    "reason": f"Assumption invalidated at gate: {assumption}",
                                    "staleness": "yellow",
                                    "propagate": True,
                                })
                                if flag_r.is_success:
                                    stale_targets.append(dec_id)

    verdict_icons = {"go": "✅", "kill": "❌", "hold": "⏸️", "recycle": "♻️"}
    icon = verdict_icons.get(verdict, "?")
    lines = [f"{icon} Gate {gate_id} evaluated: {verdict.upper()}", f"  Notes: {notes}"]
    if assumption_status:
        for assumption, status in assumption_status.items():
            status_icon = {"validated": "✓", "unvalidated": "?", "invalidated": "✗"}.get(status, "?")
            lines.append(f"  [{status_icon}] {assumption}: {status}")
    if stale_targets:
        lines.append(f"  ⚠️ Flagged stale due to invalidated assumptions: {', '.join(stale_targets)}")
    return "\n".join(lines)


# ============================================================
# Retrieval & Search
# ============================================================

def _currency_marker(hit: dict) -> str:
    """Render a search hit's currency as text, or empty when it is current.

    `/api/search` returns `status`, `superseded_by` and `stale` per hit, but
    this tool renders hits as lines of text and used to emit only type, id,
    title and snippet. The fields arrived and were dropped, which left the
    exact failure they were added to prevent: a superseded decision printed
    identically to the one that replaced it, and a reader ranking on relevance
    alone could not tell which was in force.

    The marker goes on the header line rather than the snippet, because a
    snippet is truncated and a reader skimming ids should not have to
    read prose to notice that a result is dead.
    """
    superseded_by = hit.get("superseded_by")
    if superseded_by:
        return f"  ⚠ SUPERSEDED → {superseded_by}"
    status = (hit.get("status") or "").strip().lower()
    if status in {"superseded", "retracted", "abandoned"}:
        return f"  ⚠ {status.upper()}"
    if hit.get("stale"):
        return "  ⚠ STALE"
    currency = hit.get("currentness") or {}
    if currency.get("is_current") is False:
        return "  ⚠ NOT CURRENT: " + ", ".join(currency.get("reasons", []))
    if currency.get("warnings"):
        return "  ⚠ REVIEW: " + ", ".join(currency["warnings"])
    return ""


@tool(tier="always_on", category="core")
async def rka_search(
    query: str,
    entity_types: list[str] | None = None,
    limit: int = 20,
    *,
    project_id: str,
) -> str:
    """Search across all research knowledge.

    Results show truncated snippets. Use rka_get(id) to read the full content of any result.

    Args:
        query: Search query
        entity_types: Filter by type — decision | literature | journal | mission
        limit: Max results
    """
    session = _session
    async with _client(project_id) as c:
        body = {"query": query, "entity_types": entity_types, "limit": limit}
        r = await c.post("/api/search", json=body)
        _raise_with_detail(r)
        results = r.json()

        # Backlog one-liner appended at the end of the result block
        # (v2.4 / dec_01KQQPER3XSSBACGZANFJCVQ66). Best-effort: if the
        # /maintenance/summary route is unavailable, omit the line silently.
        backlog_line = ""
        try:
            mr = await c.get("/api/maintenance/summary")
            if mr.status_code == 200:
                backlog = mr.json()
                if backlog.get("total_items", 0) > 0:
                    top = backlog.get("top_categories") or []
                    top_str = ", ".join(f"{c['name']} {c['count']}" for c in top)
                    backlog_line = f"\n\nMaintenance: {backlog['total_items']} items (top: {top_str})"
        except Exception:
            pass

        # Affordance C (Mission B): degraded-mode one-liner when embeddings
        # are unavailable so the search-result consumer knows the current
        # output is FTS-only (no semantic recall). Best-effort.
        degraded_line = ""
        try:
            cap_r = await c.get("/api/capabilities")
            if cap_r.status_code == 200:
                caps = cap_r.json()
                if not caps.get("embedding", {}).get("available"):
                    degraded_line = "\n\n⚠ FTS-only — embeddings unavailable; semantic recall is degraded."
        except Exception:
            pass

        if not results:
            return f"No results for '{query}'{degraded_line}{backlog_line}"
        lines = []
        for res in results:
            lines.append(
                f"[{res['entity_type']}] {res['entity_id']}: {res['title']}"
                f"{_currency_marker(res)}"
            )
            if res.get("snippet"):
                lines.append(f"  {res['snippet'][:500]}")
        return "\n".join(lines) + degraded_line + backlog_line


@tool(tier="always_on", category="core")
async def rka_get(
    id: str,
    *,
    project_id: str,
) -> str:
    """Get the full content of any entity by ID.

    Use this when listing tools (rka_get_journal, rka_search, etc.) truncate
    content and you need to see the complete text. Supports all entity types.

    Args:
        id: Entity ID (e.g. jrn_01..., dec_01..., lit_01..., clm_01..., ecl_01..., mis_01...)
    """
    prefix = id.split("_")[0] if "_" in id else ""
    endpoint_map = {
        "jrn": f"/api/notes/{id}",
        "dec": f"/api/decisions/{id}",
        "lit": f"/api/literature/{id}",
        "clm": f"/api/claims/{id}",
        "ecl": f"/api/clusters/{id}",
        "mis": f"/api/missions/{id}",
        "chk": f"/api/checkpoints/{id}",
    }
    endpoint = endpoint_map.get(prefix)
    if not endpoint:
        return f"Unknown ID prefix '{prefix}'. Expected: jrn_, dec_, lit_, clm_, ecl_, mis_, chk_"
    params = {}
    if prefix == "ecl":
        params["include_claims"] = "true"
    async with _client(project_id) as c:
        r = await c.get(endpoint, params=params)
        _raise_with_detail(r)
        data = r.json()
        # Format cluster claims inline for readability
        if prefix == "ecl" and data.get("claims"):
            claims = data["claims"]
            claim_summaries = []
            for cl in claims:
                content = cl.get("content", "")[:100]
                if len(cl.get("content", "")) > 100:
                    content += "…"
                v = "✓" if cl.get("verified") else "○"
                claim_summaries.append({
                    "id": cl["id"],
                    "type": cl.get("claim_type"),
                    "confidence": cl.get("confidence"),
                    "verified": v,
                    "evidence_status": cl.get("evidence_status", "unassessed"),
                    "content": content,
                })
            data["claims"] = claim_summaries
        return json.dumps(data, indent=2, default=str)


@tool(category="decision")
async def rka_get_decision_tree(
    root_id: str | None = None,
    phase: str | None = None,
    active_only: bool = False,
    *,
    project_id: str,
) -> str:
    """Get the decision tree with linked entities at each node.

    Shows hierarchical decisions with children, chosen options,
    and linked missions/journal entries/literature from entity_links.
    Use rka_get(id) to read the full content of any decision.

    Args:
        root_id: Optional decision ID to get subtree only
        phase: Filter by phase
        active_only: Only show active decisions
    """
    async with _client(project_id) as c:
        params = {}
        if root_id:
            params["root_id"] = root_id
        r = await c.get("/api/graph/decision-tree", params=params)
        _raise_with_detail(r)
        tree = r.json()

        def fmt_node(node, indent=0):
            prefix = "  " * indent
            status = node.get("status", "?")
            chosen = node.get("chosen", "?")
            # Filter by phase/active if requested
            if phase and node.get("phase") != phase:
                return []
            if active_only and status != "active":
                return []
            decided_by = node.get("decided_by", "")
            decided_tag = f", {decided_by}" if decided_by else ""
            lines = [f"{prefix}[{status}{decided_tag}] {node['id']}: {node['question'][:300]}"]
            if chosen:
                lines.append(f"{prefix}  → Chosen: {chosen}")
            for le in node.get("linked_entities", []):
                lines.append(f"{prefix}  ↔ [{le['type']}] {le['id']} ({le['link_type']})")
            for child in node.get("children", []):
                lines.extend(fmt_node(child, indent + 1))
            return lines

        output = []
        for root in tree:
            output.extend(fmt_node(root))
        return "\n".join(output) if output else "No decisions found."


@tool(category="literature")
async def rka_get_literature(
    status: str | None = None,
    query: str | None = None,
    limit: int = 20,
    *,
    project_id: str,
) -> str:
    """Get literature entries.

    Titles are truncated in listings. Use rka_get(id) to read the full record including abstract.

    Args:
        status: to_read | reading | read | cited | excluded
        query: Search in title/abstract
        limit: Max results
    """
    async with _client(project_id) as c:
        params = {"limit": limit}
        if status:
            params["status"] = status
        if query:
            params["query"] = query
        r = await c.get("/api/literature", params=params)
        _raise_with_detail(r)
        entries = r.json()
        if not entries:
            return "No literature entries found."
        lines = []
        for e in entries:
            authors = ", ".join(e.get("authors") or [])[:40]
            lines.append(f"{e['id']} [{e['status']}] {e['title'][:300]}")
            if authors:
                lines.append(f"  {authors} ({e.get('year', '?')})")
        return "\n".join(lines)


@tool(category="journal")
async def rka_get_journal(
    type: str | None = None,
    phase: str | None = None,
    confidence: str | None = None,
    status: str | None = None,
    since: str | None = None,
    limit: int = 20,
    *,
    project_id: str,
) -> str:
    """Get journal entries.

    Content is truncated in listings. Use rka_get(id) to read the full content of any entry.

    Args:
        type: note | log | directive (legacy types like finding/insight also accepted)
        phase: Filter by phase
        confidence: hypothesis | tested | verified | superseded | retracted
        status: draft | active | superseded | retracted
        since: ISO date to filter from
        limit: Max results
    """
    async with _client(project_id) as c:
        params = {"limit": limit, "hide_superseded": True}
        if type:
            params["type"] = type
        if phase:
            params["phase"] = phase
        if confidence:
            params["confidence"] = confidence
        if status:
            params["status"] = status
        if since:
            params["since"] = since
        r = await c.get("/api/notes", params=params)
        _raise_with_detail(r)
        entries = r.json()
        if not entries:
            return "No journal entries found."
        lines = []
        for e in entries:
            pi_marker = " [PI]" if e.get("source") == "pi" else ""
            lines.append(f"{e['id']} [{e['type']}]{pi_marker} ({e['confidence']}) {e['content'][:500]}")
        return "\n".join(lines)


# ============================================================
# Project Selection
# ============================================================

@tool(category="core")
async def rka_list_projects() -> str:
    """List all available projects.

    Unscoped (no project_id required). Use this to discover available
    project_ids before passing them to other tools.
    """
    async with _client() as c:
        r = await c.get("/api/projects")
        _raise_with_detail(r)
        projects = r.json()

    lines = ["## Projects", ""]
    for p in projects:
        desc = f" — {p['description']}" if p.get("description") else ""
        lines.append(f"- **{p['id']}**: {p['name']}{desc}")
    if not projects:
        lines.append("No projects found.")
    lines.append("")
    lines.append(
        "**Note (v2.6+):** there is no longer an 'active project' concept "
        "at the MCP layer. Every project-scoped tool requires `project_id` "
        "explicitly. Pass the desired project_id from the list above to "
        "every subsequent rka_* call."
    )
    return "\n".join(lines)


@tool(category="core")
async def rka_set_project(project_id: str) -> str:
    """**DEPRECATED in v2.6.** This tool no longer sets server-side state.

    Pre-v2.6: this set a per-process `_session.project_id` which was the
    silent default for every tool call. The default-fallback was the
    source of a persistent failure mode: when the MCP stdio process
    restarted (Docker rebuild, `uv tool install --force`, Claude Desktop
    relaunch), session state was lost and subsequent writes silently
    landed in `proj_default`.

    v2.6+: every project-scoped tool takes `project_id` as a required
    kwarg-only parameter. The LLM/caller passes the project explicitly
    on every call — no shared session state, no silent fallback, no
    failure mode.

    This tool remains as a deprecated no-op so existing call sites get
    a clear warning instead of a missing-tool error. It validates that
    the requested project exists (helpful pre-flight check) and returns
    a deprecation notice. **Pass `project_id` to every subsequent tool
    call.**

    Args:
        project_id: Project ID to verify exists.
    """
    async with _client() as c:
        r = await c.get("/api/projects")
        _raise_with_detail(r)
        projects = r.json()

    resolved_id = None
    for p in projects:
        if p["id"] == project_id:
            resolved_id = p["id"]
            break
    if resolved_id is None:
        for p in projects:
            if p["name"].lower() == project_id.lower():
                resolved_id = p["id"]
                break

    if resolved_id is None:
        available = "\n".join(f"  - `{p['id']}`: {p['name']}" for p in projects)
        return (
            f"**Deprecation notice:** rka_set_project is a no-op in v2.6+. "
            f"Pass `project_id` to every tool call explicitly.\n\n"
            f"Additionally, project '{project_id}' was not found. Available:\n{available}"
        )

    return (
        f"**Deprecation notice:** rka_set_project is a no-op in v2.6+.\n\n"
        f"Project `{resolved_id}` exists. Pass `project_id=\"{resolved_id}\"` "
        f"to every subsequent rka_* tool call explicitly. There is no longer "
        f"an 'active project' concept at the MCP layer — each call is "
        f"project-scoped via its required `project_id` kwarg."
    )


@tool(category="core")
async def rka_create_project(
    name: str,
    description: str | None = None,
) -> str:
    """Create a new research project.

    Unscoped (no project_id required — this CREATES one). Returns the
    new project_id; pass that to subsequent rka_* tool calls.

    Args:
        name: Human-readable project name (e.g. "Climate Policy Analysis")
        description: Brief description of the research project
    """
    async with _client() as c:
        body = {"name": name}
        if description:
            body["description"] = description
        r = await c.post("/api/projects", json=body)
        _raise_with_detail(r)
        project = r.json()

    return (
        f"Created project **{project['name']}** (`{project['id']}`).\n\n"
        f"Pass `project_id=\"{project['id']}\"` to every subsequent rka_* "
        f"tool call to operate on this project. There is no 'active project' "
        f"concept at the MCP layer in v2.6+."
    )


# ============================================================
# Project State
# ============================================================

@tool(tier="always_on", category="core")
async def rka_get_status(*, project_id: str) -> str:
    """Get full project state: phase, active mission, open checkpoints, metrics."""
    async with _client(project_id) as c:
        # Gather all status info in parallel-ish (sequential for simplicity)
        status_r = await c.get("/api/status")
        _raise_with_detail(status_r)
        status = status_r.json()

        missions_r = await c.get("/api/missions", params={"status": "active", "limit": 1})
        active_missions = missions_r.json() if missions_r.status_code == 200 else []

        chk_r = await c.get("/api/checkpoints", params={"status": "open"})
        open_chks = chk_r.json() if chk_r.status_code == 200 else []

        # Backlog summary (v2.4 / dec_01KQQPER3XSSBACGZANFJCVQ66). Best-effort:
        # if the route is unavailable or errors, we silently omit the line.
        backlog = None
        try:
            mr = await c.get("/api/maintenance/summary")
            if mr.status_code == 200:
                backlog = mr.json()
        except Exception:
            backlog = None

        lines = [
            f"## Project: {status['project_name']}",
            f"Phase: {status.get('current_phase', 'not set')}",
        ]
        if status.get("summary"):
            lines.append(f"Summary: {status['summary']}")
        if status.get("blockers"):
            lines.append(f"⚠️ Blockers: {status['blockers']}")

        if active_missions:
            m = active_missions[0]
            n_tasks = len(m.get("tasks") or [])
            lines.append(f"\n### Active Mission: {m['id']}")
            lines.append(f"Objective: {m['objective'][:500]}")
            lines.append(f"Tasks: {n_tasks}")

        if open_chks:
            lines.append(f"\n### Open Checkpoints: {len(open_chks)}")
            for chk in open_chks[:5]:
                flag = "🔴" if chk.get("blocking") else "🟡"
                lines.append(f"  {flag} {chk['id']}: {chk['description'][:300]}")

        if backlog and backlog.get("total_items", 0) > 0:
            top = backlog.get("top_categories") or []
            top_str = ", ".join(f"{c['name']} {c['count']}" for c in top)
            lines.append(f"\nMaintenance: {backlog['total_items']} items (top: {top_str})")

        # Affordance C (Mission B): capabilities block. Best-effort —
        # silently omit on error so a missing /capabilities route doesn't
        # break status display.
        #
        # Mission D (v2.4.0) removed the `llm` field from
        # /api/capabilities per the LLM-capability-removal directive
        # (jrn_01KRNZBS50K250HHHHEC58E4GC). The llm line is rendered
        # conditionally so a future re-introduction works without code
        # changes here.
        try:
            cap_r = await c.get("/api/capabilities")
            if cap_r.status_code == 200:
                caps = cap_r.json()
                emb = caps.get("embedding", {})
                lines.append("\n### Capabilities")
                emb_status = "✓ available" if emb.get("available") else f"✗ unavailable ({emb.get('reason_unavailable') or 'unknown'})"
                lines.append(f"  embedding: {emb_status}")
                if "llm" in caps:
                    llm = caps["llm"] or {}
                    llm_status = "✓ available" if llm.get("available") else f"✗ unavailable ({llm.get('reason_unavailable') or 'unknown'})"
                    lines.append(f"  llm:       {llm_status}")
        except Exception:
            pass

        return "\n".join(lines)


@tool(category="core")
async def rka_update_status(
    current_phase: str | None = None,
    summary: str | None = None,
    blockers: str | None = None,
    metrics: dict | None = None,
    *,
    content: str | None = None,  # v2.6.1 additive alias for summary
    project_id: str,
) -> str:
    """PRIMARY FIELD: summary. Update project state.

    Args:
        current_phase: New phase
        summary: Updated project summary (PRIMARY FIELD).
        blockers: Current blockers
        metrics: Key metrics dict
        content: v2.6.1 additive alias for `summary` — accepted so
            LLMs that extrapolate the universal "content is the body
            field" pattern still succeed. Collision rule: explicit
            `summary` wins; supplying both with different values
            raises 400.
    """
    # v2.6.1 — additive `content` alias for `summary`.
    if summary is None and content is not None:
        summary = content
    elif (
        summary is not None and content is not None and summary != content
    ):
        raise ValueError(
            "rka_update_status: pass either `summary` or `content` "
            "(additive alias), not both with different values"
        )

    async with _client(project_id) as c:
        body = {
            "current_phase": current_phase, "summary": summary,
            "blockers": blockers, "metrics": metrics,
        }
        r = await c.put("/api/status", json={k: v for k, v in body.items() if v is not None})
        _raise_with_detail(r)
        return "Status updated"


# ============================================================
# Export
# ============================================================

# ============================================================
# Academic / Import Tools
# ============================================================

@tool(category="literature")
async def rka_import_bibtex(
    bibtex: str,
    default_status: str = "to_read",
    skip_duplicates: bool = True,
    *,
    project_id: str,
) -> str:
    """Import literature entries from BibTeX content.

    Args:
        bibtex: Raw BibTeX string (one or more entries)
        default_status: Initial status for imported entries — to_read | reading | read
        skip_duplicates: Skip entries that already exist (by DOI or title)
    """
    async with _client(project_id) as c:
        body = {
            "bibtex": bibtex,
            "default_status": default_status,
            "added_by": "import",
            "skip_duplicates": skip_duplicates,
        }
        r = await c.post("/api/import/bibtex", json=body)
        _raise_with_detail(r)
        data = r.json()
        imported = data.get("imported", [])
        skipped = data.get("skipped", [])
        errors = data.get("errors", [])
        lines = [f"Parsed {data.get('total_parsed', 0)} entries:"]
        lines.append(f"  ✅ Imported: {len(imported)}")
        if skipped:
            lines.append(f"  ⏭️ Skipped: {len(skipped)}")
        if errors:
            lines.append(f"  ❌ Errors: {len(errors)}")
            for error in errors[:5]:
                lines.append(f"  - {error.get('title', 'BibTeX input')}: {error.get('error', 'Import failed')}")
        for item in imported[:10]:
            lines.append(f"  + {item['id']}: {item['title']}")
        return "\n".join(lines)


@tool(category="literature")
async def rka_enrich_doi(lit_id: str, *, project_id: str) -> str:
    """Enrich a literature entry by looking up its DOI via CrossRef.

    Automatically fills in missing title, authors, year, venue, abstract, and URL
    from the CrossRef database. Requires the entry to have a DOI set.

    Args:
        lit_id: Literature entry ID
    """
    async with _client(project_id) as c:
        r = await c.post(f"/api/literature/{lit_id}/enrich-doi")
        _raise_with_detail(r)
        data = r.json()
        if data.get("status") == "enriched":
            return f"Enriched {lit_id}: updated {', '.join(data['fields_updated'])}"
        return f"No updates needed for {lit_id}"


@tool(category="graph")
async def rka_export_mermaid(
    phase: str | None = None,
    active_only: bool = False,
    *,
    project_id: str,
) -> str:
    """Export the decision tree as a Mermaid flowchart diagram.

    Returns Mermaid markdown that can be pasted into docs, GitHub, or mermaid.live.

    Args:
        phase: Filter to a specific research phase
        active_only: Only include active decisions
    """
    async with _client(project_id) as c:
        params = {}
        if phase:
            params["phase"] = phase
        if active_only:
            params["active_only"] = "true"
        r = await c.get("/api/decisions/mermaid", params=params)
        _raise_with_detail(r)
        data = r.json()
        return data.get("mermaid", "graph TD\n    empty[No decisions yet]")


@tool(category="workspace")
async def rka_batch_import(
    entries: list[dict],
    actor: str = "import",
    *,
    project_id: str,
) -> str:
    """Batch import multiple entries at once.

    Each entry must have 'entity_type' and 'data' fields.

    Args:
        entries: List of {entity_type: "note"|"literature"|"decision", data: {...}}
        actor: Who is importing — brain | executor | pi | import
    """
    async with _client(project_id) as c:
        body = {"entries": entries, "actor": actor}
        r = await c.post("/api/import/batch", json=body)
        _raise_with_detail(r)
        data = r.json()
        imported = data.get("imported", [])
        errors = data.get("errors", [])
        lines = [f"Batch import: {len(imported)} imported, {len(errors)} errors"]
        for item in imported[:10]:
            lines.append(f"  + [{item['type']}] {item['id']}")
        for err in errors[:5]:
            lines.append(f"  ❌ Entry {err['index']}: {err['error']}")
        return "\n".join(lines)


@tool(category="workspace")
async def rka_ingest_document(
    content: str,
    source: IngestSourceLiteral = "brain",
    default_type: str = "finding",
    phase: str | None = None,
    tags: list[str] | None = None,
    related_literature: list[str] | None = None,
    related_decisions: list[str] | None = None,
    related_mission: str | None = None,
    split_by_headings: bool = True,
    *,
    project_id: str,
) -> str:
    """PRIMARY FIELD: content. Ingest a markdown document by
    splitting it into journal entries.

    Accepts a full markdown document (e.g. a report, analysis, literature review)
    and automatically splits it by headings (## or ###) into individual journal entries.
    Each section becomes its own entry with auto-classified type and tags derived
    from the heading. Ideal for the Brain to send structured context to the
    knowledge base.

    Args:
        content: Markdown document content to ingest
        source: Who is sending this — brain | executor | pi
        default_type: Default entry type if not auto-classified — finding | insight | methodology | observation | idea | exploration | hypothesis
        phase: Research phase for all created entries
        tags: Base tags applied to all entries (section-specific tags are added automatically)
        related_literature: Literature IDs all entries relate to
        related_decisions: Decision IDs all entries relate to
        related_mission: Mission ID all entries belong to
        split_by_headings: Whether to split by ## / ### headings (default: true). If false, creates one entry.
    """
    async with _client(project_id) as c:
        body = {
            "content": content, "source": source,
            "default_type": default_type, "phase": phase,
            "tags": tags, "related_literature": related_literature,
            "related_decisions": related_decisions,
            "related_mission": related_mission,
            "split_by_headings": split_by_headings,
        }
        r = await c.post("/api/ingest/document", json={k: v for k, v in body.items() if v is not None})
        _raise_with_detail(r)
        data = r.json()
        created = data.get("created", [])
        errors = data.get("errors", [])
        lines = [f"Ingested document: {len(created)} entries created from {data.get('total_sections', 0)} sections"]
        for item in created:
            lines.append(f"  + {item['id']} [{item['type']}] {item['heading']} ({item['length']} chars)")
        if errors:
            lines.append(f"\n❌ {len(errors)} errors:")
            for err in errors:
                lines.append(f"  - {err['section']}: {err['error']}")
        return "\n".join(lines)


# ============================================================
# Export
# ============================================================

@tool(category="session")
async def rka_export(format: str = "markdown", scope: str = "state", *, project_id: str) -> str:
    """Export research data.

    Args:
        format: markdown | json | mermaid (mermaid only for decisions scope)
        scope: state | decisions | literature | full
    """
    async with _client(project_id) as c:
        if scope == "state":
            r = await c.get("/api/status")
            _raise_with_detail(r)
            if format == "json":
                return json.dumps(r.json(), indent=2)
            s = r.json()
            return f"# {s['project_name']}\n\nPhase: {s.get('current_phase')}\n\n{s.get('summary', '')}"

        elif scope == "decisions":
            if format == "mermaid":
                r = await c.get("/api/decisions/mermaid")
                _raise_with_detail(r)
                return r.json().get("mermaid", "")
            r = await c.get("/api/decisions/tree")
            _raise_with_detail(r)
            return json.dumps(r.json(), indent=2)

        elif scope == "literature":
            r = await c.get("/api/literature", params={"limit": 200})
            _raise_with_detail(r)
            if format == "json":
                return json.dumps(r.json(), indent=2)
            entries = r.json()
            lines = []
            for e in entries:
                authors = ", ".join(e.get("authors") or [])
                lines.append(f"- [{e['status']}] {e['title']} ({authors}, {e.get('year', '?')})")
            return "\n".join(lines)

        else:
            return "Export scope 'full' not yet implemented. Use state/decisions/literature."


# ============================================================
# Phase 2: Context, Summarization, Eviction
# ============================================================

@tool(tier="always_on", category="core")
async def rka_get_context(
    topic: str | None = None,
    phase: str | None = None,
    *,
    project_id: str,
) -> str:
    """Get an importance-ranked context package.

    v2.4 (dec_01KQQPD6Y6B362T3K08368BDMP): no token budget, no temperature
    bucketing. Entries are ordered by journal.importance, entity_links
    centrality, and recency. Frontier model context windows make a
    bookkeeper-imposed truncation unnecessary.

    Args:
        topic: Search topic for semantic context retrieval. Omit for an
            importance-ranked overview of recent project state.
        phase: Filter to a specific research phase.
    """
    async with _client(project_id) as c:
        body = {"topic": topic, "phase": phase}
        r = await c.post("/api/context", json={k: v for k, v in body.items() if v is not None})
        _raise_with_detail(r)
        pkg = r.json()

        lines = []
        if pkg.get("topic"):
            lines.append(f"## Context: {pkg['topic']}")
        if pkg.get("phase"):
            lines.append(f"Phase: {pkg['phase']}")
        if pkg.get("note"):
            lines.append(f"ℹ️  {pkg['note']}")

        # v2.4: prefer the new `entries` field (single ranked list).
        # Fall back to legacy `hot_entries` only if the server is older.
        ranked = pkg.get("entries") or pkg.get("hot_entries") or []
        if ranked:
            lines.append(f"\n### Ranked entries ({len(ranked)})")
            lines.extend(ranked)

        if pkg.get("narrative"):
            lines.append("\n### Narrative")
            lines.append(pkg["narrative"])

        if pkg.get("sources"):
            lines.append(f"\n---\nSources: {', '.join(pkg['sources'][:10])}")

        return "\n".join(lines)


@tool(category="session")
async def rka_summarize(
    topic: str | None = None,
    phase: str | None = None,
    entity_ids: list[str] | None = None,
    *,
    project_id: str,
) -> str:
    """On-demand topic summarization. Produces a narrative summary
    stored as a journal entry.

    Args:
        topic: Topic to summarize
        phase: Filter to specific phase
        entity_ids: Specific entity IDs to summarize (overrides topic)
    """
    async with _client(project_id) as c:
        body = {"topic": topic, "phase": phase, "entity_ids": entity_ids}
        r = await c.post("/api/summarize", json={k: v for k, v in body.items() if v is not None})
        _raise_with_detail(r)
        data = r.json()
        return (
            f"Summary created: {data.get('summary_id', 'unknown')}\n"
            f"Sources: {data.get('source_count', 0)}\n\n"
            f"{data.get('summary', '')}"
        )


@tool(category="maintenance")
async def rka_eviction_sweep(dry_run: bool = True, *, project_id: str) -> str:
    """Propose entries for archival based on staleness rules.

    Finds superseded, abandoned, and unreferenced entries that can be
    safely archived. Default is dry_run=True (preview only).

    Args:
        dry_run: If true, show what would be archived without taking action
    """
    async with _client(project_id) as c:
        r = await c.post("/api/eviction-sweep", params={"dry_run": str(dry_run).lower()})
        _raise_with_detail(r)
        data = r.json()
        proposed = data.get("proposed", [])
        if not proposed:
            return "No entries proposed for eviction. Knowledge base is clean."

        lines = [f"{'[DRY RUN] ' if data.get('dry_run') else ''}Eviction Proposal: {len(proposed)} entries"]
        for item in proposed:
            lines.append(f"  [{item['entity_type']}] {item['entity_id']}: {item['title']}")
            lines.append(f"    Reason: {item['reason']}")
        return "\n".join(lines)


# ============================================================
# Academic Search (Semantic Scholar + arXiv)
# ============================================================

@tool(category="literature")
async def rka_search_semantic_scholar(
    query: str,
    limit: int = 10,
    year_min: int | None = None,
    fields_of_study: list[str] | None = None,
    add_to_library: bool = False,
    import_top_n: int | None = None,
    *,
    project_id: str,
) -> str:
    """Search Semantic Scholar for academic papers.

    Args:
        query: Search query
        limit: Max results (default: 10)
        year_min: Minimum publication year filter
        fields_of_study: Filter by field (e.g. ["Computer Science"])
        add_to_library: If true, automatically add results to RKA literature
        import_top_n: When add_to_library=True, import only the first N
            results. None = import all returned. Ignored when
            add_to_library=False.
    """
    import httpx as hx

    params = {"query": query, "limit": min(limit, 50)}
    fields = "title,authors,year,venue,abstract,externalIds,url,citationCount"
    params["fields"] = fields
    if year_min:
        params["year"] = f"{year_min}-"

    import os
    headers: dict[str, str] = {}
    s2_key = os.environ.get("SEMANTIC_SCHOLAR_API_KEY")
    if s2_key:
        headers["x-api-key"] = s2_key

    try:
        async with hx.AsyncClient(timeout=15.0, headers=headers) as client:
            r = await client.get(
                "https://api.semanticscholar.org/graph/v1/paper/search",
                params=params,
            )
            _raise_with_detail(r)
            data = r.json()
    except Exception as exc:
        return f"Semantic Scholar search failed: {exc}"

    papers = data.get("data", [])
    if not papers:
        return f"No results for '{query}'"

    lines = [f"Found {data.get('total', len(papers))} papers (showing {len(papers)}):"]
    added_ids = []

    # v2.7.0a2 Decision 2 (Option C): import_top_n caps the slice of
    # results that get imported when add_to_library=True. None means
    # "import all returned"; the listing itself is always full-length.
    to_import = (
        papers[:import_top_n] if (add_to_library and import_top_n is not None) else papers
    )
    import_set = {id(p) for p in to_import}

    for p in papers:
        authors = ", ".join(a.get("name", "") for a in (p.get("authors") or [])[:3])
        if len(p.get("authors") or []) > 3:
            authors += " et al."
        doi = (p.get("externalIds") or {}).get("DOI", "")
        cites = p.get("citationCount", 0)

        lines.append(f"\n📄 {p.get('title', 'Untitled')}")
        lines.append(f"   {authors} ({p.get('year', '?')}) — {p.get('venue', 'Unknown')}")
        if doi:
            lines.append(f"   DOI: {doi}")
        lines.append(f"   Citations: {cites}")
        if p.get("abstract"):
            lines.append(f"   {p['abstract'][:200]}...")

        if add_to_library and id(p) in import_set:
            try:
                async with _client(project_id) as c:
                    body = {
                        "title": p.get("title", "Untitled"),
                        "authors": [a.get("name", "") for a in (p.get("authors") or [])],
                        "year": p.get("year"),
                        "venue": p.get("venue"),
                        "doi": doi or None,
                        "url": p.get("url"),
                        "abstract": p.get("abstract"),
                        "added_by": "import",
                    }
                    resp = await c.post("/api/literature", json={k: v for k, v in body.items() if v is not None})
                    if resp.status_code == 201:
                        lit_id = resp.json()["id"]
                        added_ids.append(lit_id)
                        lines.append(f"   → Added as {lit_id}")
            except Exception:
                pass  # Skip silently if add fails (e.g. duplicate DOI)

    if added_ids:
        lines.append(f"\n✅ Added {len(added_ids)} papers to library")

    return "\n".join(lines)


@tool(category="literature")
async def rka_search_arxiv(
    query: str,
    limit: int = 10,
    sort_by: str = "relevance",
    add_to_library: bool = False,
    import_top_n: int | None = None,
    year_min: int | None = None,
    *,
    project_id: str,
) -> str:
    """Search arXiv for preprints and papers.

    Args:
        query: Search query (supports arXiv query syntax like au:surname, ti:keyword)
        limit: Max results (default: 10)
        sort_by: relevance | lastUpdatedDate | submittedDate
        add_to_library: If true, automatically add results to RKA literature
        import_top_n: When add_to_library=True, import only the first N
            results. None = import all returned. Ignored when
            add_to_library=False.
        year_min: Minimum submission year. Translates to arXiv
            `submittedDate:[YYYY0101 TO *]` filter; applied via boolean
            AND with the user query.
    """
    import httpx as hx

    search_query = f"all:{query}"
    if year_min:
        # arXiv search uses YYYYMMDDHHMM format; year-floor of Jan 1 covers
        # the whole year. Boolean AND joins to the free-text query.
        search_query = (
            f"({search_query})+AND+submittedDate:[{year_min}01010000+TO+*]"
        )

    params = {
        "search_query": search_query,
        "max_results": min(limit, 50),
        "sortBy": sort_by,
        "sortOrder": "descending",
    }

    try:
        async with hx.AsyncClient(timeout=15.0) as client:
            r = await client.get("http://export.arxiv.org/api/query", params=params)
            _raise_with_detail(r)
            xml_text = r.text
    except Exception as exc:
        return f"arXiv search failed: {exc}"

    # Parse Atom XML (lightweight, no external dep)
    import re
    entries = re.findall(r"<entry>(.*?)</entry>", xml_text, re.DOTALL)
    if not entries:
        return f"No arXiv results for '{query}'"

    lines = [f"Found {len(entries)} arXiv papers:"]
    added_ids = []

    # v2.7.0a2 Decision 2 (Option C): import_top_n caps the slice of
    # results that get imported when add_to_library=True. None = import
    # all returned. The listing itself remains full-length.
    to_import_entries = (
        entries[:import_top_n]
        if (add_to_library and import_top_n is not None)
        else entries
    )
    import_xml_set = set(to_import_entries)

    for entry_xml in entries:
        title = _xml_text(entry_xml, "title").replace("\n", " ").strip()
        summary = _xml_text(entry_xml, "summary").replace("\n", " ").strip()
        published = _xml_text(entry_xml, "published")[:10]
        year = int(published[:4]) if published else None
        arxiv_id = _xml_text(entry_xml, "id")

        # Extract authors
        authors = re.findall(r"<name>(.*?)</name>", entry_xml)

        # Extract categories
        categories = re.findall(r'category term="([^"]+)"', entry_xml)

        author_str = ", ".join(authors[:3])
        if len(authors) > 3:
            author_str += " et al."

        lines.append(f"\n📄 {title}")
        lines.append(f"   {author_str} ({published})")
        lines.append(f"   arXiv: {arxiv_id}")
        if categories:
            lines.append(f"   Categories: {', '.join(categories[:3])}")
        if summary:
            lines.append(f"   {summary[:200]}...")

        if add_to_library and entry_xml in import_xml_set:
            try:
                async with _client(project_id) as c:
                    body = {
                        "title": title,
                        "authors": authors,
                        "year": year,
                        "url": arxiv_id,
                        "abstract": summary,
                        "added_by": "import",
                    }
                    resp = await c.post("/api/literature", json={k: v for k, v in body.items() if v is not None})
                    if resp.status_code == 201:
                        lit_id = resp.json()["id"]
                        added_ids.append(lit_id)
                        lines.append(f"   → Added as {lit_id}")
            except Exception:
                pass

    if added_ids:
        lines.append(f"\n✅ Added {len(added_ids)} papers to library")

    return "\n".join(lines)


def _xml_text(xml: str, tag: str) -> str:
    """Extract text from first occurrence of <tag>...</tag>."""
    import re
    m = re.search(rf"<{tag}[^>]*>(.*?)</{tag}>", xml, re.DOTALL)
    return m.group(1).strip() if m else ""


# ============================================================
# Workspace Bootstrap
# ============================================================


@tool(category="workspace")
async def rka_scan_workspace_tree(
    folder_path: str,
    max_depth: int = 2,
    *,
    project_id: str,
) -> str:
    """Show the directory tree of a workspace folder with file counts and sizes.

    This is the FIRST tool to call when exploring a new workspace. It
    returns a fast, shallow overview without reading file contents or
    computing hashes. Use the output to decide which subdirectories to
    scan in detail with rka_scan_workspace.

    Requires RKA_HOST_FILE_ROOTS in the MCP process environment. Only
    operator-authorized local directories are read; links and reparse
    points are skipped. Entry, file, depth and elapsed-time budgets make
    the result a bounded overview, not a complete filesystem inventory.

    Typical workflow:
      1. rka_scan_workspace_tree(folder_path)     → see the shape
      2. rka_scan_workspace(folder_path + "/docs") → deep-scan a subdir
      3. rka_bootstrap_workspace(folder_path + "/docs") → ingest it

    Args:
        folder_path: Absolute path to the workspace folder
        max_depth: How many levels deep to show (default 2; 0 = top-level only)
    """
    import asyncio

    import stat
    from rka.infra.file_access import FileAccessPolicy, ScanBudget, scan_slot
    policy = FileAccessPolicy.host()
    root = policy.directory(folder_path)

    from rka.services.classify import DEFAULT_IGNORES
    ignores = set(DEFAULT_IGNORES)

    def _tree() -> list[dict]:
        budget = ScanBudget()
        if not 0 <= max_depth <= 8:
            raise ValueError("max_depth must be between 0 and 8")

        MAX_ENTRIES_PER_DIR = 200

        def _scan_dir(dirpath: Path, depth: int) -> dict:
            name = dirpath.name or str(dirpath)
            result: dict = {"name": name, "path": str(dirpath), "depth": depth}
            file_count = 0
            total_bytes = 0
            subdirs: list[dict] = []
            capped = False
            try:
                entry_count = 0
                with policy.entries(dirpath) as it:
                    for entry in it:
                        if not budget.check():
                            capped = True
                            break
                        budget.entries += 1
                        entry_count += 1
                        if entry_count > MAX_ENTRIES_PER_DIR:
                            capped = True
                            break
                        if entry.name.startswith(".") or entry.name in ignores:
                            continue
                        try:
                            info = entry.stat(follow_symlinks=False)
                        except OSError:
                            continue
                        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                            continue
                        if stat.S_ISDIR(info.st_mode):
                            if depth < max_depth:
                                subdirs.append(_scan_dir(dirpath / entry.name, depth + 1))
                            else:
                                subdirs.append({
                                    "name": entry.name,
                                    "path": str(dirpath / entry.name),
                                    "depth": depth + 1,
                                    "file_count": "?",
                                    "size_mb": "?",
                                    "subdirs": [],
                                })
                        elif stat.S_ISREG(info.st_mode):
                            budget.files += 1
                            file_count += 1
                            total_bytes += info.st_size
            except PermissionError:
                result["error"] = "permission denied"

            result["file_count"] = f"{file_count}+" if capped else file_count
            result["size_mb"] = "?" if capped else round(total_bytes / 1024 / 1024, 1)
            result["subdirs"] = subdirs
            if capped:
                result["capped"] = True
            total = file_count if isinstance(file_count, int) else MAX_ENTRIES_PER_DIR
            for sd in subdirs:
                fc = sd.get("total_files_recursive", sd.get("file_count", 0))
                if isinstance(fc, int):
                    total += fc
            result["total_files_recursive"] = total
            return result

        with scan_slot():
            return [_scan_dir(root, 0)]

    tree = await asyncio.to_thread(_tree)
    node = tree[0] if tree else {}

    def _render(n: dict, indent: int = 0) -> list[str]:
        prefix = "  " * indent
        fc = n.get("file_count", 0)
        sz = n.get("size_mb", 0)
        total = n.get("total_files_recursive", fc)
        if indent == 0:
            lines = [f"{n['path']}/  ({total} files observed; bounded by depth and scan limits)"]
        else:
            detail = f"{fc} files, {sz} MB" if fc != "?" else "not scanned yet"
            lines = [f"{prefix}{n['name']}/  ({detail})"]
        for sd in n.get("subdirs", []):
            lines.extend(_render(sd, indent + 1))
        return lines

    output_lines = _render(node)
    output_lines.append("")
    output_lines.append(
        "Tip: pick a subdirectory and run rka_scan_workspace(folder_path='<subdir>') "
        "to classify its files for ingestion."
    )
    return "\n".join(output_lines)


@tool(category="workspace")
async def rka_scan_workspace(
    folder_path: str,
    ignore_patterns: list[str] | None = None,
    max_file_size_mb: float = 50.0,
    use_llm: bool = True,
    *,
    project_id: str,
) -> str:
    """Deep-scan a workspace folder and classify files for ingestion.

    Call rka_scan_workspace_tree FIRST to see the directory structure,
    then call this tool on specific subdirectories. Do NOT call this on
    a large root folder (10K+ files) — it will be slow on external drives.

    File reading happens on the HOST (no Docker bind mount needed).

    Typical workflow:
      1. rka_scan_workspace_tree(root_path)        → overview
      2. rka_scan_workspace(root_path + "/docs")   → classify one subdir
      3. rka_bootstrap_workspace(root_path + "/docs") → ingest

    Args:
        folder_path: Absolute path to the folder to scan (prefer a subdirectory, not the root)
        ignore_patterns: Additional patterns to ignore (e.g. ["*.log", "*.json"])
        max_file_size_mb: Skip files larger than this (default: 50MB)
        use_llm: Ignored (LLM classification not available in host-side path)
    """
    import asyncio
    from rka.services.classify import DEFAULT_IGNORES

    from rka.infra.file_access import FileAccessPolicy
    policy = FileAccessPolicy.host()
    root = policy.directory(folder_path)

    ignores = set(DEFAULT_IGNORES)
    ignores.update(ignore_patterns or [])
    max_bytes = int(max_file_size_mb * 1024 * 1024)

    from rka.services.host_workspace import scan_host_files
    all_host_files, walk_budget = await asyncio.to_thread(
        scan_host_files, policy, root, ignores=ignores, max_bytes=max_bytes,
    )
    walk_timed_out = bool(walk_budget.stopped)
    walk_timeout_reason = walk_budget.stopped

    # Cap files sent to the API to avoid >1MB payloads. The summary
    # always reports the total count; only the first max_scan_files
    # are sent for classification + dedup. The user can narrow with
    # ignore_patterns to reach specific files.
    MAX_SCAN_FILES = 500
    truncated = len(all_host_files) > MAX_SCAN_FILES
    host_files = all_host_files[:MAX_SCAN_FILES]

    async with _client(project_id) as c:
        body = {
            "root_path": str(root),
            "files": host_files,
            "total_files_found": len(all_host_files),
            "ignore_patterns": list(ignores),
        }
        r = await c.post("/api/workspace/scan/from-host", json=body, timeout=120.0)
        _raise_with_detail(r)
        data = r.json()

    # Build a summary from ALL files (not just the sent subset)
    from collections import Counter
    cat_counts = Counter(f["category"] for f in all_host_files)
    target_counts = Counter(f["ingestion_target"] for f in all_host_files)
    total_size = sum(f["size_bytes"] for f in all_host_files)

    files = data.get("files", [])

    lines = [
        f"Scanned: {folder_path}",
        f"   Scan ID: {data.get('scan_id', 'n/a')}",
        f"   Total files found: {len(all_host_files)}, sent to API: {len(host_files)}"
        + (f" (capped at {MAX_SCAN_FILES})" if truncated else ""),
        f"   Total size: {total_size / 1024 / 1024:.1f} MB",
    ]

    if walk_timed_out:
        lines.append(f"\n   WARNING: {walk_timeout_reason}")
    if walk_budget.skipped:
        lines.append(f"\n   WARNING: {walk_budget.skipped} oversized or unreadable files skipped")
    if truncated:
        lines.append(
            f"\n   NOTE: {len(all_host_files) - MAX_SCAN_FILES} files not shown. "
            f"Use ignore_patterns to narrow (e.g., ignore_patterns=['*.json'] "
            f"to skip data files)."
        )

    lines.append(f"\nBy category: {dict(cat_counts.most_common())}")
    lines.append(f"By ingestion target: {dict(target_counts.most_common())}")

    # Show per-file detail only for the manageable subset
    by_target: dict[str, list] = {}
    for f in files:
        target = f.get("ingestion_target", "skip")
        by_target.setdefault(target, []).append(f)

    MAX_FILES_PER_TARGET = 20
    for target, target_files in sorted(by_target.items()):
        count = target_counts.get(target, len(target_files))
        lines.append(f"\n{'─' * 40}")
        lines.append(f"Target: {target} ({count} files)")
        shown = target_files[:MAX_FILES_PER_TARGET]
        for f in shown:
            dup = " [DUP]" if f.get("is_duplicate") else ""
            llm = " [LLM]" if f.get("llm_classified") else ""
            tags = f", tags={f['proposed_tags']}" if f.get("proposed_tags") else ""
            title = f" — {f['title_suggestion']}" if f.get("title_suggestion") else ""
            lines.append(
                f"  • {f['relative_path']} [{f['category']}→{f['proposed_type']}]{tags}{title}{dup}{llm}"
            )
        if len(target_files) > MAX_FILES_PER_TARGET:
            lines.append(f"  ... and {len(target_files) - MAX_FILES_PER_TARGET} more")

    warnings = data.get("warnings", [])
    if warnings:
        lines.append(f"\nWarnings ({len(warnings)}):")
        for w in warnings[:10]:
            lines.append(f"  - {w}")

    lines.append(f"\nTo ingest, run rka_bootstrap_workspace(folder_path='{folder_path}').")
    lines.append("Tip: use ignore_patterns=['*.json','*.csv','*.parquet'] to skip data files.")
    return "\n".join(lines)


@tool(category="workspace")
async def rka_bootstrap_workspace(
    folder_path: str,
    phase: str | None = None,
    override_tags: list[str] | None = None,
    skip_files: list[str] | None = None,
    use_llm: bool = True,
    dry_run: bool = False,
    *,
    project_id: str,
) -> str:
    """One-shot workspace bootstrap: scan + ingest all files in a folder.

    Call rka_scan_workspace_tree FIRST to pick the right subdirectory.
    Then call this on that subdirectory — not on a large root folder.

    File reading happens on the HOST (no Docker bind mount needed).
    Each file's content is sent individually to the REST API for storage.

    Args:
        folder_path: Absolute path to the folder to ingest (prefer a subdirectory)
        phase: Research phase to assign to all entries
        override_tags: Tags to add to all ingested entries
        skip_files: Relative paths of files to skip
        use_llm: Ignored in the host-side path (LLM classification deferred)
        dry_run: Preview what would be created without actually ingesting
    """
    import asyncio
    from rka.services.classify import DEFAULT_IGNORES

    # Step 1: Host-side scan (same logic as rka_scan_workspace)
    from rka.infra.file_access import FileAccessError, FileAccessPolicy, ScanBudget
    policy = FileAccessPolicy.host()
    root = policy.directory(folder_path)

    from rka.services.host_workspace import scan_host_files, validate_host_manifest, read_host_content
    ignores = set(DEFAULT_IGNORES)
    host_files, scan_budget = await asyncio.to_thread(
        scan_host_files, policy, root, ignores=ignores,
        max_bytes=policy.max_bytes, skip_files=skip_files or [],
    )

    # Get a scan manifest from the server (dedup + scan_id)
    async with _client(project_id) as c:
        body = {
            "root_path": str(root),
            "files": host_files,
            "total_files_found": len(host_files),
        }
        r = await c.post("/api/workspace/scan/from-host", json=body, timeout=120.0)
        _raise_with_detail(r)
        manifest = r.json()

    validate_host_manifest(manifest, host_files)
    read_budget = ScanBudget()

    # Step 2: Host-side file reading + per-file ingest via content API
    scan_id = manifest.get("scan_id", "")
    tags = list(override_tags or [])
    total_processed = 0
    total_created = 0
    total_skipped = 0
    total_errors = 0
    result_items: list[dict] = []

    for sf in manifest.get("files", []):
        rel = sf.get("relative_path", "")
        if sf.get("is_duplicate"):
            total_skipped += 1
            continue
        if sf.get("ingestion_target") == "skip":
            total_skipped += 1
            continue
        total_processed += 1
        full_path = policy.relative_file(root, rel)
        cat = sf.get("category", "unknown")
        target = sf.get("ingestion_target", "skip")

        if dry_run:
            result_items.append({
                "relative_path": rel,
                "category": cat,
                "ingestion_target": target,
                "success": True,
                "entity_ids": [],
                "entity_count": 0,
            })
            total_created += 1
            continue

        try:
            content, content_type, metadata = await asyncio.to_thread(
                read_host_content, policy, root, sf, read_budget,
            )
        except FileAccessError as exc:
            total_errors += 1
            result_items.append({
                "relative_path": rel, "category": cat, "ingestion_target": target,
                "success": False, "error": str(exc), "entity_ids": [], "entity_count": 0,
            })
            continue

        async with _client(project_id) as c:
            ingest_body = {
                "scan_id": scan_id,
                "relative_path": rel,
                "filename": sf.get("filename", full_path.name),
                "content": content,
                "content_type": content_type,
                "metadata": metadata,
                "tags": tags,
                "source": "pi",
                "phase": phase,
                "proposed_type": sf.get("proposed_type", "finding"),
            }
            try:
                r = await c.post(
                    "/api/workspace/ingest/with-content",
                    json=ingest_body, timeout=60.0,
                )
                _raise_with_detail(r)
                ingest_result = r.json()
                result_items.append(ingest_result)
                if ingest_result.get("success"):
                    total_created += ingest_result.get("entity_count", 1)
                else:
                    total_errors += 1
            except Exception as exc:
                total_errors += 1
                result_items.append({
                    "relative_path": rel,
                    "category": cat,
                    "ingestion_target": target,
                    "success": False,
                    "error": str(exc)[:200],
                    "entity_ids": [],
                    "entity_count": 0,
                })

    result = {
        "scan_id": scan_id,
        "total_processed": total_processed,
        "total_created": total_created,
        "total_skipped": total_skipped,
        "total_errors": total_errors,
        "results": result_items,
    }

    # Format response
    prefix = "🔍 DRY RUN — " if dry_run else "✅ "
    lines = [
        f"{prefix}Bootstrap complete for {folder_path}",
        f"   Scan ID: {manifest['scan_id']}",
        f"   Processed: {result['total_processed']}, Created: {result['total_created']}, "
        f"Skipped: {result['total_skipped']}, Errors: {result['total_errors']}",
    ]

    if scan_budget.stopped:
        lines.append(f"WARNING: {scan_budget.stopped}")
    if scan_budget.skipped:
        lines.append(f"WARNING: {scan_budget.skipped} oversized or unreadable files skipped")

    # Show results grouped by category
    for item in result.get("results", []):
        if item.get("error") and not item.get("success"):
            lines.append(f"  ❌ {item['relative_path']}: {item['error']}")
        elif item.get("entity_ids"):
            lines.append(
                f"  ✓ {item['relative_path']} → {item['entity_count']} entries "
                f"({', '.join(item['entity_ids'][:3])}{'...' if len(item.get('entity_ids', [])) > 3 else ''})"
            )

    if not dry_run:
        lines.append(
            f"\n💡 Run rka_review_bootstrap(scan_id='{manifest['scan_id']}') "
            f"to get a summary for reorganization."
        )

    return "\n".join(lines)


@tool(category="workspace")
async def rka_review_bootstrap(scan_id: str, *, project_id: str) -> str:
    """Review a completed bootstrap for reorganization.

    Returns entry counts, singleton tags, entries needing attention,
    and suggested next actions. Use after rka_bootstrap_workspace.

    Args:
        scan_id: The scan ID from rka_bootstrap_workspace output
    """
    async with _client(project_id) as c:
        r = await c.get(f"/api/workspace/review/{scan_id}", timeout=60.0)
        _raise_with_detail(r)
        data = r.json()

    lines = [
        f"📋 Bootstrap Review — {data['scan_id']}",
        f"   Total entries created: {data['total_entries_created']}",
    ]

    if data.get("entries_by_type"):
        lines.append(f"\n📊 By type: {data['entries_by_type']}")
    if data.get("entries_by_category"):
        lines.append(f"📊 By source category: {data['entries_by_category']}")
    if data.get("all_tags"):
        lines.append(f"🏷️  Tags ({len(data['all_tags'])}): {', '.join(data['all_tags'][:20])}")
    if data.get("singleton_tags"):
        lines.append(f"⚠️  Singleton tags: {', '.join(data['singleton_tags'][:15])}")
    if data.get("needs_attention"):
        lines.append(
            f"🔍 Entries needing attention: {len(data['needs_attention'])} "
            f"({', '.join(data['needs_attention'][:5])})"
        )

    if data.get("suggestions"):
        lines.append("\n📌 Suggested next actions:")
        for s in data["suggestions"]:
            icon = {"high": "🔴", "medium": "🟡", "low": "🟢"}.get(s["priority"], "•")
            lines.append(f"  {icon} [{s['priority']}] {s['action']}")
            lines.append(f"     {s['details']}")

    if data.get("narrative"):
        lines.append(f"\n📝 Overview:\n{data['narrative']}")

    return "\n".join(lines)


# ============================================================
# LLM Enrichment
# ============================================================

# ============================================================
# Graph & Research Map
# ============================================================

@tool(category="graph")
async def rka_get_graph(
    include_types: str | None = None,
    phase: str | None = None,
    limit: int = 500,
    *,
    project_id: str,
) -> str:
    """Get the full knowledge graph as nodes and edges for the research map.

    Returns all entities and their relationships from entity_links.

    Args:
        include_types: Comma-separated entity types to include (e.g. "decision,mission,journal")
        phase: Filter by research phase
        limit: Max entities per type (default 500)
    """
    async with _client(project_id) as c:
        params = {"limit": limit}
        if include_types:
            params["include_types"] = include_types
        if phase:
            params["phase"] = phase
        r = await c.get("/api/graph", params=params)
        _raise_with_detail(r)
        d = r.json()
        return (
            f"Knowledge graph: {len(d['nodes'])} nodes, {len(d['edges'])} edges\n\n"
            f"Nodes by type:\n"
            + "\n".join(f"  {t}: {sum(1 for n in d['nodes'] if n['type'] == t)}"
                       for t in sorted(set(n['type'] for n in d['nodes'])))
            + "\n\nEdges by type:\n"
            + "\n".join(f"  {t}: {sum(1 for e in d['edges'] if e['link_type'] == t)}"
                       for t in sorted(set(e['link_type'] for e in d['edges'])))
        )


@tool(category="graph")
async def rka_get_ego_graph(entity_id: str, depth: int = 1, *, project_id: str) -> str:
    """Get the neighborhood subgraph around a specific entity.

    Shows all entities connected to the given entity within `depth` hops.

    Args:
        entity_id: The entity to center on (e.g. dec_01H..., jrn_01H...)
        depth: Number of hops to traverse (1-3, default 1)
    """
    async with _client(project_id) as c:
        r = await c.get(f"/api/graph/ego/{entity_id}", params={"depth": depth})
        _raise_with_detail(r)
        d = r.json()
        lines = [f"Ego graph for {entity_id}: {len(d['nodes'])} nodes, {len(d['edges'])} edges\n"]
        for node in d["nodes"]:
            marker = " ← CENTER" if node["id"] == entity_id else ""
            lines.append(f"  [{node['type']}] {node['id']}: {node['label'][:300]}{marker}")
        lines.append("\nEdges:")
        for edge in d["edges"]:
            lines.append(f"  {edge['source']} --{edge['link_type']}--> {edge['target']}")
        return "\n".join(lines)



@tool(category="graph")
async def rka_graph_stats(*, project_id: str) -> str:
    """Get knowledge graph statistics: entity counts, edge counts by type."""
    async with _client(project_id) as c:
        r = await c.get("/api/graph/stats")
        _raise_with_detail(r)
        d = r.json()
        lines = [f"Knowledge graph: {d['total_nodes']} nodes, {d['total_edges']} edges\n"]
        lines.append("Nodes:")
        for etype, count in d["node_counts"].items():
            lines.append(f"  {etype}: {count}")
        lines.append("\nEdges by type:")
        for ltype, count in d.get("edge_counts_by_type", {}).items():
            lines.append(f"  {ltype}: {count}")
        return "\n".join(lines)


# ============================================================
# Summaries & QA (NotebookLM-style)
# ============================================================

@tool(category="session")
async def rka_generate_summary(
    scope_type: str = "project",
    scope_id: str | None = None,
    granularity: str = "paragraph",
    *,
    project_id: str,
) -> str:
    """Generate a multi-granularity summary of research progress.

    Gathers evidence from the knowledge base and produces a summary
    with source citations and identified knowledge gaps.

    Args:
        scope_type: What to summarize — project | phase | mission | tag
        scope_id: Scope ID (e.g. phase name, mission ID, tag name). None for project-wide.
        granularity: Detail level — one_line | paragraph | narrative
    """
    async with _client(project_id) as c:
        r = await c.post("/api/summaries/generate", json={
            "scope_type": scope_type,
            "scope_id": scope_id,
            "granularity": granularity,
        })
        _raise_with_detail(r)
        d = r.json()
        if "error" in d:
            return f"Summary generation failed: {d['error']}"
        lines = [f"Summary ({d.get('granularity', granularity)}) — confidence: {d.get('confidence', '?')}\n"]
        if d.get("one_line"):
            lines.append(f"One-line: {d['one_line']}\n")
        if d.get("paragraph"):
            lines.append(f"Paragraph:\n{d['paragraph']}\n")
        if d.get("narrative"):
            lines.append(f"Narrative:\n{d['narrative']}\n")
        if d.get("key_questions"):
            lines.append("Open questions:")
            for q in d["key_questions"]:
                lines.append(f"  - {q}")
        if d.get("sources"):
            lines.append(f"\nSources cited: {len(d['sources'])}")
            for s in d["sources"][:5]:
                lines.append(f"  [{s['entity_type']}:{s['entity_id']}] {s.get('excerpt', '')[:300]}")
        return "\n".join(lines)


@tool(category="session")
async def rka_ask(
    question: str,
    session_id: str | None = None,
    scope_type: str | None = None,
    scope_id: str | None = None,
    *,
    project_id: str,
) -> str:
    """Ask a research question and get an answer grounded in knowledge base evidence.

    Like NotebookLM: answers cite specific sources and suggest follow-up questions.
    Use session_id for multi-turn Q&A conversations.

    Args:
        question: Your research question
        session_id: Optional session ID for follow-up questions
        scope_type: Optional scope filter (phase, tag)
        scope_id: Optional scope ID
    """
    async with _client(project_id) as c:
        r = await c.post("/api/qa/ask", json={
            "question": question,
            "session_id": session_id,
            "scope_type": scope_type,
            "scope_id": scope_id,
        })
        _raise_with_detail(r)
        d = r.json()
        if "error" in d:
            return f"QA failed: {d['error']}"
        lines = [
            f"Answer (confidence: {d.get('confidence', '?')}):\n",
            d.get("answer", "No answer"),
        ]
        if d.get("sources"):
            lines.append(f"\n\nSources ({len(d['sources'])}):")
            for i, s in enumerate(d["sources"]):
                lines.append(f"  [{i}] [{s['entity_type']}:{s['entity_id']}] \"{s.get('excerpt', '')[:300]}\"")
        if d.get("followups"):
            lines.append("\nSuggested follow-ups:")
            for f in d["followups"]:
                lines.append(f"  → {f}")
        lines.append(f"\nSession: {d.get('session_id', 'N/A')}")
        return "\n".join(lines)


# ============================================================
# Session State
# ============================================================

@tool(category="session")
async def rka_session_digest(*, project_id: str) -> str:
    """Get a compact summary of the current MCP session.

    Args:
        project_id: RKA project ID (prj_...) — required per the v2.6
          project-scoping contract. The session digest pulls
          `/api/status` and `/api/checkpoints` which are both
          project-scoped REST endpoints; threading project_id through
          `_client(project_id)` sets the `X-RKA-Project` header so the
          REST layer scopes correctly. Sibling bug to
          rka_link_literature_to_zotero — both regressed in / after
          PR #32 (v2.6 contract). Without the kwarg this function
          previously NameError'd at the `_client(project_id)` site
          because `project_id` was an undefined symbol.
    """
    session = _session

    lines = [
        "## Session Digest",
        f"Active project: {session.project_id or 'proj_default (implicit)'}",
        f"Tool calls: {session.tool_calls}",
        f"Session started: {session.session_start}",
    ]

    if session.entities_created:
        lines.append(f"\n### Entities Created ({len(session.entities_created)})")
        for entry in session.entities_created:
            lines.append(f"  [{entry['type']}] {entry['id']}: {entry['summary']}")

    if session.decisions_made:
        lines.append(f"\n### Decisions Recorded ({len(session.decisions_made)})")
        for decision_id in session.decisions_made:
            lines.append(f"  {decision_id}")

    if session.checkpoints_raised:
        lines.append(f"\n### Checkpoints Raised ({len(session.checkpoints_raised)})")
        for checkpoint_id in session.checkpoints_raised:
            lines.append(f"  {checkpoint_id}")

    try:
        async with _client(project_id) as c:
            status_r = await c.get("/api/status")
            if status_r.is_success:
                status = status_r.json()
                lines.append("\n### Current Project State")
                lines.append(f"Phase: {status.get('current_phase', '?')}")
                if status.get("summary"):
                    lines.append(f"Summary: {status['summary'][:500]}")
                if status.get("blockers"):
                    lines.append(f"Blockers: {status['blockers']}")

            chk_r = await c.get("/api/checkpoints", params={"status": "open"})
            if chk_r.is_success:
                checkpoints = chk_r.json()
                if checkpoints:
                    lines.append(f"\n### Open Checkpoints ({len(checkpoints)})")
                    for chk in checkpoints[:5]:
                        flag = "🔴" if chk.get("blocking") else "🟡"
                        lines.append(f"  {flag} {chk['id']}: {chk['description'][:300]}")
    except Exception:
        pass

    lines.append(
        "\n---\nUse this digest as the compact session context instead of retaining earlier tool output."
    )
    return "\n".join(lines)


@tool(category="session")
async def rka_reset_session() -> str:
    """Reset MCP session tracking state without restarting the MCP server.

    Clears the tool-call counter, entities-created log, decisions-made log,
    and the per-project session_start fired-marker set. v2.6+: there is no
    'active project' to preserve since every tool call carries project_id
    explicitly.
    """
    global _session
    _session = MCPSessionState()
    return "Session state reset. Tool-call counter, entity log, and session_start fired-markers cleared."


@tool(category="session")
async def rka_generate_claude_md(
    role: str = "executor",
    *,
    project_id: str,
) -> str:
    """Generate a project-specific CLAUDE.md for Claude Code.

    Queries the live RKA database and produces a CLAUDE.md tailored to
    the current project state: active phase, established tags, open missions,
    research questions, recording conventions, and v2.0 tool guidance.

    Args:
        role: Target role — "executor" (default) or "brain"
    """
    async with _client(project_id) as c:
        r = await c.get("/api/generate-claude-md", params={"role": role})
        _raise_with_detail(r)
    data = r.json()
    md = data.get("markdown", "")
    return f"Generated CLAUDE.md for role '{data.get('role', role)}':\n\n{md}"


# ============================================================
# v2.0: Research Map, Claims, Provenance, Review Queue
# ============================================================

@tool(category="claims")
async def rka_list_clusters(
    research_question_id: str | None = None,
    confidence: str | None = None,
    limit: int = 50,
    *,
    project_id: str,
) -> str:
    """List evidence clusters with claim counts.

    Returns cluster ID, label, confidence, claim count, research question,
    and truncated synthesis for each cluster.

    Args:
        research_question_id: Filter to clusters under a specific RQ (dec_... ID)
        confidence: Filter by confidence level: strong, moderate, emerging, contested, refuted
        limit: Max results (default 50)
    """
    params: dict = {"limit": limit}
    if research_question_id:
        params["research_question_id"] = research_question_id
    if confidence:
        params["confidence"] = confidence
    async with _client(project_id) as c:
        r = await c.get("/api/clusters", params=params)
        _raise_with_detail(r)
    clusters = r.json()
    if not clusters:
        return "No clusters found matching filters."
    lines = [f"Found {len(clusters)} clusters:"]
    for cl in clusters:
        conf = cl.get("confidence", "emerging")
        rq = cl.get("research_question_id") or "unassigned"
        lines.append(
            f"  [{conf}] {cl['id']} — {cl['label']} "
            f"({cl.get('claim_count', 0)} claims){_currency_marker(cl)}"
        )
        lines.append(f"    RQ: {rq}")
        synthesis = cl.get("synthesis") or ""
        if synthesis:
            lines.append(f"    {synthesis[:100]}{'…' if len(synthesis) > 100 else ''}")
    return "\n".join(lines)


@tool(tier="always_on", category="claims")
async def rka_get_research_map(*, project_id: str) -> str:
    """Get the three-level research map: Research Questions → Evidence Clusters → Claims.

    Returns a structured overview of all research questions with cluster counts,
    confidence indicators, gap counts, and contradiction flags.
    """
    async with _client(project_id) as c:
        r = await c.get("/api/research-map")
        _raise_with_detail(r)
    data = r.json()
    lines = []
    summary = data.get("summary", {})
    lines.append(f"Research Map: {summary.get('total_rqs', 0)} RQs, "
                 f"{summary.get('total_clusters', 0)} clusters, "
                 f"{summary.get('total_claims', 0)} claims")
    lines.append(f"Gaps: {summary.get('total_gaps', 0)} | "
                 f"Contradictions: {summary.get('total_contradictions', 0)} | "
                 f"Pending review: {summary.get('pending_review', 0)}")
    lines.append("")
    for rq in data.get("research_questions", []):
        rq_status_icons = {
            "open": "●", "active": "●", "partially_answered": "◐",
            "answered": "✓", "reframed": "↻", "closed": "○",
        }
        status_icon = rq_status_icons.get(rq.get("status", ""), "●")
        total_claims = rq.get("total_claims", 0)
        cluster_count = rq.get("cluster_count", 0)
        lines.append(f"{status_icon} [{rq['id']}] {rq['question']}")
        lines.append(f"  {cluster_count} clusters, {total_claims} claims, "
                     f"{rq.get('gap_count', 0)} gaps, "
                     f"{rq.get('contradiction_count', 0)} contradictions")
        clusters = rq.get("clusters", [])
        max_shown = 10
        for i, cl in enumerate(clusters[:max_shown]):
            connector = "└─" if i == len(clusters[:max_shown]) - 1 and len(clusters) <= max_shown else "├─"
            staleness_icon = {"green": "🟢", "yellow": "🟡", "red": "🔴"}.get(
                cl.get("staleness", "green"), "🟢"
            )
            lines.append(
                f"  {connector} [{cl.get('confidence', '?')}, {staleness_icon}] {cl['label']} "
                f"({cl.get('claim_count', 0)} claims) — {cl['id']}{_currency_marker(cl)}"
            )
        if len(clusters) > max_shown:
            lines.append(f"  └─ ... ({len(clusters) - max_shown} more)")
    unassigned = data.get("unassigned_clusters", [])
    if unassigned:
        lines.append(f"\nUnassigned clusters: {len(unassigned)}")
        for uc in unassigned[:5]:
            lines.append(f"  - [{uc.get('confidence', '?')}] {uc['label']} "
                         f"({uc.get('claim_count', 0)} claims) — {uc['id']}{_currency_marker(uc)}")
        if len(unassigned) > 5:
            lines.append(f"  ... ({len(unassigned) - 5} more)")
    return "\n".join(lines)


@tool(category="claims")
async def rka_get_claims(
    source_entry_id: str | None = None,
    cluster_id: str | None = None,
    claim_type: str | None = None,
    verified: bool | None = None,
    evidence_status: EvidenceStatusLit | None = None,
    stale: bool | None = None,
    limit: int = 20,
    *,
    project_id: str,
) -> str:
    """Query claims with filters.

    Claim content is truncated in listings. Use rka_get(id) to read the full claim.

    Args:
        source_entry_id: Filter by source journal entry ID
        cluster_id: Filter by evidence cluster ID
        claim_type: Filter by type: hypothesis, evidence, method, result, observation, assumption
        verified: Filter by source-grounding/extraction-fidelity status
        evidence_status: Filter by independent scientific evidence assessment
        stale: Filter by stale status (true = needs re-distillation)
        limit: Max results (default 20)
    """
    params = {"limit": limit}
    if source_entry_id:
        params["source_entry_id"] = source_entry_id
    if cluster_id:
        params["cluster_id"] = cluster_id
    if claim_type:
        params["claim_type"] = claim_type
    if verified is not None:
        params["verified"] = verified
    if evidence_status is not None:
        params["evidence_status"] = evidence_status
    if stale is not None:
        params["stale"] = stale
    async with _client(project_id) as c:
        r = await c.get("/api/claims", params=params)
        _raise_with_detail(r)
    claims = r.json()
    if not claims:
        return "No claims found matching filters."
    lines = [f"Found {len(claims)} claims:"]
    for cl in claims:
        v = "✓" if cl.get("verified") else "○"
        evidence = cl.get("evidence_status", "unassessed")
        contradiction_state = cl.get("contradicted")
        contradicted = (
            "yes" if contradiction_state is True
            else "no" if contradiction_state is False
            else "unknown"
        )
        s = " [STALE]" if cl.get("stale") else _currency_marker(cl)
        lines.append(
            f"  [{cl['id']}] ({cl['claim_type']}) "
            f"grounded={v} evidence={evidence} contradicted={contradicted} "
            f"scope={cl.get('scope_readiness', 'missing')} "
            f"scope_rev={cl.get('scope_revision', 0)} "
            f"conf={cl.get('confidence', '?'):.2f}{s}"
        )
        lines.append(f"    {cl['content'][:500]}")
        lines.append(f"    source: {cl['source_entry_id']}")
    return "\n".join(lines)


@tool(category="claims")
async def rka_get_claim_scope(claim_id: str, *, project_id: str) -> str:
    """Fetch current claim-scope readiness plus immutable scope history."""
    async with _client(project_id) as c:
        r = await c.get(f"/api/claims/{claim_id}/scope")
        _raise_with_detail(r)
    return json.dumps(r.json(), indent=2, ensure_ascii=False)


@tool(category="claims")
async def rka_set_claim_scope(
    claim_id: str,
    expected_revision: int,
    actor: str,
    reason: str,
    conditions: list[dict] | None = None,
    uncertainty: str = "unknown",
    uncertainty_note: str | None = None,
    extension_policy: str | None = None,
    allowed_extensions: list[str] | None = None,
    prohibited_extensions: list[str] | None = None,
    falsifier_status: str = "unknown",
    falsifier: str | None = None,
    falsifier_rationale: str | None = None,
    disconfirming_claim_ids: list[str] | None = None,
    review_status: str = "draft",
    *,
    project_id: str,
) -> str:
    """Append a revision-guarded canonical claim-scope contract."""
    payload = {
        "expected_revision": expected_revision,
        "actor": actor,
        "reason": reason,
        "conditions": conditions or [],
        "uncertainty": uncertainty,
        "uncertainty_note": uncertainty_note,
        "extension_policy": extension_policy,
        "allowed_extensions": allowed_extensions or [],
        "prohibited_extensions": prohibited_extensions or [],
        "falsifier_status": falsifier_status,
        "falsifier": falsifier,
        "falsifier_rationale": falsifier_rationale,
        "disconfirming_claim_ids": disconfirming_claim_ids or [],
        "review_status": review_status,
    }
    async with _client(project_id) as c:
        r = await c.post(f"/api/claims/{claim_id}/scope", json=payload)
        _raise_with_detail(r)
    return json.dumps(r.json(), indent=2, ensure_ascii=False)


@tool(category="claims")
async def rka_get_interpretation_candidates(
    candidate_id: str | None = None,
    review_status: str | None = None,
    disposition: str | None = None,
    epistemic_kind: str | None = None,
    source_type: str | None = None,
    source_id: str | None = None,
    limit: int = 50,
    *,
    project_id: str,
) -> str:
    """List staged interpretations or fetch one detailed candidate."""
    async with _client(project_id) as c:
        if candidate_id:
            response = await c.get(f"/api/interpretations/{candidate_id}")
        else:
            response = await c.get(
                "/api/interpretations",
                params=_strip_none(
                    {
                        "review_status": review_status,
                        "disposition": disposition,
                        "epistemic_kind": epistemic_kind,
                        "source_type": source_type,
                        "source_id": source_id,
                        "limit": limit,
                    }
                ),
            )
        _raise_with_detail(response)
    return json.dumps(response.json(), indent=2)


@tool(category="artifacts")
async def rka_get_sources(
    source_id: str | None = None,
    source_kind: str | None = None,
    ownership_kind: str | None = None,
    limit: int = 50,
    *,
    project_id: str,
) -> str:
    """List registered sources or fetch one source's provenance and admissions."""
    async with _client(project_id) as c:
        if source_id:
            response = await c.get(f"/api/sources/{source_id}")
        else:
            response = await c.get(
                "/api/sources",
                params=_strip_none(
                    {
                        "source_kind": source_kind,
                        "ownership_kind": ownership_kind,
                        "limit": limit,
                    }
                ),
            )
        _raise_with_detail(response)
    return json.dumps(response.json(), indent=2, ensure_ascii=False)


def _read_registered_source_file(filepath: str) -> tuple[str, str, str]:
    """Read bounded host-local bytes only beneath operator-configured roots."""
    from rka.infra.file_access import FileAccessPolicy
    raw_limit = os.environ.get("RKA_REGISTERED_SOURCE_MAX_BYTES", str(50 * 1024 * 1024))
    try:
        max_bytes = int(raw_limit)
    except ValueError as exc:
        raise ValueError("RKA_REGISTERED_SOURCE_MAX_BYTES must be an integer") from exc
    if not 1 <= max_bytes <= 500 * 1024 * 1024:
        raise ValueError("RKA_REGISTERED_SOURCE_MAX_BYTES must be between 1 and 524288000")
    policy = FileAccessPolicy.host()
    path = policy.authorize(filepath)
    payload = policy.read_bytes(path, max_bytes=max_bytes)
    return (
        base64.b64encode(payload).decode("ascii"),
        path.name,
        hashlib.sha256(payload).hexdigest(),
    )


@tool(category="artifacts")
async def rka_register_source(
    source_kind: str,
    registered_by: str,
    title: str | None = None,
    filepath: str | None = None,
    pasted_text: str | None = None,
    stable_locator: str | None = None,
    mime: str | None = None,
    expected_content_hash: str | None = None,
    ownership_kind: str = "unknown",
    ownership_note: str | None = None,
    provenance: dict | None = None,
    *,
    project_id: str,
) -> str:
    """Register local bytes or a locator; never fetch or create canonical knowledge.

    ``filepath`` is read by this host-side MCP process, then sent to Core as
    bounded base64 bytes. The Docker REST service never needs access to the
    host path.
    """
    content_base64: str | None = None
    filename: str | None = None
    if filepath is not None:
        content_base64, filename, actual_hash = await asyncio.to_thread(
            _read_registered_source_file,
            filepath,
        )
        if (
            expected_content_hash is not None
            and expected_content_hash.strip().lower() != actual_hash
        ):
            raise ValueError(
                "source content hash does not match expected_content_hash"
            )
        expected_content_hash = actual_hash
        filepath = None
    payload = _strip_none(
        {
            "source_kind": source_kind,
            "registered_by": registered_by,
            "title": title,
            "filepath": filepath,
            "pasted_text": pasted_text,
            "content_base64": content_base64,
            "filename": filename,
            "stable_locator": stable_locator,
            "mime": mime,
            "expected_content_hash": expected_content_hash,
            "ownership_kind": ownership_kind,
            "ownership_note": ownership_note,
            "provenance": provenance or {},
        }
    )
    async with _client(project_id) as c:
        response = await c.post("/api/sources", json=payload)
        _raise_with_detail(response)
    result = response.json()
    source = result.get("source") or {}
    if source.get("id"):
        _record_entity("registered_source", source["id"], source.get("title", "")[:80])
    return json.dumps(result, indent=2, ensure_ascii=False)


@tool(category="artifacts")
async def rka_admit_source_interpretation(
    source_id: str,
    candidate_id: str,
    expected_revision: int,
    target_type: str,
    target_id: str,
    actor: str,
    reason: str,
    grounding_verified: bool,
    *,
    project_id: str,
) -> str:
    """Explicitly admit one grounded candidate to an existing canonical target."""
    payload = {
        "candidate_id": candidate_id,
        "expected_revision": expected_revision,
        "target_type": target_type,
        "target_id": target_id,
        "actor": actor,
        "reason": reason,
        "grounding_verified": grounding_verified,
    }
    async with _client(project_id) as c:
        response = await c.post(f"/api/sources/{source_id}/admissions", json=payload)
        _raise_with_detail(response)
    return json.dumps(response.json(), indent=2, ensure_ascii=False)


@tool(category="claims")
async def rka_create_interpretation_candidate(
    source_type: str,
    source_id: str,
    locator_kind: str,
    statement: str,
    epistemic_kind: str,
    created_by: str,
    extraction_tool: str,
    locator_start: int | None = None,
    locator_end: int | None = None,
    locator_value: str | None = None,
    scope_conditions: list[str] | None = None,
    uncertainty: str = "unknown",
    uncertainty_note: str | None = None,
    falsifier: str | None = None,
    proposed_claim_type: str | None = None,
    extraction_model: str | None = None,
    *,
    project_id: str,
) -> str:
    """Create one reviewable interpretation without creating a claim."""
    payload = _strip_none(
        {
            "source_type": source_type,
            "source_id": source_id,
            "locator_kind": locator_kind,
            "locator_start": locator_start,
            "locator_end": locator_end,
            "locator_value": locator_value,
            "statement": statement,
            "epistemic_kind": epistemic_kind,
            "scope_conditions": scope_conditions or [],
            "uncertainty": uncertainty,
            "uncertainty_note": uncertainty_note,
            "falsifier": falsifier,
            "proposed_claim_type": proposed_claim_type,
            "created_by": created_by,
            "extraction_tool": extraction_tool,
            "extraction_model": extraction_model,
        }
    )
    async with _client(project_id) as c:
        response = await c.post("/api/interpretations", json=payload)
        _raise_with_detail(response)
    candidate = response.json()
    _record_entity(
        "interpretation_candidate",
        candidate["id"],
        candidate["statement"][:80],
    )
    return json.dumps(candidate, indent=2)


@tool(category="claims")
async def rka_triage_interpretation_candidate(
    candidate_id: str,
    action: str,
    expected_revision: int,
    actor: str,
    reason: str | None = None,
    target_candidate_id: str | None = None,
    target_entity_id: str | None = None,
    grounding_verified: bool = False,
    claim_confidence: float = 0.5,
    evidence_role: str | None = None,
    *,
    project_id: str,
) -> str:
    """Apply one revision-guarded review or promotion action."""
    payload = _strip_none(
        {
            "action": action,
            "expected_revision": expected_revision,
            "actor": actor,
            "reason": reason,
            "target_candidate_id": target_candidate_id,
            "target_entity_id": target_entity_id,
            "grounding_verified": grounding_verified,
            "claim_confidence": claim_confidence,
            "evidence_role": evidence_role,
        }
    )
    async with _client(project_id) as c:
        response = await c.post(
            f"/api/interpretations/{candidate_id}/triage",
            json=payload,
        )
        _raise_with_detail(response)
    return json.dumps(response.json(), indent=2)


@tool(category="experiments")
async def rka_get_experiments(
    experiment_id: str | None = None,
    status: str | None = None,
    limit: int = 50,
    *,
    project_id: str,
) -> str:
    """List experiments or fetch one detailed experiment."""
    async with _client(project_id) as c:
        if experiment_id:
            response = await c.get(f"/api/experiments/{experiment_id}")
        else:
            response = await c.get(
                "/api/experiments",
                params=_strip_none({"status": status, "limit": limit}),
            )
        _raise_with_detail(response)
    return json.dumps(response.json(), indent=2)


@tool(category="experiments")
async def rka_get_experiment_runs(
    run_id: str | None = None,
    experiment_id: str | None = None,
    status: str | None = None,
    limit: int = 50,
    *,
    project_id: str,
) -> str:
    """List experiment runs or fetch one run with events and observations."""
    async with _client(project_id) as c:
        if run_id:
            response = await c.get(f"/api/experiment-runs/{run_id}")
        else:
            response = await c.get(
                "/api/experiment-runs",
                params=_strip_none(
                    {
                        "experiment_id": experiment_id,
                        "status": status,
                        "limit": limit,
                    }
                ),
            )
        _raise_with_detail(response)
    return json.dumps(response.json(), indent=2)


@tool(category="experiments")
async def rka_get_experiment_observations(
    observation_id: str | None = None,
    run_id: str | None = None,
    direction: str | None = None,
    kind: str | None = None,
    claim_id: str | None = None,
    limit: int = 50,
    *,
    project_id: str,
) -> str:
    """List immutable observations or fetch evidence/review lineage for one."""
    async with _client(project_id) as c:
        if observation_id:
            response = await c.get(
                f"/api/experiment-observations/{observation_id}"
            )
        else:
            response = await c.get(
                "/api/experiment-observations",
                params=_strip_none(
                    {
                        "run_id": run_id,
                        "direction": direction,
                        "kind": kind,
                        "claim_id": claim_id,
                        "limit": limit,
                    }
                ),
            )
        _raise_with_detail(response)
    return json.dumps(response.json(), indent=2)


@tool(category="experiments")
async def rka_create_experiment(payload: dict, *, project_id: str) -> str:
    """Create an experiment and immutable plan version 1."""
    async with _client(project_id) as c:
        response = await c.post("/api/experiments", json=payload)
        _raise_with_detail(response)
    return json.dumps(response.json(), indent=2)


@tool(category="experiments")
async def rka_append_experiment_plan(
    experiment_id: str,
    payload: dict,
    *,
    project_id: str,
) -> str:
    """Append a revision-guarded immutable plan version."""
    async with _client(project_id) as c:
        response = await c.post(
            f"/api/experiments/{experiment_id}/plans",
            json=payload,
        )
        _raise_with_detail(response)
    return json.dumps(response.json(), indent=2)


@tool(category="experiments")
async def rka_transition_experiment(
    experiment_id: str,
    payload: dict,
    *,
    project_id: str,
) -> str:
    """Transition a revision-guarded experiment lifecycle."""
    async with _client(project_id) as c:
        response = await c.post(
            f"/api/experiments/{experiment_id}/transition",
            json=payload,
        )
        _raise_with_detail(response)
    return json.dumps(response.json(), indent=2)


@tool(category="experiments")
async def rka_create_experiment_run(payload: dict, *, project_id: str) -> str:
    """Queue a run bound to one immutable experiment plan version."""
    async with _client(project_id) as c:
        response = await c.post("/api/experiment-runs", json=payload)
        _raise_with_detail(response)
    return json.dumps(response.json(), indent=2)


@tool(category="experiments")
async def rka_transition_experiment_run(
    run_id: str,
    payload: dict,
    *,
    project_id: str,
) -> str:
    """Append one revision-guarded run lifecycle event."""
    async with _client(project_id) as c:
        response = await c.post(
            f"/api/experiment-runs/{run_id}/transition",
            json=payload,
        )
        _raise_with_detail(response)
    return json.dumps(response.json(), indent=2)


@tool(category="experiments")
async def rka_record_experiment_observation(
    payload: dict,
    *,
    project_id: str,
) -> str:
    """Record one immutable observation; never infer claim support."""
    async with _client(project_id) as c:
        response = await c.post("/api/experiment-observations", json=payload)
        _raise_with_detail(response)
    return json.dumps(response.json(), indent=2)


@tool(category="experiments")
async def rka_add_evidence_locator(payload: dict, *, project_id: str) -> str:
    """Bind an observation to exact content-hashed evidence bytes."""
    async with _client(project_id) as c:
        response = await c.post("/api/evidence-locators", json=payload)
        _raise_with_detail(response)
    return json.dumps(response.json(), indent=2)


@tool(category="manuscript_planning")
async def rka_get_planning_branches(
    branch_id: str | None = None,
    manuscript_id: str | None = None,
    include_archived: bool = True,
    *,
    project_id: str,
) -> str:
    """List planning branches or fetch one exact effective snapshot."""
    async with _client(project_id) as c:
        if branch_id:
            response = await c.get(f"/api/planning/branches/{branch_id}")
        else:
            response = await c.get(
                "/api/planning/branches",
                params=_strip_none(
                    {
                        "manuscript_id": manuscript_id,
                        "include_archived": include_archived,
                    }
                ),
            )
        _raise_with_detail(response)
    return json.dumps(response.json(), indent=2)


@tool(category="manuscript_planning")
async def rka_resume_planning(
    manuscript_id: str | None = None,
    *,
    project_id: str,
) -> str:
    """Resume the exact selected branch for a project or manuscript context."""
    async with _client(project_id) as c:
        response = await c.get(
            "/api/planning/resume",
            params=_strip_none({"manuscript_id": manuscript_id}),
        )
        _raise_with_detail(response)
    return json.dumps(response.json(), indent=2)


@tool(category="manuscript_planning")
async def rka_compare_planning_branches(
    base_branch_id: str,
    other_branch_id: str,
    *,
    project_id: str,
) -> str:
    """Compare two same-context planning branches without mutating either."""
    async with _client(project_id) as c:
        response = await c.get(
            "/api/planning/branches/compare",
            params={
                "base_branch_id": base_branch_id,
                "other_branch_id": other_branch_id,
            },
        )
        _raise_with_detail(response)
    return json.dumps(response.json(), indent=2)


@tool(category="manuscript_planning")
async def rka_get_planning_artifact_versions(
    artifact_id: str,
    *,
    project_id: str,
) -> str:
    """Read every immutable version of one planning artifact."""
    async with _client(project_id) as c:
        response = await c.get(f"/api/planning/artifacts/{artifact_id}/versions")
        _raise_with_detail(response)
    return json.dumps(response.json(), indent=2)


@tool(category="manuscript_planning")
async def rka_get_planning_argument_workflow(
    branch_id: str,
    *,
    project_id: str,
) -> str:
    """Read deterministic seed-to-contribution guidance for one branch."""
    async with _client(project_id) as c:
        response = await c.get(
            f"/api/planning/branches/{branch_id}/argument-workflow"
        )
        _raise_with_detail(response)
    return json.dumps(response.json(), indent=2)


@tool(category="manuscript_planning")
async def rka_get_planning_promotions(
    branch_id: str,
    *,
    project_id: str,
) -> str:
    """Read append-only candidate promotion lineage for one branch."""
    async with _client(project_id) as c:
        response = await c.get(f"/api/planning/branches/{branch_id}/promotions")
        _raise_with_detail(response)
    return json.dumps(response.json(), indent=2)


@tool(category="manuscript_planning")
async def rka_get_planning_evaluation_workflow(
    branch_id: str,
    *,
    project_id: str,
) -> str:
    """Resolve exact claim, experiment, observation, and locator readiness."""
    async with _client(project_id) as c:
        response = await c.get(
            f"/api/planning/branches/{branch_id}/evaluation-workflow"
        )
        _raise_with_detail(response)
    return json.dumps(response.json(), indent=2)


@tool(category="manuscript_planning")
async def rka_get_planning_evaluation_events(
    branch_id: str,
    *,
    project_id: str,
) -> str:
    """Read immutable mission and result-unit lineage for an evaluation branch."""
    async with _client(project_id) as c:
        response = await c.get(
            f"/api/planning/branches/{branch_id}/evaluation-events"
        )
        _raise_with_detail(response)
    return json.dumps(response.json(), indent=2)


@tool(category="manuscript_planning")
async def rka_create_planning_branch(payload: dict, *, project_id: str) -> str:
    """Create a project- or manuscript-scoped planning branch."""
    async with _client(project_id) as c:
        response = await c.post("/api/planning/branches", json=payload)
        _raise_with_detail(response)
    return json.dumps(response.json(), indent=2)


@tool(category="manuscript_planning")
async def rka_transition_planning_branch(
    branch_id: str,
    payload: dict,
    *,
    project_id: str,
) -> str:
    """Select, activate, archive, or supersede one planning branch."""
    async with _client(project_id) as c:
        response = await c.post(
            f"/api/planning/branches/{branch_id}/transition",
            json=payload,
        )
        _raise_with_detail(response)
    return json.dumps(response.json(), indent=2)


@tool(category="manuscript_planning")
async def rka_append_planning_artifact_version(
    branch_id: str,
    payload: dict,
    *,
    project_id: str,
) -> str:
    """Append an immutable typed planning-artifact version."""
    async with _client(project_id) as c:
        response = await c.post(
            f"/api/planning/branches/{branch_id}/artifacts",
            json=payload,
        )
        _raise_with_detail(response)
    return json.dumps(response.json(), indent=2)


@tool(category="manuscript_planning")
async def rka_promote_planning_research_question(
    branch_id: str,
    payload: dict,
    *,
    project_id: str,
) -> str:
    """Promote one selected RQ candidate into a PI decision."""
    async with _client(project_id) as c:
        response = await c.post(
            f"/api/planning/branches/{branch_id}/promote-rq",
            json=payload,
        )
        _raise_with_detail(response)
    return json.dumps(response.json(), indent=2)


@tool(category="manuscript_planning")
async def rka_prepare_planning_contribution(
    branch_id: str,
    payload: dict,
    *,
    project_id: str,
) -> str:
    """Prepare a guarded semantic proposal for one selected contribution."""
    async with _client(project_id) as c:
        response = await c.post(
            f"/api/planning/branches/{branch_id}/prepare-contribution",
            json=payload,
        )
        _raise_with_detail(response)
    return json.dumps(response.json(), indent=2)


@tool(category="manuscript_planning")
async def rka_ratify_planning_contribution(
    branch_id: str,
    payload: dict,
    *,
    project_id: str,
) -> str:
    """Ratify exact applied contribution wording against a PI decision."""
    async with _client(project_id) as c:
        response = await c.post(
            f"/api/planning/branches/{branch_id}/ratify-contribution",
            json=payload,
        )
        _raise_with_detail(response)
    return json.dumps(response.json(), indent=2)


@tool(category="manuscript_planning")
async def rka_create_planning_evaluation_mission(
    branch_id: str,
    payload: dict,
    *,
    project_id: str,
) -> str:
    """Create one canonical mission for a selected missing-evidence slot."""
    async with _client(project_id) as c:
        response = await c.post(
            f"/api/planning/branches/{branch_id}/evaluation-missions",
            json=payload,
        )
        _raise_with_detail(response)
    return json.dumps(response.json(), indent=2)


@tool(category="manuscript_planning")
async def rka_prepare_planning_evaluation_result(
    branch_id: str,
    payload: dict,
    *,
    project_id: str,
) -> str:
    """Prepare a bounded result-unit proposal from exactly located evidence."""
    async with _client(project_id) as c:
        response = await c.post(
            f"/api/planning/branches/{branch_id}/evaluation-result-proposals",
            json=payload,
        )
        _raise_with_detail(response)
    return json.dumps(response.json(), indent=2)


@tool(category="manuscript")
async def rka_prepare_manuscript_outline_proposal(
    manuscript_id: str,
    payload: dict,
    *,
    project_id: str,
) -> str:
    """Prepare an outline edit/expand/condense/reorder proposal; never auto-apply."""
    async with _client(project_id) as c:
        response = await c.post(
            f"/api/manuscripts/{manuscript_id}/outline/proposals",
            json=payload,
        )
        _raise_with_detail(response)
    return json.dumps(response.json(), indent=2)


@tool(category="semantic_patches")
async def rka_get_semantic_patch_proposals(
    proposal_id: str | None = None,
    status: str | None = None,
    limit: int = 100,
    *,
    project_id: str,
) -> str:
    """List semantic proposals or fetch one exact proposal, diff, and history."""
    async with _client(project_id) as c:
        if proposal_id:
            response = await c.get(f"/api/semantic-patches/proposals/{proposal_id}")
        else:
            response = await c.get(
                "/api/semantic-patches/proposals",
                params=_strip_none({"status": status, "limit": limit}),
            )
        _raise_with_detail(response)
    return json.dumps(response.json(), indent=2)


@tool(category="semantic_patches")
async def rka_get_semantic_patch_schema(*, project_id: str) -> str:
    """Get the exact host-agent JSON schema for a generated proposal draft."""
    async with _client(project_id) as c:
        response = await c.get("/api/semantic-patches/schema")
        _raise_with_detail(response)
    return json.dumps(response.json(), indent=2)


@tool(category="semantic_patches")
async def rka_prepare_semantic_patch_context(payload: dict, *, project_id: str) -> str:
    """Persist the exact AI disclosure manifest before host or local generation."""
    async with _client(project_id) as c:
        response = await c.post("/api/semantic-patches/context-manifests", json=payload)
        _raise_with_detail(response)
    return json.dumps(response.json(), indent=2)


@tool(category="semantic_patches")
async def rka_create_semantic_patch_proposal(payload: dict, *, project_id: str) -> str:
    """Create and validate a proposal without mutating its targets."""
    async with _client(project_id) as c:
        response = await c.post("/api/semantic-patches/proposals", json=payload)
        _raise_with_detail(response)
    return json.dumps(response.json(), indent=2)


@tool(category="semantic_patches")
async def rka_apply_semantic_patch_proposal(
    proposal_id: str,
    payload: dict,
    *,
    project_id: str,
) -> str:
    """Request apply; the REST gate rejects AI/MCP transport actors."""
    async with _client(project_id) as c:
        response = await c.post(
            f"/api/semantic-patches/proposals/{proposal_id}/apply", json=payload
        )
        _raise_with_detail(response)
    return json.dumps(response.json(), indent=2)


@tool(category="semantic_patches")
async def rka_reject_semantic_patch_proposal(
    proposal_id: str,
    payload: dict,
    *,
    project_id: str,
) -> str:
    """Request rejection; the REST gate rejects AI/MCP transport actors."""
    async with _client(project_id) as c:
        response = await c.post(
            f"/api/semantic-patches/proposals/{proposal_id}/reject", json=payload
        )
        _raise_with_detail(response)
    return json.dumps(response.json(), indent=2)


@tool(category="semantic_patches")
async def rka_generate_lm_studio_semantic_patch(payload: dict, *, project_id: str) -> str:
    """Ask configured local LM Studio for a proposal; never auto-apply it."""
    async with _client(project_id) as c:
        response = await c.post(
            "/api/semantic-patches/providers/lm-studio/proposals", json=payload
        )
        _raise_with_detail(response)
    return json.dumps(response.json(), indent=2)


@tool(category="claims")
async def rka_add_interpretation_hint(
    candidate_id: str,
    related_candidate_id: str,
    kind: str,
    rationale: str,
    created_by: str,
    expected_revision: int,
    confidence: float = 0.5,
    *,
    project_id: str,
) -> str:
    """Record a typed duplicate/conflict hint between candidates."""
    async with _client(project_id) as c:
        response = await c.post(
            f"/api/interpretations/{candidate_id}/hints",
            json={
                "related_candidate_id": related_candidate_id,
                "kind": kind,
                "confidence": confidence,
                "rationale": rationale,
                "created_by": created_by,
                "expected_revision": expected_revision,
            },
        )
        _raise_with_detail(response)
    return json.dumps(response.json(), indent=2)


@tool(category="claims")
async def rka_extract_claims(
    entry_id: str,
    claims: list[dict],
    *,
    project_id: str,
) -> str:
    """Brain stages atomic interpretations from a journal entry.

    This compatibility operation no longer writes canonical ``clm_`` rows.
    It creates reviewable ``icd_`` candidates. Promote each candidate through
    ``triage_interpretation_candidate`` after checking its exact grounding.

    Args:
        entry_id: Source journal entry ID (e.g. jnl_...)
        claims: List of claim objects, each with:
            - claim_type: hypothesis, evidence, method, result, observation, assumption
            - text or content: The atomic candidate statement
            - confidence: 0.0-1.0 (default 0.5)
            - epistemic_kind: Optional explicit epistemic kind
    """
    created = []
    if any(cl.get("cluster_id") for cl in claims):
        return json.dumps(
            {
                "error": "candidate_not_claim",
                "message": (
                    "cluster_id cannot be assigned during interpretation staging; "
                    "promote the candidate, then assign the resulting clm_ id"
                ),
            },
            indent=2,
        )
    async with _client(project_id) as c:
        for cl in claims:
            claim_type = cl["claim_type"]
            start = cl.get("source_offset_start")
            statement = cl.get("content") or cl.get("text")
            if not statement or not str(statement).strip():
                return json.dumps(
                    {
                        "error": "missing_field",
                        "message": "each extract_claims item requires non-empty text or content",
                    },
                    indent=2,
                )
            statement = str(statement).strip()
            epistemic_kind = cl.get("epistemic_kind") or {
                "hypothesis": "hypothesis",
                "observation": "observation",
                "evidence": "reported_fact",
                "result": "reported_fact",
                "method": "reported_fact",
                "assumption": "hypothesis",
            }.get(claim_type, "inference")
            payload = {
                "source_type": "journal",
                "source_id": entry_id,
                "locator_kind": "text_offset" if start is not None else "record",
                "locator_start": start,
                "locator_end": cl.get("source_offset_end"),
                "locator_value": None if start is not None else "full_record",
                "statement": statement,
                "epistemic_kind": epistemic_kind,
                "scope_conditions": cl.get("scope_conditions", []),
                "uncertainty": cl.get("uncertainty", "unknown"),
                "uncertainty_note": cl.get("uncertainty_note"),
                "falsifier": cl.get("falsifier"),
                "proposed_claim_type": claim_type,
                "created_by": "brain",
                "extraction_tool": "rka_execute.extract_claims",
                "extraction_model": cl.get("extraction_model"),
            }
            r = await c.post("/api/interpretations", json=_strip_none(payload))
            _raise_with_detail(r)
            result = r.json()
            created.append(result)
            _record_entity(
                "interpretation_candidate",
                result["id"],
                f"{epistemic_kind}: {statement[:60]}",
            )

    # Mission 2 — fire post_claim_extract via the server-side hook endpoint.
    # Composite event: payload carries entry_id + claim_ids[] (the spec shape).
    # Failures silent.
    try:
        async with _client(project_id) as c:
            await c.post(
                "/api/hooks/fire",
                json={
                    "event": "post_claim_extract",
                    "payload": {
                        "entry_id": entry_id,
                        "claim_ids": [],
                        "candidate_ids": [cl["id"] for cl in created],
                        "source": "brain",
                    },
                },
            )
    except Exception:
        pass

    lines = [f"Staged {len(created)} interpretation candidates from {entry_id}:"]
    for cl in created:
        lines.append(
            f"  [{cl['id']}] ({cl['epistemic_kind']}; proposed "
            f"{cl.get('proposed_claim_type') or 'unclassified'}) rev={cl['revision']}"
        )
        lines.append(f"    {cl['statement'][:200]}")
    return "\n".join(lines)


@tool(category="claims")
async def rka_create_cluster(
    label: str,
    research_question_id: str | None = None,
    synthesis: str | None = None,
    confidence: str = "emerging",
    claim_ids: list[str] | None = None,
    *,
    project_id: str,
) -> str:
    """Brain creates an evidence cluster and optionally assigns claims to it.

    Args:
        label: Short label for the cluster theme
        research_question_id: Decision ID (kind=research_question) this cluster addresses
        synthesis: Brain's synthesis paragraph (can be added later via rka_review_cluster)
        confidence: strong, moderate, emerging, contested, refuted
        claim_ids: Optional list of claim IDs to assign as members
    """
    payload = {
        "label": label,
        "confidence": confidence,
    }
    if research_question_id:
        payload["research_question_id"] = research_question_id
    if synthesis:
        payload["synthesis"] = synthesis

    async with _client(project_id) as c:
        r = await c.post("/api/clusters", json=payload)
        _raise_with_detail(r)
        cluster = r.json()
        cluster_id = cluster["id"]
        _record_entity("cluster", cluster_id, f"Cluster: {label[:60]}")

        assigned = 0
        if claim_ids:
            for cid in claim_ids:
                edge_payload = {
                    "source_claim_id": cid,
                    "cluster_id": cluster_id,
                    "relation": "member_of",
                    "confidence": 0.5,
                }
                er = await c.post("/api/claims/edges", json=edge_payload)
                if er.is_success:
                    assigned += 1

            # Update cluster claim count
            r2 = await c.put(f"/api/clusters/{cluster_id}", json={
                "needs_reprocessing": False,
                "synthesized_by": "brain",
            })

    parts = [f"Created cluster {cluster_id} ({label}), confidence={confidence}"]
    if assigned:
        parts.append(f"{assigned} claims assigned")
    if research_question_id:
        parts.append(f"linked to RQ {research_question_id}")
    return ", ".join(parts)


@tool(category="claims")
async def rka_assign_claims_to_cluster(
    cluster_id: str,
    claim_ids: list[str],
    *,
    project_id: str,
) -> str:
    """Brain assigns existing claims to an existing evidence cluster.

    Creates member_of edges between claims and the cluster.

    Args:
        cluster_id: Target evidence cluster ID
        claim_ids: List of claim IDs to assign
    """
    results = []
    async with _client(project_id) as c:
        for cid in claim_ids:
            edge_payload = {
                "source_claim_id": cid,
                "cluster_id": cluster_id,
                "relation": "member_of",
                "confidence": 0.5,
            }
            r = await c.post("/api/claims/edges", json=edge_payload)
            if r.is_success:
                results.append(f"  {cid}: assigned")
            else:
                results.append(f"  {cid}: failed ({r.status_code})")

    return f"Assigned {len([r for r in results if 'assigned' in r])}/{len(claim_ids)} claims to cluster {cluster_id}:\n" + "\n".join(results)


@tool(category="decision")
async def rka_supersede_decision(
    old_decision_id: str,
    question: str,
    chosen: str,
    rationale: str,
    decided_by: str = "brain",
    phase: str | None = None,
    kind: str = "decision",
    related_journal: list[str] | None = None,
    related_literature: list[str] | None = None,
    related_missions: list[str] | None = None,
    parent_id: str | None = None,
    options: list[dict] | None = None,
    assumptions: list[str] | None = None,
    tags: list[str] | None = None,
    status: str | None = None,
    *,
    project_id: str,
) -> str:
    """Atomically supersede a decision and trigger re-distillation of affected knowledge.

    Marks the old decision as superseded, creates a new replacement decision,
    finds all journal entries linked to the old decision, marks their claims as stale,
    and enqueues re-distillation jobs.

    Args:
        old_decision_id: ID of the decision to supersede
        question: New decision question
        chosen: New chosen option
        rationale: Why the old decision is being overturned
        decided_by: Actor making the decision (brain, executor, pi)
        phase: Research phase. v2.7.0.6 — optional; when omitted, the
            service layer inherits from the OLD decision's phase. Pass
            an explicit value to cross phases.
        kind: Decision kind (decision, research_question, design_choice, operational)
        related_journal/related_literature/related_missions/parent_id/options/
        assumptions/tags/status: forwarded to the new decision so the supersede
            path has parity with rka_add_decision (preserves provenance discipline
            instead of silently dropping these fields).
    """
    # v2.7.0.5 — the FastAPI route at /api/decisions/{old}/supersede binds the
    # JSON body to DecisionSupersedeBody (extra='forbid'). The body must be flat;
    # old_decision_id stays in URL path only. Optional fields are stripped
    # below so DecisionSupersedeBody's defaults / None handling kicks in.
    body: dict = {
        "question": question,
        "chosen": chosen,
        "rationale": rationale,
        "decided_by": decided_by,
        "kind": kind,
    }
    for key, value in (
        ("phase", phase),
        ("related_journal", related_journal),
        ("related_literature", related_literature),
        ("related_missions", related_missions),
        ("parent_id", parent_id),
        ("options", options),
        ("assumptions", assumptions),
        ("tags", tags),
        ("status", status),
    ):
        if value is not None:
            body[key] = value

    async with _client(project_id) as c:
        r = await c.post(f"/api/decisions/{old_decision_id}/supersede", json=body)
        _raise_with_detail(r)
    result = r.json()
    _record_entity("decision", result.get("id", "?"), f"Supersedes {old_decision_id}: {question[:60]}")
    return json.dumps(result, indent=2, default=str)


# Which stored endpoint depends on the other endpoint for provenance purposes.
# RKA's link arrows are intentionally not a single causal convention:
# ``decision --justified_by--> journal`` stores the dependent at ``source``,
# while ``decision --motivated--> mission`` stores it at ``target``.  Raw
# source/target traversal therefore reverses part of every real lifecycle.
_PROVENANCE_DEPENDENT_ENDPOINT: dict[str, Literal["source", "target"]] = {
    # The source record rests on, cites, replaces, or is scoped by the target.
    "justified_by": "source",
    "derived_from": "source",
    "cites": "source",
    "references": "source",
    "answers": "source",
    "builds_on": "source",
    "supersedes": "source",
    # The target record follows from or is supported by the source.
    "informed_by": "target",
    "motivated": "target",
    "produced": "target",
    "triggered": "target",
    "resolved_as": "target",
    "evidence_for": "target",
    "supports": "target",
    "qualifies": "target",
    "member_of": "target",
}


def _provenance_endpoint_type(
    edge: dict,
    endpoint: Literal["source", "target"],
    nodes: dict[str, dict] | None,
) -> str | None:
    """Resolve an endpoint type from the edge, ego nodes, or stable ID prefix."""
    explicit_type = edge.get(f"{endpoint}_type")
    if isinstance(explicit_type, str):
        return explicit_type

    entity_id = edge.get(endpoint)
    if not isinstance(entity_id, str):
        return None
    node = (nodes or {}).get(entity_id, {})
    node_type = node.get("type") if isinstance(node, dict) else None
    if isinstance(node_type, str):
        return node_type

    prefix_types = {
        "jrn": "journal",
        "lit": "literature",
        "dec": "decision",
    }
    return prefix_types.get(entity_id.partition("_")[0])


def _provenance_dependent_endpoint(
    edge: dict,
    nodes: dict[str, dict] | None,
) -> Literal["source", "target"] | None:
    """Return the causal dependent endpoint, including typed legacy variants."""
    relation = edge.get("link_type")
    dependent_endpoint = _PROVENANCE_DEPENDENT_ENDPOINT.get(relation)
    if relation != "informed_by":
        return dependent_endpoint

    # Canonical decision provenance is literature -> decision, so the target
    # depends on the source.  The still-supported process_paper aggregate has
    # historically stored reading-note provenance as journal -> literature
    # using the same relation name; in that typed shape the source depends on
    # the target.  Preserve both stores without guessing from arrow direction.
    source_type = _provenance_endpoint_type(edge, "source", nodes)
    target_type = _provenance_endpoint_type(edge, "target", nodes)
    if (source_type, target_type) == ("journal", "literature"):
        return "source"
    return dependent_endpoint


def _provenance_neighbor(
    edge: dict,
    current_id: str,
    *,
    direction: Literal["backward", "forward"],
    nodes: dict[str, dict] | None = None,
) -> str | None:
    """Return the next causal node for one stored edge, if traversable."""
    source = edge.get("source")
    target = edge.get("target")
    dependent_endpoint = _provenance_dependent_endpoint(edge, nodes)
    if not isinstance(source, str) or not isinstance(target, str):
        return None
    if dependent_endpoint == "source":
        dependent, dependency = source, target
    elif dependent_endpoint == "target":
        dependent, dependency = target, source
    else:
        # The database constrains current relation names.  If a future
        # migration adds one, omit it until its causal semantics are explicit
        # instead of silently guessing from physical edge orientation.
        return None
    if direction == "backward" and current_id == dependent:
        return dependency
    if direction == "forward" and current_id == dependency:
        return dependent
    return None


def _walk_provenance_edges(
    entity_id: str,
    edges: list[dict],
    *,
    direction: Literal["backward", "forward"],
    max_depth: int,
    nodes: dict[str, dict] | None = None,
) -> list[dict[str, str | int]]:
    """Directed BFS that emits every relation while expanding each node once."""
    ordered_edges = sorted(
        edges,
        key=lambda edge: (
            str(edge.get("link_type", "")),
            str(edge.get("source", "")),
            str(edge.get("target", "")),
        ),
    )
    visited = {entity_id}
    emitted_edges: set[tuple[str, str, str, str, str]] = set()
    frontier = [entity_id]
    steps: list[dict[str, str | int]] = []
    for depth in range(1, max_depth + 1):
        next_frontier: list[str] = []
        for current_id in frontier:
            for edge in ordered_edges:
                neighbor = _provenance_neighbor(
                    edge,
                    current_id,
                    direction=direction,
                    nodes=nodes,
                )
                if neighbor is None:
                    continue
                edge_key = (
                    current_id,
                    neighbor,
                    str(edge.get("link_type", "?")),
                    str(edge.get("source", "")),
                    str(edge.get("target", "")),
                )
                if edge_key in emitted_edges:
                    continue
                emitted_edges.add(edge_key)
                steps.append(
                    {
                        "entity_id": neighbor,
                        "from_id": current_id,
                        "link_type": str(edge.get("link_type", "?")),
                        "depth": depth,
                    }
                )
                # A node may be reached by parallel relations, converging
                # paths, or a cycle.  Emit every distinct edge above, but only
                # enqueue a newly discovered node so traversal stays finite.
                if neighbor not in visited:
                    visited.add(neighbor)
                    next_frontier.append(neighbor)
        if not next_frontier:
            break
        frontier = next_frontier
    return steps


def _provenance_disagreements(
    entity_ids: set[str],
    edges: list[dict],
) -> list[dict]:
    """Return unique contradiction edges touching the rendered causal story."""
    disagreements: list[dict] = []
    seen_pairs: set[tuple[str, str]] = set()
    for edge in sorted(
        edges,
        key=lambda item: (
            str(item.get("source", "")),
            str(item.get("target", "")),
        ),
    ):
        if edge.get("link_type") != "contradicts":
            continue
        source = edge.get("source")
        target = edge.get("target")
        if not isinstance(source, str) or not isinstance(target, str):
            continue
        if source not in entity_ids and target not in entity_ids:
            continue
        pair = tuple(sorted((source, target)))
        if pair in seen_pairs:
            continue
        seen_pairs.add(pair)
        disagreements.append(edge)
    return disagreements


@tool(category="claims")
async def rka_trace_provenance(
    entity_id: str,
    direction: str = "both",
    max_depth: int = 3,
    *,
    project_id: str,
) -> str:
    """Trace the full reasoning chain behind any entity.

    Follows typed causal links (informed_by, justified_by, motivated, produced,
    derived_from, cites, references, supersedes) to show why something exists.
    Contradictions remain visible in a separate non-causal disagreement section.

    Args:
        entity_id: The entity ID to trace from (any type: jrn_, dec_, clm_, etc.)
        direction: backward (what led to this), forward (what this led to),
            or both. Legacy aliases upstream (backward) and downstream
            (forward) are accepted.
        max_depth: Maximum hops to traverse (default 3; endpoint range 1-3)
    """
    direction_aliases = {
        "backward": "upstream",
        "forward": "downstream",
        "upstream": "upstream",
        "downstream": "downstream",
        "both": "both",
    }
    normalized_direction = direction_aliases.get(direction)
    if normalized_direction is None:
        raise ValueError(
            "direction must be one of: backward, forward, both "
            "(legacy aliases: upstream, downstream)"
        )
    if (
        isinstance(max_depth, bool)
        or not isinstance(max_depth, int)
        or not 1 <= max_depth <= 3
    ):
        raise ValueError("max_depth must be an integer between 1 and 3")

    async with _client(project_id) as c:
        r = await c.get(f"/api/graph/ego/{entity_id}", params={"depth": max_depth})
        _raise_with_detail(r)
    data = r.json()
    nodes = {n["id"]: n for n in data.get("nodes", [])}
    edges = data.get("edges", [])

    lines = [f"Provenance for {entity_id}:"]
    rendered_entity_ids = {entity_id}

    if normalized_direction in ("upstream", "both"):
        lines.append("\n  Upstream (what led to this):")
        upstream = _walk_provenance_edges(
            entity_id,
            edges,
            direction="backward",
            max_depth=max_depth,
            nodes=nodes,
        )
        if not upstream:
            lines.append("    (none)")
        for step in upstream:
            rendered_entity_ids.add(str(step["entity_id"]))
            node = nodes.get(step["entity_id"], {})
            indent = "    " + "  " * (step["depth"] - 1)
            lines.append(
                f"{indent}← {step['link_type']} {step['entity_id']} "
                f"[{node.get('type', '?')}] {node.get('label', '')[:80]} "
                f"(depth={step['depth']}, from={step['from_id']})"
            )

    if normalized_direction in ("downstream", "both"):
        lines.append("\n  Downstream (what this led to):")
        downstream = _walk_provenance_edges(
            entity_id,
            edges,
            direction="forward",
            max_depth=max_depth,
            nodes=nodes,
        )
        if not downstream:
            lines.append("    (none)")
        for step in downstream:
            rendered_entity_ids.add(str(step["entity_id"]))
            node = nodes.get(step["entity_id"], {})
            indent = "    " + "  " * (step["depth"] - 1)
            lines.append(
                f"{indent}→ {step['link_type']} {step['entity_id']} "
                f"[{node.get('type', '?')}] {node.get('label', '')[:80]} "
                f"(depth={step['depth']}, from={step['from_id']})"
            )

    disagreements = _provenance_disagreements(rendered_entity_ids, edges)
    if disagreements:
        lines.append("\n  Contextual disagreements (non-causal):")
        for edge in disagreements:
            source_id = edge["source"]
            target_id = edge["target"]
            source_node = nodes.get(source_id, {})
            target_node = nodes.get(target_id, {})
            lines.append(
                f"    ↔ contradicts {source_id} "
                f"[{source_node.get('type', '?')}] "
                f"{source_node.get('label', '')[:60]} ↔ {target_id} "
                f"[{target_node.get('type', '?')}] "
                f"{target_node.get('label', '')[:60]}"
            )

    return "\n".join(lines)


@tool(category="claims")
async def rka_multi_hop_retrieval(
    query: str,
    seeds: list[str] | None = None,
    max_depth: int = 3,
    max_nodes: int = 50,
    edge_weights: dict[str, float] | None = None,
    *,
    project_id: str,
) -> str:
    """Query-anchored multi-hop subgraph retrieval (v2.4).

    Seeds the traversal with the top hits from rka_search(query), then BFS-expands
    through entity_links and claim_edges with per-edge weights. Each node accumulates
    a relevance score; results are capped by max_nodes and ordered by relevance
    descending. Use this when a single rka_search isn't enough — when the answer
    depends on connected entities multiple hops from the seeds (e.g., decisions
    motivated by missions whose findings are recorded in journal entries
    contradicted by later claims).

    Per dec_01KQQPDHCKHS4YMD6QP7J7K2GW; default edge weights from
    dec_01KQQRZ0CJHB68P2F6233AHEJ5.

    Args:
        query: Natural-language query for seed selection.
        seeds: Optional explicit seed entity IDs. Bypasses search-based seeding.
        max_depth: Max BFS depth (default 3, capped at 5 by the API).
        max_nodes: Max nodes returned (default 50, capped at 500).
        edge_weights: Per-relation weights override. Defaults are
            justified_by/motivated/derived_from/evidence_for/member_of=1.0,
            contradicts=1.1, supports/qualifies=0.9,
            cites/references/informed_by=0.7, produced/resolved_as/builds_on=0.5,
            supersedes=0.3.
    """
    body: dict = {"query": query, "max_depth": max_depth, "max_nodes": max_nodes}
    if seeds is not None:
        body["seeds"] = seeds
    if edge_weights is not None:
        body["edge_weights"] = edge_weights

    async with _client(project_id) as c:
        r = await c.post("/api/graph/multi-hop", json=body)
        _raise_with_detail(r)
    data = r.json()
    nodes = data.get("nodes", [])
    edges = data.get("edges", [])
    seeds_used = data.get("seeds", [])

    lines = [
        f"## Multi-hop subgraph for: {query}",
        f"Seeds: {', '.join(seeds_used) if seeds_used else '(none — search returned nothing)'}",
        f"Returned {len(nodes)} nodes, {len(edges)} edges (max_depth={max_depth}, max_nodes={max_nodes}).",
    ]
    if not nodes:
        lines.append("\n(empty result — try a broader query or explicit seeds)")
        return "\n".join(lines)

    lines.append("\n### Ranked nodes (by relevance score):")
    for n in nodes:
        score = n.get("score", 0.0)
        depth = n.get("depth", 0)
        label = (n.get("label") or "")[:80]
        lines.append(f"  [{n.get('type', '?')}|d={depth}|s={score:.3f}] {n.get('id', '?')} {label}")

    if edges:
        lines.append(f"\n### Edges ({len(edges)} shown, weighted):")
        # Show edges sorted by weight descending so the most-load-bearing surface first
        for e in sorted(edges, key=lambda x: x.get("weight", 0.0), reverse=True)[:30]:
            w = e.get("weight", 0.0)
            lines.append(f"  {e.get('source', '?')} —[{e.get('link_type', '?')} w={w:.2f}]→ {e.get('target', '?')}")

    return "\n".join(lines)


@tool(category="claims")
async def rka_collect_report_context(
    description: str,
    angle_queries: list[str] | None = None,
    max_depth: int = 2,
    max_nodes: int = 60,
    seed_limit: int = 8,
    *,
    project_id: str,
) -> str:
    """Collect the knowledge-base node set relevant to a report described in prose.

    Composite retrieval for the "I want to write a report about X" workflow:
    seeds from EVERY angle query (short 1-4 word queries you derive from the
    description — provide 3-5 of them, from different angles: components,
    bugs/fixes, decisions, evaluations), BFS-expands through entity_links +
    claim_edges with provenance-weighted edges, never lets expansion displace
    seeds, and annotates every node with `included_via` (which query + rank
    surfaced it, or which parent + link type reached it) so the bundle is
    auditable.

    ALWAYS pass angle_queries — eval-v3 measured 0.32 mean cohort recall for
    paragraph-only seeding vs 0.80 for angle-decomposed agent retrieval. After
    the call, verify borderline nodes by fetching their content, and run
    follow-up searches for report dimensions that came back thin.

    Args:
        description: The PI's prose description of the report scope.
        angle_queries: Short seed queries decomposing the description into
            search angles. The keyword-normalized description is always
            appended as one extra angle automatically.
        max_depth: BFS expansion depth (default 2, capped at 4).
        max_nodes: Result cap (default 60); seeds are exempt from the cap.
        seed_limit: Search hits taken per angle query (default 8).
    """
    body: dict = {
        "description": description,
        "max_depth": max_depth,
        "max_nodes": max_nodes,
        "seed_limit": seed_limit,
    }
    if angle_queries is not None:
        body["angle_queries"] = angle_queries

    async with _client(project_id) as c:
        r = await c.post("/api/graph/report-context", json=body)
        _raise_with_detail(r)
    data = r.json()
    nodes = data.get("nodes", [])

    lines = [
        f"## Report context for: {description[:120]}",
        f"Angle queries used: {', '.join(data.get('queries', []))}",
        f"{data.get('seed_count', 0)} seeds + {data.get('expanded_count', 0)} link-expanded nodes"
        + (" (expansion truncated by max_nodes)" if data.get("truncated") else ""),
    ]
    if not nodes:
        lines.append("\n(empty result — add angle_queries with distinctive short keywords)")
        return "\n".join(lines)

    lines.append("\n### Nodes (score-ranked; seeds first):")
    for n in nodes:
        via = n.get("included_via", {})
        if via.get("via") == "search":
            via_s = f"search:{via.get('query', '')[:30]}#{via.get('rank')}"
        else:
            via_s = f"{via.get('link_type', '?')}←{via.get('from', '?')[-8:]}"
        tags = ",".join(n.get("tags", [])[:4])
        label = (n.get("label") or "")[:70]
        lines.append(
            f"  [{n.get('type', '?')}|s={n.get('score', 0.0):.2f}|{via_s}] "
            f"{n.get('id', '?')} {label}" + (f" «{tags}»" if tags else "")
        )
    return "\n".join(lines)


@tool(category="claims")
async def rka_staleness_impact(
    entity_id: str,
    max_depth: int = 3,
    *,
    project_id: str,
) -> str:
    """Downstream blast-radius of a stale (or about-to-be-stale) entity.

    Walks DEPENDENT-direction links only (justified_by, derived_from, cites,
    answers, motivated, member_of, ...): everything whose reasoning rests on
    this entity. Use before/after superseding a decision or retracting a
    finding to see what else needs review. Raw observations (produced) are
    immutable and excluded by design.
    """
    async with _client(project_id) as c:
        r = await c.get(f"/api/graph/staleness-impact/{entity_id}",
                        params={"max_depth": max_depth})
        _raise_with_detail(r)
    data = r.json()
    lines = [
        f"## Staleness impact of {entity_id} (status={data.get('root_status')})",
        f"{len(data.get('impacted', []))} dependent entities within depth {max_depth}:",
    ]
    for n in data.get("impacted", []):
        via = n.get("via", {})
        lines.append(
            f"  [d{n.get('depth')}|{n.get('type')}|{n.get('status')}] {n.get('id')} "
            f"via {via.get('link_type')} from {via.get('from','')[-8:]} "
            f"{(n.get('label') or '')[:70]}"
        )
    if not data.get("impacted"):
        lines.append("  (no dependents found)")
    return "\n".join(lines)


@tool(category="missions")
async def rka_mission_guard(
    mission_id: str,
    *,
    project_id: str,
) -> str:
    """Negative knowledge relevant to a mission, for Executor pickup.

    Surfaces retracted/superseded findings and unresolved contradictions
    whose content overlaps the mission objective: approaches already
    falsified or contested that the Executor must not repeat unknowingly.
    Call this at mission pickup, alongside the mission context.
    """
    async with _client(project_id) as c:
        r = await c.get(f"/api/missions/{mission_id}/guard")
        _raise_with_detail(r)
    data = r.json()
    warnings = data.get("warnings", [])
    lines = [f"## Mission guard for {mission_id}: {len(warnings)} warnings"]
    for w in warnings:
        lines.append(
            f"  [{w.get('kind')}|rel={w.get('relevance')}] {w.get('id')}: "
            f"{w.get('excerpt','')[:90]}"
        )
        lines.append(f"    -> {w.get('guidance')}")
    if not warnings:
        lines.append("  (no relevant negative knowledge found)")
    return "\n".join(lines)


@tool(category="claims")
async def rka_belief_as_of(
    date: str,
    *,
    project_id: str,
) -> str:
    """Reconstruct the believed-current knowledge state at a past date.

    'What did we believe in March, and what changed since?' Supersession
    transitions are exact (successor created_at); retraction transitions are
    approximated by updated_at and marked approximate.

    Args:
        date: ISO date or timestamp, e.g. "2026-03-15".
    """
    async with _client(project_id) as c:
        r = await c.get("/api/graph/as-of", params={"date": date})
        _raise_with_detail(r)
    data = r.json()
    tc = data.get("then_current", {})
    lines = [
        f"## Belief state as of {data.get('as_of')}",
        f"Then-current: {len(tc.get('decisions', []))} decisions, "
        f"{tc.get('journal_count', 0)} journal entries.",
        "### Then-current decisions:",
    ]
    for d in tc.get("decisions", [])[:30]:
        lines.append(f"  {d['id']} {d['question'][:70]} -> {d['chosen'][:50]}")
    changed = data.get("changed_since", [])
    lines.append(f"### Changed since ({len(changed)}):")
    for ch in changed[:30]:
        approx = " (approx)" if ch.get("approximate") else ""
        lines.append(
            f"  {ch['id']} [{ch.get('type')}] was: {ch.get('was','')[:60]} "
            f"-> changed {ch.get('changed_at','?')[:10]}{approx}"
        )
    lines.append(f"Note: {data.get('note','')}")
    return "\n".join(lines)


@tool(category="claims")
async def rka_get_review_queue(
    status: str = "pending",
    limit: int = 20,
    *,
    project_id: str,
) -> str:
    """Get items in the Brain review queue.

    The review queue contains items that need Brain-level attention:
    low-confidence clusters, potential contradictions, complex syntheses,
    re-distillation reviews, cross-topic links, and stale themes.

    Args:
        status: Filter by status (pending, acknowledged, resolved, dismissed)
        limit: Max results
    """
    async with _client(project_id) as c:
        r = await c.get("/api/review-queue", params={"status": status, "limit": limit})
        _raise_with_detail(r)
    items = r.json()
    if not items:
        return f"No {status} review items."
    lines = [f"Review queue ({len(items)} {status} items):"]
    for item in items:
        lines.append(f"  [{item['id']}] {item['flag']} — {item['item_type']}:{item['item_id']}")
        if item.get("context"):
            ctx = item["context"] if isinstance(item["context"], str) else json.dumps(item["context"])
            lines.append(f"    Context: {ctx[:500]}")
        lines.append(f"    Priority: {item.get('priority', '?')} | Raised by: {item.get('raised_by', '?')}")
    return "\n".join(lines)


@tool(category="claims")
async def rka_review_cluster(
    cluster_id: str,
    confidence: str,
    synthesis: str,
    gaps: list[str] | None = None,
    contradictions: list[str] | None = None,
    resolve_queue_items: list[str] | None = None,
    research_question_id: str | None = None,
    *,
    project_id: str,
) -> str:
    """Brain reviews and enriches an evidence cluster.

    The Brain evaluates a cluster's evidence and writes back a definitive
    synthesis with proper confidence assessment. This replaces the local LLM's synthesis.

    Args:
        cluster_id: Evidence cluster to review
        confidence: Brain's assessed confidence (strong, moderate, emerging, contested, refuted)
        synthesis: Brain's written synthesis paragraph
        gaps: Brain-identified evidence gaps
        contradictions: Brain-confirmed contradictions
        resolve_queue_items: Review queue item IDs to mark as resolved
        research_question_id: Decision ID (kind=research_question) to assign this cluster to
    """
    # Update cluster
    payload = {
        "synthesis": synthesis,
        "confidence": confidence,
        "synthesized_by": "brain",
        "needs_reprocessing": False,
        "research_question_id": research_question_id,
    }
    body = {k: v for k, v in payload.items() if v is not None}
    async with _client(project_id) as c:
        r = await c.put(f"/api/clusters/{cluster_id}", json=body)
        _raise_with_detail(r)

        # Resolve review queue items
        if resolve_queue_items:
            for item_id in resolve_queue_items:
                await c.put(f"/api/review-queue/{item_id}", json={
                    "status": "resolved",
                    "resolved_by": "brain",
                    "resolution": f"Cluster {cluster_id} reviewed: {confidence}",
                })

    parts = [f"Cluster {cluster_id} updated: confidence={confidence}, synthesized_by=brain"]
    if research_question_id:
        parts.append(f"assigned to RQ {research_question_id}")
    return ", ".join(parts)


@tool(category="claims")
async def rka_review_claims(
    claim_ids: list[str],
    action: str = "approve",
    confidence_override: float | None = None,
    evidence_status: EvidenceStatusLit | None = None,
    *,
    project_id: str,
) -> str:
    """Brain reviews claim grounding and explicit evidence assessments.

    Args:
        claim_ids: List of claim IDs to review
        action: approve (mark source-grounded), reject (mark stale and ungrounded),
            adjust (set numeric confidence and/or evidence_status)
        confidence_override: New confidence value (0.0-1.0), used with action=adjust
        evidence_status: Explicit scientific evidence assessment. This is never
            inferred from approve/reject and is independent from verified.
    """
    results = []
    async with _client(project_id) as c:
        for cid in claim_ids:
            if action == "approve":
                payload = {"verified": True}
            elif action == "reject":
                payload = {"stale": True, "verified": False}
            elif action == "adjust" and (
                confidence_override is not None or evidence_status is not None
            ):
                payload = {}
            else:
                results.append(f"{cid}: invalid action")
                continue
            if action == "adjust" and confidence_override is not None:
                payload["confidence"] = confidence_override
            if evidence_status is not None:
                payload["evidence_status"] = evidence_status
            r = await c.put(f"/api/claims/{cid}", json=payload)
            if r.is_success:
                results.append(f"{cid}: {action}d")
            else:
                results.append(f"{cid}: failed ({r.status_code})")
    return "\n".join(results)


@tool(category="claims")
async def rka_resolve_contradiction(
    cluster_id: str,
    resolution: str,
    claim_actions: dict[str, str] | None = None,
    *,
    project_id: str,
) -> str:
    """Brain resolves a flagged contradiction within an evidence cluster.

    Args:
        cluster_id: The cluster containing the contradiction
        resolution: Brain's explanation of how the contradiction is resolved
        claim_actions: Dict of claim_id → action (keep, reject, reframe). Optional.
    """
    lines = [f"Resolving contradiction in cluster {cluster_id}"]
    async with _client(project_id) as c:
        if claim_actions:
            for cid, action in claim_actions.items():
                if action == "reject":
                    await c.put(f"/api/claims/{cid}", json={"stale": True})
                    lines.append(f"  {cid}: marked stale")
                elif action == "keep":
                    lines.append(f"  {cid}: kept")
                elif action == "reframe":
                    lines.append(f"  {cid}: flagged for re-extraction")

        # Resolve matching review queue items
        r = await c.get("/api/review-queue", params={"status": "pending"})
        if r.is_success:
            for item in r.json():
                if item.get("item_id") == cluster_id and item.get("flag") == "potential_contradiction":
                    await c.put(f"/api/review-queue/{item['id']}", json={
                        "status": "resolved",
                        "resolved_by": "brain",
                        "resolution": resolution,
                    })
                    lines.append(f"  Resolved review item {item['id']}")

    lines.append(f"  Resolution: {resolution}")
    return "\n".join(lines)


# ============================================================
# Researcher experience tools
# ============================================================


@tool(category="session")
async def rka_get_changelog(
    since: str,
    limit: int = 50,
    *,
    project_id: str,
) -> str:
    """Show what changed since a given date across all entity types.

    Returns created and modified entities (journal, decisions, literature,
    claims, clusters, missions) grouped by action, with a statistics block.

    Args:
        since: ISO date/datetime (e.g. "2026-04-10" or "2026-04-10T14:00:00Z")
        limit: Max results per category (default 50)
    """
    async with _client(project_id) as c:
        r = await c.get("/api/changelog", params={"since": since, "limit": limit})
        _raise_with_detail(r)
        data = r.json()

    stats = data.get("statistics", {})
    created = data.get("created", [])
    modified = data.get("modified", [])

    lines = [f"## Changelog since {since}"]
    lines.append(f"Created: {stats.get('total_created', 0)} | Modified: {stats.get('total_modified', 0)}")
    by_type = stats.get("by_type", {})
    if by_type:
        lines.append("By type: " + ", ".join(f"{k}: {v}" for k, v in by_type.items()))
    lines.append("")

    if created:
        lines.append("### Created")
        for e in created:
            lines.append(f"  [{e['entity_type']}] {e['id']}: {e.get('label', '')}")
        lines.append("")

    if modified:
        lines.append("### Modified")
        for e in modified:
            lines.append(f"  [{e['entity_type']}] {e['id']}: {e.get('label', '')}")

    if not created and not modified:
        lines.append("No changes since " + since)

    return "\n".join(lines)


@tool(category="claims")
async def rka_assemble_evidence(
    research_question_id: str,
    format: str = "progress_report",
    *,
    project_id: str,
) -> str:
    """Assemble evidence under a research question into a structured markdown draft.

    Pulls cluster syntheses, key claims, decision rationale, and cited literature
    into a document the Brain can edit and refine. No LLM needed — structured
    concatenation of existing data.

    Args:
        research_question_id: The RQ decision ID (dec_...)
        format: lit_review | progress_report | proposal_section
    """
    async with _client(project_id) as c:
        r = await c.get("/api/assemble-evidence", params={
            "research_question_id": research_question_id,
            "format": format,
        })
        _raise_with_detail(r)
        data = r.json()

    return data["content"]


@tool(category="claims")
async def rka_split_cluster(
    source_id: str,
    new_clusters: list[dict],
    *,
    project_id: str,
) -> str:
    """Split a cluster into multiple new clusters by reassigning its claims.

    Claims not mentioned in any new_cluster stay in the source.
    Claim-to-entry provenance links are preserved — only cluster membership changes.

    Args:
        source_id: Cluster to split (ecl_...)
        new_clusters: List of {label: str, claim_ids: [str], research_question_id?: str}
    """
    async with _client(project_id) as c:
        r = await c.post("/api/clusters/split", json={
            "source_id": source_id,
            "new_clusters": new_clusters,
        })
        _raise_with_detail(r)
        data = r.json()

    lines = [f"Split {source_id} → {len(data['new_clusters'])} new clusters:"]
    for nc in data["new_clusters"]:
        lines.append(f"  {nc['id']}: {nc['label']} ({nc['claim_count']} claims)")
        _record_entity("cluster", nc["id"], nc["label"])
    lines.append(f"Source retains {data['source_remaining_claims']} claims")
    return "\n".join(lines)


@tool(category="claims")
async def rka_merge_clusters(
    source_ids: list[str],
    target_label: str,
    target_synthesis: str | None = None,
    research_question_id: str | None = None,
    *,
    project_id: str,
) -> str:
    """Merge multiple clusters into one new cluster.

    All claims from source clusters are reassigned to the new cluster.
    Source clusters are left empty. Claim-to-entry provenance links are preserved.

    Args:
        source_ids: Clusters to merge (list of ecl_...)
        target_label: Label for the new combined cluster
        target_synthesis: Brain's synthesis for the merged cluster
        research_question_id: RQ to assign the new cluster to
    """
    async with _client(project_id) as c:
        r = await c.post("/api/clusters/merge", json={
            "source_ids": source_ids,
            "target_label": target_label,
            "target_synthesis": target_synthesis,
            "research_question_id": research_question_id,
        })
        _raise_with_detail(r)
        data = r.json()

    _record_entity("cluster", data["target_id"], target_label)
    lines = [
        f"Merged {len(source_ids)} clusters into {data['target_id']}: {target_label}",
        f"  {data['total_claims_moved']} claims moved",
        f"  Sources left empty: {', '.join(source_ids)}",
    ]
    return "\n".join(lines)


@tool(category="literature")
async def rka_process_paper(
    lit_id: str,
    annotations: list[dict],
    summary: str | None = None,
    *,
    project_id: str,
) -> str:
    """Process reading annotations from a paper into structured claims.

    Creates a journal entry with reading notes, then extracts claims from
    each annotation. Optionally assigns claims to clusters inline.
    Auto-advances literature status from to_read to reading.

    Args:
        lit_id: Literature entry ID (lit_...)
        annotations: List of reading annotations:
            - passage: Key text/finding from the paper
            - note: Your interpretation or commentary (optional)
            - claim_type: hypothesis | evidence | method | result | observation | assumption
            - confidence: 0.0-1.0 (default 0.5)
            - cluster_id: Optional cluster to assign to (ecl_...)
        summary: Overall paper summary (becomes the journal entry content)
    """
    async with _client(project_id) as c:
        r = await c.post("/api/literature/process-paper", json={
            "lit_id": lit_id,
            "annotations": annotations,
            "summary": summary,
        })
        _raise_with_detail(r)
        data = r.json()

    _record_entity("journal", data["journal_entry_id"], f"Reading notes for {lit_id}")
    for cl in data.get("claims", []):
        _record_entity("claim", cl["id"], f"{cl['claim_type']}: {cl['content']}")

    lines = [
        f"Processed {len(data['claims'])} annotations from {lit_id}:",
        f"  Journal entry: {data['journal_entry_id']}",
        f"  Claims created: {data['claims_created']}",
    ]
    if data["claims_assigned"] > 0:
        lines.append(f"  Claims assigned to clusters: {data['claims_assigned']}")
    lines.append(f"  Literature status: {data['literature_status']}")
    for cl in data.get("claims", []):
        cluster_str = f" → {cl['cluster_id']}" if cl.get("cluster_id") else ""
        lines.append(f"    [{cl['id']}] ({cl['claim_type']}) {cl['content']}{cluster_str}")
    return "\n".join(lines)


@tool(category="claims")
async def rka_advance_rq(
    rq_id: str,
    status: str,
    conclusion: str | None = None,
    evidence_cluster_ids: list[str] | None = None,
    *,
    project_id: str,
) -> str:
    """Advance a research question's lifecycle status.

    Tracks RQ progress from open through answered/closed. Optionally stores
    a conclusion and links supporting evidence clusters.

    Args:
        rq_id: Research question decision ID (dec_... with kind=research_question)
        status: open | partially_answered | answered | reframed | closed
        conclusion: Brain's conclusion text (recommended when status=answered)
        evidence_cluster_ids: Clusters that provide the answer (creates justified_by links)
    """
    async with _client(project_id) as c:
        r = await c.post("/api/research-questions/advance", json={
            "rq_id": rq_id,
            "status": status,
            "conclusion": conclusion,
            "evidence_cluster_ids": evidence_cluster_ids,
        })
        _raise_with_detail(r)
        data = r.json()

    status_icons = {
        "open": "●", "partially_answered": "◐",
        "answered": "✓", "reframed": "↻", "closed": "○",
    }
    icon = status_icons.get(status, "?")
    lines = [
        f"{icon} RQ {rq_id}: {data.get('previous_status', '?')} → {status}",
        f"  Question: {data.get('question', '')}",
    ]
    if data.get("conclusion_entry_id"):
        lines.append(f"  Conclusion recorded: {data['conclusion_entry_id']}")
    if data.get("evidence_clusters_linked", 0) > 0:
        lines.append(f"  Evidence clusters linked: {data['evidence_clusters_linked']}")
    return "\n".join(lines)


@tool(category="maintenance")
async def rka_check_integrity(*, project_id: str) -> str:
    """Verify knowledge base integrity — check for orphaned edges, missing references, and count mismatches.

    Run periodically or after import to ensure data consistency. Checks:
    - entity_links source/target reference existing entities
    - claim_edges reference existing claims and clusters
    - evidence_clusters.claim_count matches actual claim_edges count
    """
    async with _client(project_id) as c:
        r = await c.get("/api/integrity")
        _raise_with_detail(r)
        data = r.json()

    total = data.get("total_issues", 0)
    if total == 0:
        return "Knowledge base integrity check passed — no issues found."

    lines = [f"## Integrity Check: {total} issues found\n"]
    for issue in data.get("issues", []):
        lines.append(f"### {issue['description']} ({issue['count']})")
        shown = issue.get("ids", [])[:10]
        if shown:
            lines.append(f"  IDs: {', '.join(shown)}")
        if issue["count"] > 10:
            lines.append(f"  ... and {issue['count'] - 10} more")
        lines.append(f"  Fix: {issue.get('fix_action', 'Review manually')}")
        lines.append("")
    return "\n".join(lines)


# ============================================================
# Knowledge freshness tools
# ============================================================


@tool(category="decision")
async def rka_orphan_supersedes(*, project_id: str) -> str:
    """List decisions marked superseded that have no pointer to a replacement.

    A decision reading `superseded` with an empty `superseded_by` tells a
    reader it is dead but not what replaced it — the shape that sends a
    session hunting for a successor it cannot reach. Pair with
    `rka_link_supersession` to reconnect one.
    """
    async with _client(project_id) as c:
        r = await c.get("/api/decisions/orphan-supersedes")
        _raise_with_detail(r)
        rows = r.json()
    if not rows:
        return "No orphaned supersede chains in this project."
    lines = [f"{len(rows)} orphaned supersede chain(s):"]
    for row in rows:
        lines.append(f"  {row['id']}  [{(row.get('updated_at') or '')[:10]}]")
        lines.append(f"     {(row.get('question') or '')[:110]}")
        if row.get("chosen"):
            lines.append(f"     chose: {row['chosen'][:100]}")
    lines.append(
        "\nEach needs a human to name its replacement — the record carries no "
        "automatic pointer. Then call rka_link_supersession."
    )
    return "\n".join(lines)


@tool(category="decision")
async def rka_link_supersession(
    old_decision_id: str,
    new_decision_id: str,
    apply: bool = False,
    actor: str = "pi",
    *,
    project_id: str,
) -> str:
    """Reconnect two EXISTING decisions as superseded -> replacement.

    Distinct from `rka_supersede_decision`, which *creates* the replacement.
    Use this when both rows already exist and only the chain between them is
    missing. Replays the full supersede sequence without creating anything:
    scope-version bump, the superseded_by pointer, the `supersedes`
    entity_link, the staleness cascade over claims and clusters sourced from
    the old decision's journal entries, a re-distill review row, and the
    decision_superseded event. Idempotent — satisfied steps report ALREADY.

    Args:
        old_decision_id: the superseded decision missing its pointer
        new_decision_id: the existing decision that replaced it
        apply: False (default) previews the steps; True performs them. A
            wrong pointer is worse than a missing one, so preview first.
        actor: recorded on the backfilled rows (pi | brain | executor | system)
    """
    async with _client(project_id) as c:
        r = await c.post("/api/decisions/link-supersession", json={
            "old_decision_id": old_decision_id,
            "new_decision_id": new_decision_id,
            "apply": apply, "actor": actor,
        })
        _raise_with_detail(r)
        data = r.json()
    head = "APPLIED" if data.get("applied") else ("DRY RUN — nothing written" if data.get("dry_run") else "NO-OP")
    lines = [f"{head}: {old_decision_id} -> {new_decision_id}"]
    for step in data.get("steps", []):
        mark = {"DONE": "+", "ALREADY": "=", "WOULD": ".", "SKIPPED": "-"}.get(step["state"], "?")
        lines.append(f"  [{mark}] {step['step']}: {step['state']}  {step.get('detail') or ''}".rstrip())
    if data.get("dry_run"):
        lines.append("\nRe-call with apply=True to perform these steps.")
    return "\n".join(lines)


@tool(category="maintenance")
async def rka_flag_stale(
    entity_id: str,
    reason: str,
    staleness: str = "yellow",
    propagate: bool = True,
    *,
    project_id: str,
) -> str:
    """Flag a claim, cluster, or decision as potentially stale.

    When propagate=true, traverses dependency graph: stale claim → flag parent
    cluster (if >50% claims stale) → flag decisions citing the cluster.

    Args:
        entity_id: The entity to flag (clm_..., ecl_..., or dec_...)
        reason: Why this entity is stale (e.g., "Contradicted by newer experiment in jrn_01...")
        staleness: yellow (aging/partially conflicting) or red (directly contradicted)
        propagate: If true, traverse dependency graph and flag dependent entities
    """
    async with _client(project_id) as c:
        r = await c.post("/api/freshness/flag-stale", json={
            "entity_id": entity_id, "reason": reason,
            "staleness": staleness, "propagate": propagate,
        })
        _raise_with_detail(r)
        data = r.json()

    flagged = data.get("flagged", [])
    lines = [f"Flagged {len(flagged)} entities:"]
    for f in flagged:
        icon = "🔴" if f.get("staleness") == "red" else "🟡"
        lines.append(f"  {icon} {f['id']} → {f.get('staleness', 'yellow')}")
    return "\n".join(lines)


@tool(category="maintenance")
async def rka_check_freshness(
    days_threshold: int = 30,
    *,
    project_id: str,
) -> str:
    """Scan for potentially stale knowledge items.

    Pure SQL scan — no LLM needed. Returns items that may need Brain review:
    claims already flagged, claims with superseded sources, aging claims,
    and stale clusters.

    Args:
        days_threshold: Claims older than this may need freshness review (default 30)
    """
    async with _client(project_id) as c:
        r = await c.get("/api/freshness/check", params={"days_threshold": days_threshold})
        _raise_with_detail(r)
        data = r.json()

    total = data.get("total_items", 0)
    if total == 0:
        return "Knowledge base is fresh — no staleness issues detected."

    lines = [f"## Knowledge Freshness: {total} items need review\n"]
    for key, cat in data.get("categories", {}).items():
        if cat["count"] == 0:
            continue
        lines.append(f"### {cat['description']} ({cat['count']})")
        shown = cat["ids"][:10]
        lines.append(f"  IDs: {', '.join(shown)}")
        if len(cat["ids"]) > 10:
            lines.append(f"  ... and {len(cat['ids']) - 10} more")
        lines.append(f"  Fix: {cat['fix_action']}")
        lines.append("")
    return "\n".join(lines)


@tool(tier="deferred", category="core")
async def rka_resolve_stale(entity_id: str, verdict: str, resolution: str,
                          resolved_by: str, journal_id: str | None = None, *, project_id: str) -> dict:
    """Record a reasoned stale disposition; does not revive structural invalidity."""
    async with _client(project_id=project_id) as c:
        response = await c.post("/api/freshness/resolve-stale", json={
            "entity_id": entity_id, "verdict": verdict, "resolution": resolution,
            "resolved_by": resolved_by, "journal_id": journal_id,
        })
        _raise_with_detail(response)
        return response.json()


@tool(tier="deferred", category="core")
async def rka_record_directive_dependency(directive_id: str, decision_id: str,
                                        declared_by: str, reason: str, *, project_id: str) -> dict:
    """Explicitly declare that a directive requires a current decision."""
    async with _client(project_id) as c:
        response = await c.post(f"/api/notes/{directive_id}/dependencies", json={
            "decision_id": decision_id, "declared_by": declared_by, "reason": reason})
        _raise_with_detail(response)
        return response.json()


@tool(category="maintenance")
async def rka_detect_contradictions(
    entity_id: str,
    similarity_threshold: float = 0.7,
    max_results: int = 5,
    *,
    project_id: str,
) -> str:
    """Find claims that may contradict a given claim or journal entry.

    Uses vector similarity (or FTS fallback) to find semantically related claims,
    then surfaces them for Brain review. Does NOT auto-resolve — the Brain
    decides whether conflicts are real contradictions.

    Args:
        entity_id: A claim (clm_...) or journal entry (jrn_...) to check against
        similarity_threshold: Minimum similarity to consider (default 0.7)
        max_results: Maximum candidates to return (default 5)
    """
    async with _client(project_id) as c:
        r = await c.post("/api/freshness/detect-contradictions", json={
            "entity_id": entity_id,
            "similarity_threshold": similarity_threshold,
            "max_results": max_results,
        })
        _raise_with_detail(r)
        data = r.json()

    candidates = data.get("candidates", [])
    if not candidates:
        return data.get("message", f"No similar claims found for {entity_id}.")

    lines = [f"Contradiction check for {entity_id}:"]
    for c in candidates:
        sim = c.get("similarity", 0)
        icon = "⚠️" if sim >= 0.8 else "ℹ️"
        cluster_str = f" (cluster: {c['cluster_id']})" if c.get("cluster_id") else ""
        lines.append(f"  {icon} {sim:.2f} — {c['claim_id']} [{c['claim_type']}] \"{c['content']}\"{cluster_str}")
    lines.append("Brain: review these candidates and use rka_flag_stale if any are genuine contradictions.")
    return "\n".join(lines)


# ============================================================
# Maintenance manifest
# ============================================================


@tool(tier="always_on", category="maintenance")
async def rka_get_pending_maintenance(*, project_id: str) -> str:
    """Detect knowledge base gaps that need attention. Pure SQL — no LLM needed.

    Returns a compact manifest of:
    (a) entries without tags
    (b) entries without claims extracted
    (c) clusters needing synthesis
    (d) flagged contradictions
    (e) entries missing cross-references (no related_decisions)
    (f) decisions without justified_by links
    (g) missions without motivated_by_decision
    (h) unassigned evidence clusters

    Each category includes entity IDs, counts, and suggested fix actions.
    Use at session start to identify maintenance work before proceeding.
    """
    async with _client(project_id) as c:
        r = await c.get("/api/maintenance")
        _raise_with_detail(r)
        data = r.json()

    total = data["total_items"]
    est = data["estimated_tool_calls"]

    if total == 0:
        return "Knowledge base is clean — no pending maintenance items."

    lines = [f"## Pending Maintenance: {total} items (~{est} tool calls to fix)\n"]
    for key, cat in data["categories"].items():
        count = cat["count"]
        if count == 0:
            continue
        lines.append(f"### {cat['description']} ({count})")
        ids = cat["ids"]
        # Show up to 10 IDs inline, rest as count
        shown = ids[:10]
        lines.append(f"  IDs: {', '.join(shown)}")
        if len(ids) > 10:
            lines.append(f"  ... and {len(ids) - 10} more")
        lines.append(f"  Fix: {cat['fix_action']}")
        lines.append("")

    return "\n".join(lines)


# ============================================================
# Manuscript and entity-resolution MCP adapters
# ============================================================
# These deferred adapters are the REST-wiring layer for both the compatibility
# ``register_manuscript`` / ``manuscript`` operations and the canonical native
# manuscript aggregate exposed through the typed rka_query/rka_execute unions.
# The typed operations remain the preferred surface; these functions keep the
# dispatch layer thin and preserve the legacy-tool escape hatch.


@tool(category="manuscript")
async def rka_register_manuscript(
    venue: str,
    title: str,
    abstract: str | None = None,
    sections: list[str] | None = None,
    *,
    project_id: str,
) -> str:
    """Compatibility-register a manuscript.

    Creates the legacy ``jrn_`` manifest and its canonical native ``man_``
    aggregate.  The response includes both IDs and marks the journal ID as
    deprecated.  New callers should use ``create_manuscript`` through
    ``rka_execute``.

    Args:
        venue: Target venue (CHI, EMNLP, USENIX, IEEE-SP, NeurIPS, OSDI, Nature).
        title: Manuscript title (PI authored; stored verbatim).
        abstract: Optional manuscript abstract (PI authored; stored verbatim).
        sections: Optional initial section names; outlined status by default.
    """
    payload: dict[str, object] = {"venue": venue, "title": title}
    if abstract is not None:
        payload["abstract"] = abstract
    if sections is not None:
        payload["sections"] = sections
    async with _client(project_id) as c:
        r = await c.post("/api/manuscripts", json=payload)
        _raise_with_detail(r)
        data = r.json()
    return json.dumps(data, indent=2)


@tool(category="manuscript")
async def rka_get_manuscript(manuscript_id: str, *, project_id: str) -> str:
    """Read a manuscript by canonical ``man_`` or compatibility ``jrn_`` id.

    The route returns canonical-ID and deprecation metadata when a legacy
    journal alias is used.
    """
    async with _client(project_id) as c:
        r = await c.get(f"/api/manuscripts/{manuscript_id}")
        _raise_with_detail(r)
        data = r.json()
    return json.dumps(data, indent=2)


@tool(category="manuscript")
async def rka_resolve_entities(
    ids: list[str],
    include_sources: bool = False,
    include_edges: bool = False,
    *,
    project_id: str,
) -> str:
    """Resolve heterogeneous IDs with explicit same-project attestation."""
    payload = {
        "ids": ids,
        "include_sources": include_sources,
        "include_edges": include_edges,
    }
    async with _client(project_id) as c:
        r = await c.post("/api/entities/resolve", json=payload)
        _raise_with_detail(r)
        data = r.json()
    return json.dumps(data, indent=2)


@tool(category="manuscript")
async def rka_get_manuscript_context(
    manuscript_id: str,
    *,
    project_id: str,
) -> str:
    """Read the complete authoritative native manuscript aggregate."""
    async with _client(project_id) as c:
        r = await c.get(f"/api/manuscripts/{manuscript_id}/context")
        _raise_with_detail(r)
        data = r.json()
    return json.dumps(data, indent=2)


@tool(category="manuscript")
async def rka_get_manuscript_reference_manifest(
    manuscript_id: str,
    *,
    project_id: str,
) -> str:
    """Read active citation membership and its exact validation state."""
    async with _client(project_id) as c:
        r = await c.get(f"/api/manuscripts/{manuscript_id}/references")
        _raise_with_detail(r)
        data = r.json()
    return json.dumps(data, indent=2)


@tool(category="manuscript")
async def rka_get_manuscript_readiness(
    manuscript_id: str,
    target_phase: str = "drafting",
    *,
    project_id: str,
) -> str:
    """Evaluate evidence-linked readiness for a native lifecycle phase."""
    async with _client(project_id) as c:
        r = await c.get(
            f"/api/manuscripts/{manuscript_id}/readiness",
            params={"target_phase": target_phase},
        )
        _raise_with_detail(r)
        data = r.json()
    return json.dumps(data, indent=2)


@tool(category="manuscript")
async def rka_get_manuscript_spine(
    manuscript_id: str,
    *,
    project_id: str,
) -> str:
    """Export the deterministic manuscript projection of the native RKA spine."""
    async with _client(project_id) as c:
        r = await c.get(f"/api/manuscripts/{manuscript_id}/spine")
        _raise_with_detail(r)
        data = r.json()
    return json.dumps(data, indent=2)


@tool(category="manuscript")
async def rka_get_manuscript_outline(
    manuscript_id: str,
    *,
    project_id: str,
) -> str:
    """Read L2-L5 outline rationale, bindings, blockers, and checkpoint state."""
    async with _client(project_id) as c:
        r = await c.get(f"/api/manuscripts/{manuscript_id}/outline")
        _raise_with_detail(r)
        data = r.json()
    return json.dumps(data, indent=2)


@tool(category="manuscript")
async def rka_get_manuscript_writing_candidates(
    manuscript_id: str,
    *,
    project_id: str,
) -> str:
    """Discover claim candidates through reviewed clusters and research questions."""
    async with _client(project_id) as c:
        r = await c.get(
            f"/api/manuscripts/{manuscript_id}/writing-candidates"
        )
        _raise_with_detail(r)
        data = r.json()
    return json.dumps(data, indent=2)


@tool(category="manuscript")
async def rka_changes_since(
    cursor: int = 0,
    limit: int = 100,
    *,
    project_id: str,
) -> str:
    """Page semantic project changes after an opaque monotonic cursor.

    The response includes both ``next_cursor`` (the final delivered row) and
    ``latest_cursor`` (the current same-project ledger head). Continue while
    ``has_more`` is true.
    """
    async with _client(project_id) as c:
        r = await c.get(
            "/api/changes",
            params={"cursor": cursor, "limit": limit},
        )
        _raise_with_detail(r)
        data = r.json()
    return json.dumps(data, indent=2)


@tool(category="manuscript")
async def rka_get_manuscript_impact(
    manuscript_id: str,
    since_cursor: int = 0,
    limit: int = 100,
    *,
    project_id: str,
) -> str:
    """Map changed dependencies to affected manuscript claims and units.

    The response preserves the cursor-page contract with ``next_cursor``,
    ``latest_cursor``, and ``has_more`` even when the current page has no
    manuscript-relevant changes.
    """
    async with _client(project_id) as c:
        r = await c.get(
            f"/api/manuscripts/{manuscript_id}/impact",
            params={"since_cursor": since_cursor, "limit": limit},
        )
        _raise_with_detail(r)
        data = r.json()
    return json.dumps(data, indent=2)


@tool(category="manuscript")
async def rka_create_native_manuscript(
    title: str,
    abstract: str | None = None,
    venue: str | None = None,
    phase: str = "planning",
    state: str = "active",
    workspace_ref: str | None = None,
    legacy_journal_id: str | None = None,
    *,
    project_id: str,
) -> str:
    """Create a planning/active manuscript without inferred ratification."""
    payload = {
        "title": title,
        "abstract": abstract,
        "venue": venue,
        "phase": phase,
        "state": state,
        "workspace_ref": workspace_ref,
        "legacy_journal_id": legacy_journal_id,
    }
    async with _client(project_id) as c:
        r = await c.post("/api/manuscripts/native", json=payload)
        _raise_with_detail(r)
        data = r.json()
    return json.dumps(data, indent=2)


@tool(category="manuscript")
async def rka_update_native_manuscript(
    manuscript_id: str,
    expected_revision: int,
    updates: dict,
    *,
    project_id: str,
) -> str:
    """Update descriptive metadata using optimistic concurrency.

    Lifecycle phase/state changes must use the readiness-gated transition
    operation.
    """
    allowed = {"title", "abstract", "venue", "workspace_ref"}
    unknown = sorted(set(updates) - allowed)
    if unknown:
        return json.dumps(
            {
                "error": "invalid_field",
                "message": f"unsupported manuscript update fields: {unknown}",
            },
            indent=2,
        )
    if not updates:
        return json.dumps(
            {
                "error": "missing_field",
                "message": "at least one manuscript update field is required",
            },
            indent=2,
        )
    payload = {"expected_revision": expected_revision, **updates}
    async with _client(project_id) as c:
        r = await c.patch(
            f"/api/manuscripts/{manuscript_id}",
            json=payload,
        )
        _raise_with_detail(r)
        data = r.json()
    return json.dumps(data, indent=2)


@tool(category="manuscript")
async def rka_upsert_argument_spine(
    manuscript_id: str,
    expected_revision: int,
    spine: dict,
    *,
    project_id: str,
) -> str:
    """Report the deprecated agent-direct spine mutation contract.

    Create an attributed ``argument_spine_replace`` semantic proposal instead;
    a PI or web user performs the separate apply transition.
    """
    return json.dumps(
        {
            "error": "deprecated_operation",
            "operation": "upsert_argument_spine",
            "message": (
                "Agent-direct argument-spine replacement is not permitted. "
                "Prepare context with prepare_semantic_patch_context, then "
                "create an attributed argument_spine_replace proposal with "
                "create_semantic_patch_proposal; a PI or web user applies it "
                "in a separate transition."
            ),
            "replacement_operations": [
                "prepare_semantic_patch_context",
                "create_semantic_patch_proposal",
                "apply_semantic_patch_proposal",
            ],
            "received": {
                "manuscript_id": manuscript_id,
                "expected_revision": expected_revision,
                "spine_keys": sorted(spine),
                "project_id": project_id,
            },
        },
        indent=2,
    )


@tool(category="manuscript")
async def rka_replace_manuscript_reference_manifest(
    manuscript_id: str,
    expected_revision: int,
    members: list[dict],
    *,
    project_id: str,
) -> str:
    """Atomically replace the manuscript's authoritative citation set."""
    async with _client(project_id) as c:
        r = await c.put(
            f"/api/manuscripts/{manuscript_id}/references",
            json={
                "expected_revision": expected_revision,
                "members": members,
            },
        )
        _raise_with_detail(r)
        data = r.json()
    return json.dumps(data, indent=2)


@tool(category="manuscript")
async def rka_ratify_manuscript_claim(
    manuscript_id: str,
    claim_ref: str,
    expected_revision: int,
    decision_id: str,
    claim_version: int | None = None,
    ratified_at: str | None = None,
    *,
    project_id: str,
) -> str:
    """Bind an exact manuscript-claim version to an explicit PI decision."""
    payload = {
        "expected_revision": expected_revision,
        "decision_id": decision_id,
    }
    if claim_version is not None:
        payload["claim_version"] = claim_version
    if ratified_at is not None:
        payload["ratified_at"] = ratified_at
    async with _client(project_id) as c:
        r = await c.post(
            f"/api/manuscripts/{manuscript_id}/claims/{claim_ref}/ratifications",
            json=payload,
        )
        _raise_with_detail(r)
        data = r.json()
    return json.dumps(data, indent=2)


@tool(category="manuscript")
async def rka_transition_manuscript_phase(
    manuscript_id: str,
    expected_revision: int,
    target_phase: str,
    target_state: str | None = None,
    *,
    project_id: str,
) -> str:
    """Run server-side readiness gates and transition manuscript phase."""
    payload = {
        "expected_revision": expected_revision,
        "target_phase": target_phase,
    }
    if target_state is not None:
        payload["target_state"] = target_state
    async with _client(project_id) as c:
        r = await c.post(
            f"/api/manuscripts/{manuscript_id}/transition",
            json=payload,
        )
        _raise_with_detail(r)
        data = r.json()
    return json.dumps(data, indent=2)


@tool(category="manuscript")
async def rka_create_native_manuscript_checkpoint(
    manuscript_id: str,
    expected_revision: int,
    kind: str,
    unit_id: str | None = None,
    supersedes_id: str | None = None,
    *,
    project_id: str,
) -> str:
    """Create a pending native manuscript checkpoint."""
    payload = {
        "expected_revision": expected_revision,
        "kind": kind,
    }
    if unit_id is not None:
        payload["unit_id"] = unit_id
    if supersedes_id is not None:
        payload["supersedes_id"] = supersedes_id
    async with _client(project_id) as c:
        r = await c.post(
            f"/api/manuscripts/{manuscript_id}/checkpoints",
            json=payload,
        )
        _raise_with_detail(r)
        data = r.json()
    return json.dumps(data, indent=2)


@tool(category="manuscript")
async def rka_resolve_native_manuscript_checkpoint(
    checkpoint_id: str,
    expected_revision: int,
    decision_id: str,
    status: str,
    resolved_at: str,
    *,
    project_id: str,
) -> str:
    """Resolve a pending native manuscript checkpoint via a PI decision."""
    payload = {
        "expected_revision": expected_revision,
        "decision_id": decision_id,
        "status": status,
        "resolved_at": resolved_at,
    }
    async with _client(project_id) as c:
        r = await c.post(
            f"/api/manuscripts/checkpoints/{checkpoint_id}/resolve",
            json=payload,
        )
        _raise_with_detail(r)
        data = r.json()
    return json.dumps(data, indent=2)


@tool(category="manuscript")
async def rka_record_manuscript_verification_attestation(
    manuscript_id: str,
    expected_revision: int,
    attestation: dict,
    *,
    project_id: str,
) -> str:
    """Append an immutable multidimensional claim-verification attestation."""
    async with _client(project_id) as c:
        r = await c.post(
            f"/api/manuscripts/{manuscript_id}/verification-attestations",
            json={
                "expected_revision": expected_revision,
                "attestation": attestation,
            },
        )
        _raise_with_detail(r)
        data = r.json()
    return json.dumps(data, indent=2)


@tool(category="literature")
async def rka_get_reference_validation_status(
    manuscript_id: str,
    job_id: str,
    *,
    project_id: str,
) -> str:
    """Read one historical project- and manuscript-scoped validation job.

    Pending and in-progress responses contain job metadata; a completed
    response additionally carries ``result``, while a failed response carries
    ``error``. Core no longer initiates or executes reference validation;
    external Writer or client workflows may perform verification separately.
    """
    async with _client(project_id) as c:
        r = await c.get(
            f"/api/manuscripts/{manuscript_id}/reference-validations/{job_id}"
        )
        _raise_with_detail(r)
        data = r.json()
    return json.dumps(data, indent=2)


# ============================================================
# v2.6.3 — Navigator tools (dynamic tool surface)
# ============================================================
# Three always-on tools that operate on `_TOOL_REGISTRY`:
#   - rka_load_tools : register deferred tools on demand + fire listChanged
#   - rka_list_tools : browse the full catalog by category / keyword
#   - rka_help       : per-tool signature + docstring
#
# The MCP `tools.listChanged: true` capability is advertised via the
# `_run_stdio_async_with_list_changed` patch immediately below the prompts
# block, which threads NotificationOptions(tools_changed=True) into
# create_initialization_options().

from mcp.server.fastmcp import Context as _MCPContext


@tool(tier=_TIER_ALWAYS_ON, category="navigator")
async def rka_load_tools(names: list[str], ctx: _MCPContext) -> str:
    """Activate deferred RKA tools by name (dynamic tool surface, v2.7.0+).

    RKA's MCP server publishes 5 always-on tools at session start (3
    dispatch — rka_query + rka_execute + rka_describe — and 2 escape
    hatches — rka_load_tools + rka_help). 91 legacy tools + 8 v2.7.0a2
    verbs are tier='deferred', callable via rka_load_tools(names=[...]).
    Setting RKA_LEGACY_TOOLS=1 in env restores the v2.7.0a2 surface
    (20 always-on tools) for callers that depend on per-tool dispatch
    granularity (e.g. orchestrator subprocesses). Call this navigator
    to bring deferred tools into the active surface, then call them
    normally.

    Names are the canonical UNPREFIXED rka_* form (e.g. `rka_add_literature`).
    In Claude Code plugin mode the harness presents the tool as
    `mcp__plugin_rka_rka__rka_add_literature`; the harness handles namespace
    translation — you still pass the bare name here.

    Behavior:
      - Already-active tools are skipped (idempotent).
      - Unknown names are returned in `unknown` with no error.
      - On success, the server emits `notifications/tools/list_changed`
        which both Claude Desktop and Claude Code honor; their tool
        surfaces refresh within ~1 round-trip and the newly-loaded tools
        become callable.

    Args:
        names: list of canonical rka_* tool names to load.

    Returns:
        JSON: `{loaded: [...], already_active: [...], unknown: [...]}`.

    See also: `rka_list_tools` (browse), `rka_help` (single-tool detail).
    """
    loaded: list[str] = []
    already: list[str] = []
    unknown: list[str] = []
    for raw in names or []:
        name = (raw or "").strip()
        if not name:
            continue
        rec = _TOOL_REGISTRY.get(name)
        if rec is None:
            unknown.append(name)
            continue
        if rec["registered"]:
            already.append(name)
            continue
        # Defer to FastMCP's tool manager — same path the @mcp.tool()
        # decorator uses at module-import; the wrapper's @wraps(func)
        # preserved the inner function's signature + annotations, so
        # the resulting inputSchema matches what an always-on tool of
        # the same shape would have rendered.
        mcp._tool_manager.add_tool(rec["fn"], name=name)
        rec["registered"] = True
        loaded.append(name)
    # Fire the MCP-protocol-level notification iff anything actually
    # changed. The MCP spec says clients SHOULD refetch tools/list on
    # receipt; both Claude Desktop and Claude Code do.
    if loaded:
        try:
            await ctx.session.send_tool_list_changed()
        except Exception:
            # Failure to notify shouldn't reverse the registration —
            # the next time the client calls tools/list (e.g. on the
            # next initialization or by manual refresh) it will see the
            # full surface.
            pass
    return json.dumps(
        {"loaded": loaded, "already_active": already, "unknown": unknown},
        indent=2,
    )


@tool(tier=_TIER_ALWAYS_ON, category="navigator")
async def rka_list_tools(
    category: str | None = None,
    query: str | None = None,
    tier: str | None = None,
) -> str:
    """Browse the rka tool catalog — active + deferred, filterable.

    Use this to discover which RKA tools exist, what category they're
    in, and whether they're currently loaded. Combine with
    `rka_load_tools` to bring deferred tools online.

    Args:
        category: optional category filter — one of:
            core | literature | journal | decision | mission |
            checkpoint | claims | graph | workspace | hooks |
            maintenance | session | manuscript | navigator | skills |
            general.
        query: optional substring match against name + summary
            (case-insensitive). For finding tools when you know what
            you want to do but not the exact name.
        tier: optional `always_on` or `deferred` filter.

    Returns:
        JSON: `{categories: {<cat>: [{name, tier, summary, registered}, ...]}}`.
        Tools are grouped by category for readability.
    """
    q = (query or "").lower().strip()
    cat = (category or "").strip().lower() or None
    tier_filter = (tier or "").strip().lower() or None
    groups: dict[str, list[dict]] = {}
    for name, rec in sorted(_TOOL_REGISTRY.items()):
        if cat and rec["category"] != cat:
            continue
        if tier_filter and rec["tier"] != tier_filter:
            continue
        if q and q not in name.lower() and q not in rec["summary"].lower():
            continue
        groups.setdefault(rec["category"], []).append({
            "name": name,
            "tier": rec["tier"],
            "summary": rec["summary"],
            "registered": rec["registered"],
        })
    return json.dumps(
        {
            "total_tools": len(_TOOL_REGISTRY),
            "filtered_count": sum(len(v) for v in groups.values()),
            "categories": groups,
        },
        indent=2,
    )


@tool(tier=_TIER_ALWAYS_ON, category="navigator")
async def rka_help(name: str) -> str:
    """Render full documentation for one rka tool by canonical name.

    Works whether the tool is currently active or deferred. Use this to
    inspect a tool's signature + docstring before calling
    `rka_load_tools` to bring it online.

    Args:
        name: canonical rka_* tool name (e.g. `rka_add_literature`).

    Returns:
        JSON: `{name, tier, category, signature, summary, docstring, registered}`.
        Returns `{error: "unknown_tool", name}` if the tool does not exist.
    """
    rec = _TOOL_REGISTRY.get((name or "").strip())
    if rec is None:
        return json.dumps({"error": "unknown_tool", "name": name}, indent=2)
    return json.dumps(
        {
            "name": name,
            "tier": rec["tier"],
            "category": rec["category"],
            "signature": rec["signature"],
            "summary": rec["summary"],
            "docstring": rec["docstring"],
            "registered": rec["registered"],
        },
        indent=2,
    )


# ============================================================
# v2.6.3 — listChanged capability wiring
# ============================================================
# FastMCP's default run_stdio_async / run_streamable_http_async pass
# `notification_options=None` to create_initialization_options, which
# defaults to NotificationOptions(False, False, False) — meaning the
# server advertises `tools.listChanged: false` in the initialize
# response handshake. Both Claude Desktop and Claude Code respect that
# flag and won't re-fetch tools/list on receipt of a list-changed
# notification.
#
# We re-bind both run-* methods to pass NotificationOptions(
# tools_changed=True), preserving the rest of the body verbatim. The
# patch is at module load so importing tests that use the FastMCP
# instance see the same behavior as the production stdio entry point.

from mcp.server.lowlevel.server import NotificationOptions as _NotificationOptions
from mcp.server.stdio import stdio_server as _stdio_server


async def _run_stdio_async_with_list_changed(self) -> None:
    """v2.6.3 — same body as FastMCP.run_stdio_async, but advertises
    tools.listChanged=true via NotificationOptions."""
    async with _stdio_server() as (read_stream, write_stream):
        await self._mcp_server.run(
            read_stream,
            write_stream,
            self._mcp_server.create_initialization_options(
                notification_options=_NotificationOptions(tools_changed=True),
            ),
        )


mcp.run_stdio_async = _run_stdio_async_with_list_changed.__get__(mcp, type(mcp))


# Patch the Streamable-HTTP path the same way for parity. The default
# FastMCP body uses an internal Starlette app; the create_initialization
# options call is inside the request handler at
# mcp.server.streamable_http_manager. We override the FastMCP-level
# accessor `create_initialization_options` itself so EVERY transport
# that ultimately calls into it picks up tools_changed=True.

_orig_create_init = mcp._mcp_server.create_initialization_options


def _create_init_with_list_changed(notification_options=None, experimental_capabilities=None):
    """Force tools_changed=True regardless of caller (covers run_streamable_http_async,
    run_sse_async, and any future transport). If the caller supplied its own
    NotificationOptions, OR in tools_changed=True non-destructively."""
    if notification_options is None:
        notification_options = _NotificationOptions(tools_changed=True)
    else:
        # Preserve whatever the caller asked for on prompts/resources;
        # force tools_changed True for v2.6.3.
        notification_options = _NotificationOptions(
            prompts_changed=getattr(notification_options, "prompts_changed", False),
            resources_changed=getattr(notification_options, "resources_changed", False),
            tools_changed=True,
        )
    return _orig_create_init(notification_options, experimental_capabilities)


mcp._mcp_server.create_initialization_options = _create_init_with_list_changed


# ============================================================
# v2.7.0 PR-1 — Always-on verbs (additive; legacy 91 tools unchanged)
# ============================================================
# Eight always-on verbs that collapse the read/write surface for the
# Brain / Executor / PI loops:
#   - rka_query (project-scoped reads plus unscoped discovery/health)
#   - rka_record_note
#   - rka_record_decision
#   - rka_record_literature       (PR-2 will land via parallel agent)
#   - rka_mission                 (PR-2 will land via parallel agent)
#   - rka_checkpoint              (PR-2 will land via parallel agent)
#   - rka_review                  (PR-2 will land via parallel agent)
#   - rka_session                 (unscoped meta)
#
# This block ships 4 of the 8 (per PR-1 scope). Each verb is a thin
# adapter that calls the existing REST endpoints — the service layer
# (rka/services/*) is UNCHANGED, and the 91 legacy @tool() decorators
# above stay in place so existing call sites remain wire-compatible.
#
# Per the v2.7.0 design synthesis (workflow w2cnkgz0k):
#   - Graft A: provenance is a TOP-LEVEL kwarg, not nested deep.
#   - Graft C: every docstring leads with role tag [BRAIN]/[EXECUTOR]/[PI]/[ANY].
#   - Enum constraints (ConfidenceLit, ImportanceLit, ...) stay on
#     TOP-LEVEL kwargs so the Annotated[Literal] promotion survives
#     and FastMCP can render them into inputSchema.

from typing import Literal as _Literal

# Discriminator literals — kept inline (vs an _enums.py addition) so the
# verb-surface is self-contained and reviewable in one place.

# rka_query operation set — project-scoped read operations plus two unscoped
# Decision 1 taxonomy. Covers every read-side legacy tool plus the
# session-level get_status / get_research_map / get_context that the
# orchestrator's minimal session start uses.
#
# v2.7.0a3 additions over the v2.7.0/a1 surface:
#   - `list_projects` — promoted from rka_session(action='list_projects')
#     so project discovery is reachable from the read verb directly.
#   - `health` — promoted from rka_session(action='health') so liveness
#     probing is reachable from the read verb directly.
#
# Naming note: the discriminator parameter is `operation` in v2.7.0a3
# (was `scope` in a1/a2; a backwards-compat alias is retained on the
# call site to avoid breaking existing callers during the migration
# window — see rka_query signature).
QueryScopeLit = _Literal[
    "status", "context", "search", "entity", "journal", "literature",
    "note_attribution_history",
    "mission", "report", "checkpoints", "decision_tree", "calibration_metrics",
    "hooks", "hook_executions", "brain_notifications", "research_map",
    "review_queue", "clusters", "claims", "interpretation_candidates", "sources",
    "experiments", "experiment_runs", "experiment_observations", "manuscript",
    "resolve_entities", "changes_since", "manuscript_context",
    "manuscript_reference_manifest",
    "manuscript_readiness", "manuscript_spine", "manuscript_outline",
    "manuscript_writing_candidates", "manuscript_impact",
    "reference_validation_status", "graph",
    "ego_graph", "graph_stats", "graph_mermaid", "provenance", "multi_hop",
    "collect_report_context", "staleness_impact", "mission_guard", "belief_as_of",
    "summarize", "generate_summary", "evidence", "freshness", "contradictions",
    "integrity", "pending_maintenance", "changelog", "bootstrap_review",
    "workspace_tree", "workspace_scan",
    # v2.7.0a3 additions
    "list_projects", "health",
]

# rka_session action set — unscoped/meta operations.
SessionActionLit = _Literal[
    "list_projects", "create_project", "set_project", "reset",
    "digest", "health", "help", "export", "generate_claude_md",
]


def _strip_none(d: dict) -> dict:
    """Drop keys whose value is None — convenience for REST bodies."""
    return {k: v for k, v in d.items() if v is not None}


async def _query_get(project_id: str | None, path: str, *, params: dict | None = None) -> str:
    """GET wrapper used by rka_query dispatches. Returns raw JSON text."""
    async with _client(project_id) as c:
        r = await c.get(path, params=_strip_none(params or {}) or None)
        _raise_with_detail(r)
        return json.dumps(r.json(), indent=2)


async def _query_post(project_id: str | None, path: str, body: dict | None = None) -> str:
    """POST wrapper used by rka_query dispatches that hit POST endpoints
    (search, multi-hop, etc.). Returns raw JSON text."""
    async with _client(project_id) as c:
        r = await c.post(path, json=_strip_none(body or {}))
        _raise_with_detail(r)
        return json.dumps(r.json(), indent=2)


# v2.7.0 — Pydantic discriminated-union import. This is the canonical
# typed-arg surface for rka_query / rka_execute. The legacy untyped
# bodies of these verbs are preserved below as ``_rka_query_legacy_impl``
# and ``_rka_execute_legacy_impl`` for callers reachable via the
# ``RKA_LEGACY_TOOLS=1`` opt-in surface; the active @tool() decorators
# bind to the typed wrappers.
from rka.mcp.operation_args import (  # noqa: E402  (post-helper-import)
    BulkUpdateItem as _BulkUpdateItem,
    ExecuteArgsUnion as _ExecuteArgsUnion,
    QueryArgsUnion as _QueryArgsUnion,
)
from rka.mcp.verb_dispatch import (  # noqa: E402, F811
    dispatch_execute_typed as _dispatch_execute_typed,
    dispatch_query_typed as _dispatch_query_typed,
)


def _timeout_error(exc: httpx.TimeoutException) -> str:
    """Render an httpx timeout as something a caller can act on.

    `str(httpx.ReadTimeout())` is the empty string, so a timed-out dispatch
    surfaced as a tool error with no message at all — indistinguishable from
    a crash, and silent about which endpoint or ceiling was involved. Nothing
    in this module caught httpx exceptions: `_raise_with_detail` only ever
    sees a response, and a timeout is raised before one exists.

    A client-side timeout does not stop the server, which keeps working on
    the request after the caller has given up. Say so, rather than implying
    the operation was cancelled.
    """
    # `exc.request` is a property that raises RuntimeError when unset, so
    # `getattr(exc, "request", None)` does not fall back — it propagates, and
    # the renderer crashes on exactly the case it exists to describe.
    try:
        req = exc.request
        where = f"{req.method} {req.url.path}"
    except RuntimeError:
        where = "the API"
    return json.dumps({
        "error": "api_timeout",
        "kind": type(exc).__name__,
        "message": (
            f"No response from {where} within the client timeout. The server "
            "may still be working on it — this call gave up, it did not cancel "
            "the request."
        ),
        "api_url": API_URL,
    }, indent=2)


async def _warn_if_deprecated_operation(
    args: object,
    ctx: _MCPContext | None,
) -> None:
    """Send a compatibility notice without changing a tool result payload."""

    if ctx is None:
        return
    operation = getattr(args, "operation", None)
    if not isinstance(operation, str):
        return

    from rka.mcp.operations_schema import operation_deprecation

    notice = operation_deprecation(operation)
    if notice is None:
        return
    await ctx.warning(json.dumps({
        "event": "rka.compatibility.deprecated_operation",
        "message": (
            f"RKA Core compatibility notice: '{operation}' is deprecated. "
            "Its current behavior is preserved for legacy audit and migration; "
            "use the standalone RKA Writer project for new authoring work."
        ),
        "operation": operation,
        "owner": notice.get("owner"),
        "migration_target": notice.get("migration_target"),
        "removal_milestone": notice.get("removal_milestone"),
        "removal_version": notice.get("removal_version"),
    }, sort_keys=True))


@tool(tier=_TIER_ALWAYS_ON, category="dispatch", always_load=True)
async def rka_query(
    args: _QueryArgsUnion,
    ctx: _MCPContext = None,  # type: ignore[assignment]
) -> str:
    """[ANY] READ from the project knowledge base. ALL reads flow through this verb.

    v2.7.0 NO-COMPROMISE typed-arg surface. The ``args`` parameter is a
    Pydantic discriminated union (keyed by ``operation``) covering every
    supported read operation. FastMCP renders the union as JSON Schema
    ``oneOf`` with per-branch enum + required-field arrays, so the
    LLM cannot emit an invalid operation, missing required field, or
    out-of-set enum value — the tool surface itself rejects pre-dispatch.

    Trigger phrases: "what's the status", "what's blocked", "search for",
    "show me decisions", "list literature", "fetch entity", "context",
    "research map", "decision tree", "ego graph", "trace provenance",
    "multi-hop", "claims", "clusters", "resolve entities", "changes since",
    "what changed", "review queue", "open checkpoints", "pending maintenance",
    "list projects", "capability discovery", "health check".

    Frozen Writer/Workbench compatibility branches remain callable but are not
    part of Core's default operation index. Use ``rka_describe('<operation>')``
    for their migration notice; use the standalone RKA Writer project for new
    authoring development.

    Args:
        args: One of the typed ``Query*Args`` models from
              ``rka.mcp.operation_args``. The model's ``operation`` field
              is the discriminator. Project-scoped operations require
              ``project_id``; the unscoped reads are ``list_projects``,
              ``capabilities``, and ``health``.

    Returns:
        JSON-string payload from the underlying REST endpoint, or a
        structured error JSON for the few legacy cases that emit one.

    NOTE: discoverability — call ``rka_describe('')`` for the
    operation-name index (now <250 tokens per Phase 3 mitigation
    of v2.7.0 compromise #3) or ``rka_describe('<op_name>')`` for
    full per-operation schema + examples.
    """
    await _warn_if_deprecated_operation(args, ctx)
    try:
        return await _dispatch_query_typed(args)
    except httpx.TimeoutException as exc:
        return _timeout_error(exc)


# Legacy untyped rka_query body preserved as a fallback callable for the
# ``RKA_LEGACY_TOOLS=1`` opt-in path.
async def _rka_query_legacy_impl(
    operation: Annotated[
        QueryScopeLit,
        Field(
            description=(
                "Read operation discriminator. Pick the one that matches "
                "what you want to read (status / context / search / entity "
                "/ journal / literature / mission / report / checkpoints / "
                "decision_tree / research_map / review_queue / claims / "
                "clusters / graph / ego_graph / provenance / multi_hop / "
                "summarize / evidence / pending_maintenance / freshness / "
                "contradictions / integrity / hooks / hook_executions / "
                "brain_notifications / calibration_metrics / changelog / "
                "workspace_tree / workspace_scan / bootstrap_review / "
                "manuscript / resolve_entities / changes_since / "
                "manuscript_context / manuscript_readiness / "
                "manuscript_reference_manifest / manuscript_spine / "
                "manuscript_writing_candidates / "
                "manuscript_impact / "
                "reference_validation_status / list_projects / health)."
            )
        ),
    ] = None,  # type: ignore[assignment]
    *,
    project_id: str | None = None,
    id: str | None = None,
    query: str | None = None,
    limit: int | None = None,
    filters: dict | None = None,
    options: dict | None = None,
    ids: list[str] | None = None,
    include_sources: bool = False,
    include_edges: bool = False,
    target_phase: str | None = None,
    cursor: int = 0,
    since_cursor: int = 0,
    manuscript_id: str | None = None,
    job_id: str | None = None,
    scope: str | None = None,  # v2.7.0a3 back-compat: legacy `scope` alias
) -> str:
    """[ANY] READ from the project knowledge base. ALL reads flow through this verb.

    This is one of the 3 always-on verbs in the v2.7.0a3 surface:
      - rka_query   — ALL READS (this tool).
      - rka_execute — ALL WRITES and lifecycle transitions.
      - rka_describe — schema lookup for any operation before calling.

    Trigger phrases: "what's the status", "what's blocked", "what needs
    attention", "search for", "show me decisions", "list literature",
    "fetch entity", "context", "research map", "render the map",
    "decision tree", "ego graph", "trace provenance", "multi-hop",
    "claims", "clusters", "review queue", "open checkpoints",
    "pending maintenance", "needs ratification", "list projects",
    "health check".

    If you're unsure which operation to use or what kwargs to pass,
    call `rka_describe(operation="...")` first for the full schema +
    examples.

    Args:
        operation: One of the supported read operations (status, context, search,
            entity, journal, literature, mission, report, checkpoints,
            decision_tree, calibration_metrics, hooks, hook_executions,
            brain_notifications, research_map, review_queue, clusters,
            claims, manuscript, graph, ego_graph, graph_stats,
            graph_mermaid, provenance, multi_hop, summarize,
            generate_summary, evidence, freshness, contradictions,
            integrity, pending_maintenance, changelog, bootstrap_review,
            workspace_tree, workspace_scan, reference_validation_status,
            list_projects, health).
        project_id: Project ID (prj_...). REQUIRED for project-scoped
            operations (most of them); UNSCOPED for list_projects and
            health.
        id: Entity ID — required for entity / ego_graph / provenance /
            evidence / manuscript / report / decision_tree (root) /
            bootstrap_review (scan_id) / mission (omit for current).
        query: Free-text query — required for search / multi_hop /
            generate_summary operations.
        limit: Cap on result count for list-mode operations.
        filters: Operation-specific filter dict (e.g. {"entity_types":
            ["decision"]}, {"status": "open"}, {"phase": "design"}).
        options: Reserved for operation-specific options (e.g. depth,
            format).
        ids: Entity IDs for resolve_entities.
        include_sources: Include direct claim-source closure in resolver output.
        include_edges: Include same-project typed edges in resolver output.
        target_phase: Lifecycle phase for manuscript_readiness.
        cursor: Opaque monotonic cursor for changes_since.
        since_cursor: Last synchronized cursor for manuscript_impact.
        manuscript_id: Manuscript scope for reference_validation_status.
        job_id: Validation job ID for reference_validation_status.
        scope: DEPRECATED v2.7.0a3 alias for `operation`. Accepted for
            one release window; removal scheduled in v2.8.0.
    """
    # v2.7.0a3 back-compat: legacy `scope` kwarg routes to the new
    # `operation` parameter. The alias is documented as deprecated;
    # supplying both is rejected as ambiguous.
    if scope is not None and operation is not None and scope != operation:
        return json.dumps({
            "error": "conflicting_args",
            "message": (
                "rka_query received both `operation` and `scope` with "
                "different values. `scope` is the deprecated v2.7.0a2 "
                "alias; pass only `operation`."
            ),
        }, indent=2)
    if operation is None and scope is not None:
        operation = scope  # type: ignore[assignment]
    if operation is None:
        return json.dumps({
            "error": "missing_field",
            "message": "rka_query requires `operation` (one of the read operations).",
        }, indent=2)
    s = operation
    if limit is None:
        # Preserve the historical 20-row default for ordinary legacy list
        # operations while matching the native change-feed endpoints' 100-row
        # defaults when callers omit the argument.
        limit = 100 if s in {"changes_since", "manuscript_impact"} else 20
    # UNSCOPED operations — no project_id needed.
    if s == "list_projects":
        async with _client() as c:
            r = await c.get("/api/projects")
            _raise_with_detail(r)
            return json.dumps(r.json(), indent=2)
    if s == "health":
        try:
            async with _client() as c:
                r = await c.get("/api/projects")
                ok = r.is_success
        except Exception as exc:
            return json.dumps(
                {"ok": False, "api_url": API_URL, "error": str(exc)},
                indent=2,
            )
        return json.dumps({"ok": ok, "api_url": API_URL}, indent=2)
    # All other operations are project-scoped; project_id is required.
    if not project_id:
        return json.dumps({
            "error": "missing_field",
            "message": (
                f"rka_query(args={{'operation': {s!r}, ...}}) requires "
                "`project_id` "
                "(every project-scoped read needs explicit project pinning "
                "in v2.6+)."
            ),
        }, indent=2)
    f = filters or {}
    if s == "status":
        return await _query_get(project_id, "/api/status")
    if s == "context":
        body = {"topic": query, "phase": f.get("phase")}
        return await _query_post(project_id, "/api/context", body)
    if s == "search":
        if not query:
            return json.dumps({"error": "rka_query(scope='search') requires `query`"}, indent=2)
        body = {"query": query, "entity_types": f.get("entity_types"), "limit": limit}
        return await _query_post(project_id, "/api/search", body)
    if s == "entity":
        if not id:
            return json.dumps({"error": "rka_query(scope='entity') requires `id`"}, indent=2)
        prefix = id.split("_")[0] if "_" in id else ""
        endpoint_map = {
            "jrn": f"/api/notes/{id}", "dec": f"/api/decisions/{id}",
            "lit": f"/api/literature/{id}", "clm": f"/api/claims/{id}",
            "ecl": f"/api/clusters/{id}", "mis": f"/api/missions/{id}",
            "chk": f"/api/checkpoints/{id}",
        }
        endpoint = endpoint_map.get(prefix)
        if not endpoint:
            return json.dumps({"error": f"Unknown ID prefix '{prefix}'"}, indent=2)
        params = {"include_claims": "true"} if prefix == "ecl" else None
        return await _query_get(project_id, endpoint, params=params)
    if s == "journal":
        params = {
            "limit": limit, "hide_superseded": True,
            "type": f.get("type"), "phase": f.get("phase"),
            "confidence": f.get("confidence"), "status": f.get("status"),
            "since": f.get("since"), "source": f.get("source"),
            "tags": f.get("tags"),
        }
        return await _query_get(project_id, "/api/notes", params=params)
    if s == "literature":
        params = {
            "status": f.get("status"), "tag": f.get("tag"), "limit": limit,
        }
        return await _query_get(project_id, "/api/literature", params=params)
    if s == "mission":
        if id:
            return await _query_get(project_id, f"/api/missions/{id}")
        # fallback to current active/pending
        for st in ("active", "pending"):
            data = await _query_get(project_id, "/api/missions", params={"status": st, "limit": 1})
            arr = json.loads(data) if data else []
            if arr:
                return json.dumps(arr[0], indent=2)
        return json.dumps({"note": "no active or pending mission"}, indent=2)
    if s == "report":
        mid = id
        if not mid:
            return json.dumps({"error": "rka_query(scope='report') requires `id` (mission_id)"}, indent=2)
        return await _query_get(project_id, f"/api/missions/{mid}/report")
    if s == "checkpoints":
        return await _query_get(project_id, "/api/checkpoints", params={"status": f.get("status", "open")})
    if s == "decision_tree":
        params = {"root_id": id, "phase": f.get("phase"), "depth": f.get("depth")}
        return await _query_get(project_id, "/api/decisions/tree", params=params)
    if s == "calibration_metrics":
        return await _query_get(project_id, "/api/calibration/metrics")
    if s == "hooks":
        params = {"enabled": f.get("enabled"), "hook_type": f.get("hook_type")}
        return await _query_get(project_id, "/api/hooks", params=params)
    if s == "hook_executions":
        params = {"hook_id": f.get("hook_id"), "since": f.get("since"), "limit": limit}
        return await _query_get(project_id, "/api/hooks/executions", params=params)
    if s == "brain_notifications":
        params = {"status": f.get("status"), "limit": limit}
        return await _query_get(project_id, "/api/notifications/brain", params=params)
    if s == "research_map":
        return await _query_get(project_id, "/api/research-map")
    if s == "review_queue":
        params = {"target_type": f.get("target_type"), "limit": limit}
        return await _query_get(project_id, "/api/review-queue", params=params)
    if s == "clusters":
        params = {
            "research_question_id": f.get("research_question_id"),
            "confidence": f.get("confidence"), "limit": limit,
        }
        return await _query_get(project_id, "/api/clusters", params=params)
    if s == "claims":
        params = {
            "cluster_id": f.get("cluster_id"), "type": f.get("type"),
            "confidence": f.get("confidence"), "limit": limit,
        }
        return await _query_get(project_id, "/api/claims", params=params)
    if s == "manuscript":
        if not id:
            return json.dumps({"error": "rka_query(scope='manuscript') requires `id`"}, indent=2)
        return await _query_get(project_id, f"/api/manuscripts/{id}")
    if s == "resolve_entities":
        if not ids:
            return json.dumps(
                {"error": "rka_query(scope='resolve_entities') requires `ids`"},
                indent=2,
            )
        return await _query_post(
            project_id,
            "/api/entities/resolve",
            {
                "ids": ids,
                "include_sources": include_sources,
                "include_edges": include_edges,
            },
        )
    if s == "manuscript_context":
        if not id:
            return json.dumps(
                {"error": "rka_query(scope='manuscript_context') requires `id`"},
                indent=2,
            )
        return await _query_get(project_id, f"/api/manuscripts/{id}/context")
    if s == "manuscript_reference_manifest":
        if not id:
            return json.dumps(
                {
                    "error": (
                        "rka_query(scope='manuscript_reference_manifest') "
                        "requires `id`"
                    )
                },
                indent=2,
            )
        return await _query_get(
            project_id,
            f"/api/manuscripts/{id}/references",
        )
    if s == "manuscript_readiness":
        if not id:
            return json.dumps(
                {"error": "rka_query(scope='manuscript_readiness') requires `id`"},
                indent=2,
            )
        return await _query_get(
            project_id,
            f"/api/manuscripts/{id}/readiness",
            params={"target_phase": target_phase or "drafting"},
        )
    if s == "manuscript_spine":
        if not id:
            return json.dumps(
                {"error": "rka_query(scope='manuscript_spine') requires `id`"},
                indent=2,
            )
        return await _query_get(project_id, f"/api/manuscripts/{id}/spine")
    if s == "manuscript_outline":
        if not id:
            return json.dumps(
                {"error": "rka_query(scope='manuscript_outline') requires `id`"},
                indent=2,
            )
        return await _query_get(project_id, f"/api/manuscripts/{id}/outline")
    if s == "manuscript_writing_candidates":
        if not id:
            return json.dumps(
                {
                    "error": (
                        "rka_query(scope='manuscript_writing_candidates') "
                        "requires `id`"
                    )
                },
                indent=2,
            )
        return await _query_get(
            project_id,
            f"/api/manuscripts/{id}/writing-candidates",
        )
    if s == "changes_since":
        return await _query_get(
            project_id,
            "/api/changes",
            params={"cursor": cursor, "limit": limit},
        )
    if s == "manuscript_impact":
        if not id:
            return json.dumps(
                {"error": "rka_query(scope='manuscript_impact') requires `id`"},
                indent=2,
            )
        return await _query_get(
            project_id,
            f"/api/manuscripts/{id}/impact",
            params={"since_cursor": since_cursor, "limit": limit},
        )
    if s == "reference_validation_status":
        if not manuscript_id or not job_id:
            return json.dumps(
                {
                    "error": (
                        "rka_query(scope='reference_validation_status') "
                        "requires `manuscript_id` and `job_id`"
                    )
                },
                indent=2,
            )
        return await _query_get(
            project_id,
            (
                f"/api/manuscripts/{manuscript_id}/reference-validations/"
                f"{job_id}"
            ),
        )
    if s == "graph":
        params = {
            "include_types": f.get("include_types"),
            "phase": f.get("phase"), "limit": limit,
        }
        return await _query_get(project_id, "/api/graph", params=params)
    if s == "ego_graph":
        if not id:
            return json.dumps({"error": "rka_query(scope='ego_graph') requires `id`"}, indent=2)
        return await _query_get(project_id, f"/api/graph/ego/{id}", params={"depth": f.get("depth")})
    if s == "graph_stats":
        return await _query_get(project_id, "/api/graph/stats")
    if s == "graph_mermaid":
        return await _query_get(project_id, "/api/graph/mermaid", params={"phase": f.get("phase")})
    if s == "provenance":
        if not id:
            return json.dumps({"error": "rka_query(scope='provenance') requires `id`"}, indent=2)
        return await _query_get(
            project_id, f"/api/provenance/{id}",
            params={"depth": f.get("depth"), "direction": f.get("direction")},
        )
    if s == "multi_hop":
        if not query:
            return json.dumps({"error": "rka_query(scope='multi_hop') requires `query`"}, indent=2)
        body = {"query": query, "hops": f.get("hops"), "limit": limit}
        return await _query_post(project_id, "/api/retrieval/multi-hop", body)
    if s == "summarize":
        body = {"topic": query, "phase": f.get("phase"), "entity_ids": f.get("entity_ids")}
        return await _query_post(project_id, "/api/summarize", body)
    if s == "generate_summary":
        # v2.4.0 LLM-driven path removed; preserved as a scope stub.
        return json.dumps({"error": "rka_query(scope='generate_summary') is not yet re-wired post-v2.4.0"}, indent=2)
    if s == "evidence":
        if not id:
            return json.dumps({"error": "rka_query(scope='evidence') requires `id` (research_question_id)"}, indent=2)
        return await _query_get(
            project_id, "/api/assemble-evidence",
            params={"research_question_id": id, "format": f.get("format")},
        )
    if s == "freshness":
        params = {"threshold": f.get("threshold"), "limit": limit}
        return await _query_get(project_id, "/api/freshness/check", params=params)
    if s == "contradictions":
        params = {"scope": f.get("scope"), "limit": limit}
        return await _query_get(project_id, "/api/contradictions/detect", params=params)
    if s == "integrity":
        return await _query_get(project_id, "/api/maintenance/integrity")
    if s == "pending_maintenance":
        return await _query_get(project_id, "/api/maintenance")
    if s == "changelog":
        params = {"since": f.get("since"), "limit": limit}
        return await _query_get(project_id, "/api/changelog", params=params)
    if s == "bootstrap_review":
        if not id:
            return json.dumps({"error": "rka_query(scope='bootstrap_review') requires `id` (scan_id)"}, indent=2)
        return await _query_get(project_id, f"/api/workspace/review/{id}")
    if s == "workspace_tree":
        # Read-only filesystem probe; delegated to /api/workspace/tree if
        # the REST surface exposes it. Legacy rka_scan_workspace_tree did
        # local os.scandir; the verb routes via the REST layer for
        # bookkeeper-clean adaptation. Service-layer responsible.
        params = {"folder_path": f.get("folder_path"), "max_depth": f.get("max_depth")}
        return await _query_get(project_id, "/api/workspace/tree", params=params)
    if s == "workspace_scan":
        body = {
            "folder_path": f.get("folder_path"),
            "ignore_patterns": f.get("ignore_patterns"),
            "max_files": f.get("max_files"),
        }
        return await _query_post(project_id, "/api/workspace/scan", body)
    return json.dumps({"error": f"rka_query: unknown scope '{s}'"}, indent=2)



@tool(tier=_TIER_ALWAYS_ON, category="verb")
async def rka_record_note(
    content: str,
    *,
    project_id: str,
    source: SourceLiteral = "executor",
    type: str = "note",
    confidence: ConfidenceLiteral = "hypothesis",
    importance: ImportanceLiteral = "normal",
    verbatim_input: str | None = None,
    phase: str | None = None,
    tags: list[str] | None = None,
    provenance: dict | None = None,
    capture_mode: JournalCaptureModeLit = "unknown",
) -> str:
    """[BRAIN/EXECUTOR/PI] RECORD a journal entry (note, log, directive).

    Trigger phrases: "log this", "add a note", "record finding", "save the
    PI directive", "journal".

    PROVENANCE (Graft A — top-level): pass `provenance={"related_decisions":
    [dec_..], "related_literature": [lit_..], "related_mission": "mis_..",
    "supersedes": "jrn_.."}`. Each provenance key flattens to a top-level
    field on the underlying POST /api/notes body so existing service-layer
    handlers remain unchanged.

    PI input requires exact verbatim_input, except explicit raw_capture may
    snapshot supplied content on creation. Omitted mode remains unknown.

    Args:
        content: Note body (PRIMARY).
        project_id: Project ID (prj_...). REQUIRED.
        source: brain | executor | pi | web_ui | llm.
        type: note | log | directive (legacy types auto-normalised).
        confidence: hypothesis | tested | verified | superseded | retracted.
        importance: critical | high | normal | low | archived.
        verbatim_input: Exact original; PI source requires it unless raw capture snapshots content.
        capture_mode: unknown (default), raw_capture, or agent_restatement.
        phase: Research phase override.
        tags: Free-form tag list.
        provenance: Optional dict with related_decisions / related_literature /
            related_mission / supersedes.
    """
    if capture_mode != "raw_capture" and source == "pi" and not (verbatim_input or "").strip():
        return json.dumps({
            "error": "rka_record_note(source='pi') requires `verbatim_input` "
                     "(exact PI wording). This is the Phase-X²-prime polish "
                     "enforcement — PI intellectual attribution must be "
                     "preserved verbatim."
        }, indent=2)
    prov = provenance or {}
    body = _strip_none({
        "content": content, "type": type, "source": source,
        "phase": phase, "verbatim_input": verbatim_input,
        "capture_mode": capture_mode,
        "related_decisions": prov.get("related_decisions"),
        "related_literature": prov.get("related_literature"),
        "related_mission": prov.get("related_mission"),
        "supersedes": prov.get("supersedes"),
        "confidence": confidence, "importance": importance, "tags": tags,
    })
    async with _client(project_id) as c:
        r = await c.post("/api/notes", json=body)
        _raise_with_detail(r)
        d = r.json()
        _record_entity("journal", d["id"], content)
        return f"Created {d['id']} [{d['type']}] confidence={d['confidence']}"


@tool(tier=_TIER_ALWAYS_ON, category="verb")
async def rka_record_decision(
    question: str,
    chosen: str,
    rationale: str,
    *,
    project_id: str,
    decided_by: DecidedByLiteral,
    kind: DecisionKindLiteral,
    related_journal: list[str],
    options: list[dict] | None = None,
    supersedes_decision_id: str | None = None,
    confidence: ConfidenceLiteral = "tested",
    tags: list[str] | None = None,
    phase: str | None = None,
    parent_id: str | None = None,
    related_literature: list[str] | None = None,
    assumptions: list[str] | None = None,
) -> str:
    """[BRAIN/PI] RECORD a decision node. Provenance-required.

    Trigger phrases: "record decision", "ratify the choice", "log decision",
    "supersede dec_...", "design choice".

    Provenance discipline (Run-5 hard contract): `related_journal` is
    REQUIRED non-empty — every decision must justify its conclusion against
    at least one journal entry. The orchestrator's BRAIN_SYSTEM prompt
    documents the same constraint; this verb-side check is a defense-in-
    depth guard so direct (non-orchestrator) callers also satisfy the
    invariant.

    To supersede an older decision, pass `supersedes_decision_id` —
    the new decision becomes the head with a `supersedes` link to the
    old; the older decision's status flips to 'superseded'.

    Args:
        question: Decision question (PRIMARY).
        chosen: Chosen option label.
        rationale: Why this was chosen.
        project_id: Project ID (prj_...). REQUIRED.
        decided_by: pi | brain | executor.
        kind: research_question | design_choice | decision | operational.
        related_journal: Journal entry IDs justifying this decision. REQUIRED non-empty.
        options: Considered options [{label, description}, ...].
        supersedes_decision_id: Old decision this replaces (creates supersede link).
        confidence: ACCEPTED FOR BACK-COMPAT BUT NOT FORWARDED. `DecisionCreate`
            has no `confidence` field (`extra='forbid'`); confidence is a journal
            attribute, not a decision one. See `rka/models/journal.py` for the
            real home of `ConfidenceLit`. Prior versions sent this in the body
            and 422'd. v2.7.0.6 silently drops it to preserve back-compat with
            old transcripts.
        tags: Free-form tag list.
        phase: Research phase override.
        parent_id: Parent decision ID for tree structure.
        related_literature: Literature IDs informing this decision.
        assumptions: Assumptions this decision rests on.

    Note (v2.7.0.6): the `confidence` kwarg is accepted but NOT forwarded
    to the API for the reasons above. Parity gap with
    `dispatch_record_decision`: `related_missions` is also accepted upstream
    but not forwarded here (signature does not declare it). Do not re-add
    `confidence` to the body dicts — that was the v2.7.0.4 bug class
    (extra_forbidden 422 on every call).
    """
    # confidence is intentionally NOT used; see docstring note.
    del confidence
    if not related_journal:
        return json.dumps({
            "error": "rka_record_decision requires `related_journal=[...]` "
                     "non-empty. Provenance discipline: every decision must "
                     "justify its conclusion against at least one journal entry."
        }, indent=2)
    # Supersede path: hit the supersede endpoint so the old decision's
    # status flips correctly + the supersede link materializes.
    if supersedes_decision_id:
        body: dict = {
            "question": question,
            "chosen": chosen,
            "rationale": rationale,
            "decided_by": decided_by,
            "kind": kind,
            "related_journal": related_journal,
        }
        for key, value in (
            ("phase", phase),
            ("options", options),
            ("related_literature", related_literature),
            ("tags", tags),
            ("assumptions", assumptions),
            ("parent_id", parent_id),
        ):
            if value is not None:
                body[key] = value
        async with _client(project_id) as c:
            r = await c.post(
                f"/api/decisions/{supersedes_decision_id}/supersede", json=body
            )
            _raise_with_detail(r)
            d = r.json()
            _session.decisions_made.append(d["id"])
            return (
                f"Created decision {d['id']} (supersedes "
                f"{supersedes_decision_id}): {d['question'][:80]}"
            )
    body = {
        "question": question,
        "chosen": chosen,
        "rationale": rationale,
        "decided_by": decided_by,
        "kind": kind,
        "related_journal": related_journal,
    }
    for key, value in (
        ("phase", phase),
        ("options", options),
        ("related_literature", related_literature),
        ("tags", tags),
        ("assumptions", assumptions),
        ("parent_id", parent_id),
    ):
        if value is not None:
            body[key] = value
    async with _client(project_id) as c:
        r = await c.post("/api/decisions", json=body)
        _raise_with_detail(r)
        d = r.json()
        _session.decisions_made.append(d["id"])
        return f"Created decision {d['id']}: {d['question'][:80]}"


@tool(tier=_TIER_ALWAYS_ON, category="verb")
async def rka_session(
    action: Annotated[SessionActionLit, Field(description="Session-level action.")],
    *,
    project_id: str | None = None,
    name: str | None = None,
    description: str | None = None,
    role: str = "executor",
    format: str = "markdown",
    scope: str = "state",
    since: str | None = None,
    limit: int = 20,
) -> str:
    """[ANY] UNSCOPED meta operations: project discovery / creation / session
    introspection / health / help / export / claude.md generation.

    Trigger phrases: "list projects", "create project", "session digest",
    "reset session", "health check", "help on rka_*", "generate CLAUDE.md",
    "export project".

    Most actions are unscoped (no project_id). Actions that are
    project-scoped despite living under this verb (`export`,
    `generate_claude_md`, `digest`, `changelog`) require `project_id`.

    Args:
        action: list_projects | create_project | set_project (deprecated) |
            reset | digest | health | help | export | generate_claude_md.
        project_id: Required for project-scoped actions (export, digest,
            generate_claude_md).
        name: Project name — required for action='create_project'.
        description: Project description — optional for create_project.
            For action='set_project', the project ID to verify exists.
        role: Role for generate_claude_md (executor | brain).
        format: Export format (markdown).
        scope: Export scope (state | decisions | literature).
        since: ISO date for filtered exports.
        limit: Cap for paged exports.
    """
    a = action
    if a == "list_projects":
        async with _client() as c:
            r = await c.get("/api/projects")
            _raise_with_detail(r)
            return json.dumps(r.json(), indent=2)
    if a == "create_project":
        if not name:
            return json.dumps({"error": "rka_session(action='create_project') requires `name`"}, indent=2)
        body = _strip_none({"name": name, "description": description})
        async with _client() as c:
            r = await c.post("/api/projects", json=body)
            _raise_with_detail(r)
            return json.dumps(r.json(), indent=2)
    if a == "set_project":
        # Deprecated no-op since v2.6. Validates the project exists.
        target = description or project_id
        if not target:
            return json.dumps({
                "error": "rka_session(action='set_project') is deprecated in "
                         "v2.6+ — every tool call carries `project_id` "
                         "explicitly. Pass the desired project_id directly."
            }, indent=2)
        async with _client() as c:
            r = await c.get("/api/projects")
            _raise_with_detail(r)
            projects = r.json()
        ok = any(p["id"] == target for p in projects)
        return json.dumps({
            "deprecated": True,
            "project_id": target,
            "exists": ok,
            "note": "rka_session(action='set_project') is a no-op in v2.6+. "
                    "Pass `project_id=...` to every subsequent verb call.",
        }, indent=2)
    if a == "reset":
        global _session
        _session = MCPSessionState()
        return "Session state reset. Tool-call counter, entity log, and session_start fired-markers cleared."
    if a == "digest":
        if not project_id:
            return json.dumps({"error": "rka_session(action='digest') requires `project_id`"}, indent=2)
        # Local in-process digest + a remote status pull, mirrors the
        # legacy rka_session_digest body without the buggy session.project_id
        # reference (that field was removed in v2.6).
        lines = [
            "## Session Digest",
            f"Project: {project_id}",
            f"Tool calls: {_session.tool_calls}",
            f"Session started: {_session.session_start}",
        ]
        if _session.entities_created:
            lines.append(f"\n### Entities Created ({len(_session.entities_created)})")
            for e in _session.entities_created:
                lines.append(f"  [{e['type']}] {e['id']}: {e['summary']}")
        if _session.decisions_made:
            lines.append(f"\n### Decisions Recorded ({len(_session.decisions_made)})")
            for did in _session.decisions_made:
                lines.append(f"  {did}")
        if _session.checkpoints_raised:
            lines.append(f"\n### Checkpoints Raised ({len(_session.checkpoints_raised)})")
            for cid in _session.checkpoints_raised:
                lines.append(f"  {cid}")
        try:
            async with _client(project_id) as c:
                sr = await c.get("/api/status")
                if sr.is_success:
                    st = sr.json()
                    lines.append("\n### Current Project State")
                    lines.append(f"Phase: {st.get('current_phase', '?')}")
                    if st.get("summary"):
                        lines.append(f"Summary: {st['summary'][:500]}")
                    if st.get("blockers"):
                        lines.append(f"Blockers: {st['blockers']}")
                cr = await c.get("/api/checkpoints", params={"status": "open"})
                if cr.is_success:
                    chks = cr.json()
                    if chks:
                        lines.append(f"\n### Open Checkpoints ({len(chks)})")
                        for chk in chks[:5]:
                            flag = "🔴" if chk.get("blocking") else "🟡"
                            lines.append(f"  {flag} {chk['id']}: {chk.get('description', '')[:300]}")
        except Exception:
            pass
        return "\n".join(lines)
    if a == "health":
        # Lightweight liveness probe — never raises; returns {ok, api_url}.
        try:
            async with _client() as c:
                r = await c.get("/api/projects")
                ok = r.is_success
        except Exception as exc:
            return json.dumps({"ok": False, "api_url": API_URL, "error": str(exc)}, indent=2)
        return json.dumps({"ok": ok, "api_url": API_URL}, indent=2)
    if a == "help":
        # Render the verb registry (or a specific verb if `name` given).
        # Keeps rka_help (legacy) for full-catalog browsing.
        if name:
            rec = _TOOL_REGISTRY.get(name.strip())
            if rec is None:
                return json.dumps({"error": "unknown_tool", "name": name}, indent=2)
            return json.dumps({
                "name": name, "tier": rec["tier"], "category": rec["category"],
                "signature": rec["signature"], "summary": rec["summary"],
                "docstring": rec["docstring"],
            }, indent=2)
        verbs = [
            n for n, r in _TOOL_REGISTRY.items()
            if r.get("category") == "verb"
        ]
        return json.dumps({
            "verbs": sorted(verbs),
            "note": "Eight always-on verbs (v2.7.0 PR-1+). Use `name=<verb>` "
                    "for the full signature/docstring.",
        }, indent=2)
    if a == "export":
        if not project_id:
            return json.dumps({"error": "rka_session(action='export') requires `project_id`"}, indent=2)
        params = {"format": format, "scope": scope}
        async with _client(project_id) as c:
            r = await c.get("/api/export", params=params)
            _raise_with_detail(r)
            try:
                return json.dumps(r.json(), indent=2)
            except Exception:
                return r.text
    if a == "generate_claude_md":
        if not project_id:
            return json.dumps({"error": "rka_session(action='generate_claude_md') requires `project_id`"}, indent=2)
        async with _client(project_id) as c:
            r = await c.get("/api/generate-claude-md", params={"role": role})
            _raise_with_detail(r)
            data = r.json()
            return data.get("markdown", "") or json.dumps(data, indent=2)
    return json.dumps({"error": f"rka_session: unknown action '{a}'"}, indent=2)


# ============================================================
# v2.7.0 PR-1 (Agent B) — 4 write/lifecycle verbs
# ============================================================
# Continues the Agent A surface (rka_query, rka_record_note,
# rka_record_decision, rka_session) with 4 additional write/lifecycle
# verbs:
#   - rka_record_literature  (several modes: add | bibtex | search S2 |
#                             search arXiv | enrich DOI | link_zotero |
#                             process_paper)
#   - rka_mission            (create | update | update_status |
#                             submit_report | get_report | advance_rq)
#   - rka_checkpoint         (submit | resolve | create_gate | evaluate_gate |
#                             present_decision | pi_select | record_outcome)
#   - rka_review             (~25 targets: updates, hooks, claims/clusters,
#                             maintenance, manuscript, workspace, ...)
#
# Bodies are thin adapters into rka/mcp/verb_dispatch.py — that module
# routes to existing legacy @tool(...) functions via _TOOL_REGISTRY so
# the legacy validation / hook firing / session-tracking path stays
# unchanged. Legacy 91 tools remain callable; PR-2 demotes their tier.
#
# Provenance discipline preserved at the verb layer:
#   * rka_mission(action='create') -> provenance.motivated_by_decision
#   * rka_record_literature with action='process_paper' uses lit_id
#     so the journal entry has derived_from links to the source paper.

from rka.mcp.verb_dispatch import (
    _OMITTED_VERBATIM,
    dispatch_record_literature as _dispatch_record_literature,
    dispatch_mission as _dispatch_mission,
    dispatch_checkpoint as _dispatch_checkpoint,
    dispatch_review as _dispatch_review,
    dispatch_execute as _dispatch_execute,
    EXECUTE_OPERATIONS as _EXECUTE_OPERATIONS,
)

from rka.mcp._enums import LitStatusLit as _LitStatusLit


# Action discriminator literals — kept inline alongside the verb so the
# enum surface is self-contained and review-able.
MissionActionLit = _Literal[
    "create", "update", "update_status",
    "submit_report", "get_report", "advance_rq",
]

CheckpointActionLit = _Literal[
    "submit", "resolve", "create_gate", "evaluate_gate",
    "present_decision", "pi_select", "record_outcome",
]

LiteratureActionLit = _Literal[
    "link_zotero", "import_bibtex", "enrich_doi",
    "search_semantic_scholar", "search_arxiv",
    "process_paper",
]

LiteratureSearchSrcLit = _Literal["semantic_scholar", "arxiv"]

ReviewTargetLit = _Literal[
    # Entity updates
    "note_update", "decision_update", "literature_update", "status_update",
    # Bulk
    "bulk_update", "batch_import",
    # Hooks
    "hook_add", "hook_enable", "hook_disable", "hook_delete",
    # Notifications
    "brain_notifications_clear",
    # Claims / clusters
    "extract_claims", "cluster", "claims", "cluster_create",
    "cluster_assign", "cluster_split", "cluster_merge",
    # Maintenance
    "contradiction", "flag_stale", "eviction_sweep",
    # Workspace
    "bootstrap_workspace",
    # Manuscript
    "manuscript_register",
    # Decision lifecycle (supersede is a record-shape but kept here as
    # the umbrella maintenance verb; rka_record_decision also offers
    # it via supersedes_decision_id top-level kwarg).
    "supersede_decision",
]


@tool(tier=_TIER_ALWAYS_ON, category="verb")
async def rka_record_literature(
    *,
    project_id: str,
    title: str | None = None,
    bibtex: str | None = None,
    search_query: str | None = None,
    search_source: Annotated[
        LiteratureSearchSrcLit | None,
        Field(description="Search backend for search_query mode."),
    ] = None,
    doi: str | None = None,
    authors: list[str] | None = None,
    year_min: int | None = None,
    venue: str | None = None,
    status: Annotated[
        _LitStatusLit,
        Field(description="Reading lifecycle status for new entries."),
    ] = "to_read",
    abstract: str | None = None,
    url: str | None = None,
    tags: list[str] | None = None,
    related_decisions: list[str] | None = None,
    action: Annotated[
        LiteratureActionLit | None,
        Field(description="Optional explicit mode discriminator."),
    ] = None,
    lit_id: str | None = None,
    zotero_key: str | None = None,
    pdf_path: str | None = None,
    annotations: list[dict] | None = None,
    summary: str | None = None,
    add_to_library: bool = False,
    import_top_n: int | None = None,
    limit: int = 10,
    year: int | None = None,
) -> str:
    """[BRAIN] WRITE / search / import / link literature. Several modes:

    - title=... (default): explicit add via fields
    - bibtex=...: import one or more entries from BibTeX
    - search_query=... + search_source='semantic_scholar' | 'arxiv': search
    - doi=... only (no title/authors): bootstrap a row + enrich later
    - action='link_zotero' + lit_id: resolve Zotero item key
    - action='enrich_doi' + lit_id: CrossRef enrichment
    - action='process_paper' + lit_id + annotations: reading-notes pipeline

    For LIST reads use `rka_query(scope='literature')`. The verb
    accepts `add_to_library=True` on search modes to auto-create
    library rows for results; pair with `import_top_n=N` to cap the
    slice of results that get imported.

    Args:
        project_id: REQUIRED. Project ID (prj_...).
        title: Paper title (default add mode).
        bibtex: Raw BibTeX (bulk import mode).
        search_query: Free-text query for S2/arXiv modes.
        search_source: 'semantic_scholar' or 'arxiv'.
        doi: DOI identifier.
        authors: Author list.
        year_min: Minimum publication year. On search modes
            (semantic_scholar / arxiv), filters results to year>=year_min.
            On default-add mode, populates the paper's publication year
            directly (a single-paper year is the floor of its own year
            set). Legacy alias: `year` — deprecated, removal v2.8.
        venue, abstract, url, tags, related_decisions: optional metadata.
        status: Reading lifecycle for the new entry. Default 'to_read'.
        action: Explicit mode discriminator; overrides kwarg-presence inference.
        lit_id: Existing literature ID — required by enrich_doi /
            link_zotero / process_paper.
        zotero_key, pdf_path: Optional adapter kwargs.
        annotations: Reading-notes list for process_paper mode.
        summary: Overall paper summary for process_paper mode.
        add_to_library: On search modes, auto-add results to RKA.
        import_top_n: On search modes with add_to_library=True, import
            only the first N results. None = import all returned.
            Ignored when add_to_library=False.
        limit: Search result cap (default 10).
        year: DEPRECATED — alias of `year_min`. Setting both raises
            `conflicting_args`. Will be removed in v2.8.
    """
    return await _dispatch_record_literature(
        project_id=project_id,
        title=title,
        bibtex=bibtex,
        search_query=search_query,
        search_source=search_source,
        doi=doi,
        authors=authors,
        year_min=year_min,
        venue=venue,
        status=status,
        abstract=abstract,
        url=url,
        tags=tags,
        related_decisions=related_decisions,
        action=action,
        lit_id=lit_id,
        manuscript_id=None,
        zotero_key=zotero_key,
        pdf_path=pdf_path,
        annotations=annotations,
        summary=summary,
        add_to_library=add_to_library,
        import_top_n=import_top_n,
        limit=limit,
        year=year,
    )


@tool(tier=_TIER_ALWAYS_ON, category="verb")
async def rka_mission(
    action: Annotated[
        MissionActionLit,
        Field(description="Mission lifecycle action."),
    ],
    *,
    project_id: str,
    mission_id: str | None = None,
    rq_id: str | None = None,
    objective: str | None = None,
    phase: str | None = None,
    tasks: list[dict] | None = None,
    context: str | None = None,
    acceptance_criteria: str | None = None,
    scope_boundaries: str | None = None,
    checkpoint_triggers: str | None = None,
    depends_on: str | None = None,
    parent_mission_id: str | None = None,
    motivated_by_decision: str | None = None,
    provenance: dict | None = None,
    tags: list[str] | None = None,
    status: MissionStatusLiteral | None = None,
    summary: str | None = None,
    content: str | None = None,
    findings: str | list[str] = "",
    anomalies: str | list[str] = "",
    questions: str | list[str] = "",
    codebase_state: str = "",
    recommended_next: str = "",
    conclusion: str | None = None,
    evidence_cluster_ids: list[str] | None = None,
) -> str:
    """[EXECUTOR][BRAIN] Mission verb — action-discriminated.

    Actions: create | update | update_status | submit_report |
    get_report | advance_rq.

    Provenance: `create` REQUIRES motivated_by_decision (top-level)
    OR provenance={"motivated_by_decision": "dec_..."} (Graft A
    surface). Preserves the decision -> mission causality chain.

    For body edits use 'update'; for lifecycle transitions use
    'update_status'. RQ lifecycle (`advance_rq`) is reserved for
    decisions of kind=research_question.

    Args:
        action: PRIMARY discriminator.
        project_id: REQUIRED.
        mission_id: REQUIRED for update / update_status / submit_report
            / get_report (optional).
        rq_id: REQUIRED for advance_rq.
        objective: Mission body — required for create.
        phase: Research phase.
        tasks: MissionTask list [{description, status}].
        context, acceptance_criteria, scope_boundaries,
            checkpoint_triggers, depends_on, parent_mission_id: optional
            create / update fields.
        motivated_by_decision: Linked dec_... — provenance entry on create.
        provenance: Graft A nested form — {motivated_by_decision: dec_...}.
        tags: Tag list.
        status: Lifecycle status — for update_status.
        summary, content: Submit-report body. (content is the v2.6.1 alias.)
        findings, anomalies, questions, codebase_state, recommended_next:
            optional structured report sections.
        conclusion, evidence_cluster_ids: advance_rq payload.
    """
    return await _dispatch_mission(
        action,
        project_id=project_id,
        mission_id=mission_id,
        rq_id=rq_id,
        objective=objective,
        phase=phase,
        tasks=tasks,
        context=context,
        acceptance_criteria=acceptance_criteria,
        scope_boundaries=scope_boundaries,
        checkpoint_triggers=checkpoint_triggers,
        depends_on=depends_on,
        parent_mission_id=parent_mission_id,
        motivated_by_decision=motivated_by_decision,
        provenance=provenance,
        tags=tags,
        status=status,
        summary=summary,
        content=content,
        findings=findings,
        anomalies=anomalies,
        questions=questions,
        codebase_state=codebase_state,
        recommended_next=recommended_next,
        conclusion=conclusion,
        evidence_cluster_ids=evidence_cluster_ids,
    )


@tool(tier=_TIER_ALWAYS_ON, category="verb")
async def rka_checkpoint(
    action: Annotated[
        CheckpointActionLit,
        Field(description="Checkpoint / gate / decision-ratification action."),
    ],
    *,
    project_id: str,
    id: str | None = None,
    mission_id: str | None = None,
    decision_id: str | None = None,
    gate_id: str | None = None,
    type: CheckpointTypeLiteral | None = None,
    description: str | None = None,
    content: str | None = None,
    task_reference: str | None = None,
    context: str | None = None,
    options: list[dict] | None = None,
    recommendation: str | None = None,
    blocking: bool = True,
    resolution: str | None = None,
    resolved_by: str | None = None,
    rationale: str | None = None,
    create_decision: bool = False,
    gate_type: str | None = None,
    deliverables: list[str] | None = None,
    pass_criteria: list[str] | None = None,
    assumptions_to_verify: list[str] | None = None,
    verdict: str | None = None,
    notes: str | None = None,
    assumption_status: dict[str, str] | None = None,
    confirmation_brief: str | None = None,
    pi_preference: str | None = None,
    selected_option_id: str | None = None,
    override_rationale: str | None = None,
    outcome: str | None = None,
    outcome_details: str | None = None,
    lessons: str | None = None,
    recorded_by: str = "pi",
) -> str:
    """[BRAIN][PI] Checkpoint + gate + decision-ratification verb.

    Actions: submit | resolve | create_gate | evaluate_gate |
    present_decision | pi_select | record_outcome.

    `present_decision` is the TWO-TAP autonomy-licensing surface
    (PI ratification gate for privileged writes); `pi_select`
    records the PI's answer. `record_outcome` closes the
    calibration loop — write the real-world result of a decision
    once known.

    Pass `decision_id` (or `id`) on present_decision / pi_select /
    record_outcome. Pass `mission_id` on submit / create_gate.
    Pass `gate_id` on evaluate_gate. Pass `id` on resolve.

    Args:
        action: PRIMARY discriminator.
        project_id: REQUIRED.
        id: Checkpoint ID (resolve target).
        mission_id: For submit / create_gate.
        decision_id: For present_decision / pi_select / record_outcome.
        gate_id: For evaluate_gate.
        type: Checkpoint type for submit.
        description, content: Submit body (content is v2.6.1 alias).
        task_reference, context, options, recommendation, blocking: optional submit.
        resolution, resolved_by, rationale, create_decision: resolve payload.
        gate_type, deliverables, pass_criteria, assumptions_to_verify:
            create_gate payload.
        verdict, notes, assumption_status: evaluate_gate payload.
        confirmation_brief, options, pi_preference: present_decision payload.
        selected_option_id, override_rationale: pi_select payload.
        outcome, outcome_details, lessons, recorded_by: record_outcome payload.
    """
    return await _dispatch_checkpoint(
        action,
        project_id=project_id,
        id=id,
        mission_id=mission_id,
        decision_id=decision_id,
        gate_id=gate_id,
        type=type,
        description=description,
        content=content,
        task_reference=task_reference,
        context=context,
        options=options,
        recommendation=recommendation,
        blocking=blocking,
        resolution=resolution,
        resolved_by=resolved_by,
        rationale=rationale,
        create_decision=create_decision,
        gate_type=gate_type,
        deliverables=deliverables,
        pass_criteria=pass_criteria,
        assumptions_to_verify=assumptions_to_verify,
        verdict=verdict,
        notes=notes,
        assumption_status=assumption_status,
        confirmation_brief=confirmation_brief,
        pi_preference=pi_preference,
        selected_option_id=selected_option_id,
        override_rationale=override_rationale,
        outcome=outcome,
        outcome_details=outcome_details,
        lessons=lessons,
        recorded_by=recorded_by,
    )


@tool(tier=_TIER_ALWAYS_ON, category="verb")
async def rka_review(
    target: Annotated[
        ReviewTargetLit,
        Field(description="Review / update / maintenance target."),
    ],
    *,
    project_id: str,
    payload: dict | None = None,
) -> str:
    """[BRAIN] Review + update + maintenance verb. Targets cover:

    Entity updates: note_update, decision_update, literature_update,
        status_update.
    Bulk: bulk_update, batch_import.
    Hooks: hook_add, hook_enable, hook_disable, hook_delete.
    Notifications: brain_notifications_clear.
    Claims/Clusters: extract_claims, cluster, claims, cluster_create,
        cluster_assign, cluster_split, cluster_merge.
    Maintenance: contradiction, flag_stale, eviction_sweep.
    Workspace: bootstrap_workspace.
    Manuscript: manuscript_register.
    Decision lifecycle: supersede_decision (also addressable via
        rka_record_decision with supersedes_decision_id).

    Pass per-target args as `payload={...}`. Each target validates
    required payload keys and returns a structured error if missing.

    Args:
        target: PRIMARY discriminator. See list above.
        project_id: REQUIRED.
        payload: Per-target arg bag. Schema varies by target;
            consult the legacy tool of the same name for the shape
            (e.g. target='cluster' -> rka_review_cluster's signature).
    """
    return await _dispatch_review(
        target, project_id=project_id, payload=payload
    )


# ============================================================
# v2.7.0a3 — rka_describe (schema-lookup verb)
# ============================================================
# Companion to rka_query (reads) and rka_execute (writes). The LLM is
# expected to call rka_describe BEFORE invoking rka_query or rka_execute
# on an unfamiliar operation so it knows the exact parameter shape,
# enum values, required/optional fields, and any provenance
# requirements. Schema sourced from rka/mcp/operations_schema.py.

from rka.mcp.operations_schema import (
    dispatch_describe as _dispatch_describe,
    OPERATIONS_SCHEMA as _OPERATIONS_SCHEMA,
)


@tool(tier=_TIER_ALWAYS_ON, category="dispatch", always_load=True)
async def rka_describe(operation: str = "", include_preview: bool = False) -> str:
    """[ANY] LOOKUP the parameter shape and examples for any rka operation.

    Call BEFORE invoking rka_query or rka_execute on an unfamiliar
    operation. Returns: required fields, optional fields, enum values,
    examples, related operations.

    Trigger phrases: "how do I", "what arguments", "what fields",
    "schema for", "examples of", "describe operation", "what does X
    require", "what's the shape of", "tell me about the operation".

    Modes:
      - operation='<name>'   -> full schema (signature, required/optional
                                fields, enum value sets, 1-2 examples,
                                related_operations, role_tag, notes, maturity,
                                and explicit deprecation guidance when present).
      - operation=''         -> operations index grouped by tool
                                (rka_query / rka_execute); use this when you
                                want to BROWSE. Preview and deprecated
                                operations are omitted by default and reported
                                separately. Pass
                                include_preview=True to see the whole surface.
      - unknown operation    -> {error: 'unknown_operation', did_you_mean: [...]}.

    Args:
        operation: Canonical operation name (the value you would put in
            rka_query(args={"operation": ...}) or
            rka_execute(args={"operation": ...})).
            Pass '' to browse the index.
        include_preview: include preview and deprecated compatibility
            operations. Default False keeps the browse index to the default
            usage-tested surface.

    Returns:
        JSON. For a known operation: {operation, tool, category,
        summary, signature, required_fields, optional_fields, enums,
        examples, related_operations, role_tag, notes, maturity, deprecated,
        deprecation?}. For an unknown name: {error, operation,
        did_you_mean, hint}. For empty: the operations index.
    """
    return await _dispatch_describe(operation, include_preview=include_preview)


# ============================================================
# ChatGPT connector skill adapters
# ============================================================
# Some MCP clients (Claude Desktop / Claude Code) expose MCP prompts directly,
# so they can call brain_skill / executor_skill / pi_skill from the prompt
# picker. ChatGPT custom connectors are primarily tool-oriented, so these
# deferred tools expose the same packaged skill files through normal tool calls
# without changing the default five-tool dispatch surface.


@tool(tier=_SKILL_TOOL_TIER, category="skills")
async def rka_list_skills() -> str:
    """List packaged RKA skill guides available through the MCP connector.

    Returns:
        JSON array with name, description, version, role, prompt_name, and
        recommended_for fields. Use rka_read_skill(name=...) to load one.
    """
    skills: list[dict[str, str]] = []
    for name, entry in _SKILL_ENTRYPOINTS.items():
        try:
            text = _safe_skill_path(name).read_text(encoding="utf-8")
        except OSError:
            meta = {}
        else:
            meta = _parse_skill_frontmatter(text)
        skills.append(
            {
                "name": name,
                "skill_name": meta.get("name", name),
                "version": meta.get("version", ""),
                "description": meta.get("description", ""),
                "role": entry["role"],
                "recommended_for": entry["recommended_for"],
                "prompt_name": entry["prompt"],
            }
        )
    return json.dumps({"skills": skills}, indent=2)


@tool(tier=_SKILL_TOOL_TIER, category="skills")
async def rka_read_skill(
    name: SkillNameLiteral,
    reference: str | None = None,
) -> str:
    """Read a packaged RKA skill guide or one of its referenced files.

    This is the tool-oriented equivalent of the MCP prompt entries
    `brain_skill`, `executor_skill`, and `pi_skill`, intended for clients
    such as ChatGPT connectors that do not expose MCP prompts prominently.

    Args:
        name: Skill package to read: brain, executor, pi, or mcp-credentials.
        reference: Optional path inside that skill directory, such as
            `workflows.md` for brain/executor. Omit to read SKILL.md.

    Returns:
        Markdown text for the requested skill file.
    """
    try:
        path = _safe_skill_path(name, reference)
        text = path.read_text(encoding="utf-8")
    except (OSError, ValueError) as exc:
        return json.dumps(
            {
                "error": "skill_file_unavailable",
                "name": name,
                "reference": reference,
                "detail": str(exc),
                "hint": "Call rka_list_skills() and then request a listed skill or a relative file referenced by that skill.",
            },
            indent=2,
        )
    rel = path.relative_to(_SKILLS_DIR.resolve())
    banner_role = name if reference is None else f"{name}:{reference}"
    return _live_prompt_banner(banner_role) + f"# Source: rka/skills/{rel}\n\n{text}"


@tool(tier=_SKILL_TOOL_TIER, category="skills")
async def rka_start_session(
    role: SkillNameLiteral = "pi",
    project_id: str | None = None,
) -> str:
    """Load RKA role guidance plus a concrete session-start checklist.

    Use this at the start of a ChatGPT connector conversation. It returns the
    chosen role skill and a short set of first calls to make through
    rka_query/rka_execute. It does not mutate RKA state.

    Args:
        role: RKA role guide to load. Use `pi` for ChatGPT-as-PI cockpit,
            `brain` for strategic analysis, or `executor` for implementation.
        project_id: Optional project id to thread into the suggested calls.

    Returns:
        Markdown: role guidance followed by a session-start checklist.
    """
    skill_text = await rka_read_skill(role)
    project_hint = project_id or "prj_..."
    checklist = f"""

---

# ChatGPT Connector Session Checklist

1. If you do not know the project id, call:
   `rka_query(args={{"operation": "list_projects"}})`
2. Pin the project id in the conversation. For this session use:
   `{project_hint}`
3. Load current context:
   `rka_query(args={{"operation": "context", "project_id": "{project_hint}"}})`
4. Check open blockers:
   `rka_query(args={{"operation": "checkpoints", "project_id": "{project_hint}", "filters": {{"status": "open"}}}})`
5. Check maintenance:
   `rka_query(args={{"operation": "pending_maintenance", "project_id": "{project_hint}"}})`

For unfamiliar operations, call `rka_describe(operation="<operation_name>")`
before using `rka_query` or `rka_execute`.
"""
    return skill_text + checklist


# ============================================================
# v2.7.0a3 — rka_execute (the unified WRITE/lifecycle verb)
# ============================================================
# Companion to rka_query (reads) and rka_describe (schema lookup).
#
# This verb collapses the entire write/lifecycle surface (69
# operations) behind one Annotated[Literal] discriminator so the LLM
# sees the full enum in its inputSchema. Dispatch lives in
# rka/mcp/verb_dispatch.py::dispatch_execute, which routes to the
# existing v2.7.0a2 sub-dispatchers (record_note, record_decision,
# mission, checkpoint, review, record_literature, session) without
# duplicating REST-call code. Service layer + REST + DB schema are
# UNCHANGED.
#
# Per Decision 2 (hybrid kwarg shape):
#   - operation: Annotated[Literal[...]] discriminator at the top
#     of the signature so FastMCP renders the full enum into the
#     LLM's tool definition (pre-emission catch).
#   - project_id: top-level kwarg-only, REQUIRED for all
#     project-scoped ops; UNSCOPED for create_project + reset_session.
#   - source / confidence / importance / verbatim_input / provenance /
#     tags / phase: named common kwargs at top level so the
#     Annotated[Literal] enum promotion survives for the load-bearing
#     ones (source, confidence, importance) and the orchestrator's
#     X-RKA-Project header threading + Phase-X²' validation chain
#     continue working unchanged.
#   - All operation-specific extras flow through **kw and are
#     validated by the existing rka_enums.py infrastructure on the
#     orchestrator side + the per-op `_err("missing_field", ...)`
#     guards on the dispatcher side.

# Build the Execute operation Literal from the canonical tuple in
# verb_dispatch. This keeps a single source of truth.
ExecuteOpLit = _Literal[tuple(_EXECUTE_OPERATIONS)]  # type: ignore[valid-type]


@tool(tier=_TIER_ALWAYS_ON, category="dispatch", always_load=True)
async def rka_execute(
    args: _ExecuteArgsUnion,
    ctx: _MCPContext = None,  # type: ignore[assignment]
) -> str:
    """[BRAIN/EXECUTOR/PI] WRITE / lifecycle operations on RKA. ALL writes flow through this verb.

    v2.7.0 NO-COMPROMISE typed-arg surface. The ``args`` parameter is a
    Pydantic discriminated union (keyed by ``operation``) covering all
    83 write/lifecycle operations. FastMCP renders the union as JSON
    Schema ``oneOf`` with per-branch ``required`` arrays + per-branch
    ``enum`` constraints on every Literal-typed field. The LLM CANNOT
    emit ``confidence='confirmed'``, ``decided_by='SUPERVISOR'``,
    ``kind='research_question'`` as a free-form decision, or any other
    out-of-set enum value — the tool surface itself rejects pre-dispatch
    (closes v2.7.0 pre-mortem compromise #1).

    Cross-cutting validators (closes pre-mortem compromise #4 at the
    per-operation level): ``RecordDecisionArgs`` enforces
    ``related_journal`` non-empty; ``CreateMissionArgs`` enforces
    ``motivated_by_decision``; ``RecordNoteArgs`` enforces
    ``verbatim_input`` when ``source='pi'``; alias-collision rules on
    ``SubmitCheckpointArgs`` (description/content) and
    ``SubmitReportArgs`` (summary/content) raise pre-dispatch.

    Trigger phrases: "record a note", "log a finding", "save the
    decision", "ratify the choice", "create a mission", "submit a
    checkpoint", "raise a checkpoint", "resolve checkpoint", "create a
    gate", "supersede dec_...", "import bibtex", "enrich DOI",
    "extract claims", "create cluster", "merge clusters",
    "resolve contradiction", "flag stale", "evict knowledge",
    "scan workspace", "bootstrap workspace", "create project",
    "reset session", "update mission status",
    "submit report", "advance RQ", "record PI selection",
    "record outcome", "present decision", "add hook", "enable hook",
    "clear notifications".

    Frozen Writer/Workbench compatibility branches remain callable but are not
    part of Core's default operation index. Use ``rka_describe('<operation>')``
    for their migration notice; use the standalone RKA Writer project for new
    authoring development.

    Args:
        args: One of the typed ``*Args`` models from
              ``rka.mcp.operation_args`` (e.g. ``RecordDecisionArgs``,
              ``CreateMissionArgs``, ``SubmitCheckpointArgs``,
              ``UpdateNoteArgs``, ...). The model's ``operation`` field
              is the discriminator. Project-scoped operations require
              ``project_id``; the only unscoped writes are
              ``create_project`` and ``reset_session``.

    Returns:
        JSON-string response from the underlying REST endpoint, or a
        structured error JSON.

    NOTE: discoverability — call ``rka_describe('')`` for the
    operation-name index (now <250 tokens per Phase 3 mitigation
    of v2.7.0 compromise #3) or ``rka_describe('<op_name>')`` for
    full per-operation schema + examples.
    """
    await _warn_if_deprecated_operation(args, ctx)
    try:
        return await _dispatch_execute_typed(args)
    except httpx.TimeoutException as exc:
        return _timeout_error(exc)


# Legacy untyped rka_execute body preserved as a fallback callable for
# ``RKA_LEGACY_TOOLS=1`` opt-in.
async def _rka_execute_legacy_impl(
    operation: Annotated[
        ExecuteOpLit,
        Field(
            description=(
                "Write/lifecycle operation discriminator (legacy surface)."
            )
        ),
    ],
    *,
    project_id: str | None = None,
    source: SourceLiteral | None = None,
    confidence: ConfidenceLiteral | float | None = None,
    importance: ImportanceLiteral | None = None,
    verbatim_input: str | None = _OMITTED_VERBATIM,  # type: ignore[assignment]
    provenance: dict | None = None,
    tags: list[str] | None = None,
    phase: str | None = None,
    **kw,
) -> str:
    """Legacy untyped rka_execute body — preserved for RKA_LEGACY_TOOLS=1
    callers and a small number of unit tests that probe the **kw
    signature shape directly. The canonical v2.7.0 surface is the typed
    ``rka_execute(args: ExecuteArgsUnion)`` above.

    Omitted common fields stay omitted until dispatch chooses the operation's
    defaults; injecting creation defaults here would overwrite existing notes.
    Preserve absent versus explicit-null original text for attribution corrections.
    """
    return await _dispatch_execute(
        operation,
        project_id=project_id,
        source=source,
        confidence=confidence,
        importance=importance,
        verbatim_input=verbatim_input,
        provenance=provenance,
        tags=tags,
        phase=phase,
        **kw,
    )


# ============================================================
# MCP Prompts — skill files and orientation guides
# ============================================================


@mcp.prompt()
def brain_skill() -> str:
    """Full Brain workflow guide — strategy, decisions, provenance, claim extraction, research map, gates."""
    return _live_prompt_banner("brain") + _read_skill("brain/SKILL.md")


@mcp.prompt()
def executor_skill() -> str:
    """Full Executor workflow guide — missions, backbrief, recording, escalation, reports."""
    return _live_prompt_banner("executor") + _read_skill("executor/SKILL.md")


@mcp.prompt()
def pi_skill() -> str:
    """PI quick reference — project status, research map, checkpoint resolution."""
    return _live_prompt_banner("pi") + _read_skill("pi/SKILL.md")


@mcp.prompt()
def brain_orientation() -> str:
    """Orientation guide for the Brain (Claude Desktop) — strategic AI role in RKA workflow."""
    return _live_prompt_banner("brain") + """\
# Brain Orientation — Research Knowledge Agent (RKA)

You are the **Brain**: the strategic AI layer in an RKA-powered research project.
Your counterpart is the **Executor** (Claude Code), which handles implementation.
The **PI** (human researcher) supervises both of you.

For full workflow guidance, load the `rka-brain` Agent Skill (Claude Desktop Skills
feature). The Skill lives at `rka/skills/brain/` with a top-level SKILL.md plus
architecture.md, workflows.md, decision_ux.md, and examples.md for progressive
disclosure. This orientation is the short fallback for clients without Skills
support; the Skill is authoritative.

---

## Your Role

- Think strategically: interpret findings, decide research direction, manage literature
- Do NOT implement code, run experiments, or edit files directly — delegate to Executor
- Record all significant decisions in RKA so the knowledge base is always current
- Keep the PI informed; escalate blockers as checkpoints

---

## Session Start Protocol

Always begin a session by loading context:

0. **Tool surface (v2.7.0+ typed dispatch).** RKA ships 3 always-on dispatch
   tools — `rka_query` (reads), `rka_execute` (writes/lifecycle), and
   `rka_describe` (schema lookup) — plus 2 escape hatches (`rka_load_tools`
   for legacy compat and `rka_help` as a deprecated alias of `rka_describe`).
   No activation step is required: every operation is reachable from
   `rka_query` / `rka_execute` on the first call. To browse the live operation
   index and counts, call `rka_describe("")`; for one operation's full schema call
   `rka_describe("record_decision")`. Per-branch enum + required-field
   enforcement at the FastMCP schema layer guarantees values like
   `confidence` are rejected before the call is sent.
1. **Pin the project at the start of every conversation.** v2.6+: every project-scoped operation requires `project_id` as a kwarg on every call — there is no "active project" session state. Call `rka_query(args={"operation": "list_projects"})` to discover available project_ids, ask the PI which project this conversation is about (or recall it from the conversation pin), and pass `project_id="prj_…"` to every subsequent `rka_query` / `rka_execute` call. If you omit `project_id`, the tool errors immediately with a clear message — that's by design (it replaces the pre-v2.6 silent-fallback-to-`proj_default` failure mode). Keep the project_id in your working memory; do not assume default fallback. `rka_set_project` is a deprecated no-op since v2.6.
2. `rka_query(args={"operation": "context", "project_id": "prj_..."})` — full project state (phase, open missions, recent notes, decisions)
3. `rka_query(args={"operation": "pending_maintenance", "project_id": "prj_..."})` — check for provenance gaps
4. `rka_query(args={"operation": "checkpoints", "project_id": "prj_...", "filters": {"status": "open"}})` — check for unresolved Executor blockers

**Maintenance Protocol**: If maintenance items exist, process the highest-priority
items that fit the session before greeting the user. Priority:
decisions_without_justified_by > missions_without_motivated_by >
unassigned_clusters > entries_missing_cross_refs. For a read-only query, audit,
or evaluation, inspect maintenance but do not execute fix-up writes.

If there are open checkpoints, resolve them before continuing new work.

---

## Core Workflow

All operations dispatch through `rka_query` (reads) or `rka_execute`
(writes/lifecycle). Examples below use the
`args={"operation": "...", "project_id": "...", ...}` shape — required
fields and enums are enforced at the inputSchema layer.

### Directing the Executor
- `rka_execute(args={"operation": "create_mission", "project_id": "prj_...", "phase": "...", "objective": "...", "tasks": [...], "context": "...", "acceptance_criteria": [...], "motivated_by_decision": "dec_01..."})` — assign work; returns the full mission ID. `motivated_by_decision` is required for provenance.
- `rka_query(args={"operation": "mission", "project_id": "prj_...", "id": "mis_..."})` — check progress
- `rka_execute(args={"operation": "resolve_checkpoint", "project_id": "prj_...", "id": "chk_...", "resolution": "..."})` — unblock the Executor

### Recording Knowledge
- `rka_execute(args={"operation": "record_note", "project_id": "prj_...", "content": "...", "type": "note", "source": "brain"})` — observations, analyses, insights
- `rka_execute(args={"operation": "record_note", "project_id": "prj_...", "content": "...", "type": "directive"})` — instructions to the Executor
- `rka_execute(args={"operation": "record_decision", "project_id": "prj_...", "question": "...", "phase": "...", "decided_by": "brain", "options": [...], "chosen": "...", "rationale": "...", "related_journal": ["jrn_..."]})` — record all non-trivial choices
- `rka_execute(args={"operation": "add_literature", ...})` or `rka_execute(args={"operation": "enrich_doi", "doi": "..."})` — add papers; use `rka_query(args={"operation": "search_semantic_scholar" | "search_arxiv", ...})` to find related work

### PI Input Attribution (Critical)
When recording PI input, ALWAYS set `source="pi"` and `verbatim_input` to the PI's exact words.
Your analysis goes in `content`. This preserves intellectual attribution.
Example: `rka_execute(args={"operation": "record_note", "project_id": "prj_...", "content": "PI suggests focusing on...", "source": "pi", "verbatim_input": "Let's try the transformer approach instead"})`

### Reviewing Progress
- `rka_query(args={"operation": "journal", "project_id": "prj_...", "limit": 20})` — recent notes from all actors
- `rka_query(args={"operation": "decision_tree", "project_id": "prj_..."})` — all decisions and their rationale
- `rka_query(args={"operation": "literature", "project_id": "prj_...", "status": "to_read"})` — papers waiting for review
- `rka_query(args={"operation": "report", "project_id": "prj_...", "mission_id": "mis_..."})` — read Executor's completion report

### Updating Status
- `rka_execute(args={"operation": "update_status", "project_id": "prj_...", "phase": "...", "current_focus": "...", "next_steps": [...], "blockers": [...]})` — keep the dashboard current
- `rka_query(args={"operation": "summarize", "project_id": "prj_...", "scope": "project"})` — generate a full project summary

### Session Management
- `rka_query(args={"operation": "session_digest"})` — compressed summary of the session so far
- `rka_execute(args={"operation": "reset_session"})` — clear the session tracker

---

## Provenance Enforcement (Critical)

You are responsible for maintaining provenance discipline. ALWAYS:
- `rka_execute(args={"operation": "record_decision", ..., "related_journal": ["jrn_01...", "jrn_02..."]})` — what findings justify this
- `rka_execute(args={"operation": "create_mission", ..., "motivated_by_decision": "dec_01..."})` — what decision triggers this work
- `rka_execute(args={"operation": "record_note", ..., "provenance": {"related_decisions": ["dec_01..."], "related_mission": "mis_01..."}})` — link context
- `rka_query(args={"operation": "provenance", ..., "id": "...", "filters": {"direction": "backward"}})` — understand why something exists

Orphaned entities (no links) degrade the knowledge graph. Fix them during maintenance.

## v2.0 Research Map Workflow

- `rka_query(args={"operation": "research_map", "project_id": "prj_..."})` — see the three-level view (RQs → clusters → claims)
- When creating decisions, set `kind="research_question"` for questions that organize research
- Assign orphaned evidence clusters to research questions via `rka_execute(args={"operation": "review_cluster", ..., "research_question_id": "dec_..."})`

## Review Queue (Brain-Only)

At session start, after loading context:
5. `rka_query(args={"operation": "review_queue", "project_id": "prj_..."})` — items flagged for your attention

Process high-priority items before starting new work:
- `rka_execute(args={"operation": "review_cluster", "cluster_id": "...", "confidence": "...", "synthesis": "..."})` — write definitive synthesis
- `rka_execute(args={"operation": "review_claims", "claim_ids": [...], "action": "..."})` — approve, reject, or adjust claims
- `rka_execute(args={"operation": "resolve_contradiction", "cluster_id": "...", "resolution": "..."})` — resolve flagged conflicts

Your reviewed syntheses are marked `synthesized_by: brain`. They are current
working interpretations, not empirical proof, and they never erase staged,
rejected, conflicting, or superseded history.

## Decision Lifecycle

- To overturn a past decision: `rka_execute(args={"operation": "supersede_decision", "old_decision_id": "dec_...", "question": "...", "chosen": "...", "rationale": "..."})`
- This automatically triggers re-distillation of affected knowledge
- Raw journal entries are never changed — only the interpretive layer rebuilds

---

## Session End Protocol

Before closing a conversation:
1. Add any insights or decisions from this session
2. `rka_execute(args={"operation": "submit_checkpoint", "project_id": "prj_...", "title": "...", "description": "...", "context": "..."})` if you need PI input before next session
3. `rka_execute(args={"operation": "update_status", ...})` with updated next_steps

---

## Key Principles

- **One decision at a time**: record decisions as you make them, not in bulk at the end
- **Tag consistently**: use the project's established tags (check `rka_get_context` for existing tags)
- **Confidence levels**: use `hypothesis` → `tested` → `verified` as evidence accumulates
- **Importance**: mark only genuinely critical items as `critical`; keep `high` for important-but-not-urgent
"""


@mcp.prompt()
def executor_orientation() -> str:
    """Orientation guide for the Executor (Claude Code) — implementation AI role in RKA workflow."""
    return _live_prompt_banner("executor") + """\
# Executor Orientation — Research Knowledge Agent (RKA)

You are the **Executor**: the implementation AI in an RKA-powered research project.
Your counterpart is the **Brain** (Claude Desktop), which sets strategy.
The **PI** (human researcher) supervises both.

For full workflow guidance, load the `rka-executor` Agent Skill (Claude Code
Skills feature). The Skill lives at `rka/skills/executor/` with a top-level
SKILL.md plus workflows.md and examples.md for progressive disclosure. This
orientation is the short fallback for clients without Skills support; the
Skill is authoritative.

---

## Your Role

- Implement what the Brain assigns: write code, run experiments, process data, collect results
- Record methodology and findings in RKA as you work — don't batch up at the end
- Raise checkpoints immediately when you hit a decision that requires Brain/PI input
- Do NOT make strategic research decisions unilaterally

---

## Session Start Protocol

0. **Tool surface (v2.7.0+ typed dispatch).** RKA ships 3 always-on dispatch
   tools — `rka_query` (reads), `rka_execute` (writes/lifecycle), and
   `rka_describe` (schema lookup) — plus 2 escape hatches (`rka_load_tools`
   for legacy compat and `rka_help` as a deprecated alias of `rka_describe`).
   No activation step is required: every operation is reachable from
   `rka_query` / `rka_execute` on the first call. Call `rka_describe("")` for
   the operations index; `rka_describe("<op_name>")` for full per-operation
   schema. Per-branch enum + required-field enforcement at the FastMCP schema
   layer rejects illegal values before the call is sent. If you're running
   inside the orchestrator daemon's SDK subprocess, `RKA_LEGACY_TOOLS=1` is
   set and the v2.7.0a2 per-tool surface is also available (preserves
   parent-side TWO-TAP gate granularity at `pi_decision_select`).
1. **Pin the project at the start of every conversation.** v2.6+: every project-scoped operation requires `project_id` as a kwarg on every call — there is no "active project" session state in the MCP server. Call `rka_query(args={"operation": "list_projects"})` to discover available project_ids, confirm which project the PI is asking you to work on, and pass `project_id="prj_…"` to every subsequent `rka_query` / `rka_execute` call. Omitting `project_id` fails fast with a clear error — this replaces the pre-v2.6 silent-fallback-to-`proj_default` failure mode. The discipline: keep the project_id in your working memory for the whole conversation; pass it on every call. `rka_set_project` is a deprecated no-op since v2.6.
2. `rka_query(args={"operation": "context", "project_id": "prj_..."})` — load current project state
3. `rka_query(args={"operation": "mission", "project_id": "prj_..."})` — finds the active or most recent pending mission automatically
4. If a pending mission is found, call `rka_execute(args={"operation": "update_mission_status", "project_id": "prj_...", "mission_id": "mis_...", "status": "active"})` to claim it
5. Read the mission's `objective`, `tasks`, and **context** carefully before starting
6. If the mission has `motivated_by_decision`, read that decision with `rka_query(args={"operation": "entity", "project_id": "prj_...", "id": "dec_..."})` for full context

If no mission exists, ask the Brain or PI for direction before starting.

**Mission status lifecycle**: `pending` (Brain created, not started) → `active` (you are working on it) → `complete` (done via `rka_execute(args={"operation": "submit_report", ...})`). Always activate a pending mission before starting work.

### Session Management
- `rka_query(args={"operation": "session_digest"})` — compressed summary of the session so far
- `rka_execute(args={"operation": "reset_session"})` — clear the session tracker

---

## Core Workflow

All operations dispatch through `rka_query` (reads) or `rka_execute`
(writes/lifecycle). Required fields and enums are enforced at the
inputSchema layer.

### During Implementation
- `rka_execute(args={"operation": "record_note", "project_id": "prj_...", "content": "...", "type": "note", "source": "executor", "provenance": {"related_mission": "mis_..."}})` — record results, observations, analyses
- `rka_execute(args={"operation": "record_note", "project_id": "prj_...", "content": "...", "type": "log", "source": "executor", "provenance": {"related_mission": "mis_..."}})` — document procedure steps
- `rka_execute(args={"operation": "ingest_document", "project_id": "prj_...", "path": "..."})` — import new files (PDFs, scripts, data files) into the knowledge base

### When Blocked
- `rka_execute(args={"operation": "submit_checkpoint", "project_id": "prj_...", "mission_id": "mis_...", "type": "clarification", "description": "...", "context": "...", "blocking": True})` — IMMEDIATELY when you need Brain/PI input
- Do not continue past a blocking decision; wait for the Brain to resolve the checkpoint

### On Completion
- `rka_execute(args={"operation": "submit_report", "project_id": "prj_...", "mission_id": "mis_...", "summary": "...", "findings": ["..."], "anomalies": ["..."], "questions": ["..."], "codebase_state": "...", "recommended_next": "..."})` — required at mission end
- `summary`: full narrative report. `findings`/`anomalies`/`questions`: lists of strings. `codebase_state`/`recommended_next`: plain strings.
- Include concrete findings, not just "task completed"

### Literature (when relevant)
- `rka_execute(args={"operation": "record_literature", "project_id": "prj_...", "title": "..."})` or `rka_execute(args={"operation": "enrich_doi", "project_id": "prj_...", "doi": "..."})` — if you encounter a paper worth tracking
- `rka_query(args={"operation": "search_semantic_scholar", "project_id": "prj_...", "query": "..."})` — background literature search

---

## v2.0 Recording Standards

### Entry Types (simplified)

| Type | Use when | Example |
|------|---------|---------|
| `note` | You observed, analyzed, or discovered something | Results, insights, observations |
| `log` | You did a procedure step | "Ran stress test", "Deployed config" |
| `directive` | You received or are recording instructions | PI instructions, Brain directions |

Old types (finding, insight, methodology, etc.) are accepted but mapped to these three.

### Cross-References — ALWAYS Link Your Work

Every note and report MUST be linked to its context:
- `rka_execute(args={"operation": "record_note", ..., "provenance": {"related_mission": "mis_01..."}})` — link to the active mission (MANDATORY)
- `rka_execute(args={"operation": "record_note", ..., "provenance": {"related_mission": "mis_01...", "related_decisions": ["dec_01..."]}})` — link findings to relevant decisions when applicable
- `submit_report` is linked to its mission by `mission_id`; record a separate provenance-linked note when a report finding bears on a decision.

Orphaned entries (no related_mission, no related_decisions, no entity_links) are flagged
by `rka_query(args={"operation": "pending_maintenance", ...})` and create work for the Brain. Prevent this by linking as you go.

### Research Map Awareness

- `rka_query(args={"operation": "research_map", "project_id": "prj_..."})` — see where your work fits in the big picture
- After completing a mission, check if your findings affect any research questions
- If they do, note which decisions your results justify or contradict

### Provenance

- `rka_query(args={"operation": "provenance", "project_id": "prj_...", "id": "..."})` — trace the reasoning chain behind any entity
- Use this when you need to understand why a decision was made before implementing

---

## Key Principles

- **Record as you go**: a finding not recorded is a finding lost
- **Confidence is honest**: use `hypothesis` until you've verified; don't overstate
- **Checkpoints are not failures**: raising a checkpoint when genuinely blocked is correct behavior
- **Stay in scope**: if you discover something that changes the research direction, record it and checkpoint — don't pivot unilaterally
"""
