"""Real stdio discovery, with no live backend or mutable research data."""

from __future__ import annotations

import json
import os
import sys
from datetime import timedelta
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def test_cold_client_reads_skills_without_dynamic_registration(tmp_path):
    root = Path(__file__).resolve().parents[2]
    env = {
        **os.environ,
        "PYTHONPATH": str(root),
        "PYTHONDONTWRITEBYTECODE": "1",
        "RKA_API_URL": "http://127.0.0.1:9",  # no production API, even on failure
        "RKA_DATA_DIR": str(tmp_path / "unused-data"),
        "RKA_LEGACY_TOOLS": "0",
        "RKA_SKILL_TOOLS": "0",
    }
    params = StdioServerParameters(
        command=sys.executable,
        args=["-c", "from rka.mcp.server import mcp; mcp.run(transport='stdio')"],
        cwd=str(tmp_path),
        env=env,
    )
    with (tmp_path / "mcp.stderr").open("w") as err:
        async with stdio_client(params, errlog=err) as (read, write), ClientSession(
            read, write, read_timeout_seconds=timedelta(seconds=20)
        ) as session:
            initialized = await session.initialize()
            assert initialized.capabilities.tools.listChanged is True
            listed = {tool.name: tool for tool in (await session.list_tools()).tools}
            original = {
                "rka_query", "rka_execute", "rka_describe", "rka_load_tools", "rka_help"
            }
            added = {"rka_list_skills", "rka_read_skill"}
            assert set(listed) == original | added
            for name in added:
                assert listed[name].annotations.readOnlyHint is True
                assert listed[name].annotations.destructiveHint is False
                assert listed[name].annotations.openWorldHint is False
            # Neither a new write tool nor the project-hook helper is advertised.
            assert "rka_start_session" not in listed
            assert "rka_add_note" not in listed
            assert listed["rka_execute"].annotations is None

            inventory = await session.call_tool("rka_list_skills", {})
            assert not inventory.isError
            value = json.loads(inventory.content[0].text)
            assert {s["name"] for s in value["skills"]} == {
                "brain", "executor", "pi", "mcp-credentials"
            }
            for name, reference in [
                ("executor", None),
                ("brain", "templates/stop-pivot-note.md"),
                ("brain", "templates/gap-note.md"),
            ]:
                args = {"name": name}
                if reference:
                    args["reference"] = reference
                result = await session.call_tool("rka_read_skill", args)
                assert not result.isError
                expected = (root / "rka/skills" / name / (reference or "SKILL.md"))
                assert result.content[0].text.endswith(expected.read_text())

            # An old caller's unnecessary load remains an idempotent no-op.
            repeat = await session.call_tool("rka_load_tools", {"names": sorted(added)})
            payload = json.loads(repeat.content[0].text)
            assert payload["loaded"] == []
            assert set(payload["already_active"]) == added
    assert not (tmp_path / "unused-data").exists()
