"""Regression coverage for the plugin's OS-neutral launch contract."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _load_script(relative: str, name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeResponse:
    status = 200

    def __init__(self, payload: dict[str, object]) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, limit: int) -> bytes:
        assert limit == 4096
        return self._body[:limit]


def test_plugin_launchers_use_uv_without_a_platform_specific_python_name() -> None:
    mcp = json.loads((ROOT / "plugin/.mcp.json").read_text(encoding="utf-8"))
    entry = mcp["mcpServers"]["rka"]
    assert entry == {
        "command": "uv",
        "args": [
            "run",
            "--no-project",
            "--isolated",
            "${CLAUDE_PLUGIN_ROOT}/bin/rka-mcp-bridge.py",
        ],
    }

    manifest = json.loads((ROOT / "plugin/.claude-plugin/plugin.json").read_text(encoding="utf-8"))
    command = manifest["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    assert command == (
        'uv run --no-project "${CLAUDE_PLUGIN_ROOT}/hooks/session-start.py"'
    )


def test_session_start_defaults_to_ipv4_and_reports_the_live_version(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    hook = _load_script("plugin/hooks/session-start.py", "rka_session_start_test")
    missing = tmp_path / "missing-integration.json"
    called: list[str] = []

    def fake_urlopen(url: str, timeout: int):
        called.append(url)
        assert timeout == 3
        return _FakeResponse({"status": "ok", "version": "3.0.0"})

    monkeypatch.setenv("RKA_INTEGRATION_FILE", str(missing))
    monkeypatch.setattr(hook.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(SystemExit) as exc:
        hook.main()

    assert exc.value.code == 0
    assert called == ["http://127.0.0.1:9712/api/health"]
    assert "version 3.0.0" in capsys.readouterr().out


def test_session_start_prefers_health_version_over_stale_metadata(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    hook = _load_script("plugin/hooks/session-start.py", "rka_session_start_stale_test")
    integration = tmp_path / "integration.json"
    integration.write_text(
        json.dumps(
            {
                "api_endpoint_url": "http://127.0.0.1:19712",
                "backend_version": "2.9.0",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("RKA_INTEGRATION_FILE", str(integration))
    monkeypatch.setattr(
        hook.urllib.request,
        "urlopen",
        lambda _url, timeout: _FakeResponse(
            {"status": "ok", "version": "3.0.0", "timeout": timeout}
        ),
    )

    with pytest.raises(SystemExit):
        hook.main()

    output = capsys.readouterr().out
    assert "version 3.0.0" in output
    assert "version 2.9.0" not in output


def test_desktop_helper_persists_an_absolute_uv_launcher(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    helper = _load_script("plugin/scripts/setup-claude-desktop.py", "rka_desktop_setup_test")
    uv = tmp_path / "uv"
    uv.write_text("placeholder", encoding="utf-8")
    wrapper = tmp_path / "plugin" / "bin" / "rka-mcp-bridge.py"
    monkeypatch.setattr(helper.shutil, "which", lambda name: str(uv) if name == "uv" else None)

    assert helper.mcp_entry(wrapper) == {
        "command": str(uv.resolve()),
        "args": ["run", "--no-project", "--isolated", str(wrapper)],
    }
