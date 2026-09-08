# I3c — Explicit capture contracts and conservative migration

Date: 2026-09-08. Local development, not a release or production migration.

Integration handoff (2026-09-08): the PI approved commit, PR, CI and merge.
The workflow now runs the I3-specific migration, attribution, capture, pack and
public round-trip gate on all six OS/Python wheel-matrix combinations. Its
local preflight passed 103 tests. The uncommitted/temporary-path statements
below describe the earlier validation snapshot; GitHub PR/commit history is
authoritative for subsequent integration status. No deployment is included.

## Recovery and isolation

The prior uncommitted I3a/b worktree and test environment under `/private/tmp`
were absent on resumption. The cause was not established. The Git branch was
still at E baseline `6dbb0e0f2e9d4caa74732cb1be347a2fd113558b`.
All 20 original I3a/b apply_patch calls were recovered from the durable task log
and replayed in order, changing only source-root file headers. No logged shell
or JavaScript was executed. Contract snapshots were regenerated.

New persistent source:
`/Volumes/FuSpace/Projects/rka-projects/rka/.worktrees/core-i3-journal-audit`.
Branch: `codex/core-i3-journal-audit`. Independent dependencies: sibling
`i3-py313`; synthetic test data: sibling `i3-test-data`. Recovery manifest:
sibling `i3-recovery.md`. The canonical checkout, production services/data/models
and protected sibling worktrees were not changed. No old worktree metadata was
pruned. `.worktrees/` was added only to the local Git exclusion file.

The first recovery run found system Python 3.12 lacked SQLite extension loading:
72 tests passed, one vector-integrity test failed. A task-local Python 3.13
environment then passed all **73 recovery tests** (`../i3-recovery-py313.xml`).
Old I3a/b `/private/tmp` test XML references are historical, not current files.

## Design

- Default omitted mode remains `unknown` on single-note creation. Existing
  authors, quotes and empty/missing originals are never used to infer capture.
- Explicit `raw_capture` keeps supplied original text; only creation may copy
  the supplied content when the original is absent. Content may subsequently
  change without altering the original. This is text preservation, not proof
  of the author's identity or an immutable file-byte archive.
- Explicit PI `agent_restatement` requires a separate original on all creation
  paths. Preserve older MCP PI-quotation checks for unknown-mode compatibility.
- Markdown section originals are sliced before formatting. Complete local text
  is read with line endings intact and a hard character cap. Truncated text,
  DOCX extraction, code previews and file metadata stay unknown.
- Migration 057 adds unknown-valued capture columns to old rows and correction
  events without rewriting previous fields or inventing history. Correction
  omission preserves mode; explicit changes require the same revision/reason
  protocol. Exact retry resolves omitted mode against the immutable event's
  prior mode, not today's head. SQL guards and pack chain checks include mode.
- REST/typed MCP/deferred MCP/raw dispatcher/batch creation forward the field;
  ordinary updates/bulk cannot use it to bypass correction history. No new tool
  operation or route is added. Public schemas are refreshed by the generator.
- A regression probe reproduced the old execute wrapper collapsing absent
  `verbatim_input` to explicit null during corrections. It now forwards the
  dispatcher's omission sentinel; the same tests cover both entry points.

User-facing examples and compatibility details: [contract](../../JOURNAL_ATTRIBUTION.md).

## Verification

Validation completed on macOS/Python 3.13. Embeddings and LLM access were
disabled, the test API fallback pointed to `127.0.0.1:1`, and Hugging Face was
offline. No calls targeted the live RKA service.

- Final full Core: **3768 passed, 1 skipped, 294 deselected, 5 subtests passed**
  in 334.30s (`../i3c-core-final.xml`). Six warnings: five existing SWIG warnings
  and one deliberate deprecated-field compatibility assertion.
- Final focused ingestion/capture/pack/migration/public round trips: **107
  passed** (`../i3c-final-focused.xml`). Legacy omission/field parity: **41
  passed** (`../i3c-legacy-final.xml`), including the reproduced wrapper bug.
- Both generated REST/MCP snapshots match the current code. `git diff --check`
  and compilation checks pass. Ruff's historical E4/E7/E9/F checks pass for
  changed/new files except ten pre-existing `server.py` findings; the same ten
  were verified against HEAD. Newer Ruff's broader defaults were not used as a
  claim of repository-wide lint cleanliness.
- Source-worktree startup smoke `scripts/core_startup_smoke.py --require-vec`
  passed CLI, migrations, Phase-2 locking, public REST, MCP, worker and sqlite-vec
  using a disposable database/random localhost port. Sandbox bind was initially
  denied; the approved isolated rerun passed. This was not a clean-wheel,
  rebuilt-web or Windows/Linux execution acceptance.
- First full run had one failure caused by a new test omitting its required
  project header (3766 passed). The corrected test and the subsequently found
  legacy-wrapper omission regression are both covered by the clean final run.

Persistent source backup: sibling `i3c-source-2026-09-08.tar.gz`, containing
tracked files and all non-ignored untracked source/tests/docs, without Git
metadata, dependencies or production data. Its checksum is in sibling
`i3c-final-validation.md`. All changes remain uncommitted; no push/PR/merge or
deployment was performed.

## Remaining boundaries

I4 currentness/resolve_stale and I5 directive invalidation remain separate.
This batch does not infer verified authorship, retrofit old originals, introduce
complete content history, authenticate caller-declared actors, or deploy.
PR/merge and an isolated release installation smoke remain separate gates.
