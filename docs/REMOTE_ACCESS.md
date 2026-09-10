# Remote access status — Core 3.0.0

Core 3.0.0 is a **local-first release**. Supported clients are local Codex,
Claude Code, Claude Desktop and other STDIO MCP clients. Remote/public access
is deferred pending separate authorization and isolation acceptance.

## Supported boundary

- Native REST and dashboard: `http://127.0.0.1:9712`.
- Docker may listen on `0.0.0.0` **inside** its container, with host publishing
  restricted to `127.0.0.1:9712:9712` as in the shipped Compose file.
- MCP: `rka mcp`, using STDIO and the local REST backend. No MCP TCP port is
  needed. The normal five-tool dispatch surface is unchanged.
- Host and server file access still require separate operator-owned allowroots.
  See [file access](FILE_ACCESS.md).

The REST API is a trusted-local API, **not authenticated multi-user hosting**.
Actor fields declare provenance, not identity or permissions. A tool name such
as `query`, a hidden button, or an OAuth token does not enforce read-only
execution. Do not publish REST ports or forward them with SSH/ngrok/Cloudflare,
including for a supposedly read-only demo.

## Disabled compatibility entry points

`rka mcp --transport http` and non-STDIO `RKA_MCP_TRANSPORT` values fail before
server import. Direct HTTP/SSE app factories and async runners on the shipped
MCP server also reject startup. The old `scripts/rka_mcp_oauth_proxy.py` exits
nonzero; stale ASGI imports return 503 without contacting an upstream.
`scripts/tunnel.sh` exits nonzero without invoking SSH.

This removes supported launch paths; it is not a sandbox against an operator
who rewrites Python, constructs a different gateway, or exposes the REST API.
Existing processes and third-party proxies are not stopped by upgrading source.
Stop any previously configured remote service/tunnel yourself before installing
this release. Local-only users do not need to change their MCP configuration.

## Deferred work

ChatGPT custom connectors, public demos, Hugging Face Spaces and remote
Codespaces access are **not supported by this release**. RKA App remains a
separate downstream deployment project. Merely publishing a Core image does
not validate that remote deployment.

Before restoring remote access, acceptance must cover authenticated identity,
per-operation and per-project authorization across REST, typed and legacy MCP,
filesystem isolation, side effects hidden behind reads, bulk execution,
OAuth registration/consent/PKCE abuse cases and real transport end-to-end tests.
A demo must use independent synthetic or explicitly approved data, never a
shared personal research instance.

See [the remediation plan](superpowers/plans/2026-09-05-core-audit-remediation-plan.md)
and [the installation guide](../INSTALL.md) for the release scope and local setup.
