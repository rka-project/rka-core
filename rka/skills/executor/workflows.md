# Executor — Workflows

Procedural reference for the Executor skill. Loaded on demand when `SKILL.md` points here. Keeps every-mission discipline at the top-level and the detailed procedures here.

> **v2.7.0 dispatch translation.** The legacy tool names used in this file (`rka_add_note`, `rka_submit_report`, `rka_submit_checkpoint`, `rka_get_mission`, `rka_search`, etc.) are synonyms for `rka_query` / `rka_execute` operations under the v2.7.0+ typed-arg surface. Treat any `rka_X(arg=val, ...)` example as shorthand for the discriminated-union call:
>
> | Legacy shorthand | v2.7.0 dispatch shape |
> |---|---|
> | `rka_get_mission(id="mis_...")` | `rka_query(args={"operation": "mission", "project_id": <pinned>, "id": "mis_..."})` |
> | `rka_get_context(topic="...")` | `rka_query(args={"operation": "context", "project_id": <pinned>, "query": "..."})` |
> | `rka_search(query="...")` | `rka_query(args={"operation": "search", "project_id": <pinned>, "query": "..."})` |
> | `rka_add_note(type=..., source="executor", ...)` | `rka_execute(args={"operation": "record_note", "project_id": <pinned>, ...})` |
> | `rka_submit_checkpoint(type=..., ...)` | `rka_execute(args={"operation": "submit_checkpoint", "project_id": <pinned>, ...})` |
> | `rka_submit_report(mission_id=..., ...)` | `rka_execute(args={"operation": "submit_report", "project_id": <pinned>, ...})` |
> | `rka_update_mission_status(id=..., status=...)` | `rka_execute(args={"operation": "update_mission_status", "project_id": <pinned>, "mission_id": "...", "status": "..."})` |
> | `rka_ingest_document(...)` | `rka_execute(args={"operation": "ingest_document", "project_id": <pinned>, ...})` |
> | `rka_link_literature_to_zotero(lit_id=...)` | `rka_execute(args={"operation": "link_literature_to_zotero", "project_id": <pinned>, "lit_id": "..."})` |
>
> `RKA_LEGACY_TOOLS=1` is retained only for historical callers. Legacy names
> remain deferred and may be loaded explicitly with `rka_load_tools`, but the
> typed dispatch shape is the supported surface. Index of operations:
> `rka_describe(operation="")`.

---

## Backbrief Format — Full Template

Before starting significant implementation work, present a Backbrief to the Brain. Five required elements:

1. **Plan summary** — "I will accomplish this by doing X, Y, Z."
2. **Acceptance criteria interpretation** — "I interpret the acceptance criteria to mean [specific interpretation]."
3. **Assumptions** — numbered list, referencing mission assumptions where applicable.
4. **Risks** — including any that might require scope changes.
5. **Approach** — which files will be modified, how will it be tested.

After presenting, WAIT for Brain approval, correction, or recycle before beginning implementation. Record the Backbrief as a journal note linked to the mission:

```python
rka_add_note(
    content="Executor Backbrief for mission mis_01...: [plan summary, assumptions, risks]",
    type="log",
    source="executor",
    provenance={"related_mission": "mis_01..."},  # record_note nests linkage under provenance
    tags=["backbrief"],
)
```

Worked example: `examples.md` § "Backbrief — Bug B URL fix".

### When NOT to Backbrief

- Trivial tasks (single file edit, simple config change, documentation fix).
- Tasks the Brain explicitly said "just do it" for.
- Follow-up tasks where the approach was already approved in an earlier Backbrief.

---

## Report Submission — Full Section Structure

Use `rka_submit_report` with structured sections:

- **`summary`** — full narrative of what was done, methodology, results. This is the main report body.
- **`findings`** — key findings. Dispatch shape (`rka_execute` operation=`submit_report`): a list of strings, one item per finding. Legacy `rka_submit_report` (`RKA_LEGACY_TOOLS=1` only): a single newline-separated string — the tool splits it one per line.
- **`anomalies`** — unexpected observations or issues. Same shape as `findings` (dispatch: list of strings; legacy: newline-separated string).
- **`questions`** — open questions for the Brain or PI. Same shape as `findings` (dispatch: list of strings; legacy: newline-separated string).
- **`codebase_state`** — state of the codebase after the mission.
- **`recommended_next`** — suggested next steps as a single string.

Write the summary as you'd write a PR description: what changed, what you found, how you verified it. The structured sections are for scanning; the summary is for deep reading.

### Closeout preflight and readback

1. Fetch the mission immediately before closeout. Preserve the complete task
   list, including descriptions and order.
2. Mark finished tasks `complete`. Mark a task `skipped` only when the report
   explains why it was unnecessary or out of scope. If any task remains
   `pending`, `in_progress`, or `blocked`, checkpoint or use a non-complete
   mission state instead of submitting a final report.
3. Persist the reconciled tasks with `update_mission_status`, retaining the
   mission's current non-terminal status, and fetch the mission again to verify
   the stored task states.
