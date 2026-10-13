# RKA Usage Guide — Setting Up Claude Desktop + Claude Code as Brain & Executor (v2.7.0+)

This guide walks PIs (researchers) through the full setup and research workflow, from a fresh laptop to producing research outputs with Brain (strategy) and Executor (implementation) actors.

> **Tool surface.** The rka MCP server ships a **discriminated-union dispatch architecture** plus two package-only skill reads — 7 always-on tools at startup:
>
> | Tool | Purpose |
> |---|---|
> | `rka_query` | Dispatcher for **68 read operations** (`status`, `list_projects`, `search`, `research_map`, `semantic_patch_proposals`, `manuscript_spine`, `changes_since`, …). Call with `args={"operation": "…", ...}`. |
> | `rka_execute` | Dispatcher for typed write/lifecycle operations (`record_note`, `record_decision`, `create_mission`, `create_semantic_patch_proposal`, `apply_semantic_patch_proposal`, `submit_report`, `submit_checkpoint`, …). Call with `args={"operation": "…", ...}`. |
> | `rka_describe` | Introspect operation schemas. `rka_describe("")` returns the operation index (<250 tokens); `rka_describe("record_decision")` returns the full typed schema with required fields, enums, and provenance constraints. |
> | `rka_load_tools` | Escape hatch — surface deferred legacy tools (the v2.6.x 91-tool surface and the v2.7.0a2 verb surface live at `tier=deferred`). |
> | `rka_help` | Escape hatch — list available operations or describe a single one (alias for `rka_describe`). |
> | `rka_list_skills` | List packaged role guides; available at startup without dynamic loading. |
> | `rka_read_skill` | Read a packaged guide or reference directly; no backend request or project hook. |
>
> **Call shape.** The real dispatch tools take a single `args` object — e.g. `rka_query(args={"operation": "status", "project_id": "prj_…"})`. The worked examples below elide the `args={…}` wrapper for readability (`rka_query(operation="status", …)`); what matters is that the operation name and parameter names are correct.
>
> Every operation is backed by a typed Pydantic model. FastMCP renders the catalog as `oneOf` branches with per-branch enum, required-field, and cross-field enforcement. Invalid values such as `confidence='confirmed'`, or a `submit_checkpoint` call with neither canonical `description` nor legacy `content`, are rejected at the inputSchema layer before dispatch. See `docs/v2.6.x-v2.7.0-tool-surface-arc.md` for the historical design narrative and use `rka_describe("")` as the current authority.
>
> `RKA_LEGACY_TOOLS=1` restores the historical compatibility surface; legacy tools otherwise stay at `tier=deferred`. Ordinary PI sessions should leave this compatibility switch unset.

