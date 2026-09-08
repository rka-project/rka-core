# Inspect an embedding index without changing it

Unreleased E2b1 commands, available after installing this revision. They operate
on local files, not the REST API. They never start a model, test an endpoint,
save a candidate config, enqueue work or execute a rebuild.

```sh
rka admin embedding inspect --data-dir /path/to/rka-data --json
rka admin embedding dry-run --data-dir /path/to/rka-data --json
rka admin embedding dry-run --data-dir /path/to/rka-data --target-config /path/to/candidate.json --json
```

For a database outside the data directory, add `--db /path/to/research.db`.
The data directory must contain the real persisted `embedding_config.json`.
These commands intentionally do not load `.env`, infer the model from
`RKA_EMBEDDING_MODEL`, or create missing files/directories. Pass explicit paths
for Docker-mounted data as well; a host path is not a container `/data` path.

Windows PowerShell example (quote paths containing spaces):

```powershell
rka admin embedding inspect --data-dir "C:\Users\you\.rka" --json
```

macOS/Linux example:

```sh
rka admin embedding inspect --data-dir "$HOME/.rka" --json
```

The inspection includes all projects because the current index generation is
global. There is no `--project` or `--execute` switch. Without `--json`, output
is a short human-readable summary. Exit 0 means a complete inspection (which may
report problems); exit 2 with JSON output means an input/query error or incomplete
inspection. Absence of sqlite-vec is reported explicitly; install the supported
extension before relying on physical-vector checks.

Reports distinguish hash verification from reusable vector pairs, missing
coverage from corrupt/orphan metadata, and a ready generation from matching
configuration. Provider readiness is **not tested**. Read the `assessment` and
`plan` fields rather than treating exit 0 or a zero pending queue as a repair.
All plans are advisory and explicitly mark execution unsupported. A dimension
change is not performed by passing a target config.

Default limits are 50,000 source rows and 30 seconds; `--max-rows` (up to 100,000)
and `--timeout` (up to 60 seconds) adjust those bounds. Oversize rows (2 MiB
SQLite limit), config files over 64 KiB, unsupported older schemas or exhausted
budgets fail explicitly. Do not manually delete production embedding tables to
get around a failure. Inspect a controlled backup or wait for the supervised
offline recovery workflow when a store cannot be fully assessed within these
limits.

Read-only SQLite inspection preserves WAL visibility and may use/create WAL
shared-memory sidecars. It does not grant exclusive maintenance ownership or
freeze other writers. Configuration is rechecked against the read snapshot;
execution must revalidate later under the future admission protocol. Details:
[E2b design and remaining safety gates](superpowers/specs/2026-09-07-embedding-inspection-maintenance.md).
