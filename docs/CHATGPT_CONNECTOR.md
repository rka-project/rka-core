# ChatGPT connector — deferred in Core 3.0.0

The historical OAuth/tunnel connector is no longer supported. Its launch
scripts are disabled, and HTTP/SSE MCP cannot be started by the shipped server.
Do not use old setup recipes to expose a personal RKA instance.

See [remote access status](REMOTE_ACCESS.md) for the exact boundary and remaining
acceptance work. For local Codex or Claude Code, follow [INSTALL.md](../INSTALL.md)
and use STDIO MCP. The historical implementation is preserved in Git history.
