---
name: rwf-hypothesis-test
description: Pre-registered hypothesis and test contract with mechanism, scope conditions, controls, observations and support/refute/inconclusive criteria fixed before execution.
version: 0.1.0
template_kind: hypothesis_test
---

# Hypothesis & Test (RWF v0 template 0.1.0)

A Hypothesis & Test note fixes, before any run, what mechanism is proposed,
under which conditions it is expected to hold, what will be measured against
what, and which outcomes count as support, refutation, or inconclusive. The
Executor then records observations against that contract instead of deciding
afterwards what the result meant. Shared conventions: [`README.md`](README.md).

## When to write one

- When a confirmed research question or design choice is about to be tested.
- Before `create_mission` for any experiment, benchmark, or evaluation.
- When an existing hypothesis is revised: write a new note that `supersedes`
  the old one rather than editing the criteria in place.

## Required header

Copy the shared header from `README.md` with `template: hypothesis-test@0.1.0`.
`review_anchor` is the `dec_` id the test serves; after the mission exists,
add the `mis_` id.

## Sections

1. **Mechanism.** Why the effect should occur, in one or two sentences.
2. **Hypotheses.** Numbered H1, H2, ... Each is a statement that an
   observation could contradict.
3. **Scope conditions.** The settings under which the hypotheses are claimed
   to hold (dataset, population, platform, parameter ranges, time window).
   Outside these conditions the note claims nothing.
4. **Controls and baselines.** What the treatment is compared against, and
   why that comparison isolates the mechanism.
5. **Observations and metrics.** What will be measured, in which units, with
   what uncertainty estimate (repeats, seeds, confidence intervals).
6. **Pre-registered criteria.** Three explicit sub-sections:
   - *Support*: the observation pattern that counts for each hypothesis.
   - *Refute*: the pattern that counts against it.
   - *Inconclusive*: the patterns that count as neither (too few runs,
     effect inside the uncertainty band, environment failure).
7. **Minimal test.** The smallest run that can produce a Support or Refute
   outcome under section 6.
8. **Revision log.** Empty at creation. Any later change to sections 2 to 6
   is a new note that supersedes this one, with the reason recorded here in
   the new note.

## Rules

- Write section 6 before section 7 is run. A criterion added after the data
  exists is an interpretation, not a pre-registration; record it as such.
- An environment or implementation failure is an Inconclusive outcome, never
  a Refute. Record it in a Stop / Pivot Note with
  `stop_reason: implementation_failure`.
- Support for a hypothesis does not promote any claim to `supported`. The
  Brain assesses `evidence_status` separately through `review_claims`.
- Scope conditions travel with the result. A later note that drops them has
  made a new, untested claim.

## Save to RKA (stable operations only)

```python
# 1. The contract.
rka_execute(args={"operation": "record_note",
                  "project_id": "prj_...",
                  "type": "note", "source": "brain",
                  "confidence": "hypothesis", "importance": "high",
                  "request_id": "hyp-<topic>-<date>",
                  "tags": ["rwf-hypothesis-test", "rwf-v0"],
                  "content": "<header>\n\n# Hypothesis & Test: <title>\n...",
                  "provenance": {"related_decisions": ["dec_<question or choice>"]}})

# 2. The decision that adopts the test carries H1..Hn and the scope
#    conditions as its assumptions, so they are visible without opening the note.
rka_execute(args={"operation": "record_decision",
                  "project_id": "prj_...",
                  "kind": "design_choice", "decided_by": "pi",
                  "phase": "<phase>",
                  "question": "Which test settles <question>?",
                  "chosen": "<the minimal test from section 7>",
                  "rationale": "<why this comparison isolates the mechanism>",
                  "assumptions": ["H1: ...", "H2: ...", "Scope: ..."],
                  "related_journal": ["jrn_<hypothesis note>"]})

# 3. The mission quotes the criteria verbatim, so the Executor's acceptance
#    test is the pre-registered one.
rka_execute(args={"operation": "create_mission",
                  "project_id": "prj_...",
                  "objective": "Run <minimal test> for H1..Hn",
                  "motivated_by_decision": "dec_<test decision>",
                  "acceptance_criteria": "Support: ... Refute: ... Inconclusive: ...",
                  "context": "Contract: jrn_<hypothesis note>. Scope: ..."})

# 4. Executor observations, one note per run or batch, against the contract.
rka_execute(args={"operation": "record_note",
                  "project_id": "prj_...",
                  "type": "note", "source": "executor",
                  "confidence": "tested",
                  "request_id": "obs-<mission>-<run>",
                  "tags": ["rwf-hypothesis-test", "rwf-v0", "observation"],
                  "content": "Run <id>: <metric> = <value> ± <uncertainty> (n=<repeats>). Against jrn_<hypothesis note> §6: <Support | Refute | Inconclusive> for H1 because ...",
                  "provenance": {"related_mission": "mis_...",
                                 "related_decisions": ["dec_<test decision>"]}})
```

## Retrieve later

`search` with `"query": "rwf hypothesis test <topic words>"` scoped to
`["journal", "decision"]`, then `entity` on the note and the decision. Read
`assumptions` on the decision for the hypotheses, and the mission's
`acceptance_criteria` for the criteria that were in force when it ran.

## Synthetic example (fictional project and ids)

```
template: hypothesis-test@0.1.0
project: prj_SYNTHETIC
author_role: brain
status: confirmed
sources: jrn_SYN0010 (gap note), lit_SYN0001 (§4)
review_anchor: dec_SYN0011

# Hypothesis & Test: battery-aware sampling interval

## Mechanism
Soil moisture changes slowly except during irrigation or rain; lengthening the
interval when the battery is low loses little information most of the time.

## Hypotheses
H1: Lifetime increases by at least 30 % versus fixed 15-minute sampling.
H2: Missed irrigation events stay at or below 1 per week.

## Scope conditions
Single node, outdoor plot, one growing season, battery 2000 mAh, intervals
between 15 and 120 minutes.

## Controls and baselines
Fixed 15-minute sampling on a matched node in the same plot.

## Observations and metrics
Days until shutdown; events missed per week, judged against a wired
reference sensor; 3 node pairs.

## Pre-registered criteria
Support: H1 median gain ≥ 30 % and H2 ≤ 1 missed event per week on all pairs.
Refute: H1 gain < 10 % on all pairs, or H2 > 3 missed events per week on any pair.
Inconclusive: fewer than 3 pairs complete the season; reference sensor outage > 10 % of days; gain between 10 % and 30 %.

## Minimal test
Three pairs, one season, logged weekly.

## Revision log
(empty at creation)
```
