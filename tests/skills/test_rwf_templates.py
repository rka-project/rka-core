"""Offline contract for the RWF v0 templates (Gap Note, Hypothesis & Test,
Stop / Pivot Note) and the role-skill text that points at them.

Filesystem only: no database, no REST or MCP client, no research data. These
checks lock down the shape a later session relies on when it recovers a
direction from RKA records:

1. the three templates and their README ship in both skill trees, byte for
   byte;
2. every template declares name, version and kind, and its required header
   names the same version;
3. the Stop / Pivot template keeps the four stop-reason categories apart and
   carries a reopen condition; the Hypothesis & Test template pre-registers
   Support, Refute and Inconclusive; the Gap Note bounds its claim and records
   search coverage;
4. templates save through stable operations only, never the preview surface;
5. each role skill points at the templates, the Executor procedure documents
   the three-read recovery and the `journal` tags-filter caveat, and the three
   role skills move version together;
6. the synthetic examples use obviously fake ids, so no real research record
   is quoted into packaged guidance.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILLS = REPO_ROOT / "rka" / "skills"
PLUGIN_SKILLS = REPO_ROOT / "plugin" / "skills"
TEMPLATES = SKILLS / "brain" / "templates"
ROLES = ("brain", "executor", "pi")

TEMPLATE_KINDS = {
    "gap-note.md": "gap_note",
    "hypothesis-test.md": "hypothesis_test",
    "stop-pivot-note.md": "stop_pivot_note",
}
HEADER_FIELDS = ("template:", "project:", "author_role:", "status:", "sources:", "review_anchor:")
STOP_REASONS = (
    "counterevidence",
    "implementation_failure",
    "resource_priority",
    "insufficient_evidence",
)
PREVIEW_OPERATIONS = {
    "create_experiment",
    "append_experiment_plan",
    "transition_experiment",
    "create_experiment_run",
    "transition_experiment_run",
    "record_experiment_observation",
    "add_evidence_locator",
    "experiments",
    "experiment_runs",
    "experiment_observations",
    "set_claim_scope",
    "claim_scope",
    "create_interpretation_candidate",
    "triage_interpretation_candidate",
    "admit_source_interpretation",
    "add_interpretation_hint",
    "interpretation_candidates",
    "register_source",
    "sources",
}
SEMVER = re.compile(r"^\d+\.\d+\.\d+$")
REAL_ID = re.compile(r"\b(?:jrn|dec|lit|mis|prj|clm|ecl|chk)_01[0-9A-HJKMNP-TV-Z]{20,}\b")
OPERATION_CITATION = re.compile(r'"operation"\s*:\s*"([a-z_]+)"')


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _frontmatter(text: str) -> dict[str, str]:
    assert text.startswith("---\n"), "file must start with a frontmatter block"
    body = text.split("---", 2)[1]
    out: dict[str, str] = {}
    for line in body.splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            out[key.strip()] = value.strip()
    return out


@pytest.mark.parametrize("name", ["README.md", *TEMPLATE_KINDS])
def test_template_ships_identically_in_both_skill_trees(name: str) -> None:
    packaged = TEMPLATES / name
    mirrored = PLUGIN_SKILLS / "brain" / "templates" / name
    assert packaged.is_file(), f"missing packaged template {packaged}"
    assert mirrored.is_file(), (
        f"{name} is not mirrored into plugin/skills; run the rsync in "
        "tests/test_skills_packaging.py"
    )
    assert packaged.read_bytes() == mirrored.read_bytes(), f"{name} differs between trees"


@pytest.mark.parametrize(("name", "kind"), sorted(TEMPLATE_KINDS.items()))
def test_template_frontmatter_and_header_agree_on_version(name: str, kind: str) -> None:
    text = _read(TEMPLATES / name)
    fm = _frontmatter(text)
    assert fm.get("name", "").startswith("rwf-"), fm
    assert fm.get("description"), "template needs a description for skill discovery"
    assert SEMVER.match(fm.get("version", "")), f"version must be semver, got {fm.get('version')!r}"
    assert fm.get("template_kind") == kind
    stem = name.removesuffix(".md")
    assert f"template: {stem}@{fm['version']}" in text, (
        "the required header must name the template and the frontmatter version"
    )
    for field in HEADER_FIELDS:
        assert field in text, f"{name} does not document header field {field!r}"


def test_readme_indexes_every_template() -> None:
    readme = _read(TEMPLATES / "README.md")
    assert "Template set version:" in readme
    for name in TEMPLATE_KINDS:
        assert name in readme, f"README does not list {name}"
    for field in HEADER_FIELDS:
        assert field in readme, f"README shared header lacks {field!r}"
    for tag in ("rwf-gap-note", "rwf-hypothesis-test", "rwf-stop-pivot", "rwf-v0"):
        assert tag in readme, f"README does not define tag {tag}"


def test_stop_pivot_separates_four_reasons_and_requires_reopen_condition() -> None:
    text = _read(TEMPLATES / "stop-pivot-note.md")
    for reason in STOP_REASONS:
        assert f"`{reason}`" in text, f"stop reason {reason} is not documented"
    assert "stop_reason:" in text
    assert "Reopen condition" in text
    assert "Pivot target" in text
    assert "retracted" in text, "the template must say negative observations are not retracted"


def test_hypothesis_test_preregisters_three_outcomes() -> None:
    text = _read(TEMPLATES / "hypothesis-test.md")
    assert "Pre-registered criteria" in text
    for outcome in ("Support", "Refute", "Inconclusive"):
        assert f"*{outcome}*" in text, f"criteria section must define {outcome}"
    assert "Scope conditions" in text
    assert "Revision log" in text
    assert "acceptance_criteria" in text, "the mission must quote the criteria"


def test_gap_note_bounds_its_claim_and_records_coverage() -> None:
    text = _read(TEMPLATES / "gap-note.md")
    assert "Search coverage" in text
    assert "Bounded gap statement" in text
    assert "Counterexamples" in text
    assert "Not found is not nobody did it" in text


def test_templates_save_through_stable_operations_only() -> None:
    from rka.mcp.operations_schema import OPERATIONS_SCHEMA

    for path in TEMPLATES.glob("*.md"):
        cited = set(OPERATION_CITATION.findall(_read(path)))
        unknown = cited - set(OPERATIONS_SCHEMA)
        assert not unknown, f"{path.name} cites unknown operations {sorted(unknown)}"
        preview = cited & PREVIEW_OPERATIONS
        assert not preview, f"{path.name} depends on preview operations {sorted(preview)}"


@pytest.mark.parametrize("role", ROLES)
def test_role_skill_points_at_templates(role: str) -> None:
    text = _read(SKILLS / role / "SKILL.md")
    assert "## Evidence-led research workflow (RWF v0)" in text, (
        f"{role}/SKILL.md lacks the RWF v0 section"
    )
    assert "templates/README.md" in text, f"{role}/SKILL.md does not point at the templates"


def test_executor_procedure_documents_recovery_and_tag_filter_caveat() -> None:
    text = _read(SKILLS / "executor" / "workflows.md")
    section = text.split("## Evidence-led research workflow (RWF v0)", 1)[1]
    for needle in (
        '"operation": "mission_guard"',
        '"operation": "decision_tree"',
        '"active_only": False',
        "rwf-stop-pivot",
        "filters.tags",
        "superseded_by",
        "note_write_receipt",
    ):
        assert needle in section, f"executor RWF procedure lacks {needle!r}"


def test_brain_procedure_maps_templates_to_existing_entities() -> None:
    text = _read(SKILLS / "brain" / "workflows.md")
    section = text.split("## Evidence-led research workflow (RWF v0)", 1)[1]
    for needle in (
        'record_decision(kind="research_question")',
        "assumptions=[H1.., scope]",
        "acceptance_criteria=<criteria verbatim>",
        "update_decision(status, abandonment_reason)",
        "supersede_decision",
    ):
        assert needle in section, f"brain RWF mapping lacks {needle!r}"


def test_role_skill_versions_move_together() -> None:
    versions: dict[str, str] = {}
    for role in ROLES:
        text = _read(SKILLS / role / "SKILL.md")
        fm = _frontmatter(text)
        assert SEMVER.match(fm.get("version", "")), f"{role}: bad frontmatter version"
        assert f"**Skill version: {fm['version']} " in text, (
            f"{role}: body version line disagrees with frontmatter {fm['version']}"
        )
        versions[role] = fm["version"]
    assert len(set(versions.values())) == 1, f"role skills drifted apart: {versions}"


def test_synthetic_examples_quote_no_real_record_ids() -> None:
    for path in TEMPLATES.glob("*.md"):
        leaked = REAL_ID.findall(_read(path))
        assert not leaked, f"{path.name} quotes what looks like real record ids: {leaked}"