> ### ⚠️ This guide requires the **Claude Desktop** native app — **NOT** the [claude.ai](https://claude.ai) website.
>
> RKA connects to Claude through MCP (Model Context Protocol), and **only the desktop and IDE apps support MCP servers** — the web app at claude.ai does not. You will need:
>
> | Actor | App | Role |
> |-------|-----|------|
> | **Brain** | [Claude Desktop](https://claude.ai/download) (macOS / Windows) | Strategy, synthesis, knowledge organization |
> | **Executor** | [Claude Code](https://www.anthropic.com/claude-code) (VS Code extension or CLI) | Implementation, experiments, coding |
> | **PI** | You, the human researcher | Supervises both, ratifies decisions |
>
> All three share one memory — RKA — so context survives across sessions.
>
> **Note (2026-06-03):** the role split above is unchanged, but the **human-PI cockpit seat** has shifted. The recommendation is now **Claude Code as the primary PI cockpit**, with Claude Desktop reserved for two Phase O onboarding steps. See [Cockpit reference](#cockpit-reference) below for the rationale and workflow-by-workflow guidance.

---

## Table of Contents

- [Architecture at a glance](#architecture-at-a-glance)
- [Prerequisites](#prerequisites)
- [Setup — step by step](#setup--step-by-step)
  - [Step 1. Install Docker Desktop](#step-1-install-docker-desktop)
  - [Step 2. Clone and start RKA](#step-2-clone-and-start-rka)
  - [Step 3. Install the RKA MCP binary](#step-3-install-the-rka-mcp-binary)
  - [Step 4. Configure Claude Desktop (Brain)](#step-4-configure-claude-desktop-brain)
  - [Step 5. Configure Claude Code (Executor)](#step-5-configure-claude-code-executor)
  - [Step 6. Verify everything works](#step-6-verify-everything-works)
  - [Step 7. Load the Brain skill in Claude Desktop](#step-7-load-the-brain-skill-in-claude-desktop)
  - [Step 8. Load the Executor skill in Claude Code](#step-8-load-the-executor-skill-in-claude-code)
- [Cockpit reference](#cockpit-reference)
- [Starting Your First Session](#starting-your-first-session)
  - [What Happens at Session Start](#what-happens-at-session-start)
- [The Research Lifecycle](#the-research-lifecycle)
  - [Phase 1: Define Your Research](#phase-1-define-your-research)
  - [Phase 2: Collect Evidence](#phase-2-collect-evidence)
  - [Phase 3: Assign Work to the Executor](#phase-3-assign-work-to-the-executor)
  - [Phase 4: Review and Synthesize](#phase-4-review-and-synthesize)
  - [Phase 5: Produce Research Outputs](#phase-5-produce-research-outputs)
- [Working With Claude Code (Executor)](#working-with-claude-code-executor)
  - [How the Executor Picks Up Missions](#how-the-executor-picks-up-missions)
  - [The Backbrief](#the-backbrief)
  - [Checkpoints and Escalation](#checkpoints-and-escalation)
  - [Mission Reports](#mission-reports)
- [Validation Gates](#validation-gates)
- [Knowledge Freshness](#knowledge-freshness)
- [Using the Web Dashboard](#using-the-web-dashboard)
- [Multi-Project Workflows](#multi-project-workflows)
- [Knowledge Pack Export and Import](#knowledge-pack-export-and-import)
- [Tips and Best Practices](#tips-and-best-practices)
- [Troubleshooting](#troubleshooting)

---

## Architecture at a glance

```
┌─────────────────────┐      ┌─────────────────────┐
│  Claude Desktop     │      │  Claude Code        │
│  (Brain)            │      │  (Executor)         │
└─────────┬───────────┘      └─────────┬───────────┘
          │ MCP stdio                   │ MCP stdio
          ▼                             ▼
   ┌──────────────────────────────────────────┐
   │  rka MCP binary (~/.local/bin/rka)       │   ← installed via uv
   │  Thin proxy: forwards calls over HTTP    │
   └──────────────────┬───────────────────────┘
                      │ HTTP (localhost:9712)
                      ▼
   ┌──────────────────────────────────────────┐
   │  Docker containers                       │
   │  ├─ rka-server  (FastAPI + web UI)       │   ← `docker compose up -d`
   │  ├─ rka-worker  (background embeddings)  │
   │  └─ SQLite database (persistent volume)  │
   └──────────────────────────────────────────┘
```

You install the Docker stack once, then point both Claude apps at the MCP binary. Switching between Brain and Executor is just switching apps — both read and write the same shared memory.

---

## Prerequisites

Install these before running through Setup. Click the links to download.

| # | Component | Why you need it | Where to get it |
|---|-----------|-----------------|-----------------|
| 1 | **Docker Desktop** | Runs RKA's API + database in containers | [docker.com/products/docker-desktop](https://www.docker.com/products/docker-desktop/) |
| 2 | **uv** (Python package manager) | Installs the RKA MCP binary outside Docker | [docs.astral.sh/uv/getting-started/installation](https://docs.astral.sh/uv/getting-started/installation/) |
| 3 | **git** | Clones the RKA repo | Built into macOS / [git-scm.com/downloads](https://git-scm.com/downloads) |
| 4 | **Claude Desktop** (the **native app**, not [claude.ai](https://claude.ai) web) | Hosts the Brain | [claude.ai/download](https://claude.ai/download) |
| 5 | **Claude Code** (VS Code extension or CLI) | Hosts the Executor | [anthropic.com/claude-code](https://www.anthropic.com/claude-code) |

> **Why two Claude apps?** Brain and Executor are different roles, run in different sessions, with different skills loaded. Mixing them — using one Claude conversation to "do everything" — collapses the role separation that RKA's architecture is designed around.

---

## Setup — step by step

> Before configuring both clients, skim the [Cockpit reference](#cockpit-reference) below to understand which client to use as your primary cockpit. The setup steps still install both apps; the cockpit choice is about where you spend the PI seat day-to-day.

### Step 1. Install Docker Desktop

1. Download from [docker.com/products/docker-desktop](https://www.docker.com/products/docker-desktop/) (macOS / Windows / Linux)
2. Install and launch the app. Wait until the whale icon in the menu bar / system tray says **"Docker Desktop is running"**.
3. Verify on the command line:

   ```bash
   docker --version
   docker compose version
   ```

Both should print version numbers without errors.

### Step 2. Clone and start RKA

```bash
git clone https://github.com/rka-project/rka-core.git
cd rka-core
docker compose up -d
```

The first run will pull base images and build the web UI — expect 3–5 minutes. Subsequent starts are seconds.

Verify:

```bash
docker compose ps
# Should show rka-server (healthy) and rka-worker running
```

Open [http://localhost:9712](http://localhost:9712) in your browser — you should see the RKA web dashboard.

> **Tip — keep this terminal handy.** When you bump RKA later, you'll come back here and run `git pull && docker compose up -d --build`.

### Step 3. Install the RKA MCP binary

Claude Desktop and Claude Code reach RKA through a small command-line binary that runs outside Docker. It's a thin proxy that forwards MCP tool calls to the container's REST API.

From the **same `rka/` directory** as Step 2:

```bash
UV_CACHE_DIR=/tmp/uv-cache uv tool install --force .
```

This installs `rka` at `~/.local/bin/rka`. Verify:

```bash
~/.local/bin/rka --version
```

If `~/.local/bin` is not on your `PATH`, either add it (e.g. in `~/.zshrc`: `export PATH="$HOME/.local/bin:$PATH"`) or always reference the full path in the configs below.

> **After upgrading RKA** (`git pull`), re-run this command **and** `docker compose up -d --build` to refresh both halves.

### Step 4. Configure Claude Desktop (Brain)

1. Open **Claude Desktop**.
2. Go to **Settings → Developer → Edit Config**, or open the file directly:
   - **macOS:** `~/Library/Application Support/Claude/claude_desktop_config.json`
   - **Windows:** `%APPDATA%\Claude\claude_desktop_config.json`
   - **Linux:** `~/.config/Claude/claude_desktop_config.json`
3. Add (or merge into) the following. **Replace `<your-username>` with your actual username** — the path must be absolute, not `~`:

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

   On Windows the command is something like `C:\\Users\\<your-username>\\.local\\bin\\rka.exe`.
4. Save the file and **fully quit Claude Desktop** (Cmd+Q on macOS / right-click tray icon → Quit on Windows), then reopen it. A reload alone is not always enough.

You should now see RKA tools available in any new conversation.

**State the project at the start of each session (v2.6+).** v2.6 removed the `RKA_PROJECT` env-var "default project" mechanism — it reintroduced the silent-default failure mode that v2.6 explicitly eliminates. Instead, name the project in the opening line of every conversation: *"I'm working on prj_01KSMW9R…"* (or *"on the IoT broker scalability project"* — the LLM resolves the slug via `rka_query(operation="list_projects")`). The LLM retains it in working memory and passes `project_id="prj_…"` on every `rka_query` / `rka_execute` call. If `project_id` is omitted, the dispatcher rejects the call at the inputSchema layer rather than silently writing to `proj_default`. To find your project id: open `http://localhost:9712`, switch to the project you want, and copy the id from the URL — or run `rka_query(operation="list_projects")` once in a Claude session.

### Step 5. Configure Claude Code (Executor)

Claude Code uses the same MCP binary. There are two common ways to register it:

**Option A — VS Code extension (graphical):** Open VS Code → Claude Code panel → click the gear → **MCP Servers** → **Add Server**. Use the same JSON shape as Step 4.

**Option B — Per-project config file (recommended for teams):** Create `.claude/mcp.json` in your repo root with:

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

After saving, reload the VS Code window: **Cmd+Shift+P** (macOS) / **Ctrl+Shift+P** (Windows/Linux) → **"Developer: Reload Window"**.

If you're using the Claude Code CLI instead of the extension, the equivalent config lives in `~/.claude/settings.json` under the same `mcpServers` key.

### Step 6. Verify everything works

**In Claude Desktop** (Brain), start a **new conversation** and ask:

> List my RKA projects.

On the v2.7.0 dispatch surface, `list_projects` is an always-on operation under `rka_query`. Claude should call `rka_query(operation="list_projects")` and return a list (an empty list is fine on a fresh install). No load-tools dance is required — the dispatcher is on the wire from the first turn.

**In Claude Code** (Executor), open any project and ask:

> Check the RKA status.

Claude should call `rka_query(operation="status", project_id="prj_…")` and return the current project state. The first time this lands without a `project_id` you'll see a dispatcher validation error — that's by design (see "State the project at the start of each session" above).

If either fails:
- ✓ Is Docker running? `docker compose ps` should show `rka-server (healthy)`.
- ✓ Is the binary installed? `~/.local/bin/rka mcp` should hang waiting for stdin — Ctrl+C to exit.
- ✓ Did you fully quit-and-reopen Claude Desktop, or reload the VS Code window?
- ✓ Is the path in the JSON config absolute (not `~/.local/bin/rka`)?

> **Optional — manage MCP credentials with `rka cred init` (v2.7.0.4+).** If your projects use API-keyed MCP servers (Semantic Scholar, SerpAPI, Zotero, OpenAlex, etc.), `rka cred init` walks you through writing a single XDG-compliant vault at `~/.config/rka/creds.env` (mode 0600). Subcommands: `rka cred set <KEY>`, `rka cred env`, `rka cred check`, `rka cred propagate`. See [`docs/CRED_VAULT.md`](docs/CRED_VAULT.md) for the full reference. This replaces ad-hoc per-project `.env` files for shared credentials; per-project addons are supported but optional.

### Step 7. Load the Brain skill in Claude Desktop

The MCP server ships **role skills** as MCP prompts: `brain_skill`, `executor_skill`, and `pi_skill`. These are detailed workflow guides (~450 lines for the Brain) that teach Claude the session-start protocol, PI-attribution discipline, provenance rules, and anti-patterns.

In Claude Desktop, the simplest way is to type:

> Load your brain skill.

Claude will call the `brain_skill` MCP prompt and adopt that workflow for the rest of the conversation.

You can also invoke it explicitly: type `/` in the chat input → select **rka → brain_skill** from the prompt menu (Claude Desktop surfaces all `@mcp.prompt()` entries from connected servers there).

The Brain skill covers:
- Session start protocol (the exact tool-call sequence on every new conversation)
- PI attribution discipline (`source="pi"`, `verbatim_input="..."` — preserving your exact words)
- Provenance discipline (linking decisions ↔ journal ↔ missions ↔ literature)
- Claim extraction best practices
- Multi-task parsing (splitting compound instructions into separate missions)
- Coordinating with the Executor
- Research Map navigation
- Anti-patterns to avoid

> **Repeat at the start of every Brain conversation.** MCP prompts don't auto-load; loading the skill once at the top of a chat is the standard pattern.

### Step 8. Load the Executor skill in Claude Code

In Claude Code, ask:

> Load your executor skill.

Claude will call the `executor_skill` MCP prompt. The Executor skill covers:
- How to pick up a mission (`rka_query(operation="mission")` → read `motivated_by_decision` → load context)
- The **Backbrief** protocol (presenting your plan before starting)
- When to raise a checkpoint vs. proceed autonomously
- Mission-report format (summary / findings / anomalies / questions)
- Scope discipline (no out-of-scope changes; raise a checkpoint instead)

> **Tip — pin a project-level CLAUDE.md.** For repos where the Executor will work often, keep a project-specific `CLAUDE.md` at the repo root (its RKA `project_id`, phase conventions, and where evidence lives). Claude Code auto-loads it on every session, so the Executor starts with the right context even before the skill is loaded.

### Optional — using RKA from ChatGPT (custom connector)

Prefer to drive RKA from ChatGPT instead of (or alongside) Claude? RKA ships a remote-connector path: run the local MCP server in HTTP mode on `127.0.0.1:9713` with `RKA_SKILL_TOOLS=1` (the 8-tool skill surface), put the OAuth reverse proxy (`scripts/rka_mcp_oauth_proxy.py`) in front of it on `127.0.0.1:9720`, expose that over HTTPS with ngrok, and register the resulting `https://<ngrok-host>/mcp` URL as a ChatGPT custom connector with OAuth. Only the MCP server is tunneled — the web UI stays private — and no secret values are ever printed. See [`docs/CHATGPT_CONNECTOR.md`](docs/CHATGPT_CONNECTOR.md) for the full step-by-step.

---

## Historical Agentic cockpit reference — unsupported

> **Shelved on 2026-08-27.** This section records the former Agentic cockpit
> evaluation and is not current RKA Core guidance. For supported clients, use
> the typed Core MCP surface and the Brain/Executor/PI role guides described
> above. Do not install or operate the orchestrator described below.

Brain and Executor remain distinct roles, but for the **human-PI cockpit seat** (the surface where you supervise, ratify, and respond to parked interrupts) the recommendation as of 2026-06-03 is **Claude Code as the primary cockpit, Claude Desktop reserved for two specific Phase O onboarding steps**.

### Default cockpit choice (Claude Code)

The recommendation rests on a single load-bearing empirical fact. The rka MCP server emits the Anthropic-documented `_meta["anthropic/alwaysLoad"]: true` hint on all 5 always-on dispatch tools (`rka/mcp/server.py:485-513`), and on all 16 always-on orchestrator tools. Claude Code honors the hint — the tagged tools bypass per-turn retrieval filtering and surface deterministically at session start. Claude Desktop applies an opaque Retrieval-Augmented Tool Selection (RATS) filter that **ignores `alwaysLoad`** (`docs/v2.6.x-v2.7.0-tool-surface-arc.md:136-150`, `CHANGELOG.md:27-29` v2.7.0a2: "Empirically NO-OP on Claude Desktop"). Three closed-without-fix GitHub issues against `anthropics/claude-code` corroborate the divergence.

On 2026-06-02 / 2026-06-03 the pathology reproduced in production. The Desktop cockpit was running v2.7.0 with all 5 always-on rka tools tagged `alwaysLoad`, yet Desktop's RATS filter retrieved only 3 of them at session start (`rka_query`, `rka_describe`, `rka_load_tools` visible; `rka_execute` and `rka_help` silently dropped). The same filter degraded the orchestrator surface to 1 of 16 always-on tools. The PI had to type a trigger phrase ("I want to use `rka_execute`…") to force RATS to re-rank. Claude Code would have surfaced the full 5+16 set immediately.

Code is therefore the right default for everything that depends on deterministic tool surfacing: orchestrator inbox polling, the TWO-TAP autonomy-licensing gate at `pi_decision_select`, every other `pi_*` ratification gate, multi-day mission runs (`--resume` + JSONL audit), engineering on RKA itself (CLAUDE.md auto-load, slash commands), and `CLAUDE.md` + `MEMORY.md` continuity across sessions. The orchestrator daemon is client-agnostic (FastAPI surface at `README.md:32` + `rka/mcp/server.py:385`) and `workflow_thread_id` is the durable handle, so handing a run between Code and Desktop is a configuration switch, not a re-architecture. Migration cost ≈ 30–60 min; zero rollback risk because the Desktop config stays in place.

### When to use Claude Desktop

Two Phase O onboarding steps are deliberately retained on Desktop because they exploit chat-app affordances Code does not match:

- **Phase O O1 `pi_idea_capture`** — drag-and-drop ingest of the PI's proposal/PDF/DOCX. Desktop's native file-upload surface is the path of least resistance.
- **Phase O O2 `pi_deepresearch_prompt`** — Desktop's built-in multi-source Deep Research feature. Phase O was designed around it; until Code ships an equivalent, this step lives on Desktop.

### Code vs Desktop — capability comparison

| Dimension | Claude Code | Claude Desktop |
|---|---|---|
| `_meta["anthropic/alwaysLoad"]: true` (alwaysLoad hint) | Honored — tagged tools surface every turn | **Ignored** — empirically NO-OP (2026-06-03: 3-of-5 always-on rka tools surfaced, 1-of-16 orchestrator) |
| Per-turn tool filtering | `tool_search` is opt-in; `alwaysLoad` bypasses it | Opaque RATS filter ranks tools by similarity + history + cardinality; no bypass |
| `notifications/tools/list_changed` (runtime surface refresh) | Honored | Not honored empirically (falsified the v2.6.3 navigator pattern) |
| `AskUserQuestion` (structured choice prompts; required by orchestrator-pi skill, including TWO-TAP gate) | Native | No equivalent; fall back to numbered Markdown choice with verbatim-label reply |
| `CLAUDE.md` + auto-memory auto-load at session start | Yes (root + user-scope; `MEMORY.md` accumulates) | No equivalent; context re-loaded manually |
| Session resume (`--resume` + JSONL audit log) | Yes — multi-day runs are recoverable | No equivalent; conversations restart fresh |
| Slash commands (`/orchestrator-*`, `/rka-*`) | Native | Renders as prompts only |
| Drag-and-drop PDF/DOCX ingest | CLI-first; less ergonomic | Native chat-app file ingest — path of least resistance for Phase O O1 |
| Native multi-source Deep Research | Not present | Built-in — Phase O O2 was designed around it |
| Orchestrator daemon compatibility | Daemon subprocess uses `RKA_LEGACY_TOOLS=1` (`claude-agent-sdk` is Code-equivalent) | Cockpit drives daemon over MCP; daemon does not run inside Desktop |

### Workflow-by-workflow recommendation

| Workflow | Cockpit | Rationale |
|---|---|---|
| Steady-state orchestrator supervision (`orchestrator_inbox` polling, terminal-state review) | **Code** | Deterministic always-on surfacing of `orchestrator_*` tools via `alwaysLoad` |
| `pi_greenlight` / `pi_acceptance` ratification | **Code** | `AskUserQuestion` is native; alwaysLoad keeps `orchestrator_accept` / `reject` / `correct` visible |
| `pi_decision_select` TWO-TAP autonomy gate | **Code** | TWO-TAP requires deterministic dispatch-tool surfacing every turn; Desktop's RATS is unsafe at this gate |
| `pi_scope_ratify` / `pi_plan_ratify` / `pi_phase_entry_ack` | **Code** | Same `AskUserQuestion` + alwaysLoad reasoning |
| Engineering on RKA itself (rka/, orchestrator/, web UI edits) | **Code** | `CLAUDE.md` auto-load, slash commands, `--resume` |
| Multi-day mission supervision | **Code** | `--resume` + JSONL transcript = durable session continuity |
| RKA writes via `/orchestrator-*` and `/rka-*` slash commands | **Code** | Slash menu is Code-native |
| CLAUDE.md + auto-memory editing | **Code** | Auto-loaded; no Desktop equivalent |
| Phase O O1 — `pi_idea_capture` (drag-drop PDF/DOCX/proposal) | **Desktop** | Native file-upload surface |
| Phase O O2 — `pi_deepresearch_prompt` (multi-source Research) | **Desktop** | Desktop's native Deep Research feature |

### The 5 rka MCP prompts — when to load which

The rka MCP server exposes 5 prompts via `@mcp.prompt()` handlers at `rka/mcp/server.py:6974-7265`. They appear in Claude Desktop's `+` menu under "Add from rka" and in Claude Code's `/` slash menu under `rka`. Three are full skill files; two are shorter inline orientation fallbacks.

| Prompt | Source | Audience | When to load | Cockpit fit |
|---|---|---|---|---|
| `brain_skill` | `rka/skills/brain/SKILL.md` (289 lines, authoritative) | Brain-LLM (strategic) | Session start when a Claude session is taking the Brain role; before drafting a Confirmation Brief / Research Protocol / multi-choice decision; before reviewing Executor Backbriefs | Client-agnostic content; loads in Code or Desktop |
| `executor_skill` | `rka/skills/executor/SKILL.md` (188 lines, authoritative) | Executor-LLM (implementation) | On mission pickup; before producing a Backbrief or mission report; at session start for any Executor (including the daemon subprocess) | Code-leaning — workflows assume Bash/Read/Write/Edit/Grep/Glob native to Code |
| `pi_skill` | `rka/skills/pi/SKILL.md` (70 lines, lightweight) | The LLM helping the human PI supervise | Any PI cockpit session — checkpoint review, banking verbatim PI guidance, project-status / research-map inspection. Optimized for Code (auto-load + slash commands) but works in both | **Code** (primary) |
| `brain_orientation` | Inline string in `server.py:6992-7137` (~145 lines) | Brain-LLM, fallback for clients without Agent Skills | When the full `brain_skill` cannot be loaded (legacy/minimal MCP clients); as a token-efficient refresher for an already-oriented Brain | Either cockpit; explicitly defers to `brain_skill` where supported |
| `executor_orientation` | Inline string in `server.py:7140-7265` (~125 lines) | Executor-LLM, fallback for clients without Agent Skills | When the full `executor_skill` cannot be loaded; as a token-efficient refresher; NOT loaded inside the daemon subprocess (which already injects `EXECUTOR_SYSTEM`) | Either cockpit; explicitly defers to `executor_skill` where supported |

Quick rule: load a `*_skill` whenever the cockpit supports Agent Skills (both Code and Desktop do). The `*_orientation` prompts are the legacy/minimal-client fallback and the short refresh option. Do not load both Brain and Executor skills in the same session — they teach distinct roles and mixing them bloats context and blurs the role boundary.

### Cross-session pattern

Because the orchestrator daemon is client-agnostic and `workflow_thread_id` is the durable cross-cockpit handle, a typical Phase O run can hand off cleanly between cockpits:

1. **Start in Code.** `/orchestrator-onboard prj_…` from Claude Code. The first parked interrupt (`pi_onboarding_topic` or `pi_idea_capture`) appears; the workflow_thread_id is minted.
2. **Hand off to Desktop for O1 + O2.** Open Claude Desktop; ask "check orchestrator inbox" — Desktop's `orchestrator_inbox()` lists the same parked interrupt because the daemon shares state. Drag the PDF/proposal into Desktop for `pi_idea_capture`; later, use Desktop's Deep Research feature for `pi_deepresearch_prompt`.
3. **Return to Code for O3–O5 + the H-step mission queue handoff.** Reopen Code, ask "check orchestrator inbox" or run `orchestrator_get_run(<workflow_thread_id>)`. Code surfaces the next parked interrupt; steady-state supervision resumes with `--resume` durability and CLAUDE.md auto-load.

The Desktop config stays installed alongside Code's. No state migration; the `workflow_thread_id` makes the handoff a configuration switch, not a re-architecture.

---

## Starting Your First Session

### What Happens at Session Start

When you start a conversation with the Brain, it should automatically:

1. **Check what changed** — `rka_query(operation="changelog", filters={"since": "2026-07-05"}, project_id="prj_…")` shows new entries, decisions, claims, and missions since your last session
2. **Load the research map** — `rka_query(operation="research_map", project_id="prj_…")` shows your research questions, evidence clusters, and claim counts
3. **Process maintenance** — `rka_query(operation="pending_maintenance", project_id="prj_…")` detects provenance gaps. The Brain silently fixes up to 10 items (adding missing links, tags, or claim extractions)
4. **Greet you** with a summary of where things stand

You don't need to tell Claude to do this — the skill guide instructs it to.

---

## The Research Lifecycle

### Phase 1: Define Your Research

**You (PI) tell the Brain your research direction:**

> "I want to study whether horizontal sharding can solve MQTT broker scalability problems under high device density."

**What the Brain does:**

1. **Confirmation Brief** — The Brain restates your intent to verify understanding:

   > *"Let me make sure I understand: you want to identify the device density threshold where MQTT brokers degrade, and then test whether horizontal sharding mitigates the problem. Assumptions: lab environment, QoS 1, single-broker baseline. Does this match your intent?"*

2. After you confirm, the Brain **records your direction** with proper attribution:

   ```
   rka_execute(
     operation="record_note",
     content="Brain analysis: PI directed study of MQTT broker scalability...",
     source="pi",
     verbatim_input="I want to study whether horizontal sharding can solve MQTT broker scalability problems under high device density.",
     type="directive",
     tags=["research-protocol", "gate-0"],
     project_id="prj_…"
   )
   ```

   Your exact words are preserved in `verbatim_input`. The Brain's interpretation goes in `content`. These are kept separate so your intellectual contribution is always traceable.

3. The Brain **creates research questions** as decision nodes:

   ```
   rka_execute(
     operation="record_decision",
     question="At what device density does MQTT broker performance degrade beyond 5% packet loss?",
     chosen="Investigate the 300–500 device range first",
     rationale="Smith & Lee report degradation above 400 devices; bracket that with our own trials.",
     kind="research_question",
     decided_by="pi",
     phase="gate-0",
     related_journal=["jrn_…"],
     assumptions=["Network latency negligible in lab", "Devices publish at 1 msg/sec"],
     project_id="prj_…"
   )
   ```

### Phase 2: Collect Evidence

**Adding literature:**

> "I found a paper by Smith and Lee (2024) on MQTT broker stress testing. They report 12% packet loss above 400 devices."

The Brain records the paper and processes your reading annotations:

```
rka_execute(
  operation="record_literature",
  title="MQTT Broker Performance Under Stress",
  authors=["Smith, J.", "Lee, K."],
  year=2024,
  project_id="prj_…"
)

rka_execute(
  operation="process_paper",
  lit_id="lit_01...",
  summary="Benchmarks MQTT brokers at scale. Key finding: 12% packet loss above 400 devices.",
  annotations=[
    {"text": "Table 3: 12% packet loss at 400 devices", "note": "Threshold lower than expected",
     "claim_type": "evidence", "confidence": "tested", "cluster_id": "ecl_01..."}
  ],
  project_id="prj_…"
)
```

`process_paper` does three things in one call:
- Creates a journal entry with your reading notes
- Extracts structured claims from each annotation
- Assigns claims to evidence clusters

**The Brain creates evidence clusters** to organize related claims:

```
rka_execute(
  operation="create_cluster",
  label="Broker Performance Thresholds",
  research_question_id="dec_01...",
  project_id="prj_…"
)
```

As you discuss more papers and findings, the Brain extracts claims and assigns them to clusters. The Research Map grows organically.

### Phase 3: Assign Work to the Executor

When there's implementation work to do (experiments, code, data collection), the Brain creates a **mission** for the Executor:

> "We need to run our own stress test to verify Smith & Lee's numbers."

The Brain creates a mission with a structured handoff:

```
rka_execute(
  operation="create_mission",
  objective="Run stress test to verify packet loss measurements at 400 devices",
  tasks=[
    {"description": "Replicate Smith & Lee setup (Mosquitto 2.0, 4-core, QoS 1)"},
    {"description": "Run 5 trials at 400 devices, compute mean and stddev"},
    {"description": "Compare results with published 12% figure"}
  ],
  context="INTENT: Verify published packet loss threshold...\nBACKGROUND: Smith & Lee report 12%...\nCONSTRAINTS: Do not modify broker config...\nASSUMPTIONS: 1. Network latency negligible...\nVERIFICATION: Mean packet loss with 95% CI",
  motivated_by_decision="dec_01...",
  project_id="prj_…"
)
```

**To hand this to the Executor**: Open Claude Code and tell it:

> "Pick up mission mis_01... from RKA"

See [Working With Claude Code (Executor)](#working-with-claude-code-executor) for details.

### Phase 4: Review and Synthesize

After the Executor completes work and submits a report, the Brain:

1. **Reviews the report** — `rka_query(operation="report", id="mis_01...", project_id="prj_…")`
2. **Checks for contradictions** — `rka_query(operation="contradictions", id="clm_01...", project_id="prj_…")`
3. **Flags stale evidence** — `rka_execute(operation="flag_stale", entity_id="clm_01...", reason="Contradicted by our experiment", project_id="prj_…")`
4. **Writes cluster syntheses** — `rka_execute(operation="review_cluster", cluster_id="ecl_01...", confidence="moderate", synthesis="Our 5-trial experiment shows 8.2% mean packet loss...", project_id="prj_…")`
5. **Advances research questions** — `rka_execute(operation="advance_rq", rq_id="dec_01...", status="partially_answered", conclusion="Threshold identified at ~400 devices", project_id="prj_…")`

### Phase 5: Produce Research Outputs

When you need a draft for a paper section, literature review, or progress report:

> "Give me a progress report on the broker scalability question"

The Brain calls:

```
rka_query(
  operation="evidence",
  id="dec_01...",
  filters={"format": "progress_report"},
  project_id="prj_…"
)
```

This produces a structured markdown document pulling together:
- Key findings (top claims by confidence)
- Decisions made (with rationale)
- Current gaps
- Suggested next steps

Three formats are available (passed as `filters={"format": "…"}`):
- `progress_report` — findings + decisions + gaps + next steps
- `briefing` — a concise summary for quick orientation
- `audit` — the full evidence trail with provenance for verification

The output is a starting point — the Brain refines it before presenting to you.

---

## Working With Claude Code (Executor)

### How the Executor Picks Up Missions

In Claude Code, tell it to pick up a mission:

> "Pick up your RKA mission"

or if you have a specific mission ID:

> "Work on mission mis_01KP4DB5PZF7YXYRPV2AGQJSE6"

The Executor will:
1. Call `rka_query(operation="mission", project_id="prj_…")` to load the mission details
2. Read the `motivated_by_decision` to understand WHY the work exists
3. Read all context links (journal entries, decisions, literature)
4. Load the Executor skill for workflow guidance

### The Backbrief

Before starting significant work, the Executor presents a **Backbrief** — its plan for accomplishing the mission. This catches misalignment early:

> *"Before I start, here's my plan: I'll replicate the Smith & Lee setup in Docker, run 5 independent trials at 400 devices, and compute mean ± stddev. I interpret 'verify' to mean checking if our results fall within the published confidence interval..."*

The Executor records the Backbrief as a journal entry tagged `backbrief` and waits for the Brain to approve. You can review it in Claude Desktop:

> "The Executor submitted a backbrief for the stress test mission. Review it."

### Checkpoints and Escalation

During execution, the Executor raises **checkpoints** when it hits problems:

- **Assumption invalidation** — "The mission assumes network latency is negligible, but I measured 5ms"
- **Scope expansion** — "Fixing this requires changes outside the stated scope"
- **Contradictory results** — "Our measurements don't match the expected values"

Checkpoints appear in Claude Desktop via `rka_query(operation="checkpoints", filters={"status": "open"}, project_id="prj_…")`. You and the Brain resolve them:

> "The Executor flagged that network latency isn't negligible. Tell it to re-run with simulated latency."

### Mission Reports

When the Executor finishes, it submits a report via `rka_execute(operation="submit_report", ...)` with:
- **Summary**: What was done and what was found
- **Findings**: Key results
- **Anomalies**: Unexpected observations
- **Questions**: Open questions for the PI

The Brain reviews the report and either marks the mission complete or creates follow-up missions.

---

## Validation Gates

Gates are formal go/no-go checkpoints at critical transitions. They prevent compounding errors by forcing evaluation before proceeding.

### The 4 Gate Types

| Gate | When | Who Creates | Who Evaluates |
|------|------|-------------|---------------|
| **Gate 0: Problem Framing** | Before research starts | Brain | Brain + PI |
| **Gate 1: Plan Validation** | After mission created, before Executor starts | Brain | Brain (reviews Backbrief) |
| **Gate 2: Evidence Review** | After experiments/evidence gathering | Executor | Brain + PI |
| **Gate 3: Synthesis Validation** | Before committing conclusions | Brain | Brain + PI |

### Example: Using Gates

**You say**: "Create a gate before the Executor starts the stress test."

The Brain creates a Gate 1:

```
rka_execute(
  operation="create_gate",
  mission_id="mis_01...",
  gate_type="plan_validation",
  deliverables=["Executor Backbrief journal entry"],
  pass_criteria=["Plan addresses all tasks", "Assumptions are consistent"],
  assumptions_to_verify=["Network latency is negligible"],
  project_id="prj_…"
)
```

After the Executor submits its Backbrief, the Brain evaluates:

```
rka_execute(
  operation="evaluate_gate",
  gate_id="chk_01...",
  verdict="go",
  notes="Plan is aligned. Proceed.",
  assumption_status={"Network latency is negligible": "validated"},
  project_id="prj_…"
)
```

Verdicts:
- **Go** — proceed to the next phase
- **Kill** — abandon this direction
- **Hold** — wait for more information
- **Recycle** — revise and resubmit

If any assumption is marked `"invalidated"`, RKA automatically flags the related decision as stale and propagates through the knowledge graph.

### When to Use Gates

Not every task needs all 4 gates:
- **Quick bug fix**: Gate 1 only (Backbrief)
- **New research direction**: All 4 gates
- **Literature review**: Gate 0 (protocol) + Gate 3 (synthesis validation)
- **Experiment**: Gate 1 (plan) + Gate 2 (evidence review)

---

## Knowledge Freshness

RKA tracks whether evidence is still current. As new findings arrive, old claims may become stale.

### Staleness Levels

| Level | Meaning | Icon |
|-------|---------|------|
| Green | Fresh — no known issues | 🟢 |
| Yellow | Aging or partially conflicting | 🟡 |
| Red | Directly contradicted or invalidated | 🔴 |

### How Staleness Works

1. **Detection**: The Brain runs `rka_query(operation="freshness", project_id="prj_…")` to find aging claims, superseded sources, and clusters with stale evidence
2. **Flagging**: `rka_execute(operation="flag_stale", entity_id, reason, propagate=true, project_id="prj_…")` marks a claim as stale
3. **Propagation**: When `propagate=true`, staleness cascades:
   - Stale claim → if >50% of claims in a cluster are stale → cluster flagged
   - Stale cluster → decisions citing it are flagged
4. **Resolution**: The Brain reviews stale items and either updates them with new evidence or confirms they're still valid

### Contradiction Detection

When new evidence conflicts with existing claims:

```
rka_query(operation="contradictions", id="clm_01...", project_id="prj_…")
```

Returns semantically similar claims that may conflict. The Brain reviews and decides:
- Are they genuinely contradictory?
- Should the old claim be flagged stale?
- Does this change any decisions?

---

## Using the Web Dashboard

The web dashboard at http://localhost:9712 provides a visual interface for browsing your research without using Claude.

### Key Pages

| Page | What You See |
|------|-------------|
| **Dashboard** | Project overview, recent entries, active missions, export/import controls |
| **Research Map** | Three-level drill-down: research questions → clusters → claims. Click a cluster to see full synthesis, all claims, and edit confidence |
| **Journal** | Timeline of all entries grouped by date, with type/confidence filters |
| **Decisions** | Interactive decision tree visualization |
| **Literature** | Table with reading pipeline status (to_read → reading → read) |
| **Missions** | Active and historical missions with task progress |
| **Knowledge Graph** | Entity relationship graph showing provenance links |
| **Settings** | Embedding-backend configuration (Settings → Embeddings), API health, database stats |

### Project Selection

Use the sidebar to switch between projects. The dashboard stores your active project locally and applies it to all API calls.

---

## Multi-Project Workflows

RKA supports multiple isolated research projects in the same database.

**Create a new project:**

> "Create a new RKA project called 'IoT Broker Scalability'"

The LLM calls `rka_execute(operation="create_project", name="IoT Broker Scalability", ...)` and gets back the new `prj_…` id.

**Switch between projects:**

> "We're now working on the IoT Broker Scalability project (prj_01ABC...)"

There is no "switch" command in v2.6+ — `rka_set_project` was a deprecated no-op as of v2.6 and is `tier=deferred` on the v2.7.0 dispatch surface. Project scoping is per-call: every `rka_query` / `rka_execute` operation takes `project_id` as a kwarg and the LLM threads it from conversation memory. If the LLM omits `project_id`, the dispatcher rejects the call at the inputSchema layer — replacing the pre-v2.6 silent-write-to-`proj_default` failure mode with a hard error.

---

## Knowledge Pack Export and Import

Knowledge packs are portable snapshots of a project — all data in a single `.rka-pack_v2.zip` file.

### Export

**From the web dashboard**: Dashboard → Export Pack

**From the REST API**: `GET /api/projects/export` (export is REST-only — there is no `export` operation on the `rka_query` dispatch surface; the web dashboard's **Export Pack** button calls this endpoint).

The pack includes schema version metadata and table counts. The categorized table registry ensures no tables are silently dropped during export.

### Import

**From the web dashboard**: Dashboard → Import Pack → select the .zip file

**From REST API**:
```bash
curl -X POST http://localhost:9712/api/projects/import \
  -F "file=@my_project.rka-pack_v2.zip"
```

After import, RKA automatically runs an integrity check and reports any issues (orphaned edges, missing references, count mismatches).

### Before Upgrades

Before upgrading RKA to a new version:
1. Export all projects as knowledge packs
2. Run `rka_query(operation="integrity", project_id="prj_…")` to verify current state
3. Upgrade and rebuild Docker
4. Verify the migration ran cleanly
5. Run `rka_query(operation="integrity", project_id="prj_…")` again

---

## Tips and Best Practices

### For the PI

1. **Be specific when giving direction** — The Brain will create a Confirmation Brief to verify understanding. Correct any misalignment immediately — it's much cheaper to fix now than after implementation.

2. **Let the Brain handle recording** — Don't worry about which tool to use. Just tell the Brain what you're thinking. It handles the attribution (`source: "pi"`, `verbatim_input: "your exact words"`).

3. **Review the Research Map regularly** — Open http://localhost:9712/research-map or ask the Brain: "Show me the research map." It tells you at a glance which questions have strong evidence and which have gaps.

4. **Use the web dashboard for browsing** — It's faster than asking Claude for routine lookups. The Research Map page lets you click into clusters, see all claims, and even edit confidence and synthesis directly.

5. **Keep sessions focused** — Start each Brain session with context about what you want to accomplish. The Brain loads prior state automatically, but knowing your goal for *this session* helps it prioritize.

### For Working With the Brain

1. **Trust the session start protocol** — The Brain checks for changes, processes maintenance, and loads the research map before greeting you. This takes a few seconds but ensures it has full context.

2. **Give compound instructions naturally** — If you say "fix the search, update the docs, and check the import," the Brain should parse this into separate missions for the Executor rather than bundling everything together.

3. **Review gate evaluations** — When the Brain evaluates gates, it records assumption status. If assumptions are invalidated, staleness propagates automatically. Check these evaluations to stay informed.

### For Working With the Executor

1. **Let it backbrief** — When the Executor presents its plan, read it. Catching misalignment here saves hours.

2. **Don't skip missions** — Even for small tasks, creating a mission ensures the work is recorded with proper provenance (who asked for it, why, what was found).

3. **Check reports** — When the Executor submits a report, review it in Claude Desktop. The Brain can verify findings against the knowledge base.

---

## Troubleshooting

### "RKA tools not showing up in Claude Desktop"

1. Check the MCP config file path and JSON syntax (see [Step 4](#step-4-configure-claude-desktop-brain))
2. Make sure the `command` path is **absolute** — `~/.local/bin/rka` will fail; use `/Users/<your-username>/.local/bin/rka`
3. Fully quit Claude Desktop (Cmd+Q on macOS / right-click tray icon → Quit on Windows), then reopen — a window reload is not enough
4. Verify the binary works: `~/.local/bin/rka mcp` should hang waiting for stdin (Ctrl+C to exit)
5. Check Docker is running: `docker compose ps` should show `rka-server (healthy)`

### "RKA tools not showing up in Claude Code"

1. Check `.claude/mcp.json` (or VS Code's MCP settings) — same config shape as Claude Desktop
2. Reload the VS Code window: Cmd+Shift+P → "Developer: Reload Window"
3. If using the Claude Code CLI, edit `~/.claude/settings.json` instead

### "Brain skill not loading / Claude doesn't follow the workflow"

The skill is an MCP **prompt**, not a tool — it must be loaded explicitly at the top of each conversation. Type `/` in the chat input and look for the **rka → brain_skill** entry, or just ask: *"Load your brain skill."*

### "Tools return errors about connection refused"

The MCP binary proxies to `http://localhost:9712`. Make sure:
- Docker container is running and healthy
- Port 9712 is not blocked by firewall
- No other service is using port 9712

### "After code changes, tools behave the same as before"

The MCP binary caches aggressively. After any code changes:

```bash
uv tool uninstall rka
rm -rf /tmp/uv-cache
UV_CACHE_DIR=/tmp/uv-cache uv tool install --force --reinstall .
docker compose up -d --build
```

Then restart Claude Desktop and reload the VS Code window.

### "Knowledge pack export fails"

Run `rka_query(operation="integrity", project_id="prj_…")` to check for issues. Common causes:
- Tables missing from the registry (shows as explicit error naming the table)
- Orphaned edges (the integrity check reports these)

### "The web dashboard shows an old version number"

The version on the dashboard is read live from `/api/health`, which reflects the version baked into the running container — not the source code on disk. After `git pull`, rebuild the container:

```bash
docker compose up -d --build
```

If the build fails on macOS with errors about `._*` files (AppleDouble metadata on non-APFS volumes), run `dot_clean .` in the repo root and retry.

---

## Quick Reference Card

### Brain (Claude Desktop) — Key Commands

| You say... | Brain does... |
|------------|--------------|
| "Start a new research project about X" | Creates project, research protocol, initial RQs |
| "I found a paper by..." | Records literature, processes annotations, extracts claims |
| "We should focus on X" | Confirmation Brief → records directive with `verbatim_input` |
| "Create a mission for the Executor to..." | Creates mission with structured handoff |
| "Review the Executor's report" | Reads report, checks findings, marks mission complete |
| "Show me the research map" | Displays RQs → clusters → claims hierarchy |
| "What changed since yesterday?" | Runs `rka_query(operation="changelog", filters={"since": "2026-07-05"}, project_id="prj_…")` |
| "Give me a progress report on RQ1" | Assembles evidence as structured markdown |
| "Check for stale evidence" | Runs freshness scan, flags outdated claims |

### Executor (Claude Code) — Key Commands

| You say... | Executor does... |
|------------|-----------------|
| "Pick up your mission" | Loads mission, reads context, presents Backbrief |
| "Check RKA status" | Shows project state, active missions, open checkpoints |
| "Submit your report" | Submits findings, anomalies, and recommendations |
| "Raise a checkpoint" | Creates blocking checkpoint for Brain/PI input |

### Web Dashboard — Key URLs

| URL | Page |
|-----|------|
| http://localhost:9712 | Dashboard (overview + export/import) |
| http://localhost:9712/research-map | Research Map (RQs → clusters → claims) |
| http://localhost:9712/journal | Journal entries timeline |
| http://localhost:9712/decisions | Decision tree visualization |
| http://localhost:9712/missions | Missions with task progress |
| http://localhost:9712/docs | API documentation (Swagger UI) |

---

## Historical Agentic Distribution — unsupported

> **Shelved on 2026-08-27. Do not use this section as current setup or usage
> guidance.** It is preserved as a historical operating record. RKA Core and
> RKA Writer do not require or support the Agentic runtime. See
> [ADR 0013](docs/adr/0013-shelve-agentic-and-focus-core-writer.md).

### When to use the orchestrator

The orchestrator is for **structured, multi-phase research workflows** where you want the Brain⇄Executor loop to advance automatically through well-defined gates, surfacing to you only at ratification points. Use it when:

- You have a defined research mission and want autonomous Brain reasoning between explicit PI gates
- You want the workflow to resume cleanly across sessions (state persists in SqliteSaver)
- You want a single audit trail per workflow run (every artifact tagged with `workflow_thread_id`)

For open-ended exploratory work, use the main-branch direct Brain⇄Executor pattern above. The two coexist — same RKA project can have both ad-hoc Brain sessions and orchestrator runs.

### Setup (one-time, in addition to main-branch setup)

```bash
# 1. Switch to the agentic branch (if not already)
git checkout agentic

# 2. Install the orchestrator MCP binary on the host
cd orchestrator
uv tool install --force .   # produces ~/.local/bin/rka-orchestrator-mcp

# 3. Mint a Claude Max OAuth token (in a separate terminal, NOT this Claude session)
claude setup-token          # browser flow; copies a long-lived token to stdout

# 4. Put the token in orchestrator/.env (gitignored, mode 0600)
nano orchestrator/.env
# CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-...
# SEMANTIC_SCHOLAR_API_KEY=...   (optional, for richer lit-search)
# SERPAPI_KEY=...                (optional, for web search)
chmod 600 orchestrator/.env

# 5. Bring up the orchestrator daemon alongside rka
cd ..
docker compose -f docker-compose.yml \
               -f orchestrator/docker-compose.yml up -d --build

# 6. Verify the daemon
curl http://localhost:9713/health     # {"status":"ok",...}

# 7. Add the second MCP server to claude_desktop_config.json
#    (alongside the existing "rka" entry)
```

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

Restart Claude Desktop. Your PI session now has **two layers of MCP tools**: `rka_*` for direct knowledge-base work and `orchestrator_*` for workflow driving.

### Driving a mission workflow

In any Claude Code or Claude Desktop session with both MCP entries configured:

```
[You:]
> Start orchestrator on mis_01XYZ in project prj_01ABC

[Claude:]
[invokes orchestrator_run_start(mission_id="mis_01XYZ", project_id="prj_01ABC")]
[graph runs ~2-5 min; parks at pi_greenlight with a Confirmation Brief]
[invokes orchestrator_inbox(); renders the Brief as a structured markdown block]
[uses AskUserQuestion to present: Accept / Reject / Correct]
```

You pick. On Accept, Claude calls `orchestrator_accept(interrupt_id)`. The daemon resumes the LangGraph (`backbrief_draft` → `gate1_validation` → `mission_execute` → `cluster_review` → `decision_present`), then parks again at `pi_decision_select` — **the privileged ratification gate** where Brain's `proposed_actions` are surfaced.

For `pi_decision_select`, the orchestrator-pi skill enforces a **TWO-TAP confirmation** (the actions about to be dispatched mean real RKA writes via the parent-side `execute_ratified_actions`). After your second confirm, Claude calls `orchestrator_accept`, the writes commit, and the graph proceeds to `pi_acceptance` and terminates.

### Slash commands (agentic distribution)

| Command | Purpose |
|---|---|
| `/orchestrator-start <mission_id>` | Start a mission workflow with health-check + mission inspection up-front |
| `/orchestrator-inbox` | Show pending PI interrupts across all runs; renders each per the orchestrator-pi skill |
| `/orchestrator-status [filter]` | List active + recent runs with cost + ETA |
| `/orchestrator-onboard <project_id>` | Run the Phase D MVP onboarding wizard for a new project (tool discovery + credential setup) |
| `/orchestrator-manifest <project_id>` | Show the project's current tool manifest |

### Onboarding a new project (Phase D MVP)

When you create a new RKA project that needs a per-project tool surface (e.g., a finance research project needing `sec-edgar-mcp`), the onboarding wizard handles tool discovery + credential setup:

```
[You:]
> /orchestrator-onboard prj_01NEW

[Claude:]
[invokes orchestrator_onboard_start(project_id="prj_01NEW")]
[parks at pi_onboarding_topic]
> "Tell me about the project — summary, field, target venue, keywords."

[You type a paragraph]

[Claude calls orchestrator_correct(interrupt_id, response_text=<your text>)]
[graph runs research_toolkit_node; Brain reasons over the curated registry + suggests tools]
[parks at pi_toolkit_ratify with a scored list]
[TWO-TAP: pick Accept all, then confirm authorization]

[Claude calls orchestrator_accept; graph runs draft_manifest_node]
[writes ~/rka-projects/<project_id>/tools.json + .env template (Phase D MVP convention)]
[parks at pi_credentials_ready with the file path + expected secrets list]

[You open ~/rka-projects/<project_id>/.env in your editor, fill in values, save, return]
[Claude calls orchestrator_accept]
[graph runs finalize_node: probes each secret, emits the audit journal entry]
[returns terminal_state="complete"]
```

The credential validation **never echoes secret values in chat**. The orchestrator probes each declared API server-side; you only see "valid" / "rejected" / "missing" classifications per secret name. (Phase O design will move the workspace under `~/Research/{slug}/.rka/` and add 4 more onboarding sub-phases for idea capture, deep research integration, hygiene, and plan synthesis — see `orchestrator/docs/phase-o-project-onboarding-design.md`.)

### Combining orchestrator + Deep Research + literature ingestion in Claude Desktop

The PI's Claude Desktop session has **three superimposed capability layers** when both MCP entries are wired up:

| Layer | Tools | Purpose |
|---|---|---|
| **Orchestrator (agentic)** | `orchestrator_run_start`, `orchestrator_onboard_start`, `orchestrator_inbox`, `orchestrator_accept`, ... | Drive structured LangGraph workflows |
| **RKA core (main)** | `rka_query` / `rka_execute` dispatchers — literature operations include `record_literature`, `enrich_doi`, `import_bibtex`, `ingest_document`, `link_literature_to_zotero`, …; arXiv / Semantic Scholar search are deferred tools (`rka_search_arxiv`, `rka_search_semantic_scholar`) loaded on demand via `rka_load_tools` | Ad-hoc literature work, journal entries, claims |
| **Claude Desktop native** | Web search, Deep Research (Max tier), Projects, Artifacts | Open-ended exploration |

Practical pattern: use Claude Desktop's Deep Research for broad exploration. When you find a paper worth keeping, call `rka_execute(operation="record_literature", doi="...", project_id="prj_…")` to land it in the RKA project's knowledge base, then `rka_execute(operation="enrich_doi", lit_id="lit_…", project_id="prj_…")` to fill in metadata from the DOI. When the exploration coalesces into a defined mission, `orchestrator_run_start` invokes the structured workflow — the Brain reads everything you ad-hoc-ingested as context.

### Inspecting workflow state

| | |
|---|---|
| List runs | `orchestrator_list_runs(status="awaiting_pi")` or `GET http://localhost:9713/runs` |
| One run's details | `orchestrator_get_run(workflow_thread_id)` |
| Pending interrupts | `orchestrator_inbox()` |
| Project's manifest | `orchestrator_get_manifest(project_id)` |
| Cancel a run | `orchestrator_cancel(workflow_thread_id)` |

Run artifacts in RKA are recoverable via `rka_query(operation="journal", filters={"tags": [<workflow_thread_id>]}, project_id="prj_…")` — every write during the workflow auto-tags the thread ID.

### Cross-references

- `orchestrator/README.md` — orchestrator package reference + phase history
- `docs/archive/2026-q2/orchestrator/phase-d-onboarding-design.md` — Phase D MVP design (archived; superseded by Phase O)
- `orchestrator/docs/phase-o-project-onboarding-design.md` — Phase O full-workflow design (next implementation phase)
- `plugin/skills/orchestrator-pi/SKILL.md` — PI cockpit rendering + TWO-TAP rules
- `CLAUDE.md` (root, agentic-branch section) — agent operating instructions for orchestrator work
