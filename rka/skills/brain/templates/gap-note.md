---
name: rwf-gap-note
description: Evidence-backed research gap statement bounded by the sources actually examined. Use before opening a research question or when a session claims a gap exists.
version: 0.1.0
template_kind: gap_note
---

# Gap Note (RWF v0 template 0.1.0)

A Gap Note states what the nearest existing work does, where it falls short
for the question at hand, what would dissolve the gap, and how far the search
reached. Its conclusion is bounded by the materials listed in it. Shared
conventions (header, tags, evidence-status boundary): [`README.md`](README.md).

## When to write one

- Before `record_decision(kind="research_question")` opens a new direction.
- When a session is about to assert "no prior work does X".
- When the PI asks whether a direction is worth pursuing.

## Required header

Copy the shared header from `README.md` with `template: gap-note@0.1.0`.
`sources` lists every `lit_` entry examined, each with a locator.

## Sections

1. **Question and scope.** One precise question. What is in scope, what is
   explicitly out.
2. **Nearest existing work.** For each candidate, the `lit_` id, the locator
   examined, and one sentence on what it does. Three to seven entries is
   typical. If a candidate was only skimmed, say so in the locator.
3. **Specific shortfall per work.** For each entry in section 2, the concrete
   condition, setting, metric or assumption under which it does not answer the
   question. "Different" is not a shortfall; name the difference that matters.
4. **Counterexamples that would dissolve the gap.** What kind of paper, dataset
   or result, if found, would mean the gap does not exist. Include anything
   already found that comes close and why it does not close the gap.
5. **Search coverage.** Which stores and databases were searched (the project's
   own `literature` entries, external indexes, Zotero), the query strings, the
   date, and what was not read in full (`pdf_path` missing, paywalled,
   abstract only).
6. **Bounded gap statement.** One or two sentences of the form "Within the
   materials in sections 2 and 5, no examined work does Y under condition Z."
7. **What would confirm or refute this note.** The next search or read that
   would most change section 6.

## Rules

- Not found is not nobody did it. Section 6 must stay inside sections 2 and 5.
- A gap is a reading of sources, not a research result. Start with
  `confidence="hypothesis"`. PI confirmation authorizes the direction; it
  changes the template header to `status: confirmed`, not source confidence.
  Only a separate source check with recorded verification evidence permits
  `confidence="verified"`; even then, it does not prove that the gap is real.
- Do not infer a gap from titles or abstracts alone without saying so in the
  locator.
- Methodological novelty is one kind of gap. Replication, measurement,
  dataset and exploratory questions are legitimate and use the same note.

## Save to RKA (stable operations only)

```python
# 1. The note itself, before PI confirmation.
rka_execute(args={"operation": "record_note",
                  "project_id": "prj_...",
                  "type": "note", "source": "brain",
                  "confidence": "hypothesis", "importance": "high",
                  "request_id": "gap-<topic>-<date>",
                  "tags": ["rwf-gap-note", "rwf-v0"],
                  "content": "<header>\n\n# Gap Note: <title>\n...",
                  "provenance": {"related_literature": ["lit_...", "lit_..."]}})

# 2. After the PI confirms the direction, open the research question with the
#    note as its justification.
rka_execute(args={"operation": "record_decision",
                  "project_id": "prj_...",
                  "kind": "research_question", "decided_by": "pi",
                  "phase": "<phase>",
                  "question": "<the question from section 1>",
                  "chosen": "Open this question", "rationale": "<PI's reason>",
                  "related_journal": ["jrn_<gap note>"],
                  "related_literature": ["lit_...", "lit_..."]})

# 3. Back-link the note and update only its confirmation header.
#    Keep the full body, source limitations, and existing confidence.
rka_execute(args={"operation": "update_note",
                  "project_id": "prj_...", "id": "jrn_<gap note>",
                  "related_decisions": ["dec_<research question>"],
                  "content": "<same full Gap Note with status: confirmed and review_anchor: dec_<research question>>"})
```

The PI's confirmation itself is a separate `record_note` with `source="pi"`
and `verbatim_input`, linked to the same decision.

Do not add `confidence="verified"` to step 3. If the cited locators are
independently checked later, record what was checked and its limits in a
linked verification note before a separate confidence update. PI approval,
source grounding, and scientific support are different review results.

## Retrieve later

`rka_query(args={"operation": "search", "project_id": "prj_...",
"query": "rwf gap note <topic words>", "filters": {"entity_types":
["journal"]}})`. Then read the full note with `entity`, and check the linked
research question's current status with `entity` on the `dec_` id.

## Synthetic example (fictional project and ids)

```
template: gap-note@0.1.0
project: prj_SYNTHETIC
author_role: brain
status: draft
sources: lit_SYN0001 (§4, Table 2), lit_SYN0002 (abstract only), lit_SYN0003 (§3.1, Fig. 3)
review_anchor: pending-pi-confirmation

# Gap Note: duty-cycled sampling for soil-moisture sensors under intermittent solar power

## Question and scope
Does adapting the sampling interval to the battery state extend node lifetime
without losing irrigation-relevant events? In scope: single-node policies,
outdoor deployments. Out: mesh scheduling, cloud-side imputation.

## Nearest existing work
- lit_SYN0001 (§4, Table 2): fixed 15-minute sampling, lifetime measured in a lab chamber.
- lit_SYN0002 (abstract only): battery-aware duty cycling for air-quality sensors.
- lit_SYN0003 (§3.1, Fig. 3): event-triggered sampling with a wired power supply.

## Specific shortfall per work
- lit_SYN0001: no adaptation; lifetime figures assume constant temperature.
- lit_SYN0002: different sensor dynamics; the abstract does not state the event-loss metric. Full text not read.
- lit_SYN0003: event detection assumes unlimited power, so the trade-off this note asks about does not arise.

## Counterexamples that would dissolve the gap
A field study of battery-aware sampling for slowly varying soil variables that
reports both lifetime and missed-event rate. lit_SYN0002 may be this once read
in full.

## Search coverage
Project literature store (12 entries), two external indexes, queries
"adaptive sampling soil moisture battery", "duty cycle sensor lifetime
event loss", searched 2026-10-11. lit_SYN0002 full text unavailable.

## Bounded gap statement
Within the three works examined, none reports lifetime and missed-event rate
together for battery-aware sampling of a slowly varying outdoor variable.

## What would confirm or refute this note
Reading lit_SYN0002 in full; one more search on the event-loss metric.
```
