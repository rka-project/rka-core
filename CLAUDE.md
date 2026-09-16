# CLAUDE.md — RKA Codebase (Executor instructions)

This is the **Research Knowledge Agent (RKA)** source repository.
When working here you are modifying the tool itself, not using it for research.

---

## Stack

| Layer | Technology |
|---|---|
| MCP server | `rka/mcp/server.py` — FastMCP, stdio, thin HTTP proxy to REST |
| REST API | `rka/api/` — FastAPI, mounted under `/api/` |
| Services | `rka/services/` — all business logic, shared by MCP + REST |
| DB | SQLite + FTS5 + sqlite-vec, schema in `rka/db/schema.sql` |
| Web UI | `web/` — React + Vite + shadcn/ui, built to `web/dist/` (served by FastAPI) |
| CLI | `rka/cli.py` — `rka serve` (REST+UI), `rka mcp` (MCP stdio) |

## Key Conventions

- **Actor values**: `brain | executor | pi | llm | web_ui | system` — enforced by DB CHECK constraint
- **ID prefix**: `jrn_` journal, `lit_` literature, `dec_` decision, `mis_` mission, `clm_` claim, `ecl_` evidence cluster, `chk_` checkpoint, `prj_` project, `lnk_` entity link, `scn_` scan
- **MCP tools**: all prefixed `rka_`, defined in `server.py` via `@mcp.tool()`
- **MCP prompts**: defined at end of `server.py` via `@mcp.prompt()`
- **API routes**: thin adapters only — no business logic, always delegate to service layer
- **Tests**: Core is `python -m pytest --strict-markers -m "not writer and not agentic"`; see [`docs/CORE_PROFILE.md`](docs/CORE_PROFILE.md)
- **v2.7.0+ tool surface (MCP)**: 3 always-on dispatch tools (`rka_query` / `rka_execute` / `rka_describe`) + 2 escape hatches (`rka_load_tools` / `rka_help`) = 5 broadcast tools. Typed Pydantic operation models in `rka/mcp/operation_args.py` provide per-branch enum + required-field enforcement at the FastMCP `inputSchema` layer (`oneOf` with `discriminator='operation'`). Use `rka_describe("")` for the live operation index and counts instead of copying a static total into agent guidance. The legacy tools and v2.7.0a2 verbs remain at `tier='deferred'`, callable via `rka_load_tools`; `RKA_LEGACY_TOOLS=1` is retained only for historical callers and is not the normal surface. Setting `RKA_SKILL_TOOLS=1` promotes the three ChatGPT skill-adapter tools (`rka_list_skills` / `rka_read_skill` / `rka_start_session`) to always-on (8-tool surface); this is historical adapter compatibility, not permission to expose MCP remotely. Core 3.0.0 disables HTTP/SSE MCP and the old OAuth/tunnel entry points; see [`docs/REMOTE_ACCESS.md`](docs/REMOTE_ACCESS.md). Normal local stdio clients leave it unset. The historical design arc is documented in [`docs/v2.6.x-v2.7.0-tool-surface-arc.md`](docs/v2.6.x-v2.7.0-tool-surface-arc.md) and `CHANGELOG.md`.
- **Credential management**: use `rka cred` subcommands (`init` / `set` / `get` / `env` / `propagate` / `check`) — vault lives at `~/.config/rka/creds.env` (XDG-compliant, mode 0600). Never commit creds to any repo or `.env` tracked by git. Full reference: [`docs/CRED_VAULT.md`](docs/CRED_VAULT.md).

## Active product boundary

- RKA Core owns durable research records, provenance, integrity, retrieval,
  migrations, and public REST/MCP contracts.
