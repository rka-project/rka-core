# E2 embedding index recovery: staged implementation

- Date: 2026-09-07. Approved program: [Core audit remediation E2](../plans/2026-09-05-core-audit-remediation-plan.md).
- Baseline: `3cf1210` (PR #159). Worktree branch: `codex/core-e2-index-recovery`.
- This is audit-remediation E2, not the already-completed Core-separation E2.
- Core remains local-first. No production database, model runtime or sibling
  worktree is used for implementation tests.

## E2a: document parity and conservative legacy adoption

The first batch unifies the existing seven document recipes in a pure module.
Ordinary service writes, five entity-worker handlers, artifact/figure builders,
bulk backfill and hash verification use the same untemplated document. The
existing UTF-8 SHA256 first-16-hex digest is retained. Plain fields are stripped
at their edges; interior whitespace and canonical source records are preserved.
Artifact/figure labels, sorted metadata JSON and the five-claim figure recipe
are unchanged. This is not a new chunking or normalization policy.

Provider templates remain downstream of composition. Document templates and
explicit embedding-space IDs retain their existing signature semantics;
query-only templates do not reset stored vectors. No persisted identity, hash
format, configuration schema, REST/MCP surface or database migration is added.

Legacy adoption keeps its existing prerequisites: default document template,
no explicit unprovable legacy space ID, matching metadata model/dimension, all
six physical vec tables matching the dimension, and complete source/vector/
metadata correspondence. Unknown entity types and orphan/cross-project rows
fail this prerequisite; they cannot be adopted on a content hash alone.

After these gates, a keyset-paged checker verifies each source/metadata pair.
Only the unverified pairs lose derived vectors and metadata; other verified
rows, including other projects, remain byte-for-byte intact. Claim invalidation
sets its derived pending flag. Invalidation and initial generation registration
share the existing writer/migration transaction, so both roll back on failure.
Empty documents are not reused as useful vectors; malformed documents remain
unverified. New-space transitions still clear the global derived index and
cannot preserve unknown metadata as members of the new generation.

The checker is a read-only internal primitive, **not** a complete health report
or a standalone space-identity proof. It returns identifiers and fixed status
codes, never documents or exception text. It scans at most eight rows per fetch;
this is a row-count bound, not a hard byte/RSS limit. A caller must own a database
transaction if it requires snapshot consistency. Adoption already does.

Readiness checks only missing-metadata records and applies the same recipe for
eligibility. SQLite's ASCII `trim` and raw figure-claims JSON are not substitutes
for that recipe. Invalid composition blocks readiness rather than being treated
as empty. Global generation scope and optional coverage scopes remain intact.

## E2b: inspect / dry-run and maintenance ownership (next)

Build the Core inspection/dry-run service and CLI on these proofs, loading the
persisted config rather than constructing an unrelated environment-default
backend. Report physical schema, generation/config compatibility, coverage,
hash drift and intended global impact separately. Inspection must not load a
model, change configuration, enqueue a job or repair the index.

Before exposing destructive maintenance, specify and test cross-platform
exclusion across API, worker and maintenance processes. An idle queue or a
database write lock alone does not prove peers have released old vec0 schemas.
Define lock acquisition order, stop/new-start behavior and recovery after owner
death. All supported entrypoints must participate; old/uncontrolled processes
must be rejected or explicitly stopped, not assumed cooperative.

## E2c: offline rebuild / resume (after E2b)

Under proven exclusion, create and verify a coherent SQLite backup plus config
snapshot, persist recovery intent, then change schema/config through one
controlled global transition. Define failure boundaries before destructive work:
backup failure, process termination, config replacement, schema transaction,
provider failure, resume and rollback. Never remove a user's recovery copy as
an incidental cleanup step. API/worker admission must honor unfinished recovery.

Acceptance includes populated 768-to-384 conversion, empty first setup,
same-space selective repair of managed generations, changed document policy,
query-only changes, multi-project preservation and native Windows/Linux/macOS
exclusion/recovery. No manual production `DELETE` instructions are a substitute.

## Explicit non-completion claims

E2a does not add an offline maintenance CLI, backups, a persisted maintenance
intent, exclusion protocol, cross-dimension recovery or managed-generation hash
repair at startup. Existing online populated-dimension rejection stays in place.
It does not automatically audit every ready generation at startup. The E1
resource ceiling, real-model RSS evidence and issue #158 remain unresolved;
synthetic vectors do not establish real-model memory safety.
