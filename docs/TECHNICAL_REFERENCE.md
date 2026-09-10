# RKA Technical Reference

This is the compact entry point for RKA's command-line, MCP, REST, configuration, and development surfaces. Runtime schemas and the live OpenAPI document remain authoritative when they differ from static documentation.

## Runtime

The default Docker deployment starts:

| Component | Purpose | Address |
|---|---|---|
| RKA server | REST API and web dashboard | `http://127.0.0.1:9712` |
| Worker | Indexing and embedding jobs | internal |
| SQLite volume | Persistent project data | `/data/rka.db` in the container |

```bash
docker compose up -d
docker compose ps
docker compose logs -f rka
```

After source changes, rebuild rather than restarting:

```bash
docker compose up -d --build
```

## CLI

| Command | Purpose |
|---|---|
| `rka init <name>` | Initialize a workspace and project. |
| `rka serve` | Start the REST API (and dashboard when its built assets are present). |
| `rka worker` | Run background indexing and embedding jobs. |
| `rka start-all` | Start the REST API and worker as Dockerless background processes. |
| `rka stop-all` | Stop processes created by `rka start-all`. |
| `rka mcp` | Start the default stdio MCP adapter. |
| `rka mcp --transport http` | Disabled in Core 3.0.0; exits with an explanatory error. |
| `rka status` | Show current project status. |
| `rka backup` | Create an online, integrity-checked SQLite backup. |
| `rka migrate` | Run pending migrations. |
| `rka bootstrap scan <folder>` | Inspect a workspace before ingestion. |
| `rka bootstrap ingest <folder>` | Ingest an approved workspace scan. |
| `rka cred ...` | Initialize, inspect, and propagate the credential vault. |

`python -m rka` is equivalent to the `rka` console command and is the stable
entry point for launchers that do not rely on shell `PATH`. Use `rka --help`
and `rka <command> --help` for the installed version's complete option set.
See [CORE_RECOVERY.md](CORE_RECOVERY.md) for the disposable upgrade,
knowledge-pack, and pinned-runtime rollback procedure.

## MCP

The MCP binary is a thin proxy to the REST API configured by `RKA_API_URL`, which defaults to `http://127.0.0.1:9712`.

### Default dispatch tools

| Tool | Purpose |
|---|---|
| `rka_query` | Read and navigation operations. |
| `rka_execute` | Writes and lifecycle transitions. |
| `rka_describe` | Runtime operation index and typed schemas. |
| `rka_load_tools` | Load deferred compatibility tools when required. |
| `rka_help` | Discovery and help alias. |

Use `rka_query(args={"operation": "capabilities"})` to discover the connector and
Core versions, the supported `rka-core/v1` contract, REST/MCP interface
contracts, runtime capabilities, and connector/backend compatibility. Optional
`required_contract` and `required_capabilities` fields fail with structured
guidance when the current runtime cannot satisfy them.

Use `rka_describe("")` for the default operation index and
`rka_describe("<operation>")` for exact required fields, enums, usage-readiness
maturity, and deprecation guidance. The historical `stable`/`preview` maturity
axis reflects current production use, not a versioned compatibility promise;
explicit deprecation is reported separately. Pass `include_preview=true` only
when intentionally exploring preview or compatibility operations. This runtime
description is authoritative and should be preferred over copied operation
lists.

### Legacy Writer compatibility

