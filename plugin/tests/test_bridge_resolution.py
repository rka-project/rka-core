from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[1]
BRIDGE = PLUGIN / "bin" / "rka-mcp-bridge.py"

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="stub binaries are POSIX shell scripts")


def _load_bridge():
    spec = importlib.util.spec_from_file_location("rka_mcp_bridge_resolution", BRIDGE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _stub(path: Path, name: str, *, version_exit: int = 0, shebang: str = "#!/bin/sh") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"{shebang}\n"
        'case "$1" in\n'
        f'  --version) echo "rka, version 0.0.0+{name}"; exit {version_exit} ;;\n'
        f'  mcp) echo "STUB={name}"; exit 0 ;;\n'
        "esac\n"
        "exit 2\n"
    )
    path.chmod(0o755)
    return path


def _run_bridge(
    home: Path,
    *,
    path_dirs: list[Path],
    venv: Path | None = None,
    integration: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    env = {
        "HOME": str(home),
        "USERPROFILE": str(home),
        "PATH": os.pathsep.join(str(d) for d in path_dirs),
        "RKA_INTEGRATION_FILE": str(integration or home / "absent-integration.json"),
    }
    if venv is not None:
        env["VIRTUAL_ENV"] = str(venv)
    return subprocess.run(
        [sys.executable, str(BRIDGE)],
        input="",
        capture_output=True,
        text=True,
        env=env,
    )


@posix_only
class TestBinaryResolution:
    def test_venv_rka_with_dead_shebang_is_skipped_for_the_uv_tool_binary(self, tmp_path: Path):
        home = tmp_path / "home"
        venv = tmp_path / "project" / ".venv"
        broken = _stub(venv / "bin" / "rka", "venv", shebang="#!/nonexistent/python3")
        uv_tool = _stub(home / ".local" / "bin" / "rka", "uv-tool")

        result = _run_bridge(home, path_dirs=[venv / "bin"], venv=venv)

        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "STUB=uv-tool"
        assert f"NOTICE: using {uv_tool} (uv-tool); not tried: {broken} (VIRTUAL_ENV)." in result.stderr
        assert "[Errno 2]" not in result.stderr

    def test_healthy_venv_rka_does_not_outrank_the_uv_tool_binary(self, tmp_path: Path):
        home = tmp_path / "home"
        venv = tmp_path / "project" / ".venv"
        venv_rka = _stub(venv / "bin" / "rka", "venv")
        uv_tool = _stub(home / ".local" / "bin" / "rka", "uv-tool")

        result = _run_bridge(home, path_dirs=[venv / "bin", home / ".local" / "bin"], venv=venv)

        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "STUB=uv-tool"
        assert f"NOTICE: using {uv_tool} (uv-tool); not tried: {venv_rka} (VIRTUAL_ENV)." in result.stderr

    def test_integration_json_binary_path_still_wins(self, tmp_path: Path):
        home = tmp_path / "home"
        venv = tmp_path / "project" / ".venv"
        _stub(venv / "bin" / "rka", "venv")
        _stub(home / ".local" / "bin" / "rka", "uv-tool")
        launcher = _stub(tmp_path / "launcher" / "rka", "integration")
        integration = tmp_path / "integration.json"
        integration.write_text(
            json.dumps(
                {
                    "schema_version": "rka.integration/v1",
                    "backend_version": "3.0.0",
                    "binary_path": str(launcher),
                }
            )
        )

        result = _run_bridge(
            home,
            path_dirs=[venv / "bin", home / ".local" / "bin"],
            venv=venv,
            integration=integration,
        )

        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "STUB=integration"
        assert "skipped rka candidate" not in result.stderr

    def test_venv_rka_is_the_last_resort_and_says_so(self, tmp_path: Path):
        home = tmp_path / "home"
        venv = tmp_path / "project" / ".venv"
        only = _stub(venv / "bin" / "rka", "venv")

        result = _run_bridge(home, path_dirs=[venv / "bin"], venv=venv)

        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "STUB=venv"
        assert f"NOTICE: using {only} (VIRTUAL_ENV)." in result.stderr
        assert "uv tool install" in result.stderr

    def test_candidate_whose_version_probe_fails_is_skipped(self, tmp_path: Path):
        home = tmp_path / "home"
        failing = _stub(home / ".local" / "bin" / "rka", "uv-tool", version_exit=3)
        fallback = _stub(tmp_path / "opt" / "bin" / "rka", "path")

        result = _run_bridge(home, path_dirs=[tmp_path / "opt" / "bin"])

        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "STUB=path"
        assert f"NOTICE: skipped rka candidate {failing} (uv-tool): --version exited 3" in result.stderr
        assert f"NOTICE: using {fallback} (PATH)." in result.stderr

    def test_no_usable_candidate_fails_and_names_every_rejection(self, tmp_path: Path):
        home = tmp_path / "home"
        venv = tmp_path / "project" / ".venv"
        broken = _stub(venv / "bin" / "rka", "venv", shebang="#!/nonexistent/python3")

        result = _run_bridge(home, path_dirs=[venv / "bin"], venv=venv)

        assert result.returncode == 1
        assert result.stdout == ""
        assert (
            f"NOTICE: skipped rka candidate {broken} (VIRTUAL_ENV): "
            "shebang interpreter missing: /nonexistent/python3"
        ) in result.stderr
        assert "ERROR: rka binary not found" in result.stderr


@posix_only
def test_version_probe_gives_up_on_a_hanging_binary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    bridge = _load_bridge()
    hanging = tmp_path / "rka"
    hanging.write_text("#!/bin/sh\nsleep 5\n")
    hanging.chmod(0o755)
    monkeypatch.setattr(bridge, "VERSION_PROBE_TIMEOUT", 0.2)

    assert bridge.rejection_reason(str(hanging)) == "--version did not finish within 0.2s"


@posix_only
@pytest.mark.skipif(shutil.which("uv") is None, reason="uv is not installed")
def test_manifest_flags_keep_uv_from_adopting_the_working_directory_venv(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    subprocess.run(["uv", "venv", "-q", "--python", sys.executable, str(project / ".venv")], check=True)
    stale = _stub(project / ".venv" / "bin" / "rka", "venv", shebang="#!/nonexistent/python3")
    manifest = json.loads((PLUGIN / ".mcp.json").read_text())["mcpServers"]["rka"]
    flags = [arg for arg in manifest["args"] if arg.startswith("--")]
    probe = "import shutil; print(shutil.which('rka') or '')"

    plain = subprocess.run(
        ["uv", "run", "--no-project", "python", "-c", probe],
        cwd=project, capture_output=True, text=True, check=True,
    )
    with_manifest_flags = subprocess.run(
        [manifest["command"], "run", *flags, "python", "-c", probe],
        cwd=project, capture_output=True, text=True, check=True,
    )

    assert Path(plain.stdout.strip()).resolve() == stale.resolve()
    picked = with_manifest_flags.stdout.strip()
    assert not picked or not Path(picked).resolve().is_relative_to(project.resolve()), picked
