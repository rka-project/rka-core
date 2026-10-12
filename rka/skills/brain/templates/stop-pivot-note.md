---
name: rwf-stop-pivot-note
description: Record of why a direction was stopped or turned, with the reason category, the evidence, the conditions it applies to, and what would justify reopening it. Written so a later session can recover it.
version: 0.1.0
template_kind: stop_pivot_note
---

# Stop / Pivot Note (RWF v0 template 0.1.0)

A Stop / Pivot Note is the record a future session reads before repeating an
attempt. It separates four reasons for stopping that are routinely collapsed
into "the hypothesis failed", names the evidence for the reason, states the
conditions under which the reason holds, and gives the condition under which
the direction may be reopened. Shared conventions: [`README.md`](README.md).

## When to write one

- A mission ends without a Support or Refute outcome.
- A hypothesis is refuted under its pre-registered criteria.
- The PI deprioritizes or closes a direction.
- A direction is replaced by a different one (pivot).

## Required header

Copy the shared header from `README.md` with `template: stop-pivot-note@0.1.0`.
`review_anchor` is the `dec_` id being stopped or superseded, plus the `mis_`
id if a mission was running.

## Sections

1. **Direction and version.** The research question or design choice being
   stopped (`dec_` id), and the Hypothesis & Test note that was in force
   (`jrn_` id), if any.
2. **Stop reason.** Exactly one of these four categories, written as
   `stop_reason: <category>`:
   - `counterevidence`: an observation met the pre-registered Refute
     criterion. Cite the observation notes.
   - `implementation_failure`: code, environment, data access or tooling
     prevented the test from running as specified. The hypothesis is untested.
   - `resource_priority`: time, budget, staffing or a PI priority change
     ended the work. The hypothesis is untested or partially tested.
   - `insufficient_evidence`: the test ran but met the Inconclusive
     criterion, or the materials needed (full text, dataset, baseline) could
     not be obtained.
3. **Evidence for the reason.** The `jrn_` observation notes, error output,
   mission report, or PI directive that establishes the category. For
   `counterevidence`, quote the criterion that was met.
4. **Conditions under which the reason holds.** The scope conditions from the
   Hypothesis & Test note, the environment version, the dataset, the budget
   assumption. Outside these the stop says nothing.
5. **What the stop does not say.** One or two sentences preventing
   over-reading: an environment failure is not a disconfirmation; a priority
   change is not evidence against the mechanism.
6. **Reopen condition.** The concrete change that would justify trying
   again (a dependency fix, a new dataset, a budget, a contradicting result
   elsewhere). "Never" is allowed only for `counterevidence` with a stated
   scope.
7. **Pivot target.** If the direction is replaced, the new question or
   choice and the `dec_` id that supersedes the old one.

## Rules

- One category per note. If two apply, the one that would have stopped the
  work alone comes first; mention the other in section 5.
- `counterevidence` requires a pre-registered Refute criterion and the
  observation that met it. Without both, use `insufficient_evidence`.
- Never set the note's own `confidence` to `retracted` to hide it. A valid
  negative observation stays `active` so retrieval finds it. `retracted` is
  for notes that were wrong when written.
- Stopping effort is not a scientific contradiction. Do not create a
  `contradicts` relation between claims on the strength of a
  `resource_priority` or `implementation_failure` stop.

## Mission completion is separate from the research outcome

If required work remains unfinished, preserve `partial`, `blocked`, or
`cancelled` as appropriate and save the progress/stop summary as a
`record_note` linked through `provenance.related_mission`. Do not call `submit_report`
for an unfinished mission: Core changes `partial` to `complete` on report
submission and rejects reports for `blocked` or `cancelled` missions.

An Inconclusive scientific result does not by itself mean the mission is
unfinished. If the agreed work and acceptance criteria are satisfied, reconcile
all tasks and submit a completion report normally, preserving the Inconclusive
result and its limits. A Stop / Pivot Note records the direction's fate; it
does not force the mission into a non-complete state.

## Save to RKA (stable operations only)

