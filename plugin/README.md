<p align="center">
  <img src="assets/rka-project-plugin-app-icon.svg" alt="RKA Project mark" width="96">
</p>

# rka — Claude Code plugin for the Research Knowledge Agent

This is the official Claude Code plugin for [RKA](https://github.com/rka-project/rka-core). It lives inside the upstream RKA Core repository at `plugin/`, distributed via the local marketplace at `.claude-plugin/marketplace.json` in the repo root.

> **Quick install** — see [INSTALL.md at the repo root](../INSTALL.md). The TL;DR: install Git, uv, and Docker Compose v2; clone the repo; run `docker compose up -d` and `uv tool install --force --reinstall .`; then in Claude Code run `/plugin marketplace add /path/to/cloned/rka` followed by `/plugin install rka@rka`.

---

## What this plugin provides

| Surface | What | When |
|---|---|---|
| **MCP server** | Typed `rka_query` / `rka_execute` dispatch plus schema/help operations | Available in every Claude Code session after install |
| **3 skills** | Brain, Executor, and PI, plus the shared RWF v0 templates (Gap Note, Hypothesis & Test, Stop / Pivot Note) under `skills/brain/templates/` | Loaded on demand for role-specific guidance |
| **5 slash commands** | Status, search, pending, explicit project pinning, and Desktop setup | Quick shortcuts; setup is one-time |
| **SessionStart hook** | Cross-platform Python; reports backend reachability and reminds callers that project ids are explicit | Fires automatically on every new session |

Academic writing is deliberately outside this plugin. Install the separate
`rka-writer` plugin only in sessions where manuscript assistance is wanted;
it consumes RKA through the public MCP contract and is never auto-activated by
RKA Core.

---

## Cross-platform support

Everything in this plugin works on **macOS, Windows, and Linux**:

- **Wrapper script** (`bin/rka-mcp-bridge.py`): launched through `uv run --no-project`; no platform-specific Python command name is assumed.
- **SessionStart hook** (`hooks/session-start.py`): launched through uv and uses only Python's standard library for its bounded health check.
- **Setup helper** (`scripts/setup-claude-desktop.py`): launched through uv, OS-detects the right `claude_desktop_config.json` path, and performs an atomic merge with backup.

Path differences handled by the plugin:

| OS | Claude Desktop config | RKA `integration.json` |
|---|---|---|
| macOS | `~/Library/Application Support/Claude/claude_desktop_config.json` | `~/Library/Application Support/RKA/integration.json` |
| Windows | `%APPDATA%\Claude\claude_desktop_config.json` | `%APPDATA%\RKA\integration.json` |
| Linux | `~/.config/Claude/claude_desktop_config.json` | `~/.local/share/RKA/integration.json` (XDG) |

Microsoft Store install vs standalone `.exe` install of Claude Desktop on Windows: both write to the same `%APPDATA%\Claude\` path. The plugin handles them uniformly.

---

## Prerequisites

- **RKA backend running** — Docker via `docker compose up -d` from the rka repo. Verify with `curl http://127.0.0.1:9712/api/health` on macOS/Linux or `Invoke-RestMethod http://127.0.0.1:9712/api/health` in Windows PowerShell.
- **`rka` stdio binary OR `integration.json`** — the wrapper script needs one of:
  1. An optional `integration.json` written by a launcher/native app at the OS-specific path above, with `binary_path` pointing at a working `rka` binary and an explicit `backend_version`, OR
  2. `rka` on `PATH` (install via `uv tool install --force --reinstall .` from the rka repo — lands at `~/.local/bin/rka` on macOS/Linux, `%USERPROFILE%\.local\bin\rka.exe` on Windows).

  If neither is present, the wrapper exits with a clear error pointing at both options.
- **uv** — required for the wrapper, hook, and setup helper. It uses an existing Python 3 runtime or installs a managed one consistently across macOS, Windows, and Linux.

---

## Connecting Claude Desktop too

Run `/rka-setup-claude-desktop` from any Claude Code chat. It calls `scripts/setup-claude-desktop.py`, which:

1. Detects your OS and the right `claude_desktop_config.json` location.
2. Verifies the RKA backend is reachable (refuses setup if not, unless `--force`).
3. Backs up the existing config to `*.backup-YYYYMMDD-HHMMSS`.
4. Atomically merges the `mcpServers.rka` entry pointing at this plugin's Python wrapper.
5. Conflict-detects: refuses to replace an existing different entry without `--force`.
6. Restores from backup if any post-backup step fails.
7. Prints OS-specific quit-and-reopen instructions.

Then fully quit + reopen Claude Desktop. RKA tools become available in any new chat.

For natural-language equivalents, the `rka:rka-pi` skill teaches Claude Code to handle phrasings like *"set up RKA for Claude Desktop too"* by routing to the same command.

---

## Compatibility

| Plugin version | Compatible RKA versions | Wrapper compatibility glob |
|---|---|---|
| 2.0.0 (this branch) | 3.0.0 or newer | minimum `3.0.0` |

If an explicit `integration.json.backend_version` is below the minimum, the wrapper exits with a clear error message. A missing metadata file does not block startup; the wrapper resolves the installed binary directly.

---

## Development

This plugin lives at `plugin/` in the rka repo. The marketplace manifest is at `.claude-plugin/marketplace.json` in the repo root. To iterate on the plugin:

1. Edit files under `plugin/`.
2. From any Claude Code session: `/plugin uninstall rka@rka` then `/plugin install rka@rka` — install snapshots the source tree at install time, so changes to skills, commands, hooks, or the wrapper require this refresh.
3. Restart your Claude Code session to load the new cache.

For Claude Desktop, the wrapper picks up changes automatically (no install step), but you may need to fully quit + reopen Claude Desktop to clear its in-memory MCP server connection.

---

## Provenance

This plugin was scaffolded as part of the empirical-verification probe for plugin architecture (mission `mis_01KQNN8YZG7A4ZAGDCQ8ZVA97Z`, decision `dec_01KQNPC7A683HK0KRX1PAGNNED` — Option B: wrapper exec's local stdio binary, no HTTP MCP bridge). The probe's findings shape the v1.0 design; a future native launcher may automate the setup currently handled by `/rka-setup-claude-desktop`. Both ids are RKA knowledge-base entities; query them through the RKA dispatch surface or visit the corresponding entity in the web dashboard at `http://127.0.0.1:9712`.

Upstream RKA Core: [github.com/rka-project/rka-core](https://github.com/rka-project/rka-core)
