"""Security gate rejects unknown coverage, not only known advisories."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.verify_dependency_audit import audit_failures


def clean():
    return {"dependencies": [{"name": "example", "version": "1.0", "vulns": []}], "fixes": []}


def test_clean_report():
    assert audit_failures(clean()) == []


@pytest.mark.parametrize("report", [
    None, [], {}, {"dependencies": []}, {"dependencies": "bad"},
    {"dependencies": [None]}, {"dependencies": [{}]},
    {"dependencies": [{"name": "example", "version": "1"}]},
    {"dependencies": [{"name": "example", "version": "1", "vulns": None}]},
    {"dependencies": [{"name": "example", "vulns": []}]},
    {"dependencies": [{"name": "example", "skip_reason": "unavailable"}]},
])
def test_incomplete_or_skipped_report_fails(report):
    assert audit_failures(report)


def test_vulnerabilities_fail_without_a_direct_transitive_exemption():
    report = clean()
    report["dependencies"][0]["vulns"] = [{"id": "GHSA-synthetic"}]
    assert audit_failures(report) == ["example 1.0: 1 vulnerability reports"]


@pytest.mark.parametrize("valid", [True, False])
def test_cli_exit_status(tmp_path, valid):
    path = tmp_path / "audit.json"
    path.write_text(json.dumps(clean()) if valid else "{invalid", encoding="utf-8")
    script = Path(__file__).resolve().parents[1] / "scripts" / "verify_dependency_audit.py"
    result = subprocess.run([sys.executable, str(script), str(path)], capture_output=True, text=True)
    assert result.returncode == (0 if valid else 2)