4. Submit the structured report.
5. Fetch both `mission` and `report`. Verify status `complete`, terminal tasks,
   no task `consistency_warnings`, and exact persistence of the report's
   load-bearing findings, anomalies, questions, and codebase state.

The report operation closes the mission, but it does not infer task outcomes.
Closeout is therefore a write-then-readback workflow, not a single report call.

Good/bad report contrast: `examples.md` § "Reports — Good vs Bad".

---

## Escalation Triggers — Detailed Examples

### Must Escalate (type="decision")

**Assumption invalidation** — an assumption from the mission turns out to be false:

> "Mission assumes `entity_links` has no FK constraints, but it does — the additive-only migration plan needs revision."

**Scope expansion required** — completing the task properly requires changes outside the stated scope:

> "To fix `DecisionService`, I also need to fix `MissionService` and `NoteService` — expanding scope. Also: the schema column they all use has a different name than the mission assumed."

**Contradictory results** — experiment results contradict the expected outcome or existing knowledge:

> "Migration converted 80 edges but maintenance still reports 29 decisions without `justified_by` — the conversion logic isn't finding all the rows the Brain expected."

### Should Escalate (type="clarification")

**Ambiguous acceptance criteria** — multiple valid interpretations exist and the choice matters:

> "Acceptance says 'export succeeds' but doesn't specify whether empty tables are valid or require skipping. Both interpretations are defensible; the choice changes the schema of the exported archive."

**Missing context** — information referenced in the mission doesn't exist or can't be found:

> "Mission references `dec_01KPE2QWY...` but that ID isn't in the DB. Either it was deleted or the mission context has a typo."

**Unexpected complexity** — task is significantly more complex than the mission anticipated:

> "Mission estimates ~1 day; root-cause investigation shows this is a 3-day refactor because the bug involves the shared HTTP client, not just the PUT wrappers."

### Can Proceed — Document in Report

- Technical choices within scope (which library, which algorithm, which test approach).
- Minor bugs found and fixed along the way.
- Performance optimizations that don't change behavior.
- Code quality improvements in files you're already modifying.

Don't checkpoint for these — log them in the report's `anomalies` section.

---

## Your Counterpart: The Brain

### What the Brain Provides in Missions

- `motivated_by_decision` — the decision that triggered this work. Read it with `rka_get(id="dec_...")` to understand WHY.
- `context` field — follows INTENT / BACKGROUND / CONSTRAINTS / ASSUMPTIONS / VERIFICATION. Contains related journal/decision/literature IDs. Read these before starting.
- `acceptance_criteria` — testable assertions you must verify before submitting a report.
- `scope_boundaries` — what NOT to do. Respect these strictly.
- `checkpoint_triggers` — explicit triggers the Brain wants you to escalate on.

### Gate 1: Plan Validation

For significant missions, the Brain may create a Gate 1 (`plan_validation`) checkpoint that formally records the Backbrief approval. This gate blocks until the Brain evaluates your plan. When you see a gate checkpoint on your mission, present your Backbrief and wait for the Brain to evaluate it with `rka_evaluate_gate`. The gate verdict (`go` / `kill` / `hold` / `recycle`) determines whether you proceed, revise, or stop.

### The Confirmation Brief — Why Your Missions Are Vetted

Before creating your mission, the Brain verified its understanding of the PI's intent through a Confirmation Brief — restating the PI's direction and getting explicit correction. This means:

- The mission's objectives have been validated against the PI's actual intent.
- The numbered assumptions in the mission context have been reviewed by the PI.
- If the mission still seems confusing or contradictory, that's a signal to raise a checkpoint — the Confirmation Brief may have missed something.

Find the Confirmation Brief via `rka_search(query="confirmation-brief", filters={"entity_types": ["journal"]})`.

### When to Raise a Checkpoint vs Proceed

- Strategic ambiguity (which approach?) → checkpoint `type="decision"`.
- Missing information (what did the PI mean?) → checkpoint `type="clarification"`.
- Work needs review before continuing → checkpoint `type="inspection"`.
- Technical choice within scope → proceed, document in report.

---

## Evidence-led research workflow (RWF v0)

Templates and shared conventions: `../brain/templates/README.md` and the three template files beside it. Over MCP without a file system: `rka_load_tools(["rka_read_skill"])`, then `rka_read_skill("brain", "templates/stop-pivot-note.md")`. The Brain-side sequence (Gap Note, contract, decisions) is `../brain/workflows.md` § "Evidence-led research workflow (RWF v0)". This section is what the Executor does.

### Recovering negative history at pickup

Run all of these before the Backbrief. Each covers something the others miss.

```python
rka_query(args={"operation": "mission_guard", "project_id": P, "id": "mis_..."})
rka_query(args={"operation": "search", "project_id": P,
                "query": "rwf stop pivot <topic words>",
                "filters": {"entity_types": ["journal"]}})
rka_query(args={"operation": "search", "project_id": P,
                "query": "<topic words>", "filters": {"entity_types": ["decision"]}})
rka_query(args={"operation": "decision_tree", "project_id": P,
                "filters": {"active_only": False}})
```

