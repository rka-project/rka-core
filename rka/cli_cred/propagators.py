"""Propagation targets for `rka cred propagate`.

Each propagator returns a `PropagationResult` describing what changed
(or would change in dry-run). Apply path uses the atomic write helper.
Each consumer is implemented as an independent function so failures in
one don't tank the others.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from rka.cli_cred.vault import atomic_write_text, parse_dotenv

# Zotero keys are the propagated set in Phase 1.
ZOTERO_KEYS = ("ZOTERO_API_KEY", "ZOTERO_LIBRARY_ID", "ZOTERO_LIBRARY_TYPE")

# Excluded from orchestrator/.env propagation per spec.
EXCLUDED_FROM_ORCHESTRATOR = ("ANTHROPIC_API_KEY",)


# ----------------------------------------------------------------------
# Result type
# ----------------------------------------------------------------------


@dataclass
class PropagationResult:
    consumer: str = ""
    status: str = ""  # 'unchanged' | 'would_change' | 'applied' | 'skipped' | 'error'
    summary: str = ""
    changes: dict[str, tuple[str, str]] = field(default_factory=dict)
    # changes maps key -> (old_value_redacted, new_value_redacted).
    target_path: str = ""
    needs_rebuild: bool = False
    rebuild_hint: str = ""


def _redact(value: str) -> str:
    if not value:
        return "<empty>"
    if len(value) <= 4:
        return "***"
    return value[:2] + "***" + value[-2:]


# ----------------------------------------------------------------------
# Claude Desktop config (mcpServers.zotero.env merge)
# ----------------------------------------------------------------------


def claude_desktop_config_path() -> Path:
    """OS-specific Claude Desktop config path."""
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming"))
        return Path(appdata) / "Claude" / "claude_desktop_config.json"
    # Linux fallback.
    return Path.home() / ".config" / "Claude" / "claude_desktop_config.json"


def propagate_claude_desktop(
    creds: dict[str, str],
    *,
    apply: bool,
    config_path: Path | None = None,
) -> PropagationResult:
    """Merge ZOTERO_* into mcpServers.zotero.env. Preserves all other entries.

    Returns a `PropagationResult`. Idempotent: status='unchanged' when
    no diff.
    """
    path = config_path or claude_desktop_config_path()
    result = PropagationResult(consumer="claude_desktop", target_path=str(path))

    if not path.exists():
        result.status = "skipped"
        result.summary = f"file not present at {path} (Claude Desktop not installed)"
        return result

    try:
        existing = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        result.status = "error"
        result.summary = f"invalid JSON: {exc!s}"
        return result

    if not isinstance(existing, dict):
        result.status = "error"
        result.summary = "top-level JSON is not an object"
        return result

    mcp_servers = existing.setdefault("mcpServers", {})
    if not isinstance(mcp_servers, dict):
        result.status = "error"
        result.summary = "mcpServers is not an object"
        return result

    zotero = mcp_servers.get("zotero")
    if not isinstance(zotero, dict):
        result.status = "skipped"
        result.summary = "mcpServers.zotero not present (zotero MCP not installed)"
        return result

    env_block = zotero.setdefault("env", {})
    if not isinstance(env_block, dict):
        result.status = "error"
        result.summary = "mcpServers.zotero.env is not an object"
        return result

    changes: dict[str, tuple[str, str]] = {}
    for key in ZOTERO_KEYS:
        new_val = creds.get(key, "")
        if not new_val:
            continue  # don't propagate empty
        old_val = env_block.get(key, "")
        if old_val != new_val:
            changes[key] = (_redact(str(old_val)), _redact(new_val))

    if not changes:
        result.status = "unchanged"
        result.summary = "zotero env block already matches creds.env"
        return result

    if not apply:
        result.status = "would_change"
        result.changes = changes
        result.summary = f"would update {len(changes)} key(s) in mcpServers.zotero.env"
        return result

    # Apply.
    for key in changes:
        env_block[key] = creds[key]
    body = json.dumps(existing, indent=2) + "\n"
    atomic_write_text(path, body, mode=0o600)
    result.status = "applied"
    result.changes = changes
    result.summary = f"updated {len(changes)} key(s) in mcpServers.zotero.env"
    result.needs_rebuild = True
    result.rebuild_hint = "restart Claude Desktop to pick up env changes"
    return result


# ----------------------------------------------------------------------
# Claude Code per-user (~/.claude.json) — same shape
# ----------------------------------------------------------------------


def claude_code_json_path() -> Path:
    return Path.home() / ".claude.json"


def propagate_claude_code(
    creds: dict[str, str],
    *,
    apply: bool,
    config_path: Path | None = None,
) -> PropagationResult:
    """Merge ZOTERO_* into ~/.claude.json mcpServers.zotero.env.

    Same JSON shape as claude_desktop_config.json under mcpServers.zotero.env.
    """
    path = config_path or claude_code_json_path()
    result = PropagationResult(consumer="claude_code_json", target_path=str(path))

    if not path.exists():
        result.status = "skipped"
        result.summary = f"file not present at {path} (Claude Code not configured)"
        return result

    try:
        existing = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        result.status = "error"
        result.summary = f"invalid JSON: {exc!s}"
        return result

    if not isinstance(existing, dict):
        result.status = "error"
        result.summary = "top-level JSON is not an object"
        return result

    mcp_servers = existing.setdefault("mcpServers", {})
    if not isinstance(mcp_servers, dict):
        result.status = "error"
        result.summary = "mcpServers is not an object"
        return result

    zotero = mcp_servers.get("zotero")
    if not isinstance(zotero, dict):
        result.status = "skipped"
        result.summary = "mcpServers.zotero not present (zotero MCP not installed in Code)"
        return result

    env_block = zotero.setdefault("env", {})
    if not isinstance(env_block, dict):
        result.status = "error"
        result.summary = "mcpServers.zotero.env is not an object"
        return result

    changes: dict[str, tuple[str, str]] = {}
    for key in ZOTERO_KEYS:
        new_val = creds.get(key, "")
        if not new_val:
            continue
        old_val = env_block.get(key, "")
        if old_val != new_val:
            changes[key] = (_redact(str(old_val)), _redact(new_val))

    if not changes:
        result.status = "unchanged"
        result.summary = "zotero env block already matches creds.env"
        return result

    if not apply:
        result.status = "would_change"
        result.changes = changes
        result.summary = f"would update {len(changes)} key(s) in mcpServers.zotero.env"
        return result

    for key in changes:
        env_block[key] = creds[key]
    body = json.dumps(existing, indent=2) + "\n"
    atomic_write_text(path, body, mode=0o600)
    result.status = "applied"
    result.changes = changes
    result.summary = f"updated {len(changes)} key(s) in mcpServers.zotero.env"
    result.needs_rebuild = True
    result.rebuild_hint = "restart Claude Code session to pick up env changes"
    return result


# ----------------------------------------------------------------------
# rka-server REST: PUT /api/config/zotero
# ----------------------------------------------------------------------


def propagate_rka_server(
    creds: dict[str, str],
    *,
    apply: bool,
    api_url: str = "http://127.0.0.1:9712",
    http_client=None,
) -> PropagationResult:
    """PUT /api/config/zotero with creds.env values.

    NOTE: rka-server probes Zotero before persisting — a failed probe
    returns 422. This means propagate's --apply doubles as a validity
    check on the supplied creds.

    `http_client` is an optional injected httpx-like client for tests.
    """
    result = PropagationResult(
        consumer="rka_server_rest",
        target_path=f"{api_url}/api/config/zotero",
    )

    required_keys = ("ZOTERO_API_KEY", "ZOTERO_LIBRARY_ID")
    missing = [k for k in required_keys if not creds.get(k)]
    if missing:
        result.status = "skipped"
        result.summary = f"missing required creds: {', '.join(missing)}"
        return result

    payload = {
        "api_key": creds["ZOTERO_API_KEY"],
        "library_id": creds["ZOTERO_LIBRARY_ID"],
        "library_type": creds.get("ZOTERO_LIBRARY_TYPE") or "user",
    }

    # GET current to detect drift.
    client = http_client or _default_httpx_client()
    if client is None:
        result.status = "error"
        result.summary = "httpx not available; cannot probe rka-server"
        return result

    try:
        current_resp = client.get(f"{api_url}/api/config/zotero", timeout=3.0)
    except Exception as exc:  # noqa: BLE001
        result.status = "error"
        result.summary = f"rka-server unreachable: {exc!s}"
        return result

    current: dict[str, str] = {}
    if current_resp.status_code == 200:
        try:
            current = current_resp.json()
        except Exception:  # noqa: BLE001
            current = {}

    changes: dict[str, tuple[str, str]] = {}
    cur_lib_id = current.get("library_id", "") or ""
    cur_lib_type = current.get("library_type", "") or ""
    cur_api_present = bool((current.get("api_key", "") or "") and current.get("api_key") != "")

    if cur_lib_id != payload["library_id"]:
        changes["ZOTERO_LIBRARY_ID"] = (_redact(cur_lib_id), _redact(payload["library_id"]))
    if cur_lib_type != payload["library_type"]:
        changes["ZOTERO_LIBRARY_TYPE"] = (_redact(cur_lib_type), _redact(payload["library_type"]))
    if not cur_api_present:
        changes["ZOTERO_API_KEY"] = ("<empty>", _redact(payload["api_key"]))

    if not changes:
        result.status = "unchanged"
        result.summary = "rka-server config already matches creds.env"
        return result

    if not apply:
        result.status = "would_change"
        result.changes = changes
        result.summary = f"would PUT /api/config/zotero (probes Zotero before persist)"
        return result

    try:
        put_resp = client.put(
            f"{api_url}/api/config/zotero",
            params={"actor": "cred-vault"},
            json=payload,
            timeout=10.0,
        )
    except Exception as exc:  # noqa: BLE001
        result.status = "error"
        result.summary = f"PUT failed: {exc!s}"
        return result

    if put_resp.status_code != 200:
        result.status = "error"
        try:
            detail = put_resp.json()
        except Exception:  # noqa: BLE001
            detail = put_resp.text
        result.summary = f"PUT returned {put_resp.status_code}: {detail}"
        return result

    result.status = "applied"
    result.changes = changes
    result.summary = "PUT /api/config/zotero succeeded (Zotero probe passed)"
    return result


def _default_httpx_client():
    try:
        import httpx

        return httpx
    except ImportError:
        return None


# ----------------------------------------------------------------------
# orchestrator/.env file (overwrite, exclude ANTHROPIC_API_KEY)
# ----------------------------------------------------------------------


def orchestrator_env_path() -> Path | None:
    """Resolve orchestrator/.env path.

    Honours HOST_ORCH_ENV; else looks in CWD/orchestrator/.env;
    else returns None (orchestrator package not present).
    """
    override = os.environ.get("HOST_ORCH_ENV", "").strip()
    if override:
        return Path(override).expanduser()
    candidate = Path.cwd() / "orchestrator" / ".env"
    if candidate.exists():
        return candidate
    return None


def propagate_orchestrator_env(
    creds: dict[str, str],
    *,
    apply: bool,
    target_path: Path | None = None,
) -> PropagationResult:
    """Overwrite ZOTERO_* keys in orchestrator/.env. Preserves other keys
    (e.g. CLAUDE_CODE_OAUTH_TOKEN). Excludes ANTHROPIC_API_KEY per spec.
    """
    path = target_path or orchestrator_env_path()
    if path is None:
        result = PropagationResult(consumer="orchestrator_env_file")
        result.status = "skipped"
        result.summary = "orchestrator/.env not present (orchestrator package not in CWD)"
        return result

    result = PropagationResult(consumer="orchestrator_env_file", target_path=str(path))

    if not path.exists():
        result.status = "skipped"
        result.summary = f"file not present at {path}"
        return result

    body = path.read_text()
    dot = parse_dotenv(body)

    changes: dict[str, tuple[str, str]] = {}
    for key, value in creds.items():
        if key in EXCLUDED_FROM_ORCHESTRATOR:
            continue
        # Phase 1: only propagate ZOTERO_* + a few well-known optional creds.
        if not (key.startswith("ZOTERO_") or key in ("SEMANTIC_SCHOLAR_API_KEY", "SERPAPI_KEY")):
            continue
        if not value:
            continue
        existing_value = dot.get(key) or ""
        if existing_value != value:
            changes[key] = (_redact(existing_value), _redact(value))

    if not changes:
        result.status = "unchanged"
        result.summary = "orchestrator/.env already matches creds.env"
        return result

    if not apply:
        result.status = "would_change"
        result.changes = changes
        result.summary = f"would update {len(changes)} key(s) in {path}"
        return result

    for key in changes:
        dot.set(key, creds[key])

    atomic_write_text(path, dot.render(), mode=0o600)
    result.status = "applied"
    result.changes = changes
    result.summary = f"updated {len(changes)} key(s) in orchestrator/.env"
    result.needs_rebuild = True
    result.rebuild_hint = (
        "docker compose -f docker-compose.yml -f orchestrator/docker-compose.yml "
        "up -d --force-recreate rka-orchestrator"
    )
    return result


# ----------------------------------------------------------------------
# Public batch entry — list of consumers in canonical order
# ----------------------------------------------------------------------


def all_propagators():
    """Returns the list of (name, callable) pairs in canonical order.

    Each callable takes (creds_dict, apply: bool) and returns a
    PropagationResult.
    """
    return [
        ("claude_desktop", propagate_claude_desktop),
        ("claude_code_json", propagate_claude_code),
        ("rka_server_rest", propagate_rka_server),
        ("orchestrator_env_file", propagate_orchestrator_env),
    ]
