"""ChatGPT connector adapters for packaged RKA skills.

Surface/tier assertions run in a pristine subprocess interpreter instead of
importlib.reload: reload re-executes rka.mcp.server in place and rebinds
module globals (e.g. _TOOL_REGISTRY), which silently breaks every other test
module that bound those objects at collection time.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import get_args

import pytest

import rka.mcp.server as server

_SURFACE_SNIPPET = (
    "import json, rka.mcp.server as s;"
    "vis = sorted(t.name for t in s.mcp._tool_manager.list_tools());"
    "tiers = {n: e['tier'] for n, e in s._TOOL_REGISTRY.items()"
    " if e['category'] == 'skills'};"
    "adv = 'rka_start_session' in s.RKA_INSTRUCTIONS;"
    "print(json.dumps({'visible': vis, 'tiers': tiers, 'advertised': adv}))"
)

_SKILL_TOOLS = ("rka_list_skills", "rka_read_skill", "rka_start_session")
_SKILL_READS = {"rka_list_skills", "rka_read_skill"}
_DISPATCH_SURFACE = {"rka_query", "rka_execute", "rka_describe", "rka_load_tools", "rka_help"}


def _surface(**flags: str) -> dict:
    """Import rka.mcp.server in a fresh interpreter with the given env flags
    and report the visible tool surface + skills-category tiers."""
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("RKA_LEGACY_TOOLS", "RKA_SKILL_TOOLS")
    }
    env.update(flags)
    out = subprocess.run(
        [sys.executable, "-c", _SURFACE_SNIPPET],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(out.stdout)


@pytest.mark.parametrize("flag", [None, "0", "false"])
def test_package_skill_reads_are_visible_at_startup(flag: str | None) -> None:
    """Only two package-only reads are added; the session helper stays deferred."""
    result = _surface(**({} if flag is None else {"RKA_SKILL_TOOLS": flag}))
    assert set(result["visible"]) == _DISPATCH_SURFACE | _SKILL_READS
    for name in _SKILL_READS:
        assert result["tiers"][name] == "always_on"
    assert result["tiers"]["rka_start_session"] == "deferred"
    assert result["advertised"] is False


def test_skill_adapter_tools_always_on_with_skill_flag() -> None:
    """The existing compatibility flag additionally exposes the session helper."""
    result = _surface(RKA_SKILL_TOOLS="1")
    visible = set(result["visible"])
    for name in _SKILL_TOOLS:
        assert result["tiers"][name] == "always_on"
        assert name in visible
    # Dispatch surface intact alongside the skill adapters: 5 + 3 = 8.
    assert _DISPATCH_SURFACE <= visible
    assert len(visible) == 8
    # Server instructions tell ChatGPT to call rka_start_session first.
    assert result["advertised"] is True


@pytest.mark.parametrize("flag, count", [("0", 24), ("1", 25)])
def test_legacy_mode_preserves_skill_helper_opt_in(flag: str, count: int) -> None:
    result = _surface(RKA_LEGACY_TOOLS="1", RKA_SKILL_TOOLS=flag)
    assert len(result["visible"]) == count
    assert _SKILL_READS <= set(result["visible"])
    assert ("rka_start_session" in result["visible"]) == (flag == "1")


async def test_list_skills_returns_packaged_role_guides() -> None:
    payload = json.loads(await server.rka_list_skills())
    names = {entry["name"] for entry in payload["skills"]}
    assert names == {"brain", "executor", "pi", "mcp-credentials"}
    assert "writer" not in names


def test_writer_is_not_a_valid_core_session_role() -> None:
    literal = get_args(server.SkillNameLiteral)[0]
    assert set(get_args(literal)) == {"brain", "executor", "pi", "mcp-credentials"}
    assert "writer" not in server._SKILL_ENTRYPOINTS


async def test_read_skill_returns_markdown_for_pi_role() -> None:
    text = await server.rka_read_skill("pi")
    assert "# Source: rka/skills/pi/SKILL.md" in text
    assert "# PI Skill" in text


async def test_read_skill_rejects_path_traversal() -> None:
    payload = json.loads(await server.rka_read_skill("brain", "../pi/SKILL.md"))
    assert payload["error"] == "skill_file_unavailable"


async def test_startup_reads_have_readonly_annotations() -> None:
    descriptors = {t.name: t for t in await server.mcp.list_tools()}
    for name in _SKILL_READS:
        t = descriptors[name]
        assert t.annotations.readOnlyHint is True
        assert t.annotations.destructiveHint is False
        assert t.annotations.idempotentHint is True
        assert t.annotations.openWorldHint is False
        assert t.meta["anthropic/alwaysLoad"] is True


async def test_package_reads_never_contact_api_or_fire_project_hooks(monkeypatch) -> None:
    def no_client(*args, **kwargs):
        pytest.fail("packaged skill read attempted an API call")

    monkeypatch.setattr(server, "_client", no_client)
    before = set(server._session.session_start_fired_for)
    assert json.loads(await server.rka_list_skills())["skills"]
    for role in ("brain", "executor", "pi", "mcp-credentials"):
        assert "# Source: rka/skills/" in await server.rka_read_skill(role)
    assert "Mission completion" in await server.rka_read_skill(
        "brain", "templates/stop-pivot-note.md"
    )
    assert server._session.session_start_fired_for == before


@pytest.mark.parametrize("reference", ["/etc/passwd", "../pi/SKILL.md", "._SKILL.md"])
async def test_startup_read_rejects_unsafe_references(reference: str) -> None:
    result = json.loads(await server.rka_read_skill("brain", reference))
    assert result["error"] == "skill_file_unavailable"


async def test_startup_read_rejects_symlink_escape(tmp_path, monkeypatch) -> None:
    root = tmp_path / "skills"
    (root / "brain").mkdir(parents=True)
    outside = tmp_path / "private.txt"
    outside.write_text("must not be returned")
    try:
        (root / "brain" / "outside.md").symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation unavailable")
    monkeypatch.setattr(server, "_SKILLS_DIR", root)
    result = await server.rka_read_skill("brain", "outside.md")
    assert json.loads(result)["error"] == "skill_file_unavailable"
    assert "must not be returned" not in result


async def test_start_session_includes_checklist() -> None:
    text = await server.rka_start_session("pi", project_id="prj_test")
    assert "# PI Skill" in text
    assert "# ChatGPT Connector Session Checklist" in text
    assert '"project_id": "prj_test"' in text
