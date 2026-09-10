"""Supported transport boundary for the local-first Core release.

This is not remote authorization. REST remains a trusted-local API; it must
not be exposed to untrusted clients. Re-enabling a network MCP transport needs
an independently reviewed execution policy, not just a token or hidden tools.
"""

from mcp.server.fastmcp import FastMCP


REMOTE_DISABLED = (
    "Remote MCP is disabled in RKA Core 3.0.0 pending authorization and "
    "isolation acceptance. Use 'rka mcp' over local stdio with Codex or "
    "Claude Code; keep the REST API on host loopback. "
    "See docs/REMOTE_ACCESS.md."
)


class LocalOnlyFastMCP(FastMCP):
    """Keep STDIO unchanged; also reject direct ASGI and async HTTP entry points."""

    def sse_app(self, mount_path: str | None = None):
        raise RuntimeError(REMOTE_DISABLED)

    def streamable_http_app(self):
        raise RuntimeError(REMOTE_DISABLED)

    async def run_sse_async(self, mount_path: str | None = None) -> None:
        raise RuntimeError(REMOTE_DISABLED)

    async def run_streamable_http_async(self) -> None:
        raise RuntimeError(REMOTE_DISABLED)