```python
# 1. The note, linked to the decision being stopped and the mission if any.
rka_execute(args={"operation": "record_note",
                  "project_id": "prj_...",
                  "type": "note", "source": "executor",   # or "brain"
                  "confidence": "tested", "importance": "high",
                  "request_id": "stop-<decision>-<date>",
                  "tags": ["rwf-stop-pivot", "rwf-v0", "<stop_reason category>"],
                  "content": "<header>\n\n# Stop / Pivot Note: <title>\nstop_reason: <category>\n...",
                  "provenance": {"related_decisions": ["dec_<stopped>"],
                                 "related_mission": "mis_..."}})

# 2a. Stop with a reopen condition: the decision stays findable as revisit.
rka_execute(args={"operation": "update_decision",
                  "project_id": "prj_...", "id": "dec_<stopped>",
                  "status": "revisit",
                  "abandonment_reason": "<category>: <one line>; reopen when <condition>. See jrn_<stop note>."})

# 2b. Stop for good within the stated scope.
rka_execute(args={"operation": "update_decision",
                  "project_id": "prj_...", "id": "dec_<stopped>",
                  "status": "abandoned",
                  "abandonment_reason": "counterevidence: <criterion met>. See jrn_<stop note>."})

# 2c. Pivot: the replacement decision is created and linked atomically.
rka_execute(args={"operation": "supersede_decision",
                  "project_id": "prj_...",
                  "old_decision_id": "dec_<stopped>",
                  "question": "<new question>", "chosen": "<new direction>",
                  "rationale": "<why, citing the stop note>",
                  "related_journal": ["jrn_<stop note>"]})

# 3. Only if required mission work is unfinished, keep a non-complete state.
#    Step 1 is its linked stop summary; do not submit a completion report.
#    Use blocked or cancelled instead when that is the actual task state.
rka_execute(args={"operation": "update_mission_status",
                  "project_id": "prj_...", "mission_id": "mis_...",
                  "status": "partial"})
```

The PI's decision to stop is recorded separately with `source="pi"` and
`verbatim_input`, linked to the same decision.

## Retrieve later

Before starting or resuming work on a direction, run all three. None alone
is complete.

```python
rka_query(args={"operation": "mission_guard", "project_id": "prj_...", "id": "mis_..."})
rka_query(args={"operation": "search", "project_id": "prj_...",
                "query": "rwf stop pivot <topic words>",
                "filters": {"entity_types": ["journal"]}})
rka_query(args={"operation": "decision_tree", "project_id": "prj_...",
                "filters": {"active_only": False}})
```

From the tree, read every node whose `status` is `abandoned`, `revisit` or
`superseded` near the topic with `entity`, and follow `superseded_by` to the
current decision. `mission_guard` lists retracted or superseded journal
entries and unresolved claim contradictions; it does not list stopped
decisions, which is why the tree read is not optional.

## Synthetic example (fictional project and ids)

```
template: stop-pivot-note@0.1.0
project: prj_SYNTHETIC
author_role: executor
status: confirmed
sources: jrn_SYN0012 (hypothesis note), jrn_SYN0020, jrn_SYN0021 (run observations), jrn_SYN0022 (progress log)
review_anchor: dec_SYN0011, mis_SYN0030

# Stop / Pivot Note: battery-aware sampling, season 1
stop_reason: implementation_failure

## Direction and version
dec_SYN0011 (battery-aware sampling interval), contract jrn_SYN0012.

## Evidence for the reason
Two of three node pairs lost their wired reference sensor after week 6
(jrn_SYN0020, jrn_SYN0021). Reference outage exceeded 10 % of days, which is
the Inconclusive criterion in jrn_SYN0012 §6. Progress log jrn_SYN0022 is
linked to mis_SYN0030, which remains partial with required runs unfinished;
no completion report was submitted.

## Conditions under which the reason holds
Outdoor plot A, reference sensor model R-2, season 2026.

## What the stop does not say
Nothing about H1 or H2. The one complete pair showed a 34 % lifetime gain,
which is a single observation and not a Support outcome under the contract.

## Reopen condition
A reference sensor with a sealed connector, or a second plot. Rerun the same
contract; do not change the criteria.

## Pivot target
None. dec_SYN0011 set to revisit.
```
