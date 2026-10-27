# Core dependency security

The supported runtime is Core plus the optional embeddings, academic and
workspace profiles. The frozen `llm` extra is not part of the supported Core
runtime or the audit's acceptance claim. Development tools and container OS
packages are not covered by the Python runtime audit.

## Reproducible audit

Use Python 3.11 or 3.13 and pinned tools `uv==0.9.17` and
`pip-audit==2.10.1` in a disposable environment:

```sh
uv lock --check
uv export --frozen --no-dev --no-emit-project --no-hashes \
  --extra embeddings --extra academic --extra workspace \
  --output-file audit-requirements.txt
python -m pip_audit --disable-pip --no-deps --progress-spinner off \
  --requirement audit-requirements.txt --format json --output python-audit.json
python scripts/verify_dependency_audit.py python-audit.json
npm --prefix web audit --package-lock-only --omit=dev --audit-level=low
```

The export is a complete pinned dependency graph, not just top-level
requirements. `--no-deps --disable-pip` prevents a second resolver/install from
silently replacing audited versions. Vulnerability-service or parser failures
fail the job; an unavailable audit is not a clean audit. GitHub Actions audits
the supported Python profile on Linux, Windows and macOS, on both Python
versions, so platform markers are evaluated on their actual targets.

Every reported runtime advisory fails the gate, whether the dependency is
direct or transitive. There is currently no allowlist. If an exception becomes
necessary, require a separately reviewed advisory ID/aliases, affected
version/profile, concrete reachability rationale, owner and expiration date;
do not add blanket exceptions for transitive packages or hide tool failures.

`npm audit --omit=dev` covers production frontend dependencies. It does not
prove the build toolchain is free of vulnerabilities. Dependabot also tracks
development dependencies and pinned Actions/Docker updates; those proposals
still require normal review and CI. No auto-merge is enabled.

## Upgrade scope and compatibility

The 2026-10-26 baseline audit reported 153 advisories across 16 packages in the
supported profile. Advisory counts are a timestamped scanner result, not a
count of exploitable Core endpoints. Relevant exposed paths include Starlette
HTTP/static handling and multipart imports; MCP remains local stdio only.
HTTP/SSE/WebSocket advisories do not authorize enabling those transports.

The patch targets those vulnerable packages, rather than upgrading the whole
lock. Starlette 1.3.1 and pydantic-settings 2.14.2 are deliberately selected as
the known repair baselines. Updating Pillow also requires a compatible
FastEmbed update, so native embedding process/resource-boundary and retrieval
tests remain mandatory. No embedding model or stored vector generation is
changed by editing a dependency lock.

Core owns explicit security floors for MCP, Starlette, multipart parsing and
settings. `tool.uv.constraint-dependencies` supplies floors for dependencies
already in a selected profile; it does not force optional document or
embedding packages into base Core. These uv-only constraints are **not**
wheel dependency metadata and are not automatically honored by pip. A wheel
installed into an existing environment needs a fresh environment-level audit;
the frozen-lock claim must not be extended to arbitrary installed clients.

The primary supported reproducible installation uses `uv sync --locked`,
also used by the Docker dependency build. Keep the normal Core tests,
REST/MCP snapshots, wheel matrix, historical upgrades and startup/container
smoke in addition to these audit checks. Audit success cannot substitute for
API, packaging or native-library compatibility checks.

## Updates and release boundary

Dependabot uses the `uv` ecosystem for `uv.lock`, not `pip`; see the
[official uv integration guide](https://docs.astral.sh/uv/guides/integration/dependabot/).
The configuration also covers npm, GitHub Actions and Docker digests.
Publishing the configuration is required before GitHub can actually open PRs;
a local YAML file is not evidence that Dependabot is active.

Changing source/lockfiles does not update a published image, installed MCP,
plugin cache or running production service. Build/publish and runtime rollout
are separately authorized gates. Do not claim issue #180 fully deployed on
the strength of a clean source audit.
