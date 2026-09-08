# I-series completion candidate, 2026-09-08

Status: I1/I2 merged in #159, I3 merged in #161; I4-I7 implementation and final local
acceptance complete. No new PR, merge, release, deployment or live-data migration
is claimed by this record.

Base: `536e6abfd764f18a803904631ee452815c27386f` (#161).
Branch: `codex/core-i4-i7-completion`.
Source worktree: `.worktrees/core-i4-i7-completion` under the canonical checkout.
The canonical checkout and the journal-write-fix, portable-embedding and
research-story sibling worktrees were not edited. Test databases are synthetic;
embedding and LLM inference are disabled and the public model cache is offline.

## Completed implementation

| Slice | Boundary and regression evidence |
| --- | --- |
| I4 / #141 | Shared currentness projections in resolver/search/graph/REST/MCP, truth table, atomic audited resolution with optional journal and matching-alert closure, exact retries, reflag triggers, aging from review time, inactive search demotion, pack round-trip. Structural invalidation survives review. |
| I5 / #153 | Explicit same-project directive dependencies, no inferred authority from citations/PI authorship, shared live/admin discovery and invalidation, journal-derived claim/cluster invalidation, atomic decision replacement and exact retries, competing-intent rejection, preserved `revisit` compatibility. |
| I6 | Source FK/logical/polymorphic preflight, option ownership and directive-type checks, structured missing-reference issues, documented runtime-job exclusion, original digest validation before controlled re-keying, target hashes and atomic database/artifact publication. |
| I7 | Scoped and acyclic mission relationships, scoped review/calibration writes, once-only checkpoint resolution and mission reports, exact retries and explicit conflict errors, failure rollback, demonstrated read-chain guards against legacy malformed cross-project associations. |

Public additions are `resolve_stale`, `record_directive_dependency`,
`POST /api/freshness/resolve-stale`, and GET/POST
`/api/notes/{directive_id}/dependencies`. The five-tool MCP transport and existing
operation names stay unchanged. There are 88 stable Core MCP operations and 143
stable Core REST operations; intentional REST/MCP snapshots and count locks are
updated together.

Migration 058 installs review-reopening triggers without rewriting existing
dispositions. Migration 059 adds explicit dependencies without inferring any
historical declaration. Both are exercised on a synthetic migration-057 database.

Contracts and compatibility decisions:

- [Currentness](../../CURRENTNESS.md): review is not re-distillation or scientific validation.
- [Directive dependencies](../../DIRECTIVE_DEPENDENCIES.md): citations are not lifecycle authority.
- [Pack integrity](../../PACK_INTEGRITY.md): strict required references, explicit exclusions, unsigned hashes are not authentication.
- [Lifecycle writes](../../LIFECYCLE_WRITES.md): no silent report/resolution overwrite or automatic historical cleanup.

## Validation and limits

- Focused closeout: 63 tests passed, covering contract locks, typed/raw/legacy
  field parity, public checkpoint/report conflicts, source inspection and
  adversarial lifecycle boundaries.
- Migration/adversarial/pack-focused run: 38 tests passed, including frozen
  native-manuscript round-trip, unchanged historical rows, source corruption,
  foreign-project reads and writes, exact retries and rollback.
- Final read-projection gate: 86 tests passed, including public MCP claim/cluster
  lists and research-map summaries retaining inactive dispositions.
- Isolated startup smoke passed migrations, Phase-2 file locking, REST workflow,
  MCP, worker and real sqlite-vec. It used a random loopback port and disposable
  data, no live server. Frontend build was not part of this backend-only local
  smoke; unchanged Web and Docker build jobs remain part of CI.
- Frozen native/semantic/planning/experiment/attribution pack compatibility:
  19 tests passed (5.13 s).
- Final Core profile, after the last MCP/read-projection changes and with source
  held unchanged during the run: **3847 passed**, 1 skipped, 294 deselected,
  5 subtests passed (360.74 s). No failures; existing deprecation warnings remain.
  The skip is native Windows junction/handle behavior on this macOS host.
- The local wheel built successfully; both new migrations and helper modules
  were verified in its archive. It was installed into a **new** test venv and
  passed startup/REST/MCP/worker/sqlite-vec smoke outside any source checkout.
  Wheel SHA-256: `0e403f634329ae96a3d4cb6d8d51ce0092d177c000d3aab63f9edbe8889c7b45`.
  This is macOS/Python 3.13 artifact evidence, not a published release or a
  replacement for the Linux/Windows CI gate.
- Changed Python files have no new E4/E7/E9/F findings. The MCP server retains
  the same ten baseline findings, verified against the parent commit; unrelated
  broad cleanup is intentionally out of scope.

Local JUnit files are in the worktree parent: `i4-i7-final-core-results.xml`,
`i4-i7-final-pack-compat-results.xml`, `i4-i7-adversarial-results.xml` and
`i4-i7-closeout-focused.xml`. They are local evidence, not GitHub CI evidence.
The workflow adds I4-I7 migration/lifecycle/pack tests to the existing
Linux/macOS/Windows × Python 3.11/3.13 gate. That matrix is **not yet run for this
candidate** and is required before merge.

One frozen Writer test still expected zero runtime jobs from import. The E-series
already introduced a completed lexical import receipt. The assertion now demands
exactly that completed `pack_import` receipt with no embedding job, and still
rejects restored verification or synthesis work. Outline snapshot re-keying is
limited to the same explicitly reference-bearing intention fields as the stored
outline; arbitrary nested prose is not globally rewritten to make tests pass.

## Remaining program

I4-I7 PR/integration and native CI are separate from implementation completion.
R1 remote execution/security policy, R2 installation/maturity/legacy acceptance,
R3 release artifacts and the formal Core release are not completed by this batch.
No production data cleanup, RKA runtime restart, LM Studio request, container
deployment, shared-host upgrade, or Writer product development was performed.
