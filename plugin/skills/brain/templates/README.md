# RWF v0 templates — evidence-led research workflow

**Template set version: 0.1.0 — introduced 2026-10-11**

Three client-side templates that make a research direction recoverable from
RKA alone: what gap was claimed and on what evidence, what hypothesis was
tested against which pre-registered criteria, and why work stopped or turned.
They are shared by the Brain, Executor and PI roles. Role pointers live in each
role's `SKILL.md`; the day-to-day flow lives in `brain/workflows.md` and
`executor/workflows.md` under "Evidence-led research workflow (RWF v0)".

| Template | File | Written by | Saved as |
|---|---|---|---|
| Gap Note | [`gap-note.md`](gap-note.md) | Brain, confirmed by PI | `record_note` (type `note`, tag `rwf-gap-note`) |
| Hypothesis & Test | [`hypothesis-test.md`](hypothesis-test.md) | Brain, criteria ratified by PI, observations by Executor | `record_note` + `record_decision.assumptions` + mission `acceptance_criteria` |
| Stop / Pivot Note | [`stop-pivot-note.md`](stop-pivot-note.md) | whoever stopped the work, confirmed by PI | `record_note` + `update_decision` or `supersede_decision` |

## Boundary

These are working conventions for the client agent and the researcher. They
are not new Core entity types, journal enums, schema, API or service behavior.
Every save below uses stable `rka_execute` operations that exist today;
nothing depends on the preview surface (experiments, claim scope,
interpretation staging). If a step seems to need a preview operation or a Core
change, stop and raise a checkpoint instead of improvising.

A template is a prompt for the writer, not a permission boundary. Filling in
every section does not prove the sources were read, that the test is sound,
or that the stop reason is correct. Those judgments stay with the researcher.

## Shared header

Every note written from one of these templates starts with this header, one
field per line, so that a later session can find it with `search` and tell
which template version produced it:

```
template: <gap-note | hypothesis-test | stop-pivot-note>@<template version>
project: prj_...
author_role: brain | executor | pi
status: draft | confirmed
sources: <lit_/jrn_/dec_ ids, each with a locator such as §3.2, Fig. 4, p. 7, or "abstract only">
review_anchor: <dec_... or mis_... this note is reviewed against, or pending-pi-confirmation>
```

`sources` must name what was actually examined. "Abstract only" is a valid
locator; an unlisted source is not.

## Tags

Write both tags on every saved note: the kind tag (`rwf-gap-note`,
`rwf-hypothesis-test`, `rwf-stop-pivot`) and the set tag `rwf-v0`. Tags are
matched by `search` token by token on hyphen-delimited segments, so the query
`rwf stop pivot` scoped to journal entries surfaces every Stop / Pivot Note.
Do not rely on `journal` `filters.tags` for this; at Core 3.0.1 that filter is
not applied (see the retrieval notes in `executor/workflows.md`).

## Evidence-status boundary

Three different things are easy to conflate. Keep them apart in every note:

- `confidence=verified` on a journal entry means the sources cited were
  checked. It says nothing about scientific support.
- `evidence_status` on a claim (`unassessed`, `supported`,
  `partially_supported`, `inconclusive`, `contradicted`) is the Brain's
  scientific assessment through `review_claims`. The Executor never sets it on
  its own results.
- A write receipt (`note_write_receipt`) proves the note was persisted. It
  does not prove the note was read, reviewed, or correct.

## Writing discipline

- Pass a stable `request_id` on `record_note`. On timeout, read
  `note_write_receipt` with that id before retrying; a retry must carry the
  identical payload. No other write operation is idempotent; never assume a
  retry is safe.
- Saving one template often means several writes (note, decision, mission,
  back-links). They are not one transaction. Report partial success entry by
  entry, with the ids that were created.
- Keep the PI's words in `verbatim_input` with `source="pi"`. The template
  body is the agent's restatement unless it says otherwise.
- Changing a template's required fields is a version bump here. A saved note
  keeps the version it was written against; do not edit old notes to match a
  newer template.

## Client entry points

The protocol is one set of files; only the way a client reaches them differs.

- **Claude Code with the `rka` plugin.** The role skills load from disk and
  re-inject on trigger. The templates sit beside them at
  `plugin/skills/brain/templates/`; a role skill refers to them by relative
  path, so `../brain/templates/gap-note.md` from the Executor skill resolves.
- **Any stdio MCP client (Claude Desktop, Codex, others).** Nothing is
  installed on disk for the client. Where the client exposes MCP prompts,
  `brain_skill`, `executor_skill` and `pi_skill` return the role `SKILL.md`.
  Everywhere, `rka_load_tools(["rka_read_skill", "rka_start_session"])` brings
  the two adapter tools online; `rka_start_session("executor")` returns the
  role guide, and `rka_read_skill("brain", "templates/gap-note.md")` returns a
  template. References must stay inside the `brain` skill directory, which is
  why the templates live there.
- **Codex native skill folders are not shipped.** Codex documents repo-level
  `.agents/skills` directories and `$HOME/.agents/skills` for personal skills,
  with symlinked folders followed. RKA Core does not install into either; the
  MCP path above is the supported route. Adding a Codex skill layout is a
  separate distribution decision, not a template change.

## Negative knowledge is kept, not deleted

A Stop / Pivot Note that records a real disconfirming observation stays
`active`. Retracting it so it stops appearing in retrieval would erase the
very fact a later session needs. `mission_guard` covers retracted or
superseded journal entries and unresolved claim contradictions; it does not
cover abandoned or revisited decisions, so recovery always adds the targeted
searches described in `executor/workflows.md`.