- `mission_guard` returns retracted or superseded journal entries and unresolved claim contradictions that overlap the mission text. It does not return abandoned or revisited decisions, and its token overlap misses differently worded notes.
- The journal `search` finds Stop / Pivot Notes by their tags (`rwf-stop-pivot` is matched segment by segment) and by header text. Read each hit in full with `entity`; the listing truncates content.
- From the decision search and the tree, read every node whose `status` is `abandoned`, `revisit` or `superseded` near the topic with `entity`. For `superseded`, follow `superseded_by` until an `active`, `abandoned`, `merged` or `revisit` decision, with a visited-id guard. A `superseded` decision with no replacement is a gap to report, not a reason to pick the newest decision.
- `journal` with `filters.tags` does not filter through Core 3.0.1 (rka-project/rka-core#170 tracks the fix); it returns the unfiltered listing. Use `search` as above, which works on every version.

Put what you found in the Backbrief: which stopped directions overlap the mission, their `stop_reason`, and whether the reopen condition now holds. If the mission would repeat a `counterevidence` stop inside its scope conditions, raise a `decision` checkpoint before starting.

### Executing against a Hypothesis & Test contract

1. Read the contract note named in the mission context. Confirm the mission's `acceptance_criteria` matches its Support / Refute / Inconclusive text; if they differ, raise a `clarification` checkpoint.
2. Record each run or batch as its own `record_note` with `type="note"`, `source="executor"`, `confidence="tested"`, tags `rwf-hypothesis-test`, `rwf-v0`, `observation`, and `provenance.related_mission` plus `related_decisions`. State setup, conditions, units, uncertainty, repeats, failed runs, and which criterion the run met for which hypothesis.
3. Do not change the criteria. If the data makes a different criterion look better, say so in the report under interpretation; the Brain decides whether to write a superseding contract.
4. At closeout the report separates four things: what was observed, what that means under the contract, what limits it, and what should happen next. Support for a hypothesis is not `evidence_status="supported"`; that assessment belongs to the Brain.

### Writing a Stop / Pivot Note as the Executor

You write one when the mission cannot reach a Support or Refute outcome: a dependency, environment, data access or tooling failure (`implementation_failure`), or the Inconclusive criterion was met (`insufficient_evidence`). The Brain or PI writes `resource_priority` and `counterevidence` stops. Fill `../brain/templates/stop-pivot-note.md`; set the mission `partial` or `blocked`, never `complete`; submit a report whose summary names the category; propose the decision's new status in the report rather than changing it yourself.

### Write discipline for every save above

- `record_note` with a stable `request_id`. On timeout, read `note_write_receipt` first; retry with the identical payload only. A receipt proves persistence, not that anyone read the note.
- `update_decision`, `supersede_decision`, `create_mission`, `update_mission_status` and `submit_report` are not idempotent. Do not retry them blind; read the entity back and decide.
- A template save is several writes with no transaction around them. If one fails, report which ids exist and which do not; do not describe the set as saved.
- `confidence="verified"` means sources were checked. Scientific support is `evidence_status`, set by the Brain. Do not conflate them in notes or reports.
- Preview operations (experiments, claim scope, interpretation staging) are not part of this flow. If one seems necessary, stop and raise a checkpoint rather than loading it.

---

## Migration Table Registry

When writing a migration that creates a new table with a `project_id` column, you MUST also add it to `_TABLE_CATEGORIES` in `rka/services/knowledge_pack.py`. Choose the correct category:

- **`core_data`** — research knowledge that MUST be preserved (journal, decisions, claims, clusters, decision_options, calibration_outcomes, etc.).
- **`derived_data`** — can be rebuilt if missing (review_queue, topics, exploration_summaries).
- **`system`** — infrastructure tables not exported (jobs, kv_store, schema_migrations).
- **`bulk_logs`** — large log tables exported only on request (audit_log, events).

If you skip this step, the pre-export validation will fail with an explicit error — but catching it at development time is better than catching it at export time.

Supporting plumbing that usually goes with a new `core_data` entry: `_INSERT_ORDER` (FK-safe position), `_ID_ENTITY_TYPES` (singular-form entity name), `_DIRECT_ID_COLUMNS` (columns whose values must be remapped during import). See migration 017 commit `b577211` for the canonical pattern.

---

## MCP Binary Reinstall

After ANY code changes to `rka/` source that affect the MCP layer:

```bash
cd /path/to/rka
uv tool uninstall rka
rm -rf /tmp/uv-cache
UV_CACHE_DIR=/tmp/uv-cache uv tool install --force --reinstall .
```

Plain `uv tool install --force .` uses cached wheels and does NOT pick up changes.

After the reinstall, the PI needs to restart Claude Desktop (and you need to restart Claude Code) for the MCP subprocess to pick up the new binary. A subprocess spawned against the pre-reinstall binary keeps the old file handle through the replacement and runs stale code.

---

## Related

- Every-mission discipline (role, session start, Backbrief rule, escalation categories, guardrails): `SKILL.md`.
- Worked examples (Backbrief, reports, checkpoint timing): `examples.md`.
- Brain counterpart skill: `../brain/SKILL.md`.
- RWF v0 templates (Gap Note, Hypothesis & Test, Stop / Pivot Note): `../brain/templates/README.md`.
