# I3b — guarded attribution corrections and portable history

- Base: merged E-series `6dbb0e0`, with the uncommitted I3a foundation retained.
- Worktree: `/private/tmp/rka-core-i3.emTbB6/source`.
- Branch: `codex/core-i3-journal-audit`; no commit, push, PR, merge or deployment.
- Scope: I3b implemented locally; I3 remains open for the capture/restatement
  contract and migration. I4–I7 are not claimed complete.

## Design and implementation

The [public correction contract](../../JOURNAL_ATTRIBUTION.md) documents the
intentional rejection of ordinary source/original updates and the replacement
REST/typed MCP/deferred tool workflow. Ordinary content, lifecycle, tags and
relationship updates are not full-record optimistic locking: the new revision
counter only protects attribution.

Migration 056 adds `journal.attribution_revision` with default zero and the
immutable `journal_attribution_revisions` table. It leaves all existing authors
and originals unchanged, does not invent historical events, and preserves NULL
originals as unknown. SQL triggers protect correction rows from updates and
individual deletion, and guard current attribution updates with matching
before/after correction state. Explicit project deletion remains supported.

`NoteService.correct_attribution` validates the complete replacement, scopes
the target before looking up a retry, compares exact request intent, rejects
stale revisions, and atomically appends history, changes the current head and
adds audit evidence. Successful exact retries return the same immutable event
even if a newer event exists; they never return a newer note as the old result.
The nullable original is required explicitly on REST, typed/deferred MCP and
raw dispatch, so omission cannot accidentally clear a stored quotation.

Actor and source are distinct declarations. `actor_basis=caller_asserted` is
not authentication; source/header values grant no new authority. Security-review
guided service and SQL boundary checks, cross-project denial, concurrent
revision tests, cancellation rollback and request replay tests.

The Executor migration registry rule required canonical pack registration,
FK-safe insert/delete ordering and ID mapping. Default packs now include the
history even without audit logs. Exact quotations and correction reasons are
not rewritten as prose during re-keying. A post-insert critical integrity check
rejects broken/missing chains or a mismatched current head and rolls back the
entire import. This is consistency validation, not cryptographic authentication
of an untrusted pack.

REST/MCP snapshots intentionally change: two REST endpoints, two MCP operations,
the read-back revision and deprecated ordinary attribution-write fields. Contract
count tests now assert the two specific additions. Existing frozen Writer and
shelved Agentic ownership remains unchanged.

## Isolation and verification

All application tests load source from this worktree, using the pre-existing E
test environment solely for dependencies. Every pytest command sets an isolated
`RKA_DATA_DIR`, disables embeddings/LLMs, sets `HF_HUB_OFFLINE=1` and points the
fallback API URL at unreachable loopback port 1. HTTP tests use ASGI and fresh
temporary databases; migration/pack/delete tests use synthetic projects only.
The startup-smoke script creates its own disposable data directory and dynamic
loopback port; its subprocesses run `python -m rka` from this worktree with
models disabled, then shut down. This is source-worktree startup verification,
not a clean-wheel installation or a rebuilt web/dashboard acceptance test.
No production database, running container, installed MCP, credential or model
runtime is modified.

Completed checks:

- Initial existing journal regression: 49 passed.
- New correction/migration/pack/public-path suite: 51 passed, 11.94 s.
- Expanded migration, correction, pack, all Core MCP and contract suite:
  1633 passed, 48 deselected, 49.38 s. This includes independent database
  connections, close/reopen retry, injected failure and cancellation rollback.
- Final full Core regression (`not writer and not agentic`): **3738 passed,
  1 skipped, 294 deselected, 5 subtests passed**, 325.04 s. This includes the
  final raw-dispatch omission/null regression, typed entity read-back and both
  ownership paths. Evidence: `/private/tmp/rka-core-i3.emTbB6/i3b-full-core-v2.xml`.
  Six warnings remain: five existing SWIG deprecations and one expected access
  to the newly deprecated ordinary-update `source` field in a compatibility test.
- Final compatibility suite (existing pack behavior, both ownership paths,
  capabilities and public journal round trips): 90 passed, 21.37 s.
- Source-worktree startup smoke with `--require-vec`: passed migrations,
  Phase-2 file locking, public REST workflow, MCP and one-shot idle worker.
- Snapshot generator updated both public artifacts; runtime snapshot comparison
  passed in the expanded suite.
- Ruff passes for changed Python files except `server.py`, whose 10 existing
  diagnostics (unused locals/import and late imports) were independently
  reproduced on `git show HEAD:rka/mcp/server.py`. No new diagnostics were added.
- `git diff --check` passes.

Earlier failed runs are not acceptance evidence: one enum-drift check found a
missing published actor alias, which was added. Four new test failures were
fixture assumptions (MCP renders API errors as ordinary exceptions; project
creation returns HTTP 200), corrected against the existing adapter contract.
The first full Core run reported 4 failed / 3732 passed: the old capability
count, the old quote-rewriting expectation, and two ownership tests that still
tried to change source through ordinary updates. The capability count now
asserts both new operation names; the pack test requires literal preservation
while continuing to assert structured/prose re-keying; ownership tests now cover
both ordinary and correction paths. No checks were removed to make the run green.

## Next I3 batch

Historical handoff below has now been implemented by
[I3c](2026-09-08-core-journal-capture.md). Its recovery section also supersedes
the old temporary worktree/test artifact paths in this record.

Define explicit raw capture versus agent restatement, align creation/ingestion
surfaces, preserve historical unknowns without guessed backfill, and decide how
capture mode participates in correction revisions. Then rerun migration,
REST/MCP/bulk, pack and full Core acceptance. Do not close I3 or infer deployment
approval from this batch's local completion.
