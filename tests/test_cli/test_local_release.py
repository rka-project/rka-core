"""Local-first release boundary, without binding ports or reaching a real API."""

import importlib.util
import sys
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from click.testing import CliRunner

from rka import __version__
from rka.cli import main
from rka.cli_cred.manifest import load_manifest, load_versions, versions_path
from rka.mcp.local_transport import LocalOnlyFastMCP


@pytest.mark.parametrize("env_value", ["http", "sse", "streamable-http", "typo", "HTTP"])
def test_remote_env_rejected_before_import(monkeypatch, env_value):
    monkeypatch.setenv("RKA_MCP_TRANSPORT", env_value)
    monkeypatch.setitem(sys.modules, "rka.mcp.server", None)
    result = CliRunner().invoke(main, ["mcp"])
    assert result.exit_code == 2
    assert "Remote HTTP/SSE is disabled" in result.output


def test_remote_cli_rejected_before_import(monkeypatch):
    monkeypatch.setitem(sys.modules, "rka.mcp.server", None)
    result = CliRunner().invoke(main, ["mcp", "--transport", "http"])
    assert result.exit_code == 2
    assert "Remote HTTP/SSE is disabled" in result.output


@pytest.mark.parametrize(
    "args,env_value", [([], ""), ([], "STDIO"), (["--transport", "stdio"], "http")]
)
def test_stdio_preserved(monkeypatch, args, env_value):
    monkeypatch.setenv("RKA_MCP_TRANSPORT", env_value)
    run = Mock()
    monkeypatch.setitem(
        sys.modules, "rka.mcp.server", SimpleNamespace(mcp=SimpleNamespace(run=run))
    )
    result = CliRunner().invoke(main, ["mcp", *args])
    assert result.exit_code == 0, result.output
    run.assert_called_once_with()


@pytest.mark.parametrize("method", ["streamable_http_app", "sse_app"])
def test_direct_asgi_creation_disabled(method):
    server = LocalOnlyFastMCP("test")
    with pytest.raises(RuntimeError, match="Remote MCP is disabled"):
        getattr(server, method)()


@pytest.mark.parametrize("method", ["run_sse_async", "run_streamable_http_async"])
async def test_direct_async_transport_disabled(method):
    server = LocalOnlyFastMCP("test")
    with pytest.raises(RuntimeError, match="Remote MCP is disabled"):
        await getattr(server, method)()


@pytest.mark.parametrize("transport", ["sse", "streamable-http"])
def test_fastmcp_run_rejects_network_transports(transport):
    server = LocalOnlyFastMCP("test")
    with pytest.raises(RuntimeError, match="Remote MCP is disabled"):
        server.run(transport=transport)


@pytest.mark.parametrize("method", ["GET", "POST", "DELETE", "OPTIONS"])
@pytest.mark.parametrize(
    "path", ["/mcp", "/authorize", "/token", "/register", "/.well-known/oauth-authorization-server"]
)
async def test_old_proxy_asgi_rejects_every_route(method, path):
    source = Path(__file__).parents[2] / "scripts/rka_mcp_oauth_proxy.py"
    spec = importlib.util.spec_from_file_location("disabled_proxy", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=module.app), base_url="http://test"
    ) as client:
        response = await client.request(method, path, headers={"Authorization": "Bearer synthetic"})
    assert response.status_code == 503
    assert "disabled" in response.text
    assert response.headers["cache-control"] == "no-store"


def test_new_credential_defaults_follow_installed_core(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    result = CliRunner().invoke(main, ["cred", "init", "--non-interactive"])
    assert result.exit_code == 0, result.output
    versions = load_versions()
    assert versions.host_binaries == {"rka": __version__}
    assert versions.containers == {"rka-server": __version__}
    assert load_manifest().global_required == []


def test_existing_credential_version_pins_preserved(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    runner = CliRunner()
    assert runner.invoke(main, ["cred", "init", "--non-interactive"]).exit_code == 0
    old = '[host.binaries]\nrka = "2.8.1"\n[containers]\n"rka-server" = "2.8.1"\n'
    versions_path().write_text(old)
    assert runner.invoke(main, ["cred", "init", "--non-interactive"]).exit_code == 0
    assert versions_path().read_text() == old


def test_credential_prompt_does_not_echo_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    result = CliRunner().invoke(main, ["cred", "init"], input="synthetic-secret\n1234567\n\n\n\n")
    assert result.exit_code == 0, result.output
    assert "synthetic-secret" not in result.output


def test_actual_mcp_instance_uses_local_boundary():
    from rka.mcp.server import mcp

    assert isinstance(mcp, LocalOnlyFastMCP)


def test_disabled_proxy_cli():
    source = Path(__file__).parents[2] / "scripts/rka_mcp_oauth_proxy.py"
    result = subprocess.run(
        [sys.executable, str(source)], capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 1
    assert "disabled" in result.stderr


def test_disabled_tunnel_never_invokes_ssh(tmp_path):
    shell = shutil.which("sh")
    if not shell:
        pytest.skip("No POSIX shell on this platform")
    source = Path(__file__).parents[2] / "scripts/tunnel.sh"
    result = subprocess.run(
        [shell, str(source), "invalid.example"],
        env={"PATH": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 1
    assert "disabled" in result.stderr
    assert "not found" not in result.stderr


@pytest.mark.parametrize("program", ["rka", "rka.exe", "python -m rka"])
def test_cli_version_identity_is_stable(program):
    from rka.cli_cred.probes import _RKA_VERSION_RE

    result = CliRunner().invoke(main, ["--version"], prog_name=program)
    assert result.exit_code == 0
    assert result.output.strip() == f"rka, version {__version__}"
    assert _RKA_VERSION_RE.search(result.output).group(1) == __version__


def test_legacy_windows_binary_version_is_recognized():
    from rka.cli_cred.probes import _RKA_VERSION_RE

    assert _RKA_VERSION_RE.search("rka.exe, version 2.8.1").group(1) == "2.8.1"
