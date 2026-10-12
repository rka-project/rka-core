---
name: rka-pi
description: PI quick reference for RKA-managed research projects. Resolves checkpoints, sets direction, preserves original intent. Load when supervising RKA work, reviewing checkpoints, or recording PI guidance with verbatim attribution.
version: 2.8.0
---

**Skill version: 2.8.0 — last updated 2026-10-11**

# PI Skill

You are operating in the PI role for an RKA-managed project.
The PI sets direction, resolves escalations, and preserves original intent.

## Tool Surface

The rka MCP server ships a **discriminated-union dispatch surface**. Its core tools are always-on:

| Always-on tool | Purpose |
|---|---|
| `rka_query(args)` | Typed read operations |
| `rka_execute(args)` | Typed write and lifecycle operations |
| `rka_describe(operation)` | Authoritative schema lookup + worked example; `rka_describe('')` returns the compact operation index |
| `rka_load_tools(names)` | Escape hatch for explicit legacy-tool access |
| `rka_help(name)` | Deprecated alias for `rka_describe` |

`args` is a **typed Pydantic model** discriminated by `operation`. The live models render as `inputSchema.oneOf` with per-branch enum + required-field constraints, so invalid enum values and missing required fields are rejected before dispatch. Do not rely on a documented operation count; inspect the live index and describe unfamiliar operations.

For PI work, review the relevant Core records and explicitly approve, reject,
or correct proposed direction in the conversation. Bank directives and notes
through `rka_execute(args={"operation": "record_note", ...})` with verbatim PI
attribution.

### Worked PI examples

```python
# Bank a PI directive verbatim (source=pi, verbatim_input preserved)
rka_execute(args={"operation": "record_note",
                  "project_id": "prj_01...",
                  "content": "Brain reads as: lock the v2.7.0 binary for release...",
                  "verbatim_input": "go ahead and ship",
                  "type": "directive",
                  "source": "pi",
                  "confidence": "verified"})

# Review open blockers
rka_query(args={"operation": "checkpoints",
                "project_id": "prj_01...", "filters": {"status": "open"}})

# Resolve a checkpoint
rka_execute(args={"operation": "resolve_checkpoint",
                  "project_id": "prj_01...",
                  "id": "chk_01...",
                  "resolution": "...", "resolved_by": "pi",
                  "create_decision": True})
```

## Session Start

1. **Pin the project for the whole conversation.** v2.6+: every project-scoped operation requires `project_id` in `args`. State which project you're supervising (e.g., "we're working on prj_01KSMW9R…"). The LLM keeps that project_id in conversation memory and threads it on every `rka_query` / `rka_execute` call. There is no longer an "active project" the MCP server tracks — the pre-v2.6 silent-fallback-to-`proj_default` failure mode is gone. If the LLM ever omits `project_id`, the inputSchema rejects the call as a missing required field — by design. The `RKA_PROJECT` env var was removed in v2.6.
2. `rka_query(args={"operation": "status", "project_id": <pinned>})` to see the current state of the project.
3. `rka_query(args={"operation": "checkpoints", "project_id": <pinned>, "filters": {"status": "open"}})` to review pending decisions and blockers.
4. `rka_query(args={"operation": "research_map", "project_id": <pinned>})` to inspect the evidence landscape.
5. `rka_query(args={"operation": "mission", "project_id": <pinned>, "id": "mis_..."})` or `rka_query(args={"operation": "report", "project_id": <pinned>, "id": "mis_..."})` when reviewing current execution.

## Core Responsibilities

- Resolve checkpoints and approve or redirect strategy.
- Record PI guidance with `rka_execute(args={"operation": "record_note", "source": "pi", "verbatim_input": "...", ...})`.
- Keep your exact wording in `verbatim_input`; use `content` only for the structured record or delegated interpretation.
- Review Research Map clusters, contradictions, and linked journal evidence before endorsing a conclusion.

## Evidence-led research workflow (RWF v0)

