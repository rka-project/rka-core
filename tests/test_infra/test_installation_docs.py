"""Regression checks for the public, cross-platform installation contract."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]


def _read(relative: str) -> str:
    return (ROOT / relative).read_text()


@pytest.mark.parametrize("relative", ["README.md", "INSTALL.md", "docs/USER_MANUAL.md"])
def test_primary_install_docs_cover_all_supported_operating_systems(relative: str):
    text = _read(relative)
    assert "macOS" in text
    assert "Linux" in text
    assert "Windows" in text
    assert "uv tool install --force --reinstall ." in text
    assert ".local\\bin\\rka.exe" in text


def test_quick_start_installs_and_verifies_both_runtime_layers():
    readme = _read("README.md")
    assert "docker compose version" in readme
    assert "docker compose up -d" in readme
    assert "~/.local/bin/rka --version" in readme
    assert '"$env:USERPROFILE\\.local\\bin\\rka.exe" --version' in readme
    assert "Invoke-RestMethod http://127.0.0.1:9712/api/health" in readme


@pytest.mark.parametrize("relative", ["README.md", "INSTALL.md"])
def test_active_clone_commands_pin_the_packaged_version(relative: str):
    active = _read(relative).split("## 11.", maxsplit=1)[0]
    version = tomllib.loads(_read("pyproject.toml"))["project"]["version"]
    tags = re.findall(r"git clone --branch (\S+) --depth 1", active)
    assert len(tags) >= 2  # Both POSIX and Windows instructions.
    assert set(tags) == {f"v{version}"}


def test_active_install_guide_has_current_integration_and_codex_contracts():
    install = _read("INSTALL.md")
    active = install.split("## 11.", maxsplit=1)[0]
    version = tomllib.loads(_read("pyproject.toml"))["project"]["version"]

    assert '"schema_version": "rka.integration/v1"' in active
    assert f'"backend_version": "{version}"' in active
    assert '"version": "2.7.0"' not in active
    assert "current 152-operation" not in active
    assert "UV_CACHE_DIR=/tmp" not in active

    assert "[mcp_servers.rka]" in active
    assert "~/.codex/config.toml" in active
    assert "codex mcp add rka --" in active
    assert "codex mcp --help" in active
    assert "older Codex CLI builds do not provide `mcp add`" in active


def test_plugin_helper_and_embedding_docs_match_the_live_contract():
    helper = _read("plugin/scripts/setup-claude-desktop.py")
    embedding = _read("docs/embedding_backends.md")
    bridge = _read("plugin/bin/rka-mcp-bridge.py")

    assert "Brain should call rka_list_projects" not in helper
    assert "http://127.0.0.1:9712" in helper
    assert "fully offline" not in embedding
    assert "first uncached use downloads roughly 520 MB" in embedding
    assert 'data.get("backend_version")' in bridge
    assert "ambiguous legacy `version` field" in bridge
    assert 'data.get("backend_version")' in _read("plugin/hooks/session-start.py")


@pytest.mark.parametrize(
    "relative",
    ["README.md", "INSTALL.md", "docs/USER_MANUAL.md", "docs/embedding_backends.md", "plugin/README.md"],
)
def test_primary_markdown_local_links_resolve(relative: str):
    source = ROOT / relative
    missing: list[str] = []
    for raw_target in re.findall(r"!?\[[^\]]*\]\(([^)]+)\)", source.read_text()):
        target = raw_target.strip().strip("<>").split("#", maxsplit=1)[0]
        if not target or target.startswith(("http://", "https://", "mailto:")):
            continue
        if not (source.parent / target).resolve().exists():
            missing.append(raw_target)
    assert not missing, f"missing local links in {relative}: {missing}"
