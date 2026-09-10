# Core 3.0.0: release scope and upgrade runbook

This is a **local-first** Core release. Use local STDIO MCP with Codex, Claude
Code or Claude Desktop, and keep REST/dashboard on host loopback. It is not a
remote/public hosting release. See [remote access](REMOTE_ACCESS.md).

## What changes from 2.8.1

- Core is the durable memory/evidence service. New Writer work belongs to the
  separate Writer repository; Agentic is unsupported.
- Durable, generation-fenced embedding jobs run in the worker. Native inference
  is isolated in a spawned process. Offline inspection, rebuild, resume and
  rollback preserve verified backups. See [embedding operations](embedding-inspection.md).
- Journal edits are auditable. Original wording and attribution have explicit
  capture modes and revision-guarded correction history. Legacy unknown originals
  remain unknown. See [journal attribution](JOURNAL_ATTRIBUTION.md).
- Currentness/review reopening, lifecycle propagation, project-scope guards and
  knowledge-pack referential checks include the merged I-series fixes.
- Path-based reads now require explicit host/server allowroots, defaulting to
  none. See [file access](FILE_ACCESS.md). Existing content is not rewritten.
- Remote HTTP/SSE MCP, the legacy OAuth proxy and tunnel launcher are disabled.
  Local STDIO requires no MCP TCP listener. Existing remote processes must be
  stopped by their operator; changing source does not stop them.
- New credential manifests require no API keys and derive Core pins from the
  installed version. Existing credentials/manifests/pins are preserved. Review
  old `versions.toml` pins after upgrading; `cred init` does not reset them.

## Fresh installation

Follow [INSTALL.md](../INSTALL.md). Clone the release tag, not an arbitrary
moving main checkout. Python 3.11 and 3.13 on macOS, Windows and Linux are the
native wheel test targets. The base wheel provides REST/STDIO and lexical
storage; the separately built dashboard and embeddings are not bundled in that
base wheel. Docker is the full profile with dashboard and worker.

The release image is `ghcr.io/rka-project/rka-core`. Use the immutable manifest
digest recorded by a **successful** release workflow, not an invented digest
or an unverified `latest` tag. GitHub release existence alone does not prove
the container has finished publishing or is anonymously pullable.
This release does not publish to PyPI.

## Existing installation: coordinated upgrade

1. Record the exact old source/image digest, installed CLI version, database
   path, data directory, embedding configuration, client configurations and
   filesystem allowroots. For Compose also record its project name and actual
   named volume from `docker compose config --volumes` and container mounts.
   Keep this private: rendered Compose/configuration may contain secrets.
2. Prepare the new tagged source/image separately; preserve dirty worktrees.
   Do not point it at the real database yet. Rehearse on a disposable backup
   when your installation contains custom integrations or older schemas.
3. Stop this installation's API, worker, local MCP clients and any old remote
   proxies/tunnels. In its existing Compose context:
   `docker compose stop rka rka-worker`. Old binaries do not participate in
   the new lifetime-admission protocol; their stop must be coordinated.
4. Create a consistent SQLite backup with the verified Core `rka backup`
   command using the **explicit actual database path**. For a native setup,
   set `RKA_DB_PATH` to that absolute path and run
   `rka backup --output <new-private-backup.db>`. On PowerShell use
   `$env:RKA_DB_PATH = 'C:\path\to\rka.db'`; on POSIX use
   `RKA_DB_PATH=/absolute/path/rka.db rka backup --output /absolute/backup.db`.
   Check that the output names the intended source-derived backup and reports
   a SHA-256. A missing database or unexpected integrity finding is a stop gate,
   not a successful backup. Retain the pre-upgrade config and artifact files
   too; a SQLite snapshot is not a full data-directory backup.
5. For Compose, use a stopped, no-port, one-off container from the verified image
   with the **same named data volume**, explicit `RKA_DB_PATH=/data/rka.db`,
   and a new backup filename. Do not replace a volume with a host bind mount,
   silently change the Compose project name, or use `down -v`.
   Preserve embedding settings and operator overrides when changing source.
6. Upgrade the backend and worker together. Compose builds need
   `docker compose up -d --build` in the approved deployment context, or use
   a reviewed digest-pinned override. `restart` does not install new code.
   Reinstall local STDIO from the same release checkout with
   `uv tool install --force --reinstall .`, then fully restart the MCP clients.
7. Verify `rka --version` and `GET http://127.0.0.1:9712/api/health` both report
   `3.0.0`. Check the dashboard (Docker/full profile), a local MCP project query,
   exact journal text/attribution, lexical retrieval and background job progress.
   A health response or queued job does not mean vector backfill is complete.
   Native installations with embeddings need a separate `rka worker`.
8. Do not change a populated embedding space online. If inspection requests an
   offline transition, keep peers stopped and use the explicit
   [rebuild/resume/rollback workflow](embedding-inspection.md). Never delete
   locks or recovery markers to bypass it.

## Rollback

Code rollback is not database rollback. Keep all peers stopped; restore the
verified **pre-upgrade DB/config pair** into a separate private data directory
or replacement named volume, then start the pinned old runtime against that
restored copy. Preserve the failed upgraded store for diagnosis. Do not run old
code directly on a migrated database, copy a bare live DB while ignoring WAL,
or overwrite newer research edits. A post-upgrade rollback needs a data-recovery
plan if new writes have already occurred.

## Historical upgrade acceptance

`scripts/historical_upgrade_smoke.py` regenerates synthetic records using each
pinned historical checkout's schema and write services in its own subprocess:

| Baseline | Commit | Purpose |
|---|---|---|
| Last formal release 2.8.1 | `5c7da7125dd6c0996b7e9a740c3fea35dbdcdfe0` | Released-user upgrade path |
| Last pre-split 2.9.0 source | `0dc1842ca771962e6e07676b6de9a6abecd74ac9` | Representative upgrade-before-#158 path; reporter's exact old SHA is unknown |

Each fixture has a project plus the default project, three journals (including
supersession, PI original text and a directive), one decision, one claim,
three graph links, tags, events/audit rows and one synthetic 768-dimensional
legacy vector. IDs/timestamps are fixed; reviewed logical SHA-256 values are
embedded in the generator. File/archive checksums and migration names appear
in its JSON report. No personal database or demo pack is used.

The check preserves old columns/values, IDs/edges/vector bytes and metadata;
checks SQLite integrity and foreign keys; compares fresh/upgraded columns,
DDL, indexes and triggers; verifies repeated startup, lexical retrieval,
knowledge-pack round trips and old-runtime reopening of a restored backup.
The only allowed initial schema difference is two exact event query indexes
(`idx_events_caused_by`, `idx_events_phase`) restored by the next startup.
Fresh and upgraded DDL must agree after that startup.

Run from a full Git clone with Python 3.13 and locked Core extras:

```sh
uv sync --locked --extra embeddings --extra academic --extra workspace --extra dev
uv run --no-sync python scripts/historical_upgrade_smoke.py --report-dir /path/to/new-test-reports
```

This is old-source schema/service acceptance using current locked dependencies,
not installation of historical dependency environments, a whole-corpus memory
benchmark, or proof for every old database. Existing migration/pack tests cover
additional structures. Real Nomic/BGE resource and process-death evidence is
recorded in [E-series acceptance](superpowers/plans/2026-09-07-core-e-completion-validation.md).

## Remaining scope

Remote execution policy and Spaces/Codespaces demos remain deferred. Frozen
Writer/Agentic maturity metadata unification, broader historical fixtures,
dependency refreshes and desktop/service-manager packaging are follow-up work.
An image release does not close those items or issue #158 automatically.
