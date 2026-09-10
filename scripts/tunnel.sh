#!/bin/sh
# Compatibility tombstone: never forward the trusted-local API to a remote host.
printf '%s\n' 'RKA remote tunnels are disabled in Core 3.0.0. Use local STDIO MCP; see docs/REMOTE_ACCESS.md.' >&2
exit 1
