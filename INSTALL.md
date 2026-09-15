# RKA Installation Guide

> Core 3.0.1: local REST + STDIO MCP only. Remote connectors, tunnels and public
> Spaces are deferred. See [supported access boundaries](docs/REMOTE_ACCESS.md).
> Existing installations must back up first; see [the 3.0 upgrade runbook](docs/RELEASE_3_0.md).
> For this patch's changes and rollback notes, see [Core 3.0.1](docs/RELEASE_3_0_1.md).

> **How to read this guide**
> - **Humans**: read top-to-bottom; the quick install path is §3.
> - **A coding agent (Claude Code, Codex, or similar) asked to "install RKA" / "finish my RKA setup"**: this file is your runbook. Execute §3 in order; §6 (Claude Desktop wiring) and §7 (diagnosis) are procedures to run when the relevant step calls for them. Follow the **execution contract** in §0 before you start. §11 is retained only as an unsupported historical reference; do not install it.

## 0. Execution contract (read first if you are the installing agent)

You are running on a machine that already has Docker and a coding agent. Your job is to bring RKA up and wire it into the surfaces the user actually wants. Follow these rules:

1. **Confirm scope first.** Do **Step 0** in §3 before running anything — ask the user which surfaces to set up. Do not assume "all of them."
2. **Go in order; verify as you go.** After each step, check its **✅ success signal** before moving on. If a signal doesn't appear, **⛔ stop**, run the step's recovery/failure note, and surface the exact output — don't push forward on a broken step.
3. **Stop and ask at every 🟡 gate.** A **🟡 ASK THE USER** marker means the next action needs information or an action only the user can supply — an install location, an API key, a "yes, do the destructive thing." **Never invent, generate, or guess a secret, token, path, or scope choice.** Ask, wait for the answer, then continue.
4. **Never write secrets into the repo or a chat transcript.** Tokens, passphrases, and API keys go only into the user's local credential vault (`rka cred`, see §5.5) or their own shell/env. If the user pastes a secret to you, use it in memory only; do not echo it back or commit it.
5. **Idempotence.** Every step is safe to re-run. If something is already configured correctly, report that and move on rather than clobbering it.

**Legend used throughout:** ✅ = success signal to confirm before proceeding · 🟡 = stop and ask the user · ⛔ = stop on failure and surface the error.

---

## 1. What you get when you're done