- New manuscript, Writer, or Workbench behavior belongs in
  [`rka-project/rka-writer`](https://github.com/rka-project/rka-writer), not
  this repository. Legacy manuscript/Workbench surfaces in Core are frozen;
  change them only for correctness, security, migration, or compatibility.
- Agentic orchestration is shelved and unsupported. Do not add new Agentic
  runtime, packaging, installation, or feature work without a new explicit PI
  decision; see [ADR 0013](docs/adr/0013-shelve-agentic-and-focus-core-writer.md).
- Core must install, start, and test without either downstream product.

## Running and verification

```bash
# Start all services (API + web dashboard + background worker)
docker compose up -d

# View logs
docker compose logs -f rka

# Rebuild after code changes
docker compose up -d --build

# Run the Core test gate from a development environment
python -m pytest -q --tb=short --strict-markers \
  -m "not writer and not agentic"

# Verify migrations, REST, MCP, worker, sqlite-vec, and built dashboard
python scripts/core_startup_smoke.py --require-web --require-vec

# Rebuild web UI after frontend changes (done automatically during docker build)
# For local iteration: cd web && npm run build, then rebuild container
```

## After Frontend Changes

The web UI is built during `docker build`. To apply frontend changes:
```bash
docker compose up -d --build
```

## MCP Configuration

The MCP binary is installed via `uv tool` (outside the Docker container) because
Claude Desktop/Code needs a local stdio process. It proxies all calls to the
Docker container's REST API.

```bash
# Install / re-install after code changes:
uv tool install --force --reinstall .   # from repo root, all platforms
# Binary: ~/.local/bin/rka (macOS/Linux) or %USERPROFILE%\.local\bin\rka.exe (Windows)
```

`~/Library/Application Support/Claude/claude_desktop_config.json`:
```json
"rka": { "command": "/Users/<user>/.local/bin/rka", "args": ["mcp"] }
```

After code changes to `rka/mcp/server.py` or other source files:
1. `uv tool install --force --reinstall .` — update the MCP binary
2. `docker compose up -d --build` — update the API server + worker

## Common Pitfalls

- `actor="import"` is not a valid actor — use `actor="system"` for programmatic ingestion
- The MCP server is stateless; it proxies all calls to the REST API at `RKA_API_URL` (default: `http://127.0.0.1:9712`)
- `web/` previously had a nested `.git` — do not re-introduce submodule state there
- Large files (>10 MB) use fast composite hashing; text files are capped at 200K chars in scan
- The database lives in the Docker volume `rka-data` at `/data/rka.db` — do not use a local `rka.db`
- Keep multi-container Docker deployments on a shared **named volume**. Phase-2 sqlite-vec startup serializes server/worker with the cross-platform advisory lock `<db>.phase2.lock`; Docker Desktop host bind mounts do not reliably propagate filesystem locks across containers. If a custom deployment cannot colocate the lock with the DB on a lock-capable shared filesystem, set `RKA_PHASE2_LOCK_PATH` to one that is shared by every RKA process.
- Database clients now also retain a lifetime slot under `<db>.runtime-locks`. Offline embedding maintenance owns the admission gate and requires all API/worker/CLI and old/uncooperative peers stopped. Never delete lock files or unfinished recovery markers to bypass contention. Use `rka admin embedding rebuild/resume/rollback` with explicit paths; see [the operator workflow](docs/embedding-inspection.md). Phase-2 lock overrides do not relocate lifetime locks. Native FastEmbed runs in a spawned child with effective 2 KiB input / 4 KiB batch-padding ceilings. HTTP backends default to 16 KiB prepared inputs, retaining 16 KiB batch/padding ceilings and explicit tighter saved limits; see [resource limits](docs/embedding_backends.md#resource-limits-unreleased-hardening).
- There is no local `.venv` — all server/worker processes run in Docker
- `docker compose restart` does **not** reload service code — always use `docker compose up -d --build` for any change under `rka/`. Restart only suffices for migration-only changes (the migration runner queries `schema_migrations` on startup).
- **Auto-mode push-to-main is gated**: when an Executor session runs in auto-mode (no per-tool-call confirmation), direct `git push origin main` is blocked by a safety classifier and requires explicit PI authorization in the transcript (a sentence like *"push to main"* or *"go ahead and push to main"*) to unblock. For any main-branch mission spec, the T5/release task should anticipate this and PI should plan to ratify the push step explicitly. The block is one-shot per push — after PI authorizes, the push proceeds and subsequent operations resume normal flow. Surfaced empirically during D4 mission (v2.5.4) per `dec_01KS0BVPCYK4CBG5TKKG1QK4HM` close-out.
- **v2.7.0 dispatch surface (current MCP shape)**: only 5 tools broadcast on connect — 3 always-on dispatch (`rka_query` / `rka_execute` / `rka_describe`) + 2 escape hatches (`rka_load_tools` / `rka_help`). Query the live branch inventory with `rka_describe("")`; do not rely on a copied operation count. Legacy tools remain tier=`deferred`; load them on demand via `rka_load_tools(names=[…])`. `RKA_LEGACY_TOOLS=1` exists only for historical compatibility and should not be enabled for normal clients. Enum hallucinations and missing required fields are caught at the `inputSchema` `oneOf` branch level before leaving the client. The historical surface arc is in [`docs/v2.6.x-v2.7.0-tool-surface-arc.md`](docs/v2.6.x-v2.7.0-tool-surface-arc.md).

## Embedding backend configuration (v2.4.0+)

Pluggable embedding backends (FastEmbed, OpenAI-compat HTTP, Ollama) configurable
in the web UI at **Settings → Embeddings**. Persistent config at
`/data/embedding_config.json` (file-mode 0600, owner-readable only because of the
optional `api_key`). Full reference: [`docs/embedding_backends.md`](docs/embedding_backends.md).

LLM-driven features (`rka_ask`, `rka_generate_summary`, web-UI Q&A page) were
removed in v2.4.0 per `jrn_01KRNZBS50K250HHHHEC58E4GC`. RKA Core does not run
server-side synthesis; connected clients may synthesize from retrieved records.
`/api/capabilities` no longer returns the `llm` field (BREAKING-IN-MINOR;
documented in `CHANGELOG.md`).

## macOS AppleDouble Quirks (external / network / sync volumes)

On macOS, certain volumes — external drives, SMB/AFP network mounts, OneDrive / Dropbox / iCloud sync folders, and some case-insensitive filesystems — don't fully support extended attributes. macOS works around this by creating `._*` AppleDouble companion files alongside every file Python tools write (in `build/`, `rka.egg-info/`, project root). These break both `docker compose build` (fails with "failed to xattr ... operation not permitted") and `uv tool install` (fails with "No such file or directory: '._requires.txt'") even with `COPYFILE_DISABLE=1` set. If you cloned the repo into `~/Documents` on a stock APFS volume you'll never see this; if you cloned it onto an external drive or a synced folder, you will.

**Before any rebuild**, purge resource-fork files:

```bash
find . -maxdepth 2 -name '._*' -not -path './.git/*' -delete
```

**If `uv tool install --force --reinstall .` still fails on `._requires.txt`** (the build process re-creates AppleDouble files in `build/` on the fly), install from a `/tmp` clone instead — `/tmp` is on a stock APFS volume that doesn't have the xattr quirk:

```bash
rm -rf /tmp/rka-build && git clone -q --depth 1 "$PWD" /tmp/rka-build
cd /tmp/rka-build && UV_CACHE_DIR=/tmp/uv-cache uv tool install --force --reinstall .
```

**If `docker compose build` succeeds but the new image isn't picked up**, force-recreate:

```bash
docker compose up -d --force-recreate
```

(Plain `docker compose up -d --build` can hit the build cache or skip recreation if Compose decides nothing changed; `--force-recreate` is the reliable post-rebuild path.)
