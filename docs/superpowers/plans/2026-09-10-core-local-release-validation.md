# Core 3.0.0 local-first release acceptance

- PI decision, 2026-09-10: local first, remote deferred.
- Base: merged I-series main `6b7c2819386c45e125f92bf87d92929dc4465bfe`.
- Isolated branch: `codex/core-v3-local-release`.
- This record separates source acceptance from release/container publication.
  Current CI, merge and image evidence belongs to the linked PR/release; the
  presence of this document is not proof of publication.

## Implemented release boundary

Local STDIO stays supported. The CLI rejects non-STDIO before importing the
server; the shipped server's direct HTTP/SSE factories/runners also reject.
Legacy OAuth CLI and ASGI entry points fail closed, with no upstream client,
and the tunnel tombstone never invokes SSH. REST remains trusted-local, with
Docker internal binding retained and host loopback publishing unchanged.

New credential manifests track Core version dynamically and require no external
keys or orchestrator. Existing manifests/pins/values stay untouched. Prompts hide
input, probe URLs default to IPv4 loopback, and console/module version output
has stable identity, including historical Windows `rka.exe` parsing.

The security-review skill guided the direct-import/ASGI bypass checks and the
distinction between provenance actors and authentication. RKA Executor isolation
rules kept all tests away from the personal RKA store and sibling worktrees.

## Local evidence

Environment: macOS arm64, managed CPython 3.13.11 with real sqlite-vec 0.1.6;
Core extras from `uv sync --locked`, no legacy LLM SDK extra. Temporary data,
config, provider fallback at loopback port 1, no production API or LM Studio.
All bound test ports are ephemeral, never 9712.

- Release-boundary and credential tests: **106 passed**.
- Installation links, container release guards and wheel matrix tests:
  **19 passed, 5 subtests passed** after completing the upgrade document.
- Initial full Core: **3891 passed, 1 failed, 1 skipped, 294 deselected**,
  387.44 seconds. The sole failure was the installation link to the then-unfinished
  `docs/RELEASE_3_0.md`. This incomplete-state run is not counted as a green gate.
  Final full rerun: **3892 passed, 1 skipped, 294 deselected, 5 subtests passed**,
  369.36 seconds. The six warnings are existing deprecations; no failures.
- REST/MCP contract snapshots unchanged and verified.
- Historical-source upgrade/recovery/pack acceptance passed repeatedly for
  2.8.1 and pre-split 2.9.0; fixed logical fixture hashes match across regeneration.
  Actual vector queries enforce the migrated project metadata filter. Exact
  baseline SHAs, synthetic contents and allowed schema differences are documented
  in [the upgrade runbook](../../RELEASE_3_0.md).
- Base wheel built and installed outside the checkout: migrations, public REST
  workflow, actual five-tool STDIO MCP and worker smoke passed. Installed-module
  path was under the disposable wheel venv; HTTP/SSE factories also reject there.
  This additionally tested currently resolved base dependencies (MCP 1.30.0),
  while locked source/container tests use MCP 1.26.0.
- The standard local wheel launcher first failed because the host's
  `venv/ensurepip` subprocess aborted (SIGABRT), before package installation.
  An isolated `uv venv` + `uv pip install <wheel>` then ran the same installed
  startup smoke successfully. No global tool was reinstalled. The existing
  six-way CI launcher remains a separate platform gate.
- Web production build passed on Node 24.19.0; the earlier Node 23 run also
  built but emitted an engine warning and is not the chosen environment.
  CI/container builds remain Node 22. Full source startup with
  `--require-web --require-vec` passed.
- New/changed release Python files pass Ruff. Eight existing diagnostics in
  credential commands/probes/propagators match the base exactly; no whole-repo
  cleanup was mixed into the release. `git diff --check` passes.

Local evidence is under a private temporary release directory, not committed
databases. CI regenerates its own public synthetic reports and uploads only
the JSON acceptance result, never the fixtures or personal demo pack.

## Resource evidence and remaining artifact gates

This patch does not change inference, input budgets, embedding documents,
workers, database schemas or recovery implementation. It carries the existing
[E-series real-model acceptance](2026-09-07-core-e-completion-validation.md):
3612 accepted synthetic rows, 2 GiB API / 4 GiB worker, Nomic peak sampled RSS
about 1.17 GB, zero OOM counters, worker SIGKILL/resume and a real 768-to-384
offline transition. Oversized input is preserved and explicitly fails.
Those measurements are not newly rerun on this host or evidence for every model.

Docker is not running on the host. It was not started because doing so could
restart protected containers. Final PR CI must pass web, full Core, the six
Linux/macOS/Windows wheel jobs, installed-image recovery and the new
historical-upgrade job. Publication then follows ADR 0018: stable tag, preflight,
amd64/arm64 images, digest smoke and verified attestation. Package-public
visibility is a separate irreversible operator action; anonymous digest pull
must be read back before downstream App pins the image.

## Dependency advisory follow-up

`npm ci` reported 23 existing dependency findings; `npm audit --omit=dev`
reported 21 (4 low, 4 moderate, 13 high). This is **not a clean dependency audit**.
No automatic `npm audit fix`, dependency refresh or lockfile change was made.

A no-write Rollup module inventory of the built dashboard found 659 included
modules. Of the 21 flagged package names, only `react-router` contributed
runtime module code. Hono/Express, parsers, build utilities and Vite are not
served as Node runtimes; the final Docker stage copies only static web assets,
the Python environment and sqlite-vec.

The dashboard uses declarative `BrowserRouter`, not SSR, RSC or Framework Mode.
The upstream [manifest-route DoS advisory](https://github.com/advisories/GHSA-chx6-hx7r-mcp5)
explicitly excludes Declarative/Data Mode. The
[Link/useNavigate redirect advisory](https://github.com/advisories/GHSA-wrjc-x8rr-h8h6)
was traced through all current Link/NavLink/navigate call sites: destinations
are fixed local route prefixes, with record IDs/query text URI-encoded, not
raw attacker-provided redirect destinations. This is scoped reachability
evidence, not a claim that every dependency is vulnerability-free.

Track a separately tested frontend/build-dependency refresh. Do not expose
the existing Vite development server or execute scaffolding/build tools against
untrusted imported research material. Remote hosting remains unsupported.

## Explicitly deferred

Remote authorization/Spaces/Codespaces demos, unified frozen-surface maturity
metadata, broader historical corpora and one-command desktop/service-manager
packaging. Issue #158 is not automatically closed by this release record.
No production database/config/runtime, global MCP, sibling worktree or untracked
demo content was changed by this release preparation.
