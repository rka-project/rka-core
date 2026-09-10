"""Drift probes for `rka cred check`.

All probes are READ-ONLY (never write). Each returns a ProbeResult
with structured pass/fail/skip semantics. The CLI renders these to a
table and exits 1 if any FAIL is found (SKIP is NOT a failure).
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from rka.cli_cred.manifest import Manifest, Versions
from rka.cli_cred.propagators import (
    claude_code_json_path,
    claude_desktop_config_path,
    orchestrator_env_path,
)
from rka.cli_cred.vault import parse_dotenv


PROBE_PASS = "PASS"
PROBE_FAIL = "FAIL"
PROBE_SKIP = "SKIP"


@dataclass
class ProbeResult:
    name: str
    status: str  # PASS | FAIL | SKIP
    expected: str = ""
    found: str = ""
    hint: str = ""

    @property
    def is_failure(self) -> bool:
        return self.status == PROBE_FAIL


# ----------------------------------------------------------------------
# (0) manifest coverage
# ----------------------------------------------------------------------


def probe_manifest_coverage(manifest: Manifest, creds: dict[str, str]) -> ProbeResult:
    missing = [k for k in manifest.global_required if not creds.get(k)]
    if missing:
        return ProbeResult(
            name="manifest_coverage",
            status=PROBE_FAIL,
            expected=", ".join(manifest.global_required),
            found=f"missing: {', '.join(missing)}",
            hint="rka cred set KEY VALUE  (or rerun rka cred init)",
        )
    return ProbeResult(
        name="manifest_coverage",
        status=PROBE_PASS,
        expected=", ".join(manifest.global_required) or "(none)",
        found="all required keys present",
    )


# ----------------------------------------------------------------------
# (i) Claude Desktop zotero env matches creds.env
# ----------------------------------------------------------------------


def _mcp_zotero_env(path: Path) -> dict[str, str] | None:
    """Return the mcpServers.zotero.env dict, or None on missing/malformed."""
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    servers = data.get("mcpServers", {})
    if not isinstance(servers, dict):
        return None
    zotero = servers.get("zotero", {})
    if not isinstance(zotero, dict):
        return None
    env = zotero.get("env", {})
    if not isinstance(env, dict):
        return None
    return env


def probe_claude_desktop(creds: dict[str, str], config_path: Path | None = None) -> ProbeResult:
    path = config_path or claude_desktop_config_path()
    env = _mcp_zotero_env(path)
    if env is None:
        return ProbeResult(
            name="claude_desktop",
            status=PROBE_SKIP,
            expected=str(path),
            found="zotero MCP not installed / file missing",
        )

    mismatched: list[str] = []
    for key in ("ZOTERO_API_KEY", "ZOTERO_LIBRARY_ID", "ZOTERO_LIBRARY_TYPE"):
        c_val = creds.get(key, "")
        e_val = env.get(key, "")
        if not c_val and not e_val:
            continue
        if c_val != e_val:
            mismatched.append(key)

    if mismatched:
        return ProbeResult(
            name="claude_desktop",
            status=PROBE_FAIL,
            expected="match creds.env",
            found=f"diverged: {', '.join(mismatched)}",
            hint="rka cred propagate --apply",
        )
    return ProbeResult(
        name="claude_desktop",
        status=PROBE_PASS,
        expected="match creds.env",
        found="zotero env in sync",
    )


# ----------------------------------------------------------------------
# (ii) ~/.claude.json zotero env matches creds.env
# ----------------------------------------------------------------------


def probe_claude_code_json(creds: dict[str, str], config_path: Path | None = None) -> ProbeResult:
    path = config_path or claude_code_json_path()
    env = _mcp_zotero_env(path)
    if env is None:
        return ProbeResult(
            name="claude_code_json",
            status=PROBE_SKIP,
            expected=str(path),
            found="zotero MCP not in Claude Code config",
        )

    mismatched: list[str] = []
    for key in ("ZOTERO_API_KEY", "ZOTERO_LIBRARY_ID", "ZOTERO_LIBRARY_TYPE"):
        c_val = creds.get(key, "")
        e_val = env.get(key, "")
        if not c_val and not e_val:
            continue
        if c_val != e_val:
            mismatched.append(key)

    if mismatched:
        return ProbeResult(
            name="claude_code_json",
            status=PROBE_FAIL,
            expected="match creds.env",
            found=f"diverged: {', '.join(mismatched)}",
            hint="rka cred propagate --apply",
        )
    return ProbeResult(
        name="claude_code_json",
        status=PROBE_PASS,
        expected="match creds.env",
        found="zotero env in sync",
    )


# ----------------------------------------------------------------------
# (iii) /api/config/zotero on rka-server matches
# ----------------------------------------------------------------------


def probe_rka_server_zotero(
    creds: dict[str, str],
    api_url: str = "http://127.0.0.1:9712",
    http_client=None,
) -> ProbeResult:
    name = "rka_server_zotero"
    client = http_client
    if client is None:
        try:
            import httpx

            client = httpx
        except ImportError:
            return ProbeResult(
                name=name,
                status=PROBE_SKIP,
                expected=api_url,
                found="httpx not available",
            )

    try:
        resp = client.get(f"{api_url}/api/config/zotero", timeout=3.0)
    except Exception as exc:  # noqa: BLE001
        return ProbeResult(
            name=name,
            status=PROBE_SKIP,
            expected=api_url,
            found=f"unreachable: {exc!s}",
            hint="docker compose up -d",
        )

    if resp.status_code != 200:
        return ProbeResult(
            name=name,
            status=PROBE_FAIL,
            expected="200 OK",
            found=f"HTTP {resp.status_code}",
            hint="check rka-server logs",
        )

    try:
        data = resp.json()
    except Exception:  # noqa: BLE001
        return ProbeResult(
            name=name,
            status=PROBE_FAIL,
            expected="JSON body",
            found="non-JSON response",
        )

    mismatched: list[str] = []
    expected_lib_id = creds.get("ZOTERO_LIBRARY_ID", "")
    expected_lib_type = creds.get("ZOTERO_LIBRARY_TYPE", "user") or "user"
    expected_api_present = bool(creds.get("ZOTERO_API_KEY", ""))

    if expected_lib_id and data.get("library_id") != expected_lib_id:
        mismatched.append("library_id")
    if expected_lib_type and data.get("library_type") != expected_lib_type:
        mismatched.append("library_type")
    server_api = data.get("api_key", "")
    # API key on the wire is always '***' (redacted by server). Presence-check only.
    if expected_api_present and (not server_api):
        mismatched.append("api_key(absent on server)")

    if mismatched:
        return ProbeResult(
            name=name,
            status=PROBE_FAIL,
            expected="match creds.env",
            found=f"diverged: {', '.join(mismatched)}",
            hint="rka cred propagate --apply (PUTs /api/config/zotero)",
        )
    return ProbeResult(
        name=name,
        status=PROBE_PASS,
        expected="match creds.env",
        found="rka-server zotero config in sync",
    )


# ----------------------------------------------------------------------
# (iv,v) docker exec env probes (rka-server + rka-orchestrator)
# ----------------------------------------------------------------------


def _docker_container_running(container: str) -> bool:
    try:
        result = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Running}}", container],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and result.stdout.strip() == "true"


def _docker_exec_env(container: str) -> dict[str, str] | None:
    try:
        result = subprocess.run(
            ["docker", "exec", container, "env"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    env: dict[str, str] = {}
    for line in result.stdout.splitlines():
        key, _, value = line.partition("=")
        env[key.strip()] = value
    return env


def probe_rka_server_env(creds: dict[str, str]) -> ProbeResult:
    """rka-server container env. NUANCE: empty ZOTERO_* in rka-server is
    NORMAL post-v2.7.0.2 (creds flow via /data/zotero_config.json). We
    only treat divergence as drift when env is non-empty AND mismatches.
    """
    name = "rka_server_env"
    if not _docker_container_running("rka-server"):
        return ProbeResult(
            name=name,
            status=PROBE_SKIP,
            expected="rka-server running",
            found="container not running",
        )
    env = _docker_exec_env("rka-server")
    if env is None:
        return ProbeResult(
            name=name,
            status=PROBE_SKIP,
            expected="docker exec rka-server env",
            found="docker exec failed",
        )

    mismatched: list[str] = []
    for key in ("ZOTERO_API_KEY", "ZOTERO_LIBRARY_ID", "ZOTERO_LIBRARY_TYPE"):
        env_val = env.get(key, "")
        cred_val = creds.get(key, "")
        if env_val and env_val != cred_val:
            mismatched.append(key)

    if mismatched:
        return ProbeResult(
            name=name,
            status=PROBE_FAIL,
            expected="match creds.env (or empty per v2.7.0.2)",
            found=f"diverged: {', '.join(mismatched)}",
            hint="docker compose up -d --build rka-server",
        )
    return ProbeResult(
        name=name,
        status=PROBE_PASS,
        expected="match creds.env (or empty)",
        found="rka-server env consistent (or empty per v2.7.0.2)",
    )


def probe_rka_orchestrator_env(creds: dict[str, str]) -> ProbeResult:
    name = "rka_orchestrator_env"
    if not _docker_container_running("rka-orchestrator"):
        return ProbeResult(
            name=name,
            status=PROBE_SKIP,
            expected="rka-orchestrator running",
            found="container not running",
        )
    env = _docker_exec_env("rka-orchestrator")
    if env is None:
        return ProbeResult(
            name=name,
            status=PROBE_SKIP,
            expected="docker exec rka-orchestrator env",
            found="docker exec failed",
        )

    mismatched: list[str] = []
    for key in ("ZOTERO_API_KEY", "ZOTERO_LIBRARY_ID", "ZOTERO_LIBRARY_TYPE"):
        cred_val = creds.get(key, "")
        env_val = env.get(key, "")
        if cred_val and env_val != cred_val:
            mismatched.append(key)

    if mismatched:
        return ProbeResult(
            name=name,
            status=PROBE_FAIL,
            expected="match creds.env",
            found=f"diverged: {', '.join(mismatched)}",
            hint=(
                "rka cred propagate --apply, then docker compose -f docker-compose.yml "
                "-f orchestrator/docker-compose.yml up -d --force-recreate rka-orchestrator"
            ),
        )
    return ProbeResult(
        name=name,
        status=PROBE_PASS,
        expected="match creds.env",
        found="orchestrator env in sync",
    )


# ----------------------------------------------------------------------
# (vi) host `rka --version` matches versions.toml
# ----------------------------------------------------------------------


_RKA_VERSION_RE = re.compile(r"\brka(?:\.exe)?,\s+version\s+(\S+)")


def probe_host_rka_version(versions: Versions) -> ProbeResult:
    name = "host_rka_version"
    expected = versions.host_binaries.get("rka", "")
    if not expected:
        return ProbeResult(
            name=name,
            status=PROBE_SKIP,
            expected="(unset)",
            found="versions.toml has no host.binaries.rka",
        )
    try:
        result = subprocess.run(
            ["rka", "--version"], capture_output=True, text=True, timeout=2
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ProbeResult(
            name=name,
            status=PROBE_FAIL,
            expected=expected,
            found="rka CLI not in PATH",
            hint="uv tool install --force --reinstall .",
        )
    if result.returncode != 0:
        return ProbeResult(
            name=name,
            status=PROBE_FAIL,
            expected=expected,
            found=f"rka --version exit {result.returncode}",
        )
    match = _RKA_VERSION_RE.search(result.stdout)
    if not match:
        return ProbeResult(
            name=name,
            status=PROBE_FAIL,
            expected=expected,
            found=f"unparseable output: {result.stdout.strip()[:60]}",
        )
    actual = match.group(1)
    if actual != expected:
        return ProbeResult(
            name=name,
            status=PROBE_FAIL,
            expected=expected,
            found=actual,
            hint="uv tool install --force --reinstall .",
        )
    return ProbeResult(name=name, status=PROBE_PASS, expected=expected, found=actual)


# ----------------------------------------------------------------------
# (vii) /api/health version matches
# ----------------------------------------------------------------------


def probe_rka_server_health(
    versions: Versions,
    api_url: str = "http://127.0.0.1:9712",
    http_client=None,
) -> ProbeResult:
    name = "rka_server_health"
    expected = versions.containers.get("rka-server", "")
    if not expected:
        return ProbeResult(
            name=name,
            status=PROBE_SKIP,
            expected="(unset)",
            found="versions.toml has no containers.rka-server",
        )
    client = http_client
    if client is None:
        try:
            import httpx

            client = httpx
        except ImportError:
            return ProbeResult(
                name=name,
                status=PROBE_SKIP,
                expected=expected,
                found="httpx not available",
            )
    try:
        resp = client.get(f"{api_url}/api/health", timeout=3.0)
    except Exception as exc:  # noqa: BLE001
        return ProbeResult(
            name=name,
            status=PROBE_SKIP,
            expected=expected,
            found=f"unreachable: {exc!s}",
            hint="docker compose up -d",
        )
    if resp.status_code != 200:
        return ProbeResult(
            name=name,
            status=PROBE_FAIL,
            expected=expected,
            found=f"HTTP {resp.status_code}",
        )
    try:
        data = resp.json()
    except Exception:  # noqa: BLE001
        return ProbeResult(
            name=name, status=PROBE_FAIL, expected=expected, found="non-JSON"
        )
    actual = data.get("version", "")
    if actual != expected:
        return ProbeResult(
            name=name,
            status=PROBE_FAIL,
            expected=expected,
            found=actual,
            hint="docker compose up -d --build",
        )
    return ProbeResult(name=name, status=PROBE_PASS, expected=expected, found=actual)


# ----------------------------------------------------------------------
# (viii) orchestrator version matches (via docker exec)
# ----------------------------------------------------------------------


_TOML_VERSION_RE = re.compile(r'version\s*=\s*"([^"]+)"')


def probe_orchestrator_version(versions: Versions) -> ProbeResult:
    name = "rka_orchestrator_version"
    expected = versions.containers.get("rka-orchestrator", "")
    if not expected:
        return ProbeResult(
            name=name,
            status=PROBE_SKIP,
            expected="(unset)",
            found="versions.toml has no containers.rka-orchestrator",
        )
    if not _docker_container_running("rka-orchestrator"):
        return ProbeResult(
            name=name,
            status=PROBE_SKIP,
            expected=expected,
            found="container not running",
        )
    try:
        result = subprocess.run(
            ["docker", "exec", "rka-orchestrator", "grep", "-E", "^version", "/app/orchestrator/pyproject.toml"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ProbeResult(
            name=name,
            status=PROBE_SKIP,
            expected=expected,
            found="docker exec failed",
        )
    if result.returncode != 0:
        return ProbeResult(
            name=name,
            status=PROBE_FAIL,
            expected=expected,
            found="version line not found in pyproject.toml",
        )
    match = _TOML_VERSION_RE.search(result.stdout)
    if not match:
        return ProbeResult(
            name=name,
            status=PROBE_FAIL,
            expected=expected,
            found=f"unparseable: {result.stdout.strip()[:60]}",
        )
    actual = match.group(1)
    if actual != expected:
        return ProbeResult(
            name=name,
            status=PROBE_FAIL,
            expected=expected,
            found=actual,
            hint=(
                "docker compose -f docker-compose.yml -f orchestrator/docker-compose.yml "
                "up -d --build rka-orchestrator"
            ),
        )
    return ProbeResult(name=name, status=PROBE_PASS, expected=expected, found=actual)


# ----------------------------------------------------------------------
# Batch entry — run all probes
# ----------------------------------------------------------------------


def run_all_probes(
    manifest: Manifest,
    versions: Versions,
    creds: dict[str, str],
    *,
    api_url: str = "http://127.0.0.1:9712",
    http_client=None,
    claude_desktop_path: Path | None = None,
    claude_code_path: Path | None = None,
) -> list[ProbeResult]:
    """Run every probe and return the list of results, in order."""
    return [
        probe_manifest_coverage(manifest, creds),
        probe_claude_desktop(creds, config_path=claude_desktop_path),
        probe_claude_code_json(creds, config_path=claude_code_path),
        probe_rka_server_zotero(creds, api_url=api_url, http_client=http_client),
        probe_rka_server_env(creds),
        probe_rka_orchestrator_env(creds),
        probe_host_rka_version(versions),
        probe_rka_server_health(versions, api_url=api_url, http_client=http_client),
        probe_orchestrator_version(versions),
    ]
