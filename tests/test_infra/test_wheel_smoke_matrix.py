"""Release gate coverage for the portable wheel artifact smoke."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_wheel_smoke_covers_supported_os_and_python_bounds() -> None:
    workflow = (ROOT / ".github/workflows/pytest.yml").read_text(encoding="utf-8")

    assert "os: [ubuntu-latest, windows-latest, macos-latest]" in workflow
    assert 'python-version: ["3.11", "3.13"]' in workflow
    assert "runs-on: ${{ matrix.os }}" in workflow
    assert "python scripts/wheel_artifact_smoke.py" in workflow
    assert "/tmp/rka-wheel-smoke" not in workflow


def test_wheel_smoke_uses_platform_specific_venv_interpreters() -> None:
    script = (ROOT / "scripts/wheel_artifact_smoke.py").read_text(encoding="utf-8")

    assert 'environment / "Scripts" / "python.exe"' in script
    assert 'environment / "bin" / "python"' in script
    assert 'glob("rka_core-*.whl")' in script


def test_wheel_smoke_does_not_mask_intermediate_command_failures() -> None:
    workflow = (ROOT / ".github/workflows/pytest.yml").read_text(encoding="utf-8")
    job = workflow.split("  wheel-smoke:\n", 1)[1].split("\n  docker-core:", 1)[0]

    assert "    defaults:\n      run:\n        shell: bash\n" in job
    assert "continue-on-error:" not in job
    assert job.count("shell:") == 1


def test_core_ci_actions_are_pinned_to_immutable_commits() -> None:
    workflow = (ROOT / ".github/workflows/pytest.yml").read_text(encoding="utf-8")
    revisions = re.findall(r"uses:\s+[^@\s]+@([0-9a-f]{40})(?:\s|$)", workflow)

    assert len(revisions) == workflow.count("uses:")
