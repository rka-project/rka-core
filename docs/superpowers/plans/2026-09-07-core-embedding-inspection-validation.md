# E2b1: read-only embedding inspection and dry-run

- Baseline: `e7a3495`; branch: `codex/core-e2-index-recovery`.
- Isolated source: `/private/tmp/rka-core-hardening-5OnbMY/source`.
- [Operator guide](../../embedding-inspection.md) and
  [E2b1 boundary / proposed E2b2 admission protocol](../specs/2026-09-07-embedding-inspection-maintenance.md).
- Scope: usable inspection/preview commands and ownership design, not active
  API/worker lifetime leases or executable offline recovery.
- Status: implementation, focused/base-profile, complete Core and startup smoke
  passed locally. No publication or deployment.

## Delivered

`rka admin embedding inspect` and `dry-run` require explicit local paths and a
persisted configuration. They report global source/metadata/vector integrity,
hash drift, coverage and generation/config identity without normal RKA startup.
Candidate configs describe potential recovery impact only. Error/JSON output
does not expose API keys, endpoints, templates, source text or stored error
details. No inference, queueing, migration, permissions change or config save.

The dedicated read connection uses SQLite read-only/query-only mode, snapshots
committed WAL content, closes after success/failure, blocks ATTACH/SQL extension
loading, rejects source-view replacements and only loads installed sqlite-vec.
Configuration is bounded and rechecked; deadlines/row budgets/oversize input
fail closed. Read-only is a canonical data/config guarantee, not a claim that
SQLite never touches WAL shared-memory sidecars. No maintenance lease is acquired.

## Validation evidence

All tests use synthetic temporary databases, fake providers, disabled default
embedding/LLM, loopback port 1 as the fallback API, and `HF_HUB_OFFLINE=1`.
No production RKA or LM Studio connection, Docker volume, model download,
MCP reinstall, GitHub push or deployment was performed.

- Initial CLI tests: **2 failed, 1 passed**, demonstrating the missing commands.
- Added **39 tests**, including all-seven-type reports, persisted-vs-env
  identity, whole-database/config hashes unchanged (excluding SQLite SHM),
  no provider construction, FastEmbed-free inspection, candidate query/document/
  model/space/dimension changes, legacy adoption refusal, multi-project mismatch,
  orphan/unknown rows, missing vectors, malformed/empty documents, config races,
  malformed config/generation/schema, safe error output, bounded input and budgets.
- WAL regression confirms committed rows still in WAL are visible, concurrent
  later commits are absent from the pinned snapshot, and a subsequent reader
  sees them. Read handles reject DML and ATTACH and close after exceptions.
- Final focused gate (new tests plus E2a and old backfill CLI): **95 passed**,
  17.38 seconds; `e2b-focused-final-results.xml`.
- Base-profile gate, same selection without FastEmbed/bibtexparser and with real
  sqlite-vec: **95 passed**, 18.65 seconds; `e2b-base-results.xml`.
- Complete Core: **3617 passed, 1 skipped, 294 deselected, 5 subtests passed**,
  293.76 seconds, exit 0; `e2b-core-results.xml`. The skip is the native Windows
  file-boundary case; exclusions are Writer/Agentic. Five existing PyMuPDF/SWIG
  deprecation warnings remain, with no new failure.
- Isolated startup smoke (`--require-web --require-vec`) passed: installed entry,
  migrations, Phase-2 lock, REST, MCP, worker, sqlite-vec, web assets. It binds a
  temporary loopback port, not 9712, and cleans up its own subprocesses.
- REST/MCP snapshot checks pass without changes. Changed Python files pass Ruff
  and Python 3.11 syntax compilation; workflow YAML parses and `git diff --check`
  passes. Python 3.11 compilation is not a native runtime test.
- Linux/macOS/Windows x Python 3.11/3.13 base CI matrix now includes these tests.
  No new remote CI run occurred in this turn; local evidence is macOS only.

JUnit outputs are under `/private/tmp/rka-core-hardening-5OnbMY/`.

```sh
RKA_DATA_DIR=/private/tmp/rka-core-hardening-5OnbMY/e2b-core-data \
RKA_EMBEDDINGS_ENABLED=false RKA_LLM_ENABLED=false \
RKA_API_URL=http://127.0.0.1:1 HF_HUB_OFFLINE=1 \
.venv/bin/python -m pytest -q --tb=short --strict-markers \
  -m 'not writer and not agentic' \
  --junitxml=/private/tmp/rka-core-hardening-5OnbMY/e2b-core-results.xml
```

## Safety review and outstanding gates

The focused security review traced operator paths/configs and stored schema/text
to URI opening, extension loading, SQL evaluation and CLI output. Findings drove
the independent read boundary, quoted URI paths, fixed SQL identifiers, bound
record values, trusted-schema/ATTACH restrictions, bounded reads and fixed error
codes. Tests exercise these controls; this is not a full dependency/penetration
audit or a guarantee about arbitrary SQLite VFSes/filesystems.

E2b2 still must implement and natively validate lifetime admission for API,
worker, direct DB clients and maintenance, with death/cancellation/close-order
coverage. The proposed admission-gate/runtime-slot design is documentation,
not an active exclusion mechanism. Old/uncooperative processes require explicit
supervisor/operator coordination; no PID or advisory-lock scan can prove them
absent. E2c backup, recovery intent, offline schema transitions and resume remain
unimplemented. Issue #158 and real-model RSS acceptance remain unresolved.

Main remains `f8db01b` with its pre-existing untracked audit plan. Journal,
portable-embedding and research-story sibling heads remain `fa59b4e`, `4912eaa`
and `c8281a4`. Production runtime remains outside this batch's write scope.
Only a local isolated-branch commit is intended; publication is separate.