| Surface | What you'll have |
|---|---|
| **RKA backend** | FastAPI + worker + SQLite + FTS5 + sqlite-vec running in Docker on `127.0.0.1:9712`. Web dashboard at the same URL. |
| **Claude Code (Executor)** | Core plugin: 3 role skills (`rka:rka-brain`, `rka:rka-executor`, `rka:rka-pi`), the credential setup utility, 5 slash commands (`/rka-status`, `/rka-search`, `/rka-pending`, `/rka-set-project`, `/rka-setup-claude-desktop`), a SessionStart backend check, and the typed MCP dispatch surface. |
| **Writer (optional, separate)** | Install the explicit-only [`rka-writer`](https://github.com/rka-project/rka-writer) plugin only when manuscript drafting or revision is wanted. It is not included in or activated by RKA Core. |
| **Claude Desktop (Brain)** | Typed RKA tool surface via the `mcpServers.rka` entry in `claude_desktop_config.json`. Wrapper-based config gives version checking; every scoped operation still requires an explicit project id. Skills and slash commands are Claude Code only (Claude Desktop's plugin format is separate). |
| **Remote connectors** | Unsupported in Core 3.0.0; see [remote access status](docs/REMOTE_ACCESS.md). |

For contributor installs, dependency ownership, the Core-only pytest selector,
and the disposable startup gate, see
[`docs/CORE_PROFILE.md`](docs/CORE_PROFILE.md). The production Docker image uses
that Core dependency profile and does not install legacy LLM-provider SDKs.

### 1.1 Core tool surface (v3.x)

**Path-reading upgrade note (3.0.0):** record retrieval, pasted
text and explicit byte uploads do not need filesystem permission. For local
workspace scan/bootstrap or source `filepath`, the operator must first authorize
specific input directories in the MCP process's `RKA_HOST_FILE_ROOTS`. Direct
REST path reads use the separate `RKA_SERVER_FILE_ROOTS`. Both default to `[]`;
installers must not auto-authorize a home directory or infer permission from
`project_dir`. See [File access](docs/FILE_ACCESS.md) for macOS/Linux, Windows,
container examples, limits, restart requirements and a synthetic verification.

In Claude Desktop and Claude Code, you'll see **5 always-on `rka` tools** at session start:

| Tool | Role | Operations behind it |
|---|---|---|
| `rka_query` | Read dispatch (the "search/list/get" entry point) | Typed read operations; inspect the live catalog with `rka_describe` |
| `rka_execute` | Write dispatch (the "add/update/create/submit" entry point) | Typed write/lifecycle operations; inspect the live catalog with `rka_describe` |
| `rka_describe` | Schema lookup + examples for any operation | — |
| `rka_load_tools` | Escape hatch: register legacy tool aliases for the rest of the session | — |
| `rka_help` | Alias for `rka_describe` (mnemonic surface) | — |

The typed Pydantic operations under `rka_query` / `rka_execute` carry per-branch enum and required-field enforcement at the **FastMCP schema layer**, so invalid values are rejected before dispatch. `rka_describe("")` is the authoritative operation index; do not rely on a copied count in documentation. Legacy per-tool aliases are `tier=deferred` and load on demand via `rka_load_tools`; `RKA_LEGACY_TOOLS=1` remains only as a compatibility switch for historical callers.

---

## 2. Prerequisites (5 minutes if not already installed)

| # | Component | Why | Where |
|---|---|---|---|
| 1 | **Docker with Compose v2** | Runs RKA's FastAPI + worker in containers | [Docker Desktop](https://docs.docker.com/compose/install/) includes Docker Engine, the CLI, and Compose on macOS / Windows / Linux. Linux may instead use [Docker Engine + the Compose plugin](https://docs.docker.com/compose/install/linux/). Verify with `docker compose version` (the old `docker-compose` command is not supported by this guide). |
| 2 | **Claude Desktop** (optional) | Hosts the Brain (strategy + synthesis) | [claude.ai/download](https://claude.ai/download) — macOS / Windows / Linux beta. **Windows note**: install via Microsoft Store OR standalone `.exe`; both write the config to `%APPDATA%\Claude\`, so this guide works either way. |
| 3 | **VSCode** | Hosts the Claude Code extension | [code.visualstudio.com](https://code.visualstudio.com/) — macOS / Windows / Linux |
| 4 | **Claude Code extension for VSCode** | Hosts the Executor | VSCode → Extensions → search `Claude Code` → install. Or: `code --install-extension anthropic.claude-code` from a terminal. |
| 5 | **Python 3 runtime** | Runs the cross-platform wrapper between Claude and the Docker backend | No platform-specific `python3` command is required. The required `uv` launcher in row 7 discovers an existing Python 3 runtime or installs a managed one on macOS, Windows, and Linux. |
| 6 | **git** | Clones the RKA repo for the Docker compose file | macOS/Linux: built in or via package manager. Windows: [git-scm.com/downloads](https://git-scm.com/downloads). |
| 7 | **uv** | Installs the local `rka` MCP executable in an isolated tool environment | [Official installer](https://docs.astral.sh/uv/getting-started/installation/): macOS/Linux `curl -LsSf https://astral.sh/uv/install.sh \| sh`; Windows PowerShell `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 \| iex"` or `winget install --id=astral-sh.uv -e`. Open a new shell, then run `uv --version`. |
| 8 | **Zotero desktop + Connector** *(recommended)* | Persistent literature library — the AI reads paper full text from here. Zotero Connector captures papers via your institution's authenticated browser session, so the AI inherits your access without you sharing credentials. | Desktop: [zotero.org/download](https://www.zotero.org/download/) (or `brew install --cask zotero`). Connector: [zotero.org/download/connectors](https://www.zotero.org/download/connectors) (Chrome / Safari / Firefox / Edge). |

> **Why two Claude apps?** Brain and Executor are different roles. Brain (in Claude Desktop) reasons about research direction, makes decisions, processes maintenance. Executor (in Claude Code) writes code, runs experiments, picks up missions. They share the same RKA knowledge base, so context survives across roles and sessions.

### Optional integration credentials

Core itself requires no API key. Configure only the integrations you choose;
use the local credential vault in Step 5.5:

| Key | Source | Why |
|---|---|---|
| `SEMANTIC_SCHOLAR_API_KEY` | [semanticscholar.org/product/api](https://www.semanticscholar.org/product/api#api-key-form) | Free; lifts S2 rate limits. Used by RKA literature integrations and paper-search; external reference-checking clients may also use it. |
| `ZOTERO_API_KEY` + `ZOTERO_LIBRARY_ID` | [zotero.org/settings/keys](https://www.zotero.org/settings/keys) | Generate a "Save to Server" + "Full library access" key. Library ID is the 7-digit userID on the same page. Used by Zotero integrations. |
| `PAPER_SEARCH_MCP_UNPAYWALL_EMAIL` | Your institutional email | Used for rate-limit identification (not authentication). |

Optional:
| Key | Use |
|---|---|
| `SERPAPI_KEY` | Web search fallback when curated catalogs miss something |
| `PAPER_SEARCH_MCP_CORE_API_KEY` | Open-access full-text search via CORE |

---

## 3. Quick install (the recommended path)

The plugin handles most of the work. Follow the steps in order. Step 0 decides which of the later steps you run.

### Step 0 — 🟡 Confirm scope with the user

Before installing anything, ask the user which surfaces they want. Their answer decides which steps below you run. Present these options and wait for a reply:

| Surface | What it gives them | Steps to run |
|---|---|---|
| **Claude Desktop (Brain)** | Strategy/synthesis role in the Claude desktop app | Steps 1–5 (including Step 1.5) |
| **Claude Code (Executor)** | The full plugin — skills, slash commands, hook — in VSCode/Claude Code | Steps 1–3 (including Step 1.5; Step 4 wires Desktop) |
| **Codex or another MCP client** | RKA's stdio MCP surface in a non-Claude client | Steps 1 and 1.5 + the client configuration in §8 |

Also ask whether they have any of the **optional API keys** in §2 (Semantic Scholar, Zotero, Unpaywall email, SerpAPI). You'll wire those in at Step 5.5 — RKA runs without them, but literature features are richer with them.

Everyone runs **Steps 1 and 1.5** (backend plus local MCP executable). Then run only the steps their chosen surfaces need. If the user just says "install RKA" without specifics, the sensible default is Claude Desktop + Claude Code (Steps 1–5); confirm that read-back with them before proceeding, and mention Codex is also supported over local STDIO. Remote ChatGPT connectors are deferred.

### Step 1 — Start the RKA backend

**Pre-check**: Docker must be running and Compose v2 must be installed. Confirm with `docker info` and `docker compose version`. A non-zero exit means Docker is stopped or Compose is missing; start Docker Desktop, or start the Docker Engine service on Linux, then retry.

**🟡 Precondition (clone location)**: ask the user where they want the repo cloned (default: `~/Code` on macOS/Linux, `%USERPROFILE%\Code` on Windows). `cd` into that parent, so the repo lands at `<parent>/rka-core`. **Record the absolute `<parent>/rka-core` path** — Step 2 needs it as the marketplace path. Don't clone into an unstated cwd; if the user has no preference, state the default you're using and proceed.

> **⚠️ Windows: do not clone into a OneDrive-synced folder.** On most Windows installs `Desktop` and `Documents` are backed up by OneDrive. OneDrive's Files On-Demand will silently dehydrate untouched repo files into cloud placeholders, and Docker BuildKit then refuses to send them in the build context — a later `docker compose up -d --build` fails with `invalid file request <path>`. The clone works fine; the breakage appears weeks later on the first rebuild. `%USERPROFILE%\Code` (the default above) is outside OneDrive and is the safe choice. If the repo is already in a synced folder, see [§9 Windows: rebuilding and updating](#windows-rebuilding-and-updating-an-existing-install) for the recovery procedure.

macOS or Linux:

```bash
mkdir -p ~/Code
cd ~/Code
git clone --branch v3.0.1 --depth 1 https://github.com/rka-project/rka-core.git
cd rka-core
docker compose up -d
```

Windows PowerShell:

```powershell
New-Item -ItemType Directory -Force "$env:USERPROFILE\Code" | Out-Null
Set-Location "$env:USERPROFILE\Code"
git clone --branch v3.0.1 --depth 1 https://github.com/rka-project/rka-core.git
Set-Location rka-core
docker compose up -d
```

Wait ~1 minute. Verify:

macOS or Linux:

```bash
curl http://127.0.0.1:9712/api/health
# Expect: {"status":"ok","version":"3.x.x", ...}
```

Windows PowerShell:

```powershell
Invoke-RestMethod http://127.0.0.1:9712/api/health
```

Open http://127.0.0.1:9712 in your browser to confirm the dashboard loads.

**✅ Success signal**: the health request returns JSON with `"status":"ok"` and a `"version"` field beginning with `3.` AND `http://127.0.0.1:9712` renders the dashboard HTML.

> **⚠️ Windows: if this fails, try `http://127.0.0.1:9712` before assuming the backend is broken.** `localhost` resolves to IPv6 `::1` first, and Docker Desktop's WSL2 backend publishes the container on IPv4 only. WSL2's `localhostForwarding` proxy still *accepts* the `::1` connection and then resets it, so the browser shows `ERR_CONNECTION_RESET` and `curl` reports `Recv failure: Connection was reset` — both look like a dead server when the API is perfectly healthy. Because the TCP handshake succeeds, no automatic IPv4 fallback happens. If `127.0.0.1` works and `localhost` doesn't, the backend is fine; use `127.0.0.1` throughout and see [§9 Windows](#windows-specifically) for the permanent fix.

**Recovery**: if curl returns non-zero or non-2xx, run `docker compose ps` to confirm both `rka-server` and `rka-worker` are up. If a container is restarting, run `docker compose logs --tail=20 rka rka-worker` and surface the output. If a process is `OOMKilled`, stop and inspect its logs and resource limits; do not keep raising the cap or repeatedly restart an old backfill. See [embedding recovery](docs/embedding-inspection.md).

> **What this does**: starts two containers (`rka-server` for the API + web UI, `rka-worker` for background jobs). Data is persisted in a Docker volume named `rka-data`. To stop: `docker compose down`. To stop AND wipe data: `docker compose down -v` (don't do this unless you mean it).

> **First-run and upgrade indexing:** the default FastEmbed backend downloads roughly 520 MB on its first uncached use and stores it in the persistent Docker volume. An upgrade, import, or embedding-space change can also trigger a generation rebuild. The health endpoint and web UI may already be available while this work continues; semantic search temporarily falls back to lexical retrieval until the new generation is ready. Check **Settings → Embeddings** for progress before judging retrieval quality.

> **Unreleased durable-backfill change:** startup, config-save, manual backfill,
> pack-import vectors and the legacy backfill CLI
> require the separate `rka-worker` service. An API health check alone does not
> prove indexing is running. On macOS, Windows and Linux, use `docker compose ps`
> and `docker compose logs --tail=50 rka-worker` to inspect it; update API and worker
> together. Dockerless users must run `rka worker` with the same data directory and
> database as `rka serve` (`start-all --foreground` starts only the API). Pending
> jobs survive restart; exhausted/cancelled jobs need an explicit retry. Do not
> delete vector tables to retry. See [backfill status, cancellation and retry](docs/embedding_backends.md#durable-backfill-lifecycle-unreleased-hardening).

> A pack's 202 response includes a durable import receipt: lexical search is already
> committed, while `semantic_state` may still be pending or disabled. An active
> backfill with a different project/scope can cause import to return 409; that import
> is rolled back, so retry the upload after the active task ends. The old
> `backfill-embeddings --project ... --force` command now queues current-space work;
> it does not change dimensions or constitute an offline recovery procedure.

### Step 1.5 — Install and verify the local MCP executable

Run this from the `rka-core` directory on every operating system:

```text
uv tool install --force --reinstall .
```

Then verify the absolute install path. This avoids depending on whether the uv bin directory has already been added to the current shell's `PATH`.

macOS or Linux:

```bash
~/.local/bin/rka --version
```

Windows PowerShell:

```powershell
& "$env:USERPROFILE\.local\bin\rka.exe" --version
```

Expect `rka 3.x.x`. If the command is missing, open a new shell after installing uv and check `uv tool dir --bin`; if uv reports a different bin directory, use the path it prints in all client configurations below.

#### Optional: LM Studio suggestions in the manuscript workbench

RKA does not require a local language model. If you want the workbench's **Ask
LM Studio** action, start LM Studio's local API server, load a model that
supports structured JSON output, and verify the served model ID:

```bash
curl http://127.0.0.1:1234/v1/models
```

Put that exact ID in a repo-local `.env` file (do not commit it), then recreate
only the API container:

```dotenv
RKA_WORKBENCH_LM_STUDIO_MODEL=the-served-model-id
```

```bash
docker compose up -d --force-recreate rka
```

Compose routes the container to
`http://host.docker.internal:1234/v1`. A non-Docker RKA process defaults to
`http://127.0.0.1:1234/v1`. The adapter accepts only those local-machine forms,
uses no API key, never falls back to a cloud model, and stores the result as an
unapplied semantic proposal.

### Step 2 — Add the local RKA marketplace in Claude Code

The marketplace lives inside the cloned RKA repo (at `.claude-plugin/marketplace.json`).

**Pre-check**: `ls <path>/.claude-plugin/marketplace.json` must succeed. If the file is missing, the path is wrong or the user didn't clone the right repo — surface a clear error before invoking `/plugin`.

In any Claude Code chat window (in VSCode), run:

```
/plugin marketplace add /absolute/path/to/your/cloned/rka-core
```

Replace `/absolute/path/to/your/cloned/rka-core` with the actual path where you cloned the repo in Step 1 (e.g., `/Users/<you>/Code/rka-core` on macOS, `C:\Users\<you>\Code\rka-core` on Windows). Use the absolute path, not `~`.

**Success signal**: Claude Code prints "Marketplace 'rka' added" (or equivalent confirmation).

### Step 3 — Install the plugin

```
/plugin install rka@rka
```

Wait for the install to complete (~5–10 seconds).

**Post-check**: run `/plugin list` and string-match against `rka@rka`. If absent, surface the install log and stop.

**Success signal**: `/plugin list` output contains `rka@rka`.

### Step 4 — Have Claude Code set up Claude Desktop too

In the same Claude Code chat:

```
/rka-setup-claude-desktop
```

Or if you prefer natural language, just say:

> Set up RKA for Claude Desktop too.

The plugin's `rka-pi` skill teaches Claude Code how to:
1. Detect your OS (macOS / Windows / Linux).
2. Locate `claude_desktop_config.json` at the right path.
3. Back up the existing config to `claude_desktop_config.json.backup-YYYYMMDD-HHMMSS`.
4. Atomically merge the `mcpServers.rka` entry pointing at the plugin's wrapper script.
5. Verify the merge by re-reading the file.
6. Tell you to fully quit + reopen Claude Desktop.

If anything fails at any step, the original config is restored from the backup automatically. No silent overwrites.

**Success signal**: Claude Code prints `✅ Claude Desktop config updated. The RKA MCP server entry is in place at <config_path>.` After Step 5's full quit + reopen, a fresh Claude Desktop chat will list the 5 Core dispatch tools (`rka_query`, `rka_execute`, `rka_describe`, `rka_load_tools`, `rka_help`).

### Step 4.5 (optional) — Install the standalone Writer plugin

Manuscript drafting is intentionally outside this repository. If it is wanted,
install [`rka-writer`](https://github.com/rka-project/rka-writer) by following
that repository's README. Writer is explicit-only and may use this Core MCP as
an evidence source, but Core does not install Writer tools, commands, hooks, or
dependencies.

### Step 5 — Fully quit and reopen Claude Desktop

The Claude Desktop MCP loader reads its config at startup, so the config change won't apply until you do a full quit + reopen. On macOS this means **Cmd+Q** (the red close button only minimizes — it does NOT quit, and the MCP loader will not re-read the config).

| OS | Quit |
|---|---|
| macOS | Cmd+Q (not just clicking the red close button — that minimizes) |
| Windows | Right-click the Claude tray icon → Quit, then reopen |
| Linux | Same shortcut as your desktop environment's Quit |

Open a fresh chat in Claude Desktop. Ask:

> List my RKA projects.

Brain should call `rka_query(args={"operation":"list_projects"})` through the typed dispatch surface and return the list (empty on a fresh install). If you also see a SessionStart hook line like `✅ RKA reachable at http://127.0.0.1:9712 (version 3.x.x; explicit project_id required per operation).` at session start in Claude Code, you're done. The `✅ RKA reachable` line with a `version 3.x` substring confirms that the hook handshake reached the split Core runtime.

**✅ Success signal**: SessionStart hook line contains `✅ RKA reachable` (with a `version 3.x` substring) AND Brain returns a project list (empty or otherwise) without error.

### Step 5.5 (optional) — 🟡 First-run credentials (`rka cred init`)

With the backend up and the plugin wired, bootstrap the global credential vault only if optional integrations are wanted. **Ask the user for each API key you're going to store** (from the list they gave you at Step 0) — never invent or hard-code a key. RKA runs fine with zero keys; each one just enriches literature features.

```bash
rka cred init          # creates ~/.config/rka/creds.env (mode 0600, XDG-compliant)
rka cred set SEMANTIC_SCHOLAR_API_KEY <value-the-user-gave-you>   # repeat per key: ZOTERO_API_KEY, etc. from §2
rka cred check         # verifies which keys are present + reachable
```

The key values are secrets: pass them straight to `rka cred set` and do not echo them back or write them into any file the repo tracks. `rka cred env` prints export lines for shell sourcing; `rka cred propagate` syncs the vault into selected local consumers (the RKA Zotero configuration and installed client configurations). Full reference: [`docs/CRED_VAULT.md`](docs/CRED_VAULT.md). The keys from §2 (Semantic Scholar, Zotero, Unpaywall email, optionally SerpAPI / CORE) all live here — this is the recommended path instead of hand-editing `claude_desktop_config.json` env blocks.

### Step 6 — Remote access deferred

Do not launch HTTP MCP, the old OAuth proxy, or a tunnel in Core 3.0.0.
They are disabled pending remote execution-policy acceptance. Local Codex /
Claude STDIO clients continue to work without an MCP network listener.
See [remote access status](docs/REMOTE_ACCESS.md).

---

## 4. Cross-platform reference

### Config file paths

| OS | Claude Desktop config | Optional RKA `integration.json` |
|---|---|---|
| **macOS** | `~/Library/Application Support/Claude/claude_desktop_config.json` | `~/Library/Application Support/RKA/integration.json` |
| **Windows** | `%APPDATA%\Claude\claude_desktop_config.json` (resolves to `C:\Users\<you>\AppData\Roaming\Claude\`) | `%APPDATA%\RKA\integration.json` |
| **Linux** | `~/.config/Claude/claude_desktop_config.json` | `~/.local/share/RKA/integration.json` (XDG default) |

### Wrapper script paths (after plugin install)

The plugin install copies its files to `~/.claude/plugins/cache/rka/rka/<version>/` (macOS/Linux) or `%USERPROFILE%\.claude\plugins\cache\rka\rka\<version>\` (Windows). Claude Code invokes the wrapper with `uv run --no-project <path>/bin/rka-mcp-bridge.py`; the Desktop setup helper records an absolute path to `uv` for GUI reliability.

### Backend connection (used by the wrapper)

The wrapper can read `integration.json` for an explicit binary path and backend metadata, but the file is optional and is not created by the current plugin/setup helper. Without it, the wrapper locates `rka` on `PATH` or in uv's usual per-user bin directory. RKA's client default is `http://127.0.0.1:9712`; set `RKA_API_URL` in the client config only when using a different endpoint.

When a launcher needs explicit metadata, it must write the current schema and
the exact backend version reported by `/api/health` (the path shown here is a
macOS example; use the platform path from the table above):

```json
{
  "schema_version": "rka.integration/v1",
  "backend_version": "3.0.1",
  "binary_path": "/Users/<you>/.local/bin/rka",
  "api_endpoint_url": "http://127.0.0.1:9712"
}
```

Do not add a default project: every project-scoped operation carries its own
explicit `project_id`.

### Remote access

Unsupported in Core 3.0.0. See [remote access status](docs/REMOTE_ACCESS.md).

---

## 5. Verifying the install

After Step 5, run these checks:

### In Claude Code

1. `/help` should list five `rka:rka` slash commands. String-match each of: `/rka-status`, `/rka-search`, `/rka-pending`, `/rka-set-project`, `/rka-setup-claude-desktop`. All five must be present.
2. `/context` should show the three RKA Core roles (`rka-brain`, `rka-executor`, and `rka-pi`) plus the credential setup utility. It must not show `rka-writer` unless that separate plugin was independently installed and explicitly invoked.
3. Run `/rka-status`. Expected output: project name, phase, focus, open checkpoints (or "none").
4. New chat sessions should start with an automatic line. **Expected stdout snippet**: `✅ RKA reachable at http://127.0.0.1:9712 (version 3.x.x; explicit project_id required per operation).`.

### In Claude Desktop

Ask in any new chat:

> What RKA tools do you have access to?

Brain should list **5 always-on tools (3 dispatch + 2 escape hatches)**: `rka_query`, `rka_execute`, `rka_describe`, plus `rka_load_tools` and `rka_help` as navigator escape hatches. This matches the §1.1 surface count exactly. Confirm by asking: *"Call `rka_describe` with an empty string"* — Brain should return the live operation index. Treat that response as authoritative because the operation catalog can grow between releases.

If you instead see a long list of legacy tool names (e.g., `list_projects`, `get_status`, `add_note`, etc. — surfaced as one MCP tool each), your Brain is running with `RKA_LEGACY_TOOLS=1` (orchestrator daemon mode). For the user-facing Claude Desktop session, unset this env var and restart.

### Backend

```bash
docker compose ps
# Expect both rka-server and rka-worker as "Up" / "healthy"

curl http://127.0.0.1:9712/api/health
# Expect {"status":"ok","version":"3.x.x", ...}
```

---

## 6. For Claude Code: `/rka-setup-claude-desktop` execution steps

The plugin's setup helper is the authoritative implementation. Do not recreate
its JSON merge with ad-hoc shell or Python snippets: the helper is covered by
the repository's cross-platform regression tests and preserves unrelated
Claude Desktop settings.

### 6.1 — Run the helper

From an installed plugin, invoke:

```text
/rka-setup-claude-desktop
```

The slash command runs the equivalent of:

```bash
uv run --no-project "${CLAUDE_PLUGIN_ROOT}/scripts/setup-claude-desktop.py"
```

For a source checkout, the same helper can be previewed without writing:

```bash
uv run --no-project plugin/scripts/setup-claude-desktop.py --dry-run
```

The helper performs these operations in order:

1. resolves the macOS, Windows, or Linux Claude Desktop config path;
2. locates the installed plugin wrapper;
3. verifies the backend at `http://127.0.0.1:9712/api/health` unless an
   explicit `integration.json` endpoint is present;
4. resolves `uv` and stores its absolute executable path so GUI-launched
   Claude Desktop does not depend on shell-specific `python3`, `py`, or
   `python` aliases;
5. preserves every unrelated config key and conflict-detects an existing
   `mcpServers.rka` entry;
6. writes a timestamped backup, updates atomically, and re-reads the result; and
7. restores the backup if a post-backup write or verification step fails.

The normal entry has this shape (paths vary by machine):

```json
{
  "command": "/absolute/path/to/uv",
  "args": [
    "run",
    "--no-project",
    "/absolute/path/to/plugin/bin/rka-mcp-bridge.py"
  ]
}
```

### 6.2 — Interpret the result

- Exit 0 with `already configured`: no file changed.
- Exit 0 with `config updated`: report the config and backup paths, then tell
  the user to fully quit and reopen Claude Desktop.
- Exit 1 with `Backend NOT reachable`: start RKA and retry. Do not force the
  setup merely to hide a backend failure.
- Exit 1 with `CONFLICT`: show the existing and proposed entries. Run again
  with `--force` only after the user explicitly approves replacing that one
  entry.
- Exit 2 with malformed JSON: do not rewrite the file; ask the user to repair
  or restore it.
- Exit 2 with a missing wrapper: reinstall `rka@rka`.
- Exit 2 with missing `uv`: install uv from the official installer, open a
  new terminal, verify `uv --version`, and retry.

On success, open a fresh Claude Desktop chat and ask: *"Show me the available
RKA projects."* The client should call
`rka_query(args={"operation":"list_projects"})`. Every project-scoped
operation must still include its own explicit `project_id`.

## 7. For Claude Code: skill content for related questions

When the user asks variants of *"set up RKA"*, *"finish RKA install"*, *"connect Brain"*, *"why isn't RKA showing up in Claude Desktop"*, etc., the `rka:rka-pi` skill should:

1. First check whether `/rka-setup-claude-desktop` would address it. If yes, suggest running that command.
2. For diagnosis questions ("why isn't RKA showing"), check in order:
   - Is Docker running? (`docker compose ps` from the rka repo dir)
   - Is the API healthy? (`curl http://127.0.0.1:9712/api/health`)
   - Does `claude_desktop_config.json` exist and have an `mcpServers.rka` entry?
   - Does `integration.json` exist?
   - Did the user fully quit + reopen Claude Desktop after the last config change?
3. For "uninstall RKA" requests:
   - Run `/plugin uninstall rka@rka` in Claude Code.
   - Restore Claude Desktop's config from the most recent backup.
   - Optionally: `docker compose down -v` to wipe the backend (warn user this destroys their RKA data — recommend a knowledge-pack export first via the project-scoped REST endpoint `GET /api/projects/export`, or the web dashboard's export control; see [USAGE_GUIDE.md](USAGE_GUIDE.md) for the pack import/export workflow. Note: `export` is not a typed dispatch operation, so it is not directly callable from the default 5-tool surface).

---

## 8. Manual install (advanced)

Use this path if:
- You're on a system without VSCode (e.g., Claude Code CLI on a server)
- You're scripting RKA install in CI (note: plugins don't load in `claude -p` print mode — an empirical finding)
- You want to run RKA without the plugin layer at all

### 8.1 — Install the RKA stdio binary

macOS or Linux:

```bash
mkdir -p ~/Code
cd ~/Code
git clone --branch v3.0.1 --depth 1 https://github.com/rka-project/rka-core.git
cd rka-core
uv tool install --force --reinstall .
~/.local/bin/rka --version
```

Windows PowerShell:

```powershell
New-Item -ItemType Directory -Force "$env:USERPROFILE\Code" | Out-Null
Set-Location "$env:USERPROFILE\Code"
git clone --branch v3.0.1 --depth 1 https://github.com/rka-project/rka-core.git
Set-Location rka-core
uv tool install --force --reinstall .
& "$env:USERPROFILE\.local\bin\rka.exe" --version
```

If `uv` is missing, use the [official uv installer](https://docs.astral.sh/uv/getting-started/installation/): `curl -LsSf https://astral.sh/uv/install.sh | sh` on macOS/Linux, or `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"` / `winget install --id=astral-sh.uv -e` on Windows. Open a new shell after installation.

### 8.2 — Start the backend

```bash
docker compose up -d
curl http://127.0.0.1:9712/api/health
```

Windows PowerShell uses the same Compose command and this health check:

```powershell
docker compose up -d
Invoke-RestMethod http://127.0.0.1:9712/api/health
```

### 8.3 — Configure Claude Desktop manually

Edit `claude_desktop_config.json` at the path for your OS (see §4). Add to `mcpServers`:

```json
{
  "mcpServers": {
    "rka": {
      "command": "/Users/<your-username>/.local/bin/rka",
      "args": ["mcp"]
    }
  }
}
```

Replace the command with the absolute binary path on the current machine: `/Users/<you>/.local/bin/rka` on macOS, `/home/<you>/.local/bin/rka` on Linux, or `C:\\Users\\<you>\\.local\\bin\\rka.exe` on Windows (JSON requires doubled backslashes).

**v2.6+ project discipline (no env var).** Pre-v2.6 the config included an `env.RKA_PROJECT` entry to pin a default project. That was removed in v2.6 because it reintroduced the silent-default failure mode that v2.6 explicitly eliminates: every project-scoped tool now requires `project_id` as a kwarg, and the LLM threads the project from its conversation context. At the start of every conversation, state which project you're working on (e.g., *"I'm working on prj_01KSMW9R…"* or *"the hyperscaler-auditing project"*) — the LLM keeps it in working memory and passes it on every tool call.

**v2.7.0 schema enforcement.** In v2.7.0 the args object is a discriminated Pydantic union — missing `project_id` on a scoped operation now surfaces as a `ValidationError` from FastMCP **before** the call reaches the service layer. Per-branch enum + required-field enforcement (defined in `orchestrator/rka_enums.py` mirror + the per-operation Pydantic models) means wrong values (e.g., `confidence='confirmed'`) are also rejected pre-dispatch with a structured error pointing at the offending field. See §1.1 for the user-facing tool surface this enforcement sits behind.

After restart, Brain will see exactly 5 always-on tools (3 dispatch + 2 escape hatches): `rka_query`, `rka_execute`, `rka_describe`, plus `rka_load_tools` and `rka_help` (alias for `rka_describe`). This is normal — the current operation catalog is dispatched through them and can be inspected with `rka_describe("")`.

Fully quit + reopen Claude Desktop.

### 8.4 — Configure Claude Code manually

Same JSON shape, in `.claude/mcp.json` (per-project) or `~/.claude/settings.json` under `mcpServers`. Reload the VSCode window after saving.

### 8.5 — Configure Codex

Codex reads MCP servers from `~/.codex/config.toml`, or from a trusted project's `.codex/config.toml`. The desktop app, CLI, and IDE extension share this configuration. Use an absolute executable path.

macOS or Linux (replace both placeholders):

```toml
[mcp_servers.rka]
command = "/absolute/path/to/.local/bin/rka"
args = ["mcp"]
cwd = "/absolute/path/to/rka-core"
```

Windows uses TOML literal strings so backslashes do not need escaping:

```toml
[mcp_servers.rka]
command = 'C:\Users\<you>\.local\bin\rka.exe'
args = ["mcp"]
cwd = 'C:\Users\<you>\Code\rka-core'
```

Save the file, fully restart Codex or open a fresh task, and verify with `codex mcp list` when the CLI is installed. Newer Codex CLI releases also provide a convenience command:

```bash
codex mcp add rka -- /absolute/path/to/.local/bin/rka mcp
```

```powershell
codex mcp add rka -- "$env:USERPROFILE\.local\bin\rka.exe" mcp
```

Run `codex mcp --help` first; older Codex CLI builds do not provide `mcp add`. Editing `config.toml` is the version-independent path.

---

## 9. Troubleshooting

### General

| Symptom | Likely cause | Fix |
|---|---|---|
| `docker compose up -d` fails | Docker Desktop not running | Launch Docker Desktop, wait until the whale icon says "running", retry |
| `curl http://127.0.0.1:9712/api/health` fails | Container started but unhealthy | `docker compose logs -f rka` to see what's wrong; usually port 9712 conflict — change in `docker-compose.yml` |
| `/plugin marketplace add /path/to/rka` fails | Path is wrong, or `.claude-plugin/marketplace.json` doesn't exist at that path | Verify with `ls /path/to/rka/.claude-plugin/marketplace.json`. Use the absolute path, not `~`. |
| `/plugin install rka@rka` fails after marketplace add | Marketplace file present but plugin source missing | Check the repo's marketplace.json — `source` field must point at a valid path inside the repo |
| `rka_query` / `rka_execute` / `rka_describe` absent from Brain's tool list (Claude Desktop) | Config not saved, or app not fully quit | Verify config at the path in §4 contains the `rka` entry; Cmd+Q fully (not red-X close); reopen. If you see the legacy tools (`rka_list_projects`, `rka_get_status`, etc.) instead, your Brain is running with `RKA_LEGACY_TOOLS=1` (orchestrator daemon mode) — unset and restart. |
| RKA tools missing in Claude Code | Plugin not installed, or VSCode window not reloaded | `/plugin list` to verify; reload window with Cmd+Shift+P → "Developer: Reload Window" |
| All RKA writes land in `proj_default` | LLM forgot to pass `project_id` on a write call (v2.6+ requires it as a kwarg) | In v2.7.0+ the error surface is a Pydantic `ValidationError` (`Field required: project_id for operation X`) raised by FastMCP at schema-validate time, not a `TypeError`. If you see writes silently landing in `proj_default`, you are on a pre-v2.6 install — upgrade. If on v2.7.0+, ask the LLM at conversation start to pin `project_id` and thread it through every call. |
| SessionStart hook says "RKA NOT reachable" | Docker stopped or wrong API URL | Run `docker compose up -d`; check `integration.json`'s `api_endpoint_url` |
| Wrapper rejects `backend_version` as too old | `integration.json` explicitly reports a backend older than the plugin minimum | Upgrade RKA (`git pull`, reinstall the tool, and rebuild containers) or use a plugin compatible with that backend. If the file only has legacy `version`, migrate it to `schema_version` + `backend_version`; legacy `version` is not used for the gate. |

### Windows specifically

| Symptom | Likely cause | Fix |
|---|---|---|
| `uv` is not recognized after installation | The current VSCode or terminal process still has its old `PATH` | Fully restart VSCode/Claude Code and open a new terminal; verify `uv --version`, then reinstall or rerun the plugin setup |
| Path with spaces breaks the wrapper | Unquoted path in JSON config | Escape backslashes (`\\\\`) and ensure the entire path is in JSON quotes |
| Microsoft Store install of Claude Desktop doesn't see the config edits | Config path differs (stored in app sandbox) | This appears NOT to happen empirically — both Microsoft Store and standalone .exe write to the same `%APPDATA%\Claude\` path. If you observe otherwise, surface as a bug. |
| Web dashboard shows `ERR_CONNECTION_RESET` at `localhost:9712`, but the container is healthy | `localhost` → IPv6 `::1`; WSL2's `localhostForwarding` proxy accepts the connection and resets it. Docker publishes IPv4-only, and the successful handshake suppresses IPv4 fallback | Use **`http://127.0.0.1:9712`**. Permanent fix: create `%USERPROFILE%\.wslconfig` with `[wsl2]` / `localhostForwarding=false`, then `wsl --shutdown` (stops **all** containers and WSL distros — do it when convenient) |
| `rka_query`/`rka_execute` return `{"status":"unhealthy","error":""}` while `curl http://127.0.0.1:9712/api/health` returns `ok` | An older MCP configuration may still target `http://localhost:9712` and resolve it over IPv6. Current RKA defaults to `http://127.0.0.1:9712`; the empty `error` string is the tell when an old override remains | Remove the stale `RKA_API_URL` override or pin IPv4 with an explicit `env` block in **each** MCP config (see [§9 Windows: rebuilding and updating](#windows-rebuilding-and-updating-an-existing-install)). A user env var is **not** sufficient — see the next row |
| Config or env-var change doesn't take effect after "Developer: Reload Window" | A window reload re-spawns MCP servers as children of the **already-running** VSCode process, which keeps its original environment block. `setx` writes the registry but not that block | Put per-machine settings in the MCP config's `env` block (re-read on reload), not in a user env var. Env vars need a **full application restart**, not a reload |
| `uv tool install --force --reinstall .` fails: `failed to remove file ... _pydantic_core.<abi>.pyd: Access is denied. (os error 5)` | Windows locks loaded `.pyd`/DLLs. Running `rka mcp` servers (Claude Desktop, Codex, and Claude Code windows) hold the file open | Fully quit those clients, or stop the server processes, then re-run. See [§9 Windows: rebuilding and updating](#windows-rebuilding-and-updating-an-existing-install) for a safe process filter. |
| `docker compose up -d --build` fails: `invalid file request <path>` / `failed to solve: invalid file request` | Repo is in a OneDrive-synced folder and Files On-Demand dehydrated some files into cloud placeholders (`ReparsePoint` attribute). BuildKit can't send them in the build context | Materialize the placeholders (procedure below), or move the clone outside OneDrive. `attrib +P` (pin) alone does **not** clear the reparse tag |

<a id="windows-rebuilding-and-updating-an-existing-install"></a>

### Windows: rebuilding and updating an existing install

Upgrading a Windows install (`git pull` → reinstall binary → rebuild containers) hits several platform-specific failures that don't occur on macOS/Linux. Work through these in order; each was observed on a real v2.8.0 → v2.9.0 upgrade.

#### 1. Materialize OneDrive placeholders before building

If the repo lives under a OneDrive-synced folder (`Desktop`, `Documents`), files dehydrate over time and the build fails with `invalid file request <path>`. The files still *read* fine — only BuildKit's context transfer rejects them.

Count them first (a fresh clone reports `0`):

```powershell
$repo = "C:\path\to\rka"
Get-ChildItem $repo -Recurse -File -Force |
  Where-Object { $_.FullName -notlike "*\.git\*" -and $_.FullName -notlike "*\node_modules\*" -and
                 ($_.Attributes -band [System.IO.FileAttributes]::ReparsePoint) } |
  Measure-Object | Select-Object -ExpandProperty Count
```

`attrib +P` (pin) sets the `Pinned` flag but leaves the reparse tag in place, so it does not fix the build. Rewriting each file in place does — it produces an ordinary local file with byte-identical content:

```powershell
Get-ChildItem $repo -Recurse -File -Force |
  Where-Object { $_.FullName -notlike "*\.git\*" -and $_.FullName -notlike "*\node_modules\*" -and
                 ($_.Attributes -band [System.IO.FileAttributes]::ReparsePoint) } |
  ForEach-Object {
    $bytes = [System.IO.File]::ReadAllBytes($_.FullName)
    Remove-Item -LiteralPath $_.FullName -Force
    [System.IO.File]::WriteAllBytes($_.FullName, $bytes)
  }
```

**Verify content is unchanged before building**: `git status --short` and `git diff --stat` must show no modifications to tracked files. If they do, stop — restore from `git checkout --` rather than building. Re-run the count query; expect `0`.

The durable fix is to move the clone outside OneDrive (see the Step 1 warning in §3).

#### 2. Stop the processes that lock the binary

`uv tool install --force --reinstall .` fails with `Access is denied (os error 5)` on `_pydantic_core.<abi>.pyd` while any `rka mcp` server is running — Windows locks loaded extension modules. Fully quit Claude Desktop, Codex, and Claude Code first. To stop remaining processes directly:

```powershell
Get-CimInstance Win32_Process |
  Where-Object { ($_.Name -eq 'rka.exe' -or $_.CommandLine -like '*rka-mcp-bridge.py*') -and
                 $_.ProcessId -ne $PID } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
```

> **⚠️ Match narrowly — two traps here, both observed.**
> 1. A broader filter such as `CommandLine -like '*rka*'` also matches `docker-buildx.exe`, whose command line contains the repo path. Killing it aborts an in-flight `docker compose build`.
> 2. **The `-ne $PID` guard is not optional.** When you paste this into a console, the filter string becomes part of *your own shell's* command line, so the shell matches its own filter and terminates itself mid-loop, leaving some servers running.
>
> Match on process name and the bridge script only, and always exclude the current process. Run the `Where-Object` clause on its own first and eyeball the list before adding the `Stop-Process` pipe.

If a previous attempt failed partway, `uv` may warn `Failed to uninstall package ... due to missing RECORD file` and `Installation may result in an incomplete environment`. Confirm the result is actually sound before moving on:

```powershell
rka --version                                   # expect the new version
& "$env:APPDATA\uv\tools\rka\Scripts\python.exe" -c "import pydantic_core, rka; print('ok')"
```

If the import fails, run `uv tool uninstall rka` then `uv tool install --reinstall .` for a clean environment.

#### 3. Pin `RKA_API_URL` to IPv4 in every MCP config

After the upgrade the MCP tools may report `{"status":"unhealthy","error":""}` even though the API is healthy on `127.0.0.1` — the IPv6 fault described in the table above. Add an explicit `env` block to **each** config that launches the bridge:

```json
"rka": {
  "command": "uv",
  "args": ["run", "--no-project", "${CLAUDE_PLUGIN_ROOT}/bin/rka-mcp-bridge.py"],
  "env": { "RKA_API_URL": "http://127.0.0.1:9712" }
}
```

Apply it in `%APPDATA%\Claude\claude_desktop_config.json` (Claude Desktop) and in the `.mcp.json` that Claude Code actually reads. **Do not rely on a user env var** set with `setx`: a VSCode *window reload* re-spawns the MCP server from the already-running process's stale environment block, so the variable never arrives. The `env` block is re-read on every reload.

Verify without restarting anything by driving the bridge directly — a healthy result is `{"status": "healthy", "rest_status_code": 200}`:

```powershell
$env:RKA_API_URL = "http://127.0.0.1:9712"
rka mcp   # then issue an MCP initialize + tools/call for rka_query {"operation":"health"}
```

#### 4. Know which plugin copy Claude Code is reading

When the marketplace source is a **local directory** (`/plugin marketplace add C:\path\to\rka`), `${CLAUDE_PLUGIN_ROOT}` resolves to the repo's own `plugin/` directory, so a `git pull` updates Claude Code immediately — but the copy under `%USERPROFILE%\.claude\plugins\cache\rka\rka\<version>\` can stay behind at the commit it was installed from. Confirm which one is live from the running process:

```powershell
Get-CimInstance Win32_Process |
  Where-Object { $_.CommandLine -like '*rka-mcp-bridge.py*' } |
  Select-Object ProcessId, CommandLine | Format-List
```

The path in the command line is the copy in use. If it points at the cache, refresh it from the repo (`robocopy <repo>\plugin <cache> /E /XF .mcp.json`) so skills and commands match the backend — and keep `/XF .mcp.json` so any machine-specific `env` block or absolute interpreter path survives.

#### 5. Replace legacy `python3` launcher entries

Older plugin copies used `command: "python3"`, which could resolve to the Microsoft Store alias under a bare Windows process spawn. Current plugin manifests use `uv run --no-project`, and the Desktop helper writes the absolute `uv.exe` path. If a config still shows `python3`, reinstall the plugin and rerun `/rka-setup-claude-desktop`; do not hand-edit unrelated MCP entries.

### macOS specifically

| Symptom | Likely cause | Fix |
|---|---|---|
| Hook reports `uv: command not found` | The GUI app inherited an old `PATH` | Fully quit and reopen VSCode/Claude Code after installing uv; verify `uv --version` in a new terminal |
| Spotlight indexes integration.json and creates `._integration.json` | macOS metadata pollution on volumes without full xattr support (external drives, SMB/AFP network mounts, OneDrive/Dropbox/iCloud sync folders) | `dot_clean ~/Library/Application\ Support/RKA/` or move RKA data off the affected volume |
| Native/Dockerless backend reports SQLite has no `load_extension` or `enable_load_extension` | The Python interpreter was built without loadable SQLite extensions; installing `sqlite-vec` alone cannot add that capability | Use the recommended Docker backend, or a separate extension-capable Python environment for both API and worker. The host stdio proxy does not need sqlite-vec. Do not alter the live database to fix an interpreter limitation. |

For a new, isolated native environment, `uv venv --managed-python --python 3.13
.venv-rka-native` selects a managed interpreter rather than reusing a system
Python. Install the needed Core extras there and verify that interpreter can
actually load sqlite-vec before pointing it at existing data. Python documents
the [macOS SQLite extension-build limitation](https://docs.python.org/3.13/library/sqlite3.html#sqlite3.Connection.enable_load_extension);
see also [uv-managed Python selection](https://docs.astral.sh/uv/concepts/python-versions/).
This does not replace backup, coordinated API/worker upgrade, or generation
compatibility checks.

### Linux specifically

| Symptom | Likely cause | Fix |
|---|---|---|
| Claude Desktop's config path differs | Some distros use `~/.var/app/...` for Flatpak installs | Check the actual path your Claude Desktop install uses; `find ~ -name "claude_desktop_config.json" 2>/dev/null` |
| Docker requires sudo | User not in `docker` group | `sudo usermod -aG docker $USER`, log out/in |

---

## 10. What this guide intentionally doesn't cover

- **Local LLM setup (LM Studio, Ollama, etc.) for chat/enrichment** — Chat-style enrichment tools (`rka_ask`, `rka_generate_summary`) were removed in v2.4.0 per `jrn_01KRNZBS50K250HHHHEC58E4GC`. The Brain (Claude Desktop) handles all knowledge enrichment during normal sessions. **However**, RKA v2.4.0+ supports pluggable embedding backends — configure FastEmbed (default, runs in-container), OpenAI-compatible HTTP (e.g., LM Studio, vLLM), or Ollama via **Settings → Embeddings** in the web dashboard at `http://127.0.0.1:9712`. Full reference: [`docs/embedding_backends.md`](docs/embedding_backends.md).
- **Cloud LLM API setup (OpenAI, Anthropic API key)** — not required by RKA's core workflow.
- **Knowledge pack import/export** — covered in [USAGE_GUIDE.md](USAGE_GUIDE.md) and [docs/USER_MANUAL.md](docs/USER_MANUAL.md).
- **Per-project conventions** (Brain orientation, Executor mission flow, PI attribution) — covered by the role skills the plugin loads automatically. Once installed, ask Claude Code to "load the rka brain skill" or "show me the executor session protocol" for the workflow guides.
- **Migrating from a pre-v2.3 RKA install** — for users who set up RKA before the plugin existed. See the upgrade notes in [USAGE_GUIDE.md](USAGE_GUIDE.md).

---

## 11. Historical Agentic distribution — unsupported

> **Shelved on 2026-08-27. Do not follow these installation steps.** The
> section is retained only to preserve the historical operating record. RKA
> Core and RKA Writer do not require or support this runtime. Reactivation
> requires a new explicit PI decision; see
> [ADR 0013](docs/adr/0013-shelve-agentic-and-focus-core-writer.md).

If you're on the `agentic` branch, you also have access to the **RKA Orchestrator** — a LangGraph-driven Brain⇄Executor⇄PI workflow engine with a Claude-Code-native PI surface (no stdin terminal needed). This is **optional** — the core main-branch RKA setup above works without it. If you only need the knowledge base + Brain in Claude Desktop + Executor in Claude Code, skip this section.

### When to install

Install the orchestrator if you want to:

- Drive structured Brain⇄Executor missions through PI-ratified gates from any Claude Code session
- Use the **onboarding wizard** to set up per-project tool manifests + credential validation (Phase D MVP)
- Reuse the same MCP entries in Claude Desktop for both ad-hoc work AND structured workflows

### Step 11.1 — Switch to the agentic branch

```bash
cd <your-clone-dir>/rka-core
git checkout agentic
```

**Post-check**: `git rev-parse --abbrev-ref HEAD` must equal `agentic`. If not, halt and surface the actual branch — §11 requires the agentic branch.

### Step 11.2 — Install the orchestrator package + MCP binary

`uv tool install` produces the runtime binary but does **NOT** install dev dependencies (pytest, fakes, LangGraph test fixtures); the `pytest -q` line below requires them. Run the two install commands explicitly — the binary-install gives you the production MCP entry point on PATH; the editable dev-install adds the `[dev]` extras needed by the test suite.

```bash
cd orchestrator
uv tool install --force --reinstall .                # produces ~/.local/bin/rka-orchestrator-mcp (binary only)
uv pip install -e ".[dev]"                           # adds dev deps for pytest (pytest, anyio, fakes, …)
pytest -q                                            # optional: 1100+ tests; verifies the install
```

If you skip the `uv pip install -e ".[dev]"` line, the `pytest -q` invocation will fail with `ModuleNotFoundError` (pytest itself, or the LangGraph test fixtures). The `pytest -q` step is OPTIONAL — only needed if you want to verify the install end-to-end. The orchestrator MCP binary works either way.

> **macOS AppleDouble note** (per CLAUDE.md): if you cloned the repo onto an external drive or a synced folder (Dropbox / OneDrive / iCloud), `uv tool install` may fail on `._requires.txt`. Install from a `/tmp` clone instead — `/tmp` is on a stock APFS volume that doesn't have the xattr quirk. See CLAUDE.md "macOS AppleDouble Quirks" for the exact workaround.

**Post-check**: `command -v rka-orchestrator-mcp` (macOS/Linux) or `where rka-orchestrator-mcp` (Windows) prints a path. If absent, check `uv tool list` for partial-install state and re-run with `--reinstall`.

### Step 11.3 — Mint a Claude Max OAuth token

In a **separate terminal** (NOT inside any Claude Code or Desktop session — the token is sensitive):

```bash
claude setup-token           # opens browser flow; outputs a long-lived token
```

### Step 11.4 — Drop the token in a gitignored env file

```bash
nano orchestrator/.env
```

```dotenv
# orchestrator/.env (file mode 0600, gitignored)
CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-...
# Optional — richer tool surface for the Brain subprocess:
SEMANTIC_SCHOLAR_API_KEY=...
SERPAPI_KEY=...
# Required while the orchestrator daemon's Brain⇄Executor subprocess has
# not yet been ported to v2.7.0 dispatch (rka_execute(operation=…)).
# Tracked in v2.7.0+agentic.X. Until then the daemon container needs the
# legacy 20-tool baseline:
RKA_LEGACY_TOOLS=1
```

```bash
# macOS / Linux: POSIX file mode bit 600 (rw owner, no group/other).
chmod 600 orchestrator/.env
```

```powershell
# Windows (PowerShell): no chmod equivalent; use icacls to break inheritance
# and grant the current user full control, denying everyone else by default.
icacls orchestrator\.env /inheritance:r /grant:r "$($env:USERNAME):F"
```

The file holds a long-lived OAuth token and (optionally) API keys — it must not be readable by other accounts on the same machine. The macOS/Linux mode 600 grants read/write to owner only; the Windows icacls invocation removes inherited ACLs and grants only the current user full access.

**Post-check**: verify with `stat -f %A orchestrator/.env` (macOS) / `stat -c %a orchestrator/.env` (Linux). Expect `600`. On Windows, verify with `icacls orchestrator\.env` — output should show only the current user with `(F)` (full control) and no Everyone / Users entries. If the value differs, re-run the platform-appropriate command above. Confirm the token is present with `grep -c "^CLAUDE_CODE_OAUTH_TOKEN=" orchestrator/.env` (expect `1`; on Windows use `Select-String -Pattern '^CLAUDE_CODE_OAUTH_TOKEN=' orchestrator\.env | Measure-Object -Line` and expect `Lines: 1`).

### Step 11.5 — Bring up the orchestrator service

**Pre-check**: `git rev-parse --abbrev-ref HEAD` must equal `agentic` (per Step 11.1).

The Compose overlay adds a third container (`rka-orchestrator`) alongside `rka-server` and `rka-worker` without modifying the root `docker-compose.yml`:

```bash
cd <your-clone-dir>/rka-core
docker compose -f docker-compose.yml \
               -f orchestrator/docker-compose.yml up -d --build
```

Verify:

```bash
curl http://localhost:9713/health
# {"status":"ok","db_path":"/data/orchestrator.db"}

docker compose -f docker-compose.yml -f orchestrator/docker-compose.yml ps
# Should show 3 services healthy: rka-server, rka-worker, rka-orchestrator
```

**Recovery**: if `curl http://localhost:9713/health` fails or `docker compose ps` shows `rka-orchestrator` unhealthy, run:

```bash
docker inspect rka-orchestrator --format '{{.RestartCount}} restarts; OOMKilled={{.State.OOMKilled}}'
```

If `OOMKilled=true`, bump Docker Desktop's Resources → Memory ceiling to ≥6 GB and re-up. (Per the operational note in CLAUDE.md: the FastEmbed model alone is ~250 MB; the full stack benefits from 8 GB.)

### Step 11.6 — Register the orchestrator MCP in Claude Desktop

> **IMPORTANT — do not clobber the wrapper-based `rka` entry from §6.** If you followed §3 + §6 (the recommended path), you already have an `mcpServers.rka` entry pointing at the wrapper script (`bin/rka-mcp-bridge.py`). Keep it untouched. This step adds ONLY the new `mcpServers.rka-orchestrator` entry. If you copy the JSON block below verbatim into your config, you will replace the wrapper-based `rka` entry with the `docker exec` form — that works but loses the version-checking + auto-pin behaviour the wrapper provides. The example below is correct *for users who never ran §6* (agentic distribution from scratch, no plugin install); the §6-path user should ADD only the `rka-orchestrator` key.

> **Note on `RKA_LEGACY_TOOLS=1`**: the user-facing `rka` MCP entry below exposes v2.7.0 dispatch tools (5 always-on — 3 dispatch + 2 escape hatches). The orchestrator daemon container reads `RKA_LEGACY_TOOLS=1` from `orchestrator/.env` (set externally by you in Step 11.4) and propagates it to the daemon's Brain⇄Executor subprocess via the container's env block so that subprocess still sees the v2.7.0a2 baseline (legacy 20 tools). End-users do not need to set this in Claude Desktop — the env var lives in `orchestrator/.env`, not the Desktop config.

For agent executors merging this entry programmatically: read the existing `claude_desktop_config.json`, parse, set `mcpServers.rka-orchestrator` ONLY (do not touch `mcpServers.rka`), write back atomically (same tmp+rename pattern as §6.6).

The JSON below shows the *result* of a from-scratch install (no §6 done previously); the §6-path user's result will have BOTH `rka` (wrapper) AND `rka-orchestrator` keys side-by-side:

```json
{
  "mcpServers": {
    "rka": {
      "command": "docker",
      "args": ["exec", "-i", "rka-server", "rka", "mcp"]
    },
    "rka-orchestrator": {
      "command": "/Users/<your-username>/.local/bin/rka-orchestrator-mcp",
      "args": []
    }
  }
}
```

Substitute your actual username. Fully quit and reopen Claude Desktop.

### Step 11.7 — Verify in Claude Desktop / Claude Code

```
[You:]
> Use orchestrator_health to check the daemon

[Claude:]
[calls orchestrator_health]
{"status":"ok","db_path":"/data/orchestrator.db"}
```

If you also see `orchestrator_run_start`, `orchestrator_inbox`, `orchestrator_onboard_start`, `orchestrator_get_manifest`, etc. in your tool list, the orchestrator MCP is wired correctly.

### Step 11.8 — Day-one use

```
[You:]
> /orchestrator-onboard prj_01XYZ      # for a new project's tool setup
or
> /orchestrator-start mis_01ABC        # to drive an existing mission
```

See [`USAGE_GUIDE.md`](USAGE_GUIDE.md#agentic-distribution--orchestrator-workflows) for the full agentic-distribution workflow walkthrough including the TWO-TAP discipline at `pi_decision_select` (the privileged write-authorization gate).

### Workspace bind mount — `HOST_WORKSPACE_ROOT`

For non-`$HOME` workspace roots (external drives, `/Volumes/...`, etc.), set `HOST_WORKSPACE_ROOT` in the **repo-root `.env`** (i.e., `<your-clone-dir>/rka-core/.env`), **NOT** in `orchestrator/.env`. Docker Compose reads `.env` from the directory of the first `-f` file (the repo root) for YAML `${VAR}` interpolation; the `env_file:` directive only populates env vars inside the running container and does not feed interpolation.

If you put `HOST_WORKSPACE_ROOT` in `orchestrator/.env`, the bind mount falls back to `${HOME}` and the workspace appears empty inside the container — a silent failure.

### Where orchestrator state lives

| | |
|---|---|
| Docker volume `orchestrator-data` | `/data/orchestrator.db` (parked-interrupt queue + workflow_runs) + `/data/orchestrator-saver.db` (LangGraph checkpointer) |
| `orchestrator/.env` (gitignored, mode 0600) | `CLAUDE_CODE_OAUTH_TOKEN`, optional API keys, and `RKA_LEGACY_TOOLS=1` propagated to the subprocess |
| `<your-clone-dir>/rka-core/.env` (gitignored, repo-root) | `HOST_WORKSPACE_ROOT` for non-`$HOME` workspace roots — fed to Compose YAML `${VAR}` interpolation for the workspace bind mount |
| `~/.claude.json` (mounted read-only) | Host's Claude CLI global config |
| `~/rka-projects/{project_id}/` (Phase D MVP convention) | Per-project `tools.json` + `.env` template after onboarding |

> **Phase O note**: a future implementation phase (~13.5 days, design committed) will consolidate the per-project workspace at `~/Research/{project-slug}/.rka/`. A separately installed `rka-writer` workspace may be colocated with it, but is not created or managed by Core. See `orchestrator/docs/phase-o-project-onboarding-design.md` for details. The Phase D MVP convention above continues to work in the meantime.

### Tearing down

```bash
docker compose -f docker-compose.yml -f orchestrator/docker-compose.yml down
# (Leaves the orchestrator-data volume; add -v to delete it.)
```

---

## Appendix: file inventory after a successful install

| Location | What |
|---|---|
| `<your-clone-dir>/rka-core/` | The cloned RKA Core source repo (only docker-compose.yml is needed at runtime; rest is for development) |
| Docker volume `rka-data` (managed by Docker Desktop) | The SQLite database `/data/rka.db` and any project artifacts |
| `~/.claude/plugins/cache/rka/rka/<version>/` (macOS/Linux) or `%USERPROFILE%\.claude\plugins\cache\rka\rka\<version>\` (Windows) | The installed plugin: skills, commands, hooks, wrapper script |
| `~/Library/Application Support/RKA/integration.json` (macOS), `%APPDATA%\RKA\integration.json` (Windows), or the XDG data path on Linux | Optional launcher/native-app metadata; the current plugin/setup helper does not create it and the wrapper can locate the uv-installed binary without it |
| `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) or `%APPDATA%\Claude\claude_desktop_config.json` (Windows) | Claude Desktop's MCP config with the `rka` entry; backups of any prior version live alongside it as `*.backup-YYYYMMDD-HHMMSS` |
| `~/.claude/plugins/installed_plugins.json` | Claude Code's registry of installed plugins (includes `rka@rka` entry after install) |
| `~/.claude/plugins/known_marketplaces.json` | Claude Code's registry of marketplace sources (includes the `rka-project/rka-core` GitHub source after `/plugin marketplace add`) |
| Docker volume `orchestrator-data` *(agentic only)* | `/data/orchestrator.db` (parked-interrupt queue + workflow_runs) and `/data/orchestrator-saver.db` (LangGraph checkpointer) |
| `<your-clone-dir>/rka-core/orchestrator/.env` *(agentic only, mode 0600)* | `CLAUDE_CODE_OAUTH_TOKEN`, `RKA_LEGACY_TOOLS=1`, and optional API keys |
| `<your-clone-dir>/rka-core/.env` *(agentic only, optional)* | `HOST_WORKSPACE_ROOT` for non-`$HOME` workspace bind mount (see §11 Workspace bind mount) |
| `~/.claude.json` *(agentic only, mounted read-only into container)* | Host's Claude CLI global config consumed by the orchestrator daemon's SDK subprocess |
| [`docs/v2.6.x-v2.7.0-tool-surface-arc.md`](docs/v2.6.x-v2.7.0-tool-surface-arc.md) | Historical narrative of the v2.6 → v2.7.0 tool-surface migration; use `rka_describe("")` for the current typed operation inventory |
