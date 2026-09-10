#!/usr/bin/env python3
"""Disabled legacy OAuth proxy. See docs/REMOTE_ACCESS.md.

Keep a fail-closed tombstone for old launch scripts and ASGI imports. The
historical implementation remains in Git; it is not a supported access policy.
No configuration, credentials, upstream client, or listener is initialized.
"""

import sys


MESSAGE = (
    "RKA remote OAuth proxy is disabled in Core 3.0.0. Use local STDIO MCP. "
    "See docs/REMOTE_ACCESS.md."
)


async def app(scope, receive, send):
    """Reject stale ASGI deployments as well as direct CLI launches."""
    if scope["type"] == "http":
        await send(
            {
                "type": "http.response.start",
                "status": 503,
                "headers": [
                    (b"content-type", b"text/plain; charset=utf-8"),
                    (b"cache-control", b"no-store"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": MESSAGE.encode()})
    elif scope["type"] == "websocket":
        await send({"type": "websocket.close", "code": 1008})
    else:
        raise RuntimeError(MESSAGE)


if __name__ == "__main__":
    print(MESSAGE, file=sys.stderr)
    raise SystemExit(1)
