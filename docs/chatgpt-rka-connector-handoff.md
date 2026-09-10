# ChatGPT RKA Connector Handoff

> **Historical only — superseded for Core 3.0.0 on 2026-09-10.**
> Do not execute the launch recipes below. HTTP/SSE MCP, OAuth proxy and tunnel
> scripts are now disabled. See [remote access status](REMOTE_ACCESS.md).

Date: 2026-07-06

This document summarizes the conversation, decisions, implementation work, current repo state, and remaining steps for wiring local RKA into ChatGPT as a secure MCP connector.

> **Core 3.0 migration note (2026-08-26).** This is primarily a historical
> deployment handoff. Current RKA Core packages only the `brain`, `executor`,
> and `pi` role skills plus `mcp-credentials`. Writer guidance, reference
> validation, and manuscript tooling moved to the separately installed
> [`rka-writer`](https://github.com/rka-project/rka-writer) project. The Core
> connector does not expose or auto-activate a `writer` role.

> **2026-07-06 continuation — deployment completed + always-on skill surface.**
> See the "Continuation (2026-07-06, later session)" section at the end for
> what has since been done. Summary: the CLI was reinstalled (via the /tmp
> clone workaround with uncommitted changes overlaid), the HTTP MCP on :9713
> was restarted, and a new `RKA_SKILL_TOOLS=1` env flag was added so the
> skill adapter tools are ALWAYS-ON for the ChatGPT deployment (ChatGPT never
> calls `rka_load_tools` spontaneously, so deferred tools were invisible to
> it). Local stdio clients keep the unchanged 5-tool surface. Full MCP test
> suite: 912 passed.

## Objective

Expose the local RKA MCP server to ChatGPT so ChatGPT can use RKA tools remotely, while preserving existing local access from Codex, Claude Code, and Claude Desktop.

The user also wants ChatGPT to access RKA "plugin" components such as role skills, not only the base MCP tools.

## Security Note

The user pasted OpenAI, admin, and ngrok credentials in the chat transcript. Do not copy them into files or commit them. Treat those credentials as exposed and rotate them.

This handoff intentionally redacts all secrets.

## Major Decisions

1. **Use a secure Server URL path for ChatGPT, not the OpenAI tunnel-client path for now.**
   - The OpenAI tunnel-client flow hit organization/workspace association issues in the Platform UI.
   - The ChatGPT Business workspace organization ID did not match the OpenAI Platform organization ID shown in Platform settings.
   - The tunnel UI reported it could not automatically verify the association, and later the tunnel route failed with `tunnel_use_forbidden`.
   - Decision: proceed with ChatGPT custom MCP "Server URL" using HTTPS plus OAuth.

2. **Do not expose the RKA web UI.**
   - User explicitly said only the MCP servers need remote access, not the web UI.
   - The web tunnel should remain off.

3. **Use a local OAuth-protected reverse proxy in front of RKA MCP.**
   - RKA MCP stays local on `127.0.0.1`.
   - A FastAPI OAuth proxy protects `/mcp`.
   - ngrok provides HTTPS transport to ChatGPT.
   - ChatGPT connector auth mode should be `OAuth`.

4. **Keep existing local access stable.**
   - Codex, Claude Code, and Claude Desktop rely on the local RKA MCP binary and existing MCP prompt/tool behavior.
   - New ChatGPT skill adapter tools must be deferred, not always-on.
   - Default MCP surface remains the v2.7+ five-tool dispatch surface.

5. **Expose RKA skills to ChatGPT as tools, not MCP prompts.**
   - Claude clients can use MCP prompts such as `brain_skill`, `executor_skill`, and `pi_skill`.
   - ChatGPT custom MCP connectors are primarily tool-oriented.
   - Decision: add deferred tools that read packaged skill files and provide a ChatGPT session checklist.

6. **Harden against macOS AppleDouble files.**
   - The repo is on an external/synced macOS volume and contains many `._*` AppleDouble/resource-fork sidecar files.
   - These caused wheel/install and migration/test errors.
   - Decision: prevent packaging `._*` files and ignore `._*.sql` in DB migrations.

## Current Runtime Context

Earlier in the session, the working secure route was:

- RKA API: `http://127.0.0.1:9712/api/health`
- RKA HTTP MCP: `http://127.0.0.1:9713/mcp`
- OAuth proxy: local `127.0.0.1:9720`, forwarding to `127.0.0.1:9713/mcp`
- ngrok: forwarding HTTPS to local port `9720`
- ChatGPT connector URL format: `https://<current-ngrok-host>/mcp`
- ChatGPT connector auth: `OAuth`

The exact ngrok host and OAuth passphrase should be retrieved from the current running process or regenerated. Do not reuse secrets from the chat transcript.

Useful health checks:

```bash
curl -sS http://127.0.0.1:9712/api/health
curl -sS http://127.0.0.1:9720/healthz
```

## Files Changed Or Added

### `scripts/rka_mcp_oauth_proxy.py`

Added a local OAuth-protected reverse proxy for ChatGPT MCP Server URL mode.

Capabilities:

- OAuth protected resource metadata:
  - `/.well-known/oauth-protected-resource`
  - `/.well-known/oauth-protected-resource/mcp`
- Authorization server metadata:
  - `/.well-known/oauth-authorization-server`
  - `/.well-known/openid-configuration`
- Dynamic client registration:
  - `/register`
- Passphrase-based OAuth authorization:
  - `/authorize`
- Token endpoint:
  - `/token`
- Protected MCP reverse proxy:
  - `/mcp`
- Health:
  - `/healthz`

Important environment variables:

```bash
RKA_MCP_OAUTH_PASSPHRASE=<redacted>
RKA_MCP_UPSTREAM=http://127.0.0.1:9713/mcp
RKA_MCP_OAUTH_PORT=9720
RKA_MCP_OAUTH_HOST=127.0.0.1
RKA_MCP_PUBLIC_BASE_URL=https://<current-ngrok-host>
```

### `rka/mcp/server.py`

Added deferred ChatGPT skill adapter tools:

- `rka_list_skills()`
  - Lists packaged RKA Core skill guides: `brain`, `executor`, `pi`, `mcp-credentials`.
- `rka_read_skill(name, reference=None)`
  - Reads a packaged skill `SKILL.md` or a referenced file inside that skill directory.
  - Rejects absolute paths and `..` traversal.
  - Rejects AppleDouble `._*` files.
- `rka_start_session(role="pi", project_id=None)`
  - Returns the selected role skill plus a ChatGPT Connector Session Checklist.

These tools are registered as:

```python
@tool(tier=_TIER_DEFERRED, category="skills")
```

That means they do not affect the default five-tool surface used by existing local clients.

Also updated `rka_list_tools` docs to include the `skills` category.

### `tests/test_mcp/test_skill_adapter_tools.py`

Added focused tests that verify:

- The new skill adapter tools are deferred in default mode.
- They do not appear in the visible default MCP tool list.
- Skill listing includes all expected packaged roles.
- `rka_read_skill("pi")` returns PI skill markdown.
- Path traversal is rejected.
- `rka_start_session("pi", project_id="prj_test")` includes a startup checklist.

### `pyproject.toml`

Added package-data exclusions:

```toml
[tool.setuptools.exclude-package-data]
"*" = ["._*", "**/._*", ".DS_Store", "**/.DS_Store"]
```

Purpose: prevent macOS AppleDouble/resource-fork files from being packaged into wheels.

### `rka/infra/database.py`

Hardened migration discovery to ignore `._*.sql`:

```python
if f.suffix == ".sql" and not f.name.startswith("._")
```

Purpose: avoid `UnicodeDecodeError` when AppleDouble files appear in `rka/db/migrations/`.

### `rka.egg-info/SOURCES.txt`

Removed stale `._*` entries from the generated source manifest during the session. This file may regenerate during builds; if install failures mention missing `._requires.txt` or similar, clean generated metadata again.

## Verification Already Run

Successful:

```bash
.venv/bin/python -m py_compile rka/mcp/server.py rka/infra/database.py scripts/rka_mcp_oauth_proxy.py
.venv/bin/python -m pytest tests/test_mcp/test_skill_adapter_tools.py tests/test_mcp/test_v270a3_dispatch_surface.py -q
```

Result:

```text
20 passed
```

Additional migration-hardening checks passed when `RKA_DATA_DIR` was set to a writable temp dir:

```bash
RKA_DATA_DIR=/private/tmp/rka-test-data .venv/bin/python -m pytest tests/test_mcp/test_record_outcome.py::test_tool_happy_path -q
RKA_DATA_DIR=/private/tmp/rka-test-data .venv/bin/python -m pytest tests/test_mcp/test_hooks_integration.py::test_scenario_A_session_start_brain_notify -q
```

Result:

```text
1 passed
1 passed
```

## Known Verification Limitations

1. `pip wheel .` failed in this sandbox because network access is blocked and local Python environments do not have importable `setuptools`.
2. `uv build` and `uv tool install` panic inside this sandbox's macOS/system-configuration layer before reaching the project build.
3. Binding a replacement local HTTP MCP server inside the sandbox failed with `operation not permitted`.
4. Running broader `tests/test_mcp` without `RKA_DATA_DIR` failed because the sandbox cannot create `/Users/ceron/.rka`.

These are environment/sandbox constraints, not confirmed code regressions.

## Current Git State To Expect

Expected relevant modified/untracked files:

```text
M  pyproject.toml
M  rka/infra/database.py
M  rka/mcp/server.py
?? scripts/rka_mcp_oauth_proxy.py
?? tests/test_mcp/test_skill_adapter_tools.py
```

There are many unrelated untracked eval artifacts and reports in the worktree. Do not delete or revert them unless the user explicitly asks.

## How ChatGPT Should Use The New Skill Tools

After the updated MCP server is running, in ChatGPT call:

```text
rka_load_tools(names=["rka_list_skills", "rka_read_skill", "rka_start_session"])
```

Then:

```text
rka_start_session(role="pi")
```

or, with a known project id:

```text
rka_start_session(role="pi", project_id="prj_...")
```

For manuscript guidance, install and invoke the separate
[`rka-writer`](https://github.com/rka-project/rka-writer) plugin. A Core call
such as `rka_read_skill(name="writer")` is intentionally unsupported after the
3.0 split.

Only request files inside the selected skill directory. Absolute paths and `..` traversal are intentionally rejected.

## Remaining Steps

1. Clean generated AppleDouble files before reinstall:

```bash
find build rka.egg-info -name '._*' -delete
```

If needed, clean source-side AppleDouble files too:

```bash
find . -name '._*' -not -path './.git/*' -delete
```

Only run the broader source cleanup if the user is comfortable deleting macOS sidecar files from the working tree.

2. Reinstall the local RKA CLI from the repo in a normal terminal:

```bash
cd /Volumes/FuSpace/Projects/rka
COPYFILE_DISABLE=1 UV_CACHE_DIR=/tmp/uv-cache uv tool install --force .
```

If install still fails due AppleDouble files, use the repo's documented `/tmp` clone workaround:

```bash
rm -rf /tmp/rka-build
git clone -q --depth 1 "$PWD" /tmp/rka-build
cd /tmp/rka-build
COPYFILE_DISABLE=1 UV_CACHE_DIR=/tmp/uv-cache uv tool install --force .
```

3. Restart the RKA HTTP MCP process on port `9713`.

Example:

```bash
RKA_API_URL=http://127.0.0.1:9712 rka mcp --transport http --host 127.0.0.1 --port 9713
```

4. Restart or confirm the OAuth proxy:

```bash
RKA_MCP_OAUTH_PASSPHRASE='<redacted>' \
RKA_MCP_UPSTREAM='http://127.0.0.1:9713/mcp' \
RKA_MCP_OAUTH_PORT=9720 \
rka-or-python-command-for scripts/rka_mcp_oauth_proxy.py
```

Use the actual Python command/environment that works on the machine. The previous session used a local Python environment.

5. Start or confirm ngrok forwarding to port `9720`:

```bash
ngrok http 9720
```

6. In ChatGPT connector settings:

- Connection: `Server URL`
- Server URL: `https://<current-ngrok-host>/mcp`
- Authentication: `OAuth`
- Complete OAuth authorization using the configured passphrase.

7. In ChatGPT, load the skill adapter tools:

```text
rka_load_tools(names=["rka_list_skills", "rka_read_skill", "rka_start_session"])
```

8. Validate from ChatGPT:

```text
rka_start_session(role="pi")
rka_query(args={"operation": "list_projects"})
```

## Compatibility Expectations

Existing Codex, Claude Code, and Claude Desktop local access should remain compatible because:

- The new skill tools are deferred.
- The default MCP visible tool surface remains unchanged.
- Existing MCP prompts remain in place.
- The HTTP/OAuth/ngrok path is additive and does not replace stdio MCP configs.

Local clients may need to restart their MCP subprocess to pick up new code after `uv tool install --force .`.

## Do Not Do Without Explicit User Approval

- Do not commit or push.
- Do not delete unrelated eval artifacts or reports.
- Do not paste or persist secrets from the transcript.
- Do not expose the web UI tunnel unless the user reverses the decision.
- Do not revert unrelated worktree changes.

---

## Continuation (2026-07-06, later session)

### Deployment completed

1. AppleDouble `._*` files purged (32 at repo top levels).
2. Direct `uv tool install` failed on `._requires.txt` (build re-creates
   AppleDouble files on this external volume). Used the documented `/tmp`
   clone workaround — **with a twist**: the working-tree changes are
   uncommitted, so after `git clone "$PWD" /tmp/rka-build` the three
   package-relevant modified files (`rka/mcp/server.py`,
   `rka/infra/database.py`, `pyproject.toml`) were copied over the clone
   (`cp -X`) before installing.
3. **uv caching gotcha**: re-installing from the same `/tmp/rka-build` path
   after further edits reused the cached wheel and silently deployed stale
   code. Use `uv tool install --force --no-cache .` when iterating, and
   verify with
   `grep -c RKA_SKILL_TOOLS ~/.local/share/uv/tools/rka/lib/python3.11/site-packages/rka/mcp/server.py`.
   Also: `python3 -c "import rka..."` run from the repo root imports the
   repo source, NOT the installed package — verify from a neutral cwd with
   `python3 -I`.
4. HTTP MCP on :9713 restarted from the new install
   (log: `/tmp/rka-mcp-http-9713.log`).

### New: `RKA_SKILL_TOOLS=1` — always-on skill tools for ChatGPT

The user goal is the plugin-with-skills experience in ChatGPT. ChatGPT
custom connectors cannot use Claude Code plugins or MCP prompts, and they
do not call `rka_load_tools` on their own — so deferred tools are
effectively invisible to ChatGPT. Added an env flag in `rka/mcp/server.py`:

- `RKA_SKILL_TOOLS=1` promotes `rka_list_skills` / `rka_read_skill` /
  `rka_start_session` to `always_on` (visible surface = 5 dispatch + 3
  skills = 8 tools) and appends a "Skill Tools" section to the FastMCP
  server instructions telling the model to call
  `rka_start_session(role=...)` first.
- Flag unset (default): identical 5-tool surface as before — local
  Claude Code / Claude Desktop / Codex stdio clients are unaffected.
- Mirrors the `RKA_LEGACY_TOOLS` pattern: read at module import time.

The :9713 HTTP MCP process (the ChatGPT path) now runs with
`RKA_SKILL_TOOLS=1`:

```bash
RKA_API_URL=http://127.0.0.1:9712 RKA_SKILL_TOOLS=1 \
  rka mcp --transport http --host 127.0.0.1 --port 9713
```

Verified end-to-end with a raw streamable-HTTP MCP handshake against :9713:
fresh session `tools/list` shows all 8 tools; `rka_start_session(role="pi")`
returns the v2.7.0 PI skill + session checklist without any prior
`rka_load_tools` call.

### Test-suite fix

`tests/test_mcp/test_skill_adapter_tools.py` originally used
`importlib.reload(rka.mcp.server)` to flip env flags. Reload re-executes the
module in place and rebinds module globals (e.g. `_TOOL_REGISTRY` becomes a
new dict), which poisoned 60+ later tests in the same pytest run (e.g.
`test_v270_verb_dispatch.py` patches the registry object it imported at
collection time, while dispatch reads the rebound one). Rewritten reload-free:
surface/tier assertions run `sys.executable -c "import rka.mcp.server ..."`
in a pristine subprocess per flag combination; content tests call the module
functions directly. Full `tests/test_mcp` suite: **912 passed**.

### Skill consistency + new-machine sync (2026-07-06, same session)

Goal: every skill guide must teach the ACTUAL v2.7.0 dispatch surface, and a
fresh `uv tool install` / plugin install on any machine must ship identical,
correct skills.

**Audit.** A multi-agent workflow read all skill files carrying tool usage,
cross-checked every `rka_*` name / operation / enum / required-field against
`rka/mcp/operation_args.py`, then adversarially verified each finding.
Result: **90 confirmed inconsistencies** across 21 files (60 would break a
caller). Dominant classes: legacy op-names inside `args={"operation": ...}`
(e.g. `get_decision_tree` → `decision_tree`), worked examples missing
required fields (`record_decision` needs chosen/decided_by/kind/phase;
`create_mission` needs motivated_by_decision), invalid enums (`confidence:
"high"`/`"confirmed"`), and `record_note` provenance links placed top-level
instead of nested under `provenance={...}`. All 90 applied (parallel per-file
editors), an 8-item residual set hand-fixed, and every touched file
re-verified clean.

**Packaging bug found + fixed.** `[tool.setuptools.package-data]` used only
`skills/**/*.md|*.yaml|*.py`, so **14 files never shipped in the wheel** —
Python's glob doesn't match dot-prefixed names, so the writer
workspace-template (`main.tex`, `refs.bib`, `.planning/*.md`, `.mcp.json`,
`.latexmkrc`, `.gitkeep`, `render.sh`) was silently absent on every fresh
install. Added explicit patterns; rebuild now ships all files (verified 0
missing).

**Regression guards** (`tests/test_skills_packaging.py`): (1) `plugin/skills/`
must stay byte-identical to `rka/skills/` (fails CI on drift, prints the
rsync fix); (2) every file under `rka/skills/` must be covered by a
package-data glob (fails CI if a new skill asset wouldn't ship). Source of
truth is `rka/skills/`; mirror to `plugin/skills/` with:
`rsync -rc --exclude='._*' --exclude='__pycache__' --exclude='__init__.py' --exclude='/SKILL.md' rka/skills/ plugin/skills/`

**Synced all three layers**: repo `plugin/skills`, the marketplace source
`~/Code/rka/plugin/skills`, and the installed plugin cache (via uninstall +
reinstall — `claude plugin update` is a no-op when the manifest version is
unchanged). Reinstalled the MCP wheel; verified `:9713` serves the corrected
content end-to-end. Full suite: **912 + 23 passed**.

**Build gotchas (recurring on this external volume):** `uv tool install`
caches the wheel by source path — after editing, use
`--force --no-cache` or it silently redeploys stale code. Verify the installed
package, not the repo: `python3 -I -c "import rka..."` from a neutral cwd
(repo-root import shadows the installed package). Direct install trips on
`._requires.txt`; the `/tmp` clone workaround needs the uncommitted changes
overlaid (`rsync rka/skills/` + `cp -X` the code files) before building.

### Still open

- ChatGPT side: refresh/reconnect the connector so it re-fetches the tool
  list (ChatGPT caches connector tools) and confirm the 8-tool surface.
- Rotate the credentials exposed in the original chat transcript (OpenAI,
  admin, ngrok).
- Working-tree changes remain uncommitted by design (no commit/push without
  explicit approval). The durable fix for cross-machine consistency is
  committing the skill corrections + packaging fix + tests, since GitHub HEAD
  still carries the old drift; `~/Code/rka` then needs
  `git checkout -- plugin/skills` before its next pull.
