"""manifest.toml + versions.toml read + write.

For READ: use the stdlib `tomllib` (Python 3.11+).
For WRITE: we hand-roll a writer that only handles the trivial shapes
we emit (flat sections, key=string, key=array-of-strings). This
intentionally avoids adding `tomli-w` as a runtime dep.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from rka import __version__
from rka.cli_cred.vault import atomic_write_text, manifest_path, versions_path


# ----------------------------------------------------------------------
# Defaults
# ----------------------------------------------------------------------


DEFAULT_MANIFEST: dict[str, dict[str, list[str]]] = {
    "global": {
        "required": [],
        "optional": [
            "ZOTERO_API_KEY",
            "ZOTERO_LIBRARY_ID",
            "ZOTERO_LIBRARY_TYPE",
            "SEMANTIC_SCHOLAR_API_KEY",
            "SERPAPI_KEY",
        ],
    },
}


DEFAULT_VERSIONS: dict[str, dict[str, str]] = {
    "host.binaries": {
        "rka": __version__,
    },
    "containers": {
        "rka-server": __version__,
    },
}


# ----------------------------------------------------------------------
# Manifest read/write
# ----------------------------------------------------------------------


@dataclass
class Manifest:
    """Parsed manifest.toml."""

    global_required: list[str] = field(default_factory=list)
    global_optional: list[str] = field(default_factory=list)
    # Phase 2 will populate this from [projects.<slug>] sections.
    projects: dict[str, dict[str, list[str]]] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict) -> Manifest:
        global_section = data.get("global", {}) or {}
        return cls(
            global_required=list(global_section.get("required", []) or []),
            global_optional=list(global_section.get("optional", []) or []),
            projects=dict(data.get("projects", {}) or {}),
        )


def load_manifest(env: dict[str, str] | None = None) -> Manifest:
    path = manifest_path(env)
    if not path.exists():
        return Manifest.from_dict(DEFAULT_MANIFEST)
    data = tomllib.loads(path.read_text())
    return Manifest.from_dict(data)


def render_manifest(data: dict) -> str:
    """Render a trivial-shape manifest dict to TOML text.

    Supports only:
    - top-level sections (dict of dicts)
    - key = "string"
    - key = ["a", "b"]
    """
    out: list[str] = [
        "# RKA Cred Vault manifest — declarative requirements per project.",
        "# Edit this file to add a new project or change required keys.",
    ]
    for section, body in data.items():
        out.append("")
        out.append(f"[{section}]")
        for key, value in body.items():
            out.append(f"{key} = {_render_value(value)}")
    out.append("")
    out.append("# Phase 2: add [projects.<slug>] sections here when per-project")
    out.append("# addon credentials become necessary.")
    out.append("")
    return "\n".join(out)


def write_default_manifest(env: dict[str, str] | None = None, force: bool = False) -> Path:
    path = manifest_path(env)
    if path.exists() and not force:
        return path
    body = render_manifest(DEFAULT_MANIFEST)
    atomic_write_text(path, body, mode=0o600)
    return path


# ----------------------------------------------------------------------
# Versions read/write
# ----------------------------------------------------------------------


@dataclass
class Versions:
    host_binaries: dict[str, str] = field(default_factory=dict)
    containers: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict) -> Versions:
        # TOML's "host.binaries" becomes nested {"host": {"binaries": {...}}}.
        host_section = data.get("host", {}) or {}
        host_bin = host_section.get("binaries", {}) or {}
        containers = data.get("containers", {}) or {}
        return cls(
            host_binaries=dict(host_bin),
            containers=dict(containers),
        )


def load_versions(env: dict[str, str] | None = None) -> Versions:
    path = versions_path(env)
    if not path.exists():
        # Fall back to defaults so probes can still run.
        return _versions_from_defaults()
    data = tomllib.loads(path.read_text())
    return Versions.from_dict(data)


def _versions_from_defaults() -> Versions:
    return Versions(
        host_binaries=dict(DEFAULT_VERSIONS["host.binaries"]),
        containers=dict(DEFAULT_VERSIONS["containers"]),
    )


def render_versions(host_binaries: dict[str, str], containers: dict[str, str]) -> str:
    out: list[str] = [
        "# RKA Cred Vault versions — expected binary + container versions.",
        "# Compared against live versions by `rka cred check`.",
        "",
        "[host.binaries]",
    ]
    for key, value in host_binaries.items():
        out.append(f'{key} = "{value}"')
    out.append("")
    out.append("[containers]")
    for key, value in containers.items():
        # Container names like "rka-server" must be quoted.
        out.append(f'"{key}" = "{value}"')
    out.append("")
    return "\n".join(out)


def write_default_versions(
    env: dict[str, str] | None = None,
    force: bool = False,
    detected_host_binaries: dict[str, str] | None = None,
    detected_containers: dict[str, str] | None = None,
) -> Path:
    """Write versions.toml. Falls back to DEFAULT_VERSIONS if detection
    isn't supplied.
    """
    path = versions_path(env)
    if path.exists() and not force:
        return path
    host_binaries = (
        detected_host_binaries
        if detected_host_binaries is not None
        else dict(DEFAULT_VERSIONS["host.binaries"])
    )
    containers = (
        detected_containers
        if detected_containers is not None
        else dict(DEFAULT_VERSIONS["containers"])
    )
    body = render_versions(host_binaries, containers)
    atomic_write_text(path, body, mode=0o600)
    return path


# ----------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------


def _render_value(value) -> str:
    if isinstance(value, list):
        items = ", ".join(f'"{_escape_string(s)}"' for s in value)
        return f"[{items}]"
    if isinstance(value, str):
        return f'"{_escape_string(value)}"'
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _escape_string(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')