The manuscript, planning, semantic-patch, and historical reference-validation
operations retained in Core are frozen compatibility surfaces. They remain
callable so existing projects can read, audit, and migrate legacy Writer state,
but they are deprecated, omitted from the default stable MCP tool listings,
and marked `deprecated: true` in OpenAPI. Calls also receive an out-of-band
notice through MCP logging or the REST `X-RKA-Compatibility-Status`,
`X-RKA-Removal-Milestone`, and `Link` headers; response bodies are unchanged.
New manuscript and Workbench development belongs in
[`rka-project/rka-writer`](https://github.com/rka-project/rka-writer).
Removal is not part of E2.4: it is gated on E2.3 export verification, downstream
readiness, the E5 slimming milestone, and a future explicitly approved breaking
release. No removal version or date is currently scheduled.

Project-scoped operations require an explicit `project_id`. `list_projects`,
`capabilities`, `health`, `create_project`, and `reset_session` are intentionally
unscoped.

### Transport modes

- **stdio:** `rka mcp`
- **HTTP/SSE:** disabled in Core 3.0.0.

The REST API is trusted-local, not an authenticated hosting service. Remote
connectors and tunnels are deferred. See [REMOTE_ACCESS.md](REMOTE_ACCESS.md).

## REST API

- Base URL: `http://localhost:9712/api`
- OpenAPI UI: `http://localhost:9712/docs`
- Health: `http://localhost:9712/api/health`
- Capability discovery: `http://localhost:9712/api/capabilities`

Most endpoints are project-scoped. Pass the project identifier through the documented header or query parameter. Use the live OpenAPI schema for exact request and response models.

`GET /api/capabilities` is additive and versioned by
`schema_version=rka.core-capabilities/v1`. It preserves the historical top-level
`embedding` block and keeps the removed `llm` field absent. Repeat the
`required_capability` query parameter to require multiple runtime capabilities;
unsupported contracts or combinations return HTTP 409 with the supported
contract/capability set and a recovery hint.

Major route families include projects, status, notes, decisions, literature, missions, checkpoints, claims, clusters, research maps, freshness, search, context, graphs, artifacts, registered sources, workspace ingestion, audit history, and knowledge-pack import/export. Registered-source routes preserve bytes or locator manifests under `/api/sources`; only the separate admission route can connect a reviewed artifact interpretation to an existing canonical target.

## Configuration

Core runtime settings use the `RKA_` prefix. Common settings include:

| Variable | Default | Purpose |
|---|---|---|
| `RKA_API_URL` | `http://127.0.0.1:9712` | REST target used by MCP. |
| `RKA_HOST` | `127.0.0.1` | API bind address outside Docker. |
| `RKA_PORT` | `9712` | API port. |
| `RKA_DATA_DIR` | `~/.rka` | Persistent data root outside Docker; Docker sets `/data`. |
| `RKA_DB_PATH` | `<RKA_DATA_DIR>/rka.db` | Explicit database override; relative values use `RKA_PROJECT_DIR`. |
| `RKA_EMBEDDINGS_ENABLED` | `false` | Enable optional embedding generation; Docker sets `true`. |
| `RKA_MANUSCRIPT_WORKSPACE_ROOTS` | empty | `os.pathsep`-separated allowlist for local Markdown/LaTeX source access; an empty value disables source synchronization. |
| `RKA_MANUSCRIPT_SOURCE_MAX_BYTES` | `2097152` | Maximum UTF-8 bytes accepted for one synchronized manuscript source file. |
| `RKA_REGISTERED_SOURCE_MAX_BYTES` | `52428800` | Maximum bytes copied into one registered source artifact (hard cap 500 MiB). |
| `RKA_SKILL_TOOLS` | unset | Promote ChatGPT skill-adapter tools on the connector surface. |
| `RKA_LEGACY_TOOLS` | unset | Restore the compatibility always-on tool surface when required. |

Store credentials with `rka cred`; do not commit secrets to `.env` files. See [CRED_VAULT.md](CRED_VAULT.md).

Embedding backends are configured through the dashboard or persistent embedding configuration. See [embedding_backends.md](embedding_backends.md).

## Data model

RKA uses prefixed ULIDs to keep entity identity explicit and sortable. Core prefixes include:

| Entity | Prefix |
|---|---|
| Project | `prj_` |
| Journal entry | `jrn_` |
| Decision | `dec_` |
| Literature | `lit_` |
| Mission | `mis_` |
| Checkpoint | `chk_` |
| Claim | `clm_` |
| Evidence cluster | `ecl_` |
| Entity link | `lnk_` |
| Workspace scan | `scn_` |

Every write emits audit/event information with the responsible actor. Valid actor values are `brain`, `executor`, `pi`, `llm`, `web_ui`, and `system`.

## Knowledge packs

Projects can be exported and imported as portable knowledge packs. Import creates a separate project and remaps project-scoped entity IDs and internal references. Export/import behavior must be covered by tests whenever a new persisted entity or relationship is introduced.

## Development

Run the independently selectable Core profile from a development environment:

```bash
python -m pip install -e ".[embeddings,academic,workspace,dev]"
python -m pytest -q --tb=short --strict-markers \
  -m "not writer and not agentic"
```

The exact dependency, ownership, full-compatibility, and startup-smoke commands
are documented in [RKA Core install and test profile](CORE_PROFILE.md).

The principal source directories are:

```text
rka/models/      typed domain models
rka/services/    shared business logic
rka/api/         FastAPI adapters
rka/mcp/         MCP adapter and operation schemas
rka/db/          schema and migrations
rka/skills/      packaged role skills
web/             React dashboard
tests/           service, API, MCP, workflow, and serialization tests
```

Before modifying the repository, read [`CLAUDE.md`](../CLAUDE.md). It is the authoritative working-instructions file for both human contributors and coding agents.

## Further documentation

- [Installation](../INSTALL.md)
- [Usage Guide](../USAGE_GUIDE.md)
- [User Manual](USER_MANUAL.md)
- [Architecture](ARCHITECTURE.md)
- [Embedding Backends](embedding_backends.md)
- [Credential Vault](CRED_VAULT.md)
- [ChatGPT Connector](CHATGPT_CONNECTOR.md)
- [MCP tool-surface design history](v2.6.x-v2.7.0-tool-surface-arc.md)
