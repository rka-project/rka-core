#!/usr/bin/env python3
"""Fail closed when pip-audit evidence is incomplete, skipped or vulnerable."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def audit_failures(report: object) -> list[str]:
    if not isinstance(report, dict):
        return ["audit report must be a JSON object"]
    dependencies = report.get("dependencies")
    if not isinstance(dependencies, list) or not dependencies:
        return ["audit report must contain a non-empty dependency list"]
    failures = []
    for index, dependency in enumerate(dependencies):
        if not isinstance(dependency, dict):
            failures.append(f"dependency {index}: invalid record")
            continue
        name = dependency.get("name")
        if not isinstance(name, str) or not name.strip():
            failures.append(f"dependency {index}: missing package name")
            continue
        if "skip_reason" in dependency:
            failures.append(f"{name}: audit skipped ({dependency['skip_reason']})")
            continue
        version = dependency.get("version")
        vulnerabilities = dependency.get("vulns")
        if not isinstance(version, str) or not version.strip() or not isinstance(vulnerabilities, list):
            failures.append(f"{name}: incomplete audit evidence")
        elif vulnerabilities:
            failures.append(f"{name} {version}: {len(vulnerabilities)} vulnerability reports")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    args = parser.parse_args()
    try:
        report = json.loads(args.report.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        print(f"Audit evidence unavailable or invalid: {exc}", file=sys.stderr)
        return 2
    failures = audit_failures(report)
    if failures:
        print("\n".join(failures), file=sys.stderr)
        return 1
    print(f"Verified {len(report['dependencies'])} audited dependencies: no skips or known vulnerabilities")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