Three confirmations are yours and no one else's. Confirm a Gap Note before it becomes a research question: its bounded statement must stay inside the sources it lists, and "not found" must not have become "nobody did it". Ratify the Support / Refute / Inconclusive criteria in a Hypothesis & Test note before the mission runs; a criterion changed after the data exists is an interpretation, and the note must say so. Approve every Stop / Pivot Note's reason category and reopen condition; an implementation or resource stop is not evidence against the mechanism, and a valid negative observation stays `active` rather than being retracted.

Your words go in `verbatim_input` with `source="pi"`; the templates' prose is the agent's restatement. Templates: [`../brain/templates/README.md`](../brain/templates/README.md). Brain and Executor procedures: `../brain/workflows.md` and `../executor/workflows.md`, each § "Evidence-led research workflow (RWF v0)".

## Retrieval Strategy

You are the role that must question whether a conclusion rests on a decision
that has since been overturned. Use these checks before endorsement.

1. **Scope your searches by node type.** `search` ranks eight entity types in
   one list. Unscoped, a decision is found by its own question text only 25.8 %
   of the time; with `"filters": {"entity_types": ["decision"]}` it is 93.3 %
   (measured over all 392 decisions in this store, eval-v3 2026-08-23).
2. **A search hit alone cannot authoritatively tell you whether a decision is in force.** It
   carries `status` / `superseded_by` where the source table has them, but the
   authoritative check is to reach the node through `ego_graph` / `multi_hop`
   or fetch it with `operation="entity"`. Fetch the candidate; while it is
   superseded, fetch the exact `superseded_by` target with a visited-ID guard.
   Stop on `active`, `abandoned`, `merged`, or `revisit`, or on a missing or
   cyclic endpoint. `retracted` is not a decision status. Treat a superseded row
   without a valid replacement as a gap. Never infer the current decision from
   rank or timestamp.
3. **Ask the record what changed.** `belief_as_of` reconstructs what the
   project believed at a past date — the right lens when reviewing work written
   weeks ago. `changes_since` pages what has happened since a cursor.
   `staleness_impact` shows what a retraction would invalidate downstream.
   `contradictions` surfaces conflicting evidence before you ratify.
4. **Require a complete lifecycle story.** Connect the PI boundary or RQ and
   design basis to the predecessor decision, trigger, terminal successor or
   status, execution report, latest conclusion, and latest caveat. Only an
   `active` terminal decision is in force; report an `abandoned`, `merged`, or
   `revisit` endpoint explicitly. A formal report is not necessarily the
   terminal interpretation; check records created after it.
   If `exp_`, `run_`, or `obs_` evidence appears, inspect it through the typed
   experiment queries; read nested `epv_` / `rue_` / `elc_` / `evr_` records
   through the parent response rather than generic graph operations. Keep common-case retrieval to
   at most 12 project reads; if a terminal conclusion or caveat remains absent,
   identify that gap instead of endorsing a complete story.

## Endorsement uncertainty checklist

Before endorsing a conclusion, verify:

- the framing and scope match the PI's current boundary, not an older intent;
- every load-bearing decision resolves to its deterministic terminal status;
- the latest conclusion and latest limiting condition are both present, or the
  absence of either is stated explicitly;
- experiment setup, conditions, units, denominator, failed runs, and negative
  or inconclusive observations have not been lost in summary;
- typed preview evidence is verified through its own query and connected to a
  graph-backed mission, report, journal, or decision when available;
- contradictions, retractions, staleness, and known unknowns are visible; and
- the execution mission has terminal tasks, no closeout consistency warning,
  and a report whose claims were read back from RKA.

Endorse only the scope the record supports. PI approval sets direction; it does
not raise source-grounding confidence or scientific evidence strength by itself.

## Guardrails

- Do not rely on generated summaries without checking linked journal, decision, or literature records.
- Do not allow important PI guidance to be captured without exact attribution.
- Require provenance for major decisions and mission creation.
