# Core audit E2a: document parity and conservative legacy adoption

- Date: 2026-09-07. Baseline: `3cf1210` (merged PR #159).
- Isolated branch: `codex/core-e2-index-recovery`.
- Worktree: `/private/tmp/rka-core-hardening-5OnbMY/source`.
- Design and remaining E2b/E2c gates:
  [Embedding index recovery](../specs/2026-09-07-embedding-index-recovery.md).
- Implementation, focused/base-profile checks, startup smoke and final complete
  Core regression passed locally. This is not a release or deployment.

## Implemented boundary

1. Seven canonical document recipes now share a provider-free implementation.
   Ordinary writes, entity workers, artifact/figure helpers, bulk backfill and
   stored-hash verification agree. Existing hash format and space signatures
   remain unchanged. No source text or artifact files are rewritten/read by the
   new codec; the old service-module artifact/figure builder imports still work.
2. Eligible pre-generation indexes retain independently hash-verified vectors.
   Only failed source/metadata pairs are invalidated, scoped by entity and
   project. Shared artifact/figure storage respects entity type. Initial
   generation registration and invalidation are one rollback unit. Claim
   invalidation marks its derived pending flag; repair still binds generation.
3. Unknown model/policy identity, unknown entity types, orphaned or mismatched
   project/vector/source records do not acquire trust from matching content.
   Global new-space transitions remove all derived metadata, including unknown
   types that the per-table reshape loops do not enumerate.
4. Coverage uses the same recipe, not SQLite ASCII whitespace handling or raw
   figure JSON. Empty JSON claims do not stall completion, while metadata-only
   artifacts and malformed figures cannot be silently marked complete. Keyset
   inspection includes an empty-string legacy ID; unaddressable NULL IDs fail
   readiness rather than repeat a cursor indefinitely.
5. Internal verification returns identifiers and fixed statuses only. Source
   fields are fetched in pages of at most eight rows; arbitrary exception text
   and document contents are not emitted. This is not a hard byte/RSS bound or
   a complete health report. Callers supply snapshot ownership and identity/
   structural checks; legacy adoption does so under its existing transaction.

## Validation

All databases and providers are synthetic. Environment: temporary RKA data
directories, embeddings/LLM disabled by default, fallback API at loopback port
1, `HF_HUB_OFFLINE=1`. No production RKA/LM Studio calls, model downloads, Docker
volume changes, MCP reinstall, image publishing or sibling-worktree writes.

- Initial behavioral regressions: **12 failed, 3 passed**. Five ordinary/worker
  text mismatches and coverage errors for Unicode whitespace/empty figure JSON
  were reproduced before the fix.
- Final focused suite: **61 passed**, 12.88 seconds. Includes actual seven-type
  vector/metadata writes through ordinary, worker and bulk paths; each type's
  selective invalidation/repair; 19-row/two-project preservation; rollback;
  unknown identity/types; read-only paging; malformed/empty document boundaries;
  plus existing generation/config/resource regression checks.
- Base-profile environment (no FastEmbed, no bibtexparser; real sqlite-vec
  `v0.1.6`, managed Python 3.13.11): **132 passed**, 37.09 seconds. Includes durable
  backfill ownership, API jobs, legacy entrypoints and CLI regressions.
- Fixtures were corrected to supply required actors/project IDs and to bind
  the adopted generation. The NULL-ID fault is injected at the read boundary
  because current change-event triggers reject such an insert. Guards and
  source/vector/hash assertions were retained. Initial incomplete full-suite
  runs and fixture-failing runs are not counted as validation passes.
- Final full Core: **3578 passed, 1 skipped, 294 deselected, 5 subtests passed**,
  290.05 seconds, exit 0. This includes 38 additional tests over the merged
  baseline. The skip is the native Windows-only file-boundary case; exclusions
  are Writer/Agentic, and the five warnings are existing PyMuPDF/SWIG deprecations.
- Core startup smoke (`--require-web --require-vec`) passed with synthetic data
  and a random loopback port, covering migrations, Phase-2 locking, REST, MCP,
  worker, sqlite-vec and web assets. No service was started on port 9712.
- Existing REST/MCP contract snapshots pass read-only checks. Changed Python
  files pass Ruff and Python 3.11 syntax compilation; workflow YAML parses;
  `git diff --check` passes. The syntax check is not a Python 3.11 test run.
- CI now includes the document/verification/index suites in the existing native
  Linux/macOS/Windows x Python 3.11/3.13 base-profile matrix. That updated matrix
  has not run on GitHub in this turn; local macOS evidence is not a substitute.

Final JUnit evidence under the isolated parent directory:
`e2-focused-verified-results.xml`, `e2-base-release-check-results.xml`, and
`e2-core-release-check-results.xml`.

```sh
RKA_DATA_DIR=/private/tmp/rka-core-hardening-5OnbMY/e2-release-check-data \
RKA_EMBEDDINGS_ENABLED=false RKA_LLM_ENABLED=false \
RKA_API_URL=http://127.0.0.1:1 HF_HUB_OFFLINE=1 \
.venv/bin/python -m pytest -q --tb=short --strict-markers \
  -m 'not writer and not agentic' \
  --junitxml=/private/tmp/rka-core-hardening-5OnbMY/e2-core-release-check-results.xml
```

## Security/ownership review and remaining gates

The focused safety review traced source fields through composition, hash checks,
adoption and derived writes. SQL identifiers are fixed registry constants;
record/project/type values are bound parameters. Selective deletions follow
structural and identity gates and share the generation transaction. Regression
tests cover refusal of unknown types, exact cross-project preservation and
rollback after invalidation but before state registration. No new public
endpoint, permission bypass, provider connection or dependency was introduced.

This batch does not implement maintenance CLI inspection/dry-run, backups,
cross-process exclusion, populated dimension changes, recovery intent/resume or
automatic hash repair for already-managed generations. Existing online
dimension-change rejection remains. Issue #158, real-model RSS evidence and
overall E1/E2 acceptance remain unresolved.

Canonical checkout remains `main / f8db01b` with its existing untracked audit
plan; sibling worktree branch heads remain unchanged. No production records
were written. New PR/push/merge/deployment require separate authorization.
