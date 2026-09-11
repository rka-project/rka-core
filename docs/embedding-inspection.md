# Inspect an embedding index without changing it

Available in the tagged Core 3.0.0 release (`3425a2b`), not every earlier
development build reporting `3.0.0`. Inspect/dry-run operate
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
`RKA_EMBEDDING_MODEL`, or initialize missing databases/configuration. Pass explicit paths
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
get around a failure. Inspect a controlled backup when a store cannot be fully
assessed within these limits; offline execution uses the 100,000-row/60-second
preflight ceiling and refuses an incomplete assessment.

Read-only SQLite inspection preserves WAL visibility and may use/create WAL
shared-memory sidecars and private `.runtime-locks` admission files. It does not grant exclusive maintenance ownership or
freeze other writers. Configuration is rechecked against the read snapshot;
execution revalidates later under exclusive maintenance ownership. Details:
[E2b admission design](superpowers/specs/2026-09-07-embedding-inspection-maintenance.md).

## Offline rebuild, interrupted recovery and rollback

These are **local operator commands**, not REST/MCP endpoints. They affect the
global derived index across every project. They never delete canonical research
records or rewrite long source text. There is deliberately no online force flag.

1. Install the same compatible Core revision for API, worker and admin CLI.
2. Prepare a candidate JSON config using the existing persisted schema. `dim`
   must be explicit and match the model. Preview it with `dry-run` first.
3. Stop **all** processes that can open this DB/config, including old versions,
   inspectors, standalone CLI processes and unrelated SQLite tools. Stop means
   closed connections, not merely idle workers. No command here kills peers.
4. Run `rebuild` from the same lock-capable shared storage namespace. The required
   `--peers-stopped` flag attests that you stopped non-cooperating/old clients;
   it does not bypass locks held by cooperating clients.
5. Restart API and worker. The worker performs/resumes the durable backfill.
   `prepared`/`queued` is **not** a ready-index result; check the generation/job
   status. Failed or oversized rows remain explicit and lexical search survives.

macOS/Linux:

```sh
rka admin embedding rebuild --data-dir /path/to/rka-data --target-config /path/to/candidate.json --peers-stopped --json
```

Windows PowerShell:

```powershell
rka admin embedding rebuild --data-dir "C:\Users\you\.rka" --target-config "C:\Users\you\candidate.json" --peers-stopped --json
```

Omit `--target-config` for same-space repair/adoption. Add `--db` consistently if
using a non-default database path. A newly initialized empty Core database is
supported; a missing/uninitialized DB must first be initialized normally.

For Docker Compose, stop this deployment's server **and worker**, then run a
one-off container from the newly built compatible image with the **same named
data volume**, persisted config and candidate file. `docker compose run --rm
--no-deps rka ...` can be used when `rka` is your API service name; override its
command with the complete `rka admin embedding rebuild ...` command. Do not use
`down -v`, mount a different volume, or assume the host's `/data` is the container's
data directory. Docker Desktop host bind mounts are not a supported substitute
for lock-coherent shared named volumes.

Before changes, recovery writes a verified SQLite backup, original/target config
snapshots and a checksummed manifest under `<database>.runtime-locks/<recovery-id>/`.
Backups are private and **retained**, including after success or failure. Protect
them like the research DB: config snapshots can contain API keys. Inspection and
CLI error output never print those credentials.

An interrupted operation leaves a durable intent that blocks new runtime
connections and normal config saves. Keep peers stopped and use one of:

```sh
rka admin embedding resume --data-dir /path/to/rka-data --peers-stopped --json
rka admin embedding rollback --data-dir /path/to/rka-data --peers-stopped --json
```

`resume` revalidates the backup/config/database identity and the transaction's
commit receipt, then finishes without incrementing a generation twice. `rollback`
restores the coherent pre-operation DB/config pair using SQLite's backup API,
not a file rename over WAL sidecars. An interrupted rollback resumes as rollback.
Once normal admission has reopened, this rollback command refuses to undo later
research edits. Keep the retained backup for a separately planned restore if needed.

Paths must be stable, operator-controlled regular files on a lock-capable shared
filesystem. Hard-linked database aliases, SQLite URI paths, symlinked lock files
and disagreeing container storage namespaces are unsupported. Resolved symlink
aliases share admission, but replacing paths while processes are open is not
supported. The protocol permits 128 simultaneous connections; normal gate
contention has a bounded one-second admission wait. Do not delete lock files to
resolve contention: kernel ownership, not file existence, determines live peers.

The backup hash detects changed bytes; it does not authenticate a malicious
operator who controls both the backups and their checksums. Keep old binaries
stopped: advisory locks cannot make unrelated software obey this protocol.
