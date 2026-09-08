# E2b: inspection and maintenance admission

- Baseline: `e7a3495` (E2a); same isolated branch `codex/core-e2-index-recovery`.
- This delivery is **E2b1: read-only inspect/dry-run**. E2b2 lifetime admission
  and E2c executable backup/rebuild/resume are not implemented here.
- Parent: [E2 staged design](2026-09-07-embedding-index-recovery.md).

## Implemented read boundary

The CLI requires an explicit data directory; an optional database path supports
project-local/legacy layouts. It does not instantiate `RKAConfig` (whose
validator creates a directory) or `Database` (whose connect prepares writable
files). Persisted `embedding_config.json` is mandatory. Missing, corrupt,
oversize or indeterminate model/dimension configuration fails explicitly rather
than falling back to environment model defaults. Candidate configurations for
dry-run are read only, never saved or connection-tested.

A dedicated connection opens an existing path via quoted SQLite `mode=ro`,
sets `query_only`, disables trusted-schema functions, disallows ATTACH/DETACH
and SQL extension loading, and loads only the installed sqlite-vec package.
Missing sqlite-vec yields an incomplete report, not a healthy index. Source
views/virtual replacements and unsupported source columns fail without upgrade.
No schema initialization, migrations, checkpoints, chmod, mkdir, provider
construction, inference, config writes or queue mutations occur.

The read transaction includes committed WAL data and pins one database snapshot.
It does not use `immutable=1` or `nolock=1`: SQLite documents that immutable mode
skips locking/change detection and may be incorrect if another process writes.
See [SQLite URI semantics](https://www.sqlite.org/uri.html#uriimmutable).
Read-only means no canonical database/config mutations, not zero filesystem
activity: SQLite may create/update its WAL shared-memory sidecars and take read
locks. See [SQLite read-only WAL databases](https://www.sqlite.org/wal.html#read_only_databases).

Configuration bytes are bounded to 64 KiB and re-read before returning. A change
aborts the assessment. This is not an atomic multi-file transaction or an
execution reservation; the future executor must revalidate configuration,
generation, database identity and plan while it holds maintenance ownership.
An ABA config edit cannot be ruled out by byte equality alone.

Queries have a 30-second default deadline (at most 60 seconds), a 50,000 source
row default budget (at most 100,000), eight-row pages and a 2 MiB SQLite row/value
limit. Deadline/size/schema failures return an explicit error, never partial
counts labeled healthy. These are diagnostic admission bounds, not a hard OS
RSS limit. No embeddings or document text are emitted; only counts, supported
identity fields and fixed error/status codes. Stored provider errors, API keys,
URLs, document/query templates and arbitrary validation errors are omitted.

## Meaning of the report and plan

The global report separates source eligibility, missing/wrong-space metadata,
hash verification, vector presence, orphan/unknown rows, physical dimensions
and generation/config identity. Hash equality alone is not permission to reuse:
reusable-pair counts additionally require the physical dimension and an eligible
legacy identity or a matching managed generation.

Dry-run uses the current report plus the candidate document-space signature.
Query-only changes preserve the space; document/model/space-ID changes do not.
Dimension mismatch reports an offline rebuild requirement without reshaping.
Other actions distinguish no work, row repair, legacy adoption, generation
resume, source-error resolution and a clean space rebuild. Unknown or
structurally incoherent legacy indexes are never offered selective adoption.
All plans declare `scope=global`, `execution_supported=false` and no queued work.
They are advisory; conservative rebuild suggestions are not a command to delete
production data. The future executor may refine repairs under proven ownership.

## E2b2 admission protocol proposal (not active)

The existing `<db>.phase2.lock` covers startup schema work only. Neither that
short critical section, an idle queue, a PID file nor SQLite `BEGIN IMMEDIATE`
proves that peers have released old vec0 schema handles. E2b2 must acquire a
normal-runtime lease **before opening the research database**, retain it through
all initialization/requests/jobs, close the SQLite connection, then release it.
API, worker, direct Core CLI/database users and inspectors must participate.

Proposed portable implementation builds on existing nonblocking exclusive
file locks: a short admission gate plus one kernel-locked slot per live runtime
connection. Normal admission acquires the gate, opens/locks a unique slot, then
releases the gate. Maintenance holds the gate across the whole offline window
and proves every prior slot is unlocked before opening the database. New
runtime starts fail fast while maintenance holds the gate. Dead processes leave
unlocked slots; slot existence alone is not an active lease. Maintenance must
never unlink a slot or gate that could still be held. Cleanup requires the gate
and successful slot-lock acquisition; stale-slot cleanup is not a prerequisite
for correctness.

Lock paths must be canonical and shared across containers/processes; hard-link
aliases, symlink replacement, override disagreement and non-locking filesystems
must fail closed or be explicitly unsupported. Bootstrap races, acquisition
order (admission before Phase-2 before DB transaction), cancellation, descriptor
inheritance, close failures and process death need native tests on all three
OSes before integration. Maintenance must not promote itself from a runtime
lease while retaining old DB handles. An API/worker-held lease is contention,
not permission to kill that process.

Advisory locks cannot exclude old binaries or unrelated SQLite tools that never
join the protocol. The first controlled upgrade therefore requires an explicit
supervisor/operator stop and an agreed storage boundary; no compatible-peer
scan can certify the absence of uncooperative processes. E2c must refuse an
unproven deployment rather than silently assuming every peer was upgraded.

Tests required before calling E2b2 complete: two live readers coexist;
maintenance cannot enter while either reader/API/worker has a connection;
new readers cannot start during maintenance; owner death releases kernel locks;
failure/exception/cancellation closes the DB before releasing a lease; concurrent
first startup and maintenance cannot race; and Windows/Linux/macOS behavior is
verified natively. No offline schema-changing CLI is exposed before these gates
and E2c backup/recovery intent are in place.
