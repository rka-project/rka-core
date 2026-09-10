# RKA Cred Vault — Phase 1

Local-first credential management for RKA. Credentials live on your
host machine, **outside any git repository**, with file-mode 0600
enforced and drift detection against every consumer that needs them.

The numeric modes below describe POSIX permissions. On Windows, keep the vault
under your private user profile and verify its NTFS access permissions; POSIX
`chmod` bits are not a Windows ACL guarantee.

## Why

RKA has multiple consumers of the same credentials (the Zotero API
key shows up in at least four places: Claude Desktop config, Claude
Code's `~/.claude.json`, the rka-server's persisted config, and the
orchestrator's `.env`). Hand-editing each one drifts and is
error-prone. Cred Vault is the single source of truth + a probe
fleet that asserts every consumer matches.

Phase 1 scope: global creds only. Phase 2 will add per-project
addons under `~/.config/rka/projects/<slug>/`.

## File layout

The vault lives at `$XDG_CONFIG_HOME/rka/`, falling back to
`~/.config/rka/` when `XDG_CONFIG_HOME` is unset. The directory mode
is `0700`; `creds.env` is `0600`.

```
~/.config/rka/                              dir mode 0700
├── creds.env                               mode 0600
│                                           # Plain KEY=VALUE; one per line.
│                                           # Order + comments preserved on
│                                           # `rka cred set/unset`.
├── projects/                               Phase 2 (empty in Phase 1)
├── manifest.toml                           declarative cred requirements
└── versions.toml                           expected binary + container versions
```

Default `manifest.toml`:

```toml
[global]
required = []
optional = ["ZOTERO_API_KEY", "ZOTERO_LIBRARY_ID", "ZOTERO_LIBRARY_TYPE", "SEMANTIC_SCHOLAR_API_KEY", "SERPAPI_KEY"]
```

Default `versions.toml`:

```toml
[host.binaries]
rka = "3.0.0"

[containers]
"rka-server" = "3.0.0"
```

New defaults track the installed Core package version and do not require Zotero
or the shelved orchestrator. Core works with an empty vault. Existing manifests,
version pins and credential values are **not overwritten** by `rka cred init`.
After upgrading an existing deployment, explicitly update its Core pins in
`versions.toml` only after checking the installed CLI and backend versions.
Interactive credential input is hidden. Historical orchestrator consumers below
remain compatibility-only, not part of the supported Core install.

## CLI surface

| Command                                  | Behavior                                                                                                  |
| ---------------------------------------- | --------------------------------------------------------------------------------------------------------- |
| `rka cred init`                          | Interactive bootstrap. Prompts for each `required` then `optional` cred. Writes default manifest/versions if missing. `--non-interactive` to skip prompts. |
| `rka cred set KEY VALUE`                 | Set a key. Idempotent. Preserves comment + line ordering.                                                 |
| `rka cred unset KEY`                     | Remove a key. Idempotent.                                                                                 |
| `rka cred get KEY [--show]`              | Print value. Default is `***`; pass `--show` to unmask. Exit 1 when the key is unset.                     |
| `rka cred env [PROJECT] [--format=…]`    | Emit resolved env. Formats: `dotenv` (default), `json`, `shell`. Phase 1 ignores `PROJECT`.               |
| `rka cred propagate [--apply]`           | Push creds.env to all consumers. Dry-run by default; `--apply` actually writes.                           |
| `rka cred check [PROJECT]`               | Run all drift probes. Exit 0 when all PASS/SKIP, exit 1 if any FAIL.                                      |

### Propagation consumers (Phase 1)

| Consumer               | Target                                                              | Notes                                                                                          |
| ---------------------- | ------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------- |
| `claude_desktop`       | `~/Library/Application Support/Claude/claude_desktop_config.json`   | Merges `mcpServers.zotero.env`; preserves all other entries.                                  |
| `claude_code_json`     | `~/.claude.json`                                                    | Same shape as Claude Desktop.                                                                  |
| `rka_server_rest`      | `PUT http://127.0.0.1:9712/api/config/zotero`                       | Server probes Zotero before persist — doubles as a validity check on the supplied API key.     |
| `orchestrator_env_file`| `orchestrator/.env` (or `$HOST_ORCH_ENV`)                           | Overwrites `ZOTERO_*` keys only. **`ANTHROPIC_API_KEY` is excluded** per design.                |

`--apply` performs an atomic tmp+rename write at mode 0600. A
`would_change` (dry-run) result reports diffs as
`KEY: <old_redacted> -> <new_redacted>`.

### Drift probes (Phase 1)

| Probe                       | What it checks                                                                                       |
| --------------------------- | ---------------------------------------------------------------------------------------------------- |
| `manifest_coverage`         | Every `manifest.global.required` key resolves to a non-empty value in `creds.env`.                   |
| `claude_desktop`            | `mcpServers.zotero.env` in Claude Desktop config matches `creds.env`.                                |
| `claude_code_json`          | `mcpServers.zotero.env` in `~/.claude.json` matches `creds.env`.                                     |
| `rka_server_zotero`         | `GET /api/config/zotero` matches `creds.env`. (API key on the wire is `***`; presence-check only.)   |
| `rka_server_env`            | `docker exec rka-server env` matches creds.env. **Empty is NORMAL** post-v2.7.0.2; only non-empty mismatches FAIL. |
| `rka_orchestrator_env`      | `docker exec rka-orchestrator env` matches creds.env. SKIPs when the container isn't running.        |
| `host_rka_version`          | `rka --version` matches `versions.toml[host.binaries].rka`.                                          |
| `rka_server_health`         | `GET /api/health` `.version` matches `versions.toml[containers].rka-server`.                          |
| `rka_orchestrator_version`  | `docker exec rka-orchestrator grep version /app/orchestrator/pyproject.toml` matches versions.toml.   |

SKIP means "preconditions not met in this environment" (e.g. rka-server not
running, Claude Desktop not installed). SKIP is not a failure, but is **not proof of a healthy installation**. Verify
`GET /api/health` and an actual local MCP query separately.

## Quick-start

```bash
# 1. Bootstrap the vault. Prompts for ZOTERO_API_KEY + ZOTERO_LIBRARY_ID.
rka cred init

# 2. See what propagate WOULD change.
rka cred propagate
#   propagate: DRY-RUN (no writes) — pass --apply to commit
#   * claude_desktop          would_change  would update 2 key(s) in mcpServers.zotero.env
#         ZOTERO_API_KEY: <empty> -> ab***xy
#         ZOTERO_LIBRARY_ID: <empty> -> 96***12
#   - claude_code_json        skipped       mcpServers.zotero not present
#   * rka_server_rest         would_change  would PUT /api/config/zotero
#   * orchestrator_env_file   would_change  would update 1 key(s) in orchestrator/.env

# 3. Apply.
rka cred propagate --apply

# 4. Confirm everything is in sync.
rka cred check
#   PROBE                      STATUS  EXPECTED                        FOUND
#   ...
#   summary: 7 pass, 0 fail, 2 skip
```

After `propagate --apply`, you may need to manually:

- Restart Claude Desktop / Claude Code to pick up new env vars.
- `docker compose -f docker-compose.yml -f orchestrator/docker-compose.yml
  up -d --force-recreate rka-orchestrator` to roll the orchestrator
  container.

The propagate output enumerates required manual steps under "Manual
rebuild needed".

## Phase 1 vs Phase 2

| Capability                                | Phase 1 | Phase 2 |
| ----------------------------------------- | ------- | ------- |
| Global creds (ZOTERO_*, etc.)             | yes     | yes     |
| Per-project addons under `projects/<id>/` | no      | yes     |
| `rka cred env PROJECT` resolves overlays  | no      | yes     |
| Per-project `.rka/.env` writes            | no      | yes     |
| Drift detection                           | yes     | yes     |
| Encrypted-at-rest (e.g. age/sops)         | no      | n/a     |

## Security posture

- Vault lives outside any git repo (XDG path on host).
- File mode 0600 on `creds.env`, 0700 on the directory. Enforced
  on every write; the test suite asserts this.
- Atomic writes (tmp + rename) so a crash mid-write can't corrupt
  `creds.env`.
- `propagate` never logs raw secret values; the diff lines use a
  fixed-width redactor (`ab***xy` style).
- `rka cred get KEY` defaults to `***`. The PI must opt-in to
  unmask via `--show`.

## Non-goals (Phase 1)

- Per-project addons (deferred to Phase 2).
- Encrypted-at-rest creds (deferred design choice).
- Sync-to-cloud / Vault server / 1Password backend.
- Auto-rotation of expired tokens.
- Anything not listed in the CLI surface table above.
