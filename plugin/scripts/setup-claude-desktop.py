#!/usr/bin/env python3
"""setup-claude-desktop.py — write the rka MCP entry into claude_desktop_config.json.

Cross-platform helper invoked by the /rka-setup-claude-desktop slash command
(or directly: uv run --no-project plugin/scripts/setup-claude-desktop.py [--force]).

Behavior:
1. Detect OS, resolve `claude_desktop_config.json` and `integration.json` paths.
2. Resolve plugin location (CLAUDE_PLUGIN_ROOT env, else cwd).
3. Verify the wrapper script exists at <plugin>/bin/rka-mcp-bridge.py.
4. Verify the RKA backend is reachable at integration.json's api_endpoint_url
   (fallback http://127.0.0.1:9712); refuse setup if unreachable unless --force.
5. Backup existing claude_desktop_config.json to *.backup-YYYYMMDD-HHMMSS.
6. Read existing config (start with {} if missing/empty; refuse to overwrite
   malformed JSON unless --force).
7. Detect existing mcpServers.rka entry:
   - If absent: add the new entry.
   - If present and identical to the new entry: report idempotent success.
   - If present but different: refuse to replace unless --force.
8. Atomic write (tmp + rename), re-read to verify.
9. Print clear success or error.
10. Restore from backup if any post-backup step fails.

Exit codes:
  0 = success (entry added or already correct)
  1 = recoverable error (backup created, original preserved)
  2 = unrecoverable error (no backup possible, e.g. malformed JSON refusal)
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import shutil
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path


def claude_desktop_config_path() -> Path:
    if sys.platform == "darwin":
        return (
            Path.home()
            / "Library"
            / "Application Support"
            / "Claude"
            / "claude_desktop_config.json"
        )
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        if not appdata:
            return Path.home() / "AppData" / "Roaming" / "Claude" / "claude_desktop_config.json"
        return Path(appdata) / "Claude" / "claude_desktop_config.json"
    return Path.home() / ".config" / "Claude" / "claude_desktop_config.json"


def integration_path() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "RKA" / "integration.json"
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        if not appdata:
            return Path.home() / "AppData" / "Roaming" / "RKA" / "integration.json"
        return Path(appdata) / "RKA" / "integration.json"
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "share"
    return base / "RKA" / "integration.json"


def resolve_plugin_root() -> Path:
    """Find the plugin root (containing .claude-plugin/plugin.json)."""
    env = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if env:
        return Path(env)
    # Fallback: assume this script lives at <plugin>/scripts/setup-claude-desktop.py
    return Path(__file__).resolve().parent.parent


def uv_command_for_mcp_config() -> str:
    """Return a stable absolute uv executable for GUI-launched Claude Desktop."""
    command = shutil.which("uv")
    if not command:
        raise FileNotFoundError(
            "uv was not found on PATH; install it from "
            "https://docs.astral.sh/uv/getting-started/installation/"
        )
    # Keep a Homebrew or package-manager symlink stable across tool upgrades;
    # Path.resolve() could instead persist a versioned Cellar target.
    return str(Path(command).expanduser().absolute())


def mcp_entry(wrapper_path: Path) -> dict[str, object]:
    """Build the same cross-platform launcher shape used by the plugin."""
    return {
        "command": uv_command_for_mcp_config(),
        "args": ["run", "--no-project", "--isolated", str(wrapper_path)],
    }


def check_backend_reachable(api_url: str, timeout: float = 3.0) -> tuple[bool, str]:
    """Returns (reachable, message)."""
    try:
        with urllib.request.urlopen(api_url.rstrip("/") + "/api/health", timeout=timeout) as resp:
            if resp.status == 200:
                return True, f"healthy ({resp.status})"
            return False, f"HTTP {resp.status}"
    except urllib.error.URLError as exc:
        return False, str(exc.reason)
    except Exception as exc:
        return False, str(exc)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Set up Claude Desktop's MCP config to use the RKA wrapper."
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing mcpServers.rka entry with a different command/args. Also bypasses the unreachable-backend safety check.",
    )
    parser.add_argument(
        "--config-path",
        type=Path,
        default=None,
        help="Override the auto-detected claude_desktop_config.json path.",
    )
    parser.add_argument(
        "--plugin-root",
        type=Path,
        default=None,
        help="Override the auto-detected plugin root.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be written without making any changes.",
    )
    args = parser.parse_args()

    config_path = (args.config_path or claude_desktop_config_path()).expanduser().resolve()
    plugin_root = (args.plugin_root or resolve_plugin_root()).expanduser().resolve()
    int_path = integration_path()

    print(f"OS: {sys.platform}")
    print(f"Claude Desktop config: {config_path}")
    print(f"Plugin root: {plugin_root}")
    print(f"Integration.json: {int_path}")

    # Verify wrapper script exists.
    wrapper_path = plugin_root / "bin" / "rka-mcp-bridge.py"
    if not wrapper_path.is_file():
        print(f"\nERROR: wrapper script not found at: {wrapper_path}", file=sys.stderr)
        print("Re-install the rka plugin via /plugin install rka@rka.", file=sys.stderr)
        return 2

    try:
        new_entry = mcp_entry(wrapper_path)
    except FileNotFoundError as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        return 2

    # Resolve API URL from integration.json (or default).
    api_url = "http://127.0.0.1:9712"
    if int_path.is_file():
        try:
            data = json.loads(int_path.read_text())
            api_url = (data.get("api_endpoint_url") or api_url).strip()
        except json.JSONDecodeError as exc:
            print(f"\nWARNING: integration.json is malformed at {int_path}: {exc}", file=sys.stderr)
            print("Continuing with default API URL.", file=sys.stderr)

    # Backend reachability check.
    reachable, msg = check_backend_reachable(api_url)
    if reachable:
        print(f"\nBackend reachable at {api_url}: {msg}")
    else:
        print(f"\nWARNING: Backend NOT reachable at {api_url}: {msg}", file=sys.stderr)
        if not args.force:
            print(
                "Refusing to set up Claude Desktop integration when the backend is down. "
                "Start RKA (run `docker compose up -d` from the rka repo) and re-run this command. "
                "Or use --force to set up anyway (Claude Desktop will see 'tool unavailable' until the backend is up).",
                file=sys.stderr,
            )
            return 1
        print("--force given; continuing despite unreachable backend.", file=sys.stderr)

    print(f"\nNew mcpServers.rka entry: {json.dumps(new_entry, indent=2)}")

    # Read existing config (with malformed-JSON guard).
    existing: dict = {}
    config_existed = config_path.is_file()
    if config_existed:
        text = config_path.read_text() or "{}"
        try:
            existing = json.loads(text) if text.strip() else {}
        except json.JSONDecodeError as exc:
            print(
                f"\nERROR: existing claude_desktop_config.json is malformed JSON: {exc}",
                file=sys.stderr,
            )
            print(
                f"Refusing to overwrite. Fix the JSON manually first, or back it up and delete: {config_path}",
                file=sys.stderr,
            )
            return 2
        if not isinstance(existing, dict):
            print(
                f"\nERROR: existing claude_desktop_config.json is not a JSON object (top-level type: {type(existing).__name__})",
                file=sys.stderr,
            )
            print("Refusing to overwrite. Fix manually first.", file=sys.stderr)
            return 2

    mcp_servers = existing.get("mcpServers")
    if mcp_servers is None:
        mcp_servers = {}
        existing["mcpServers"] = mcp_servers
    elif not isinstance(mcp_servers, dict):
        print(
            f"\nERROR: existing mcpServers field is not an object (type: {type(mcp_servers).__name__})",
            file=sys.stderr,
        )
        return 2

    # Conflict detection.
    if "rka" in mcp_servers:
        current = mcp_servers["rka"]
        if current == new_entry:
            print(
                "\nClaude Desktop is already configured with the correct rka MCP entry — nothing to do."
            )
            return 0
        if not args.force:
            print(
                "\nCONFLICT: existing mcpServers.rka entry differs from the new one:",
                file=sys.stderr,
            )
            print(f"  Existing: {json.dumps(current, indent=2)}", file=sys.stderr)
            print(f"  Proposed: {json.dumps(new_entry, indent=2)}", file=sys.stderr)
            print(
                "\nRe-run with --force to replace the existing entry, "
                "or remove the entry from claude_desktop_config.json manually first.",
                file=sys.stderr,
            )
            return 1
        print("\n--force given; replacing existing rka entry.")

    if args.dry_run:
        print("\n--dry-run: would write the following config (not actually modifying file):")
        existing["mcpServers"]["rka"] = new_entry
        print(json.dumps(existing, indent=2))
        return 0

    # Backup (if file exists).
    backup_path: Path | None = None
    if config_existed:
        ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_path = config_path.with_name(config_path.name + f".backup-{ts}")
        shutil.copy2(config_path, backup_path)
        print(f"\nBacked up existing config to: {backup_path}")
    else:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        print(f"\nClaude Desktop config did not exist; will create at {config_path}")

    # Atomic write via tmp + rename.
    existing["mcpServers"]["rka"] = new_entry
    new_text = json.dumps(existing, indent=2) + "\n"

    try:
        # Write to a temp file in the same directory (so rename is atomic).
        tmp_fd, tmp_path = tempfile.mkstemp(
            prefix=config_path.name + ".",
            suffix=".tmp",
            dir=str(config_path.parent),
        )
        try:
            os.write(tmp_fd, new_text.encode("utf-8"))
        finally:
            os.close(tmp_fd)
        os.replace(tmp_path, config_path)

        # Verify by re-reading.
        verify = json.loads(config_path.read_text())
        if verify.get("mcpServers", {}).get("rka") != new_entry:
            raise RuntimeError("post-write verification failed: rka entry not present after rename")
    except Exception as exc:
        print(f"\nERROR: failed to write config: {exc}", file=sys.stderr)
        if backup_path and backup_path.is_file():
            shutil.copy2(backup_path, config_path)
            print(f"Restored original from backup: {backup_path}", file=sys.stderr)
        return 1

    # Success message.
    print(f"\n✅ Claude Desktop config updated at: {config_path}")
    if backup_path:
        print(f"   Backup of previous version: {backup_path}")
    print()
    print("NEXT STEP — fully quit and reopen Claude Desktop:")
    if sys.platform == "darwin":
        print("  • macOS: Cmd+Q (NOT just clicking the red close button — that minimizes)")
    elif sys.platform == "win32":
        print("  • Windows: right-click the Claude tray icon → Quit, then reopen")
    else:
        print("  • Linux: use your desktop environment's Quit shortcut, then reopen")
    print()
    print('Then start a fresh chat and ask: "List my RKA projects."')
    print(
        'Brain should call rka_query with args={"operation":"list_projects"} and return the list.'
    )

    return 0


if __name__ == "__main__":
    sys.exit(main())
