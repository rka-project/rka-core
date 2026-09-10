"""`rka cred ...` subcommand group.

Phase 1 commands:
    rka cred init                  — interactive bootstrap of vault + creds.env
    rka cred set KEY VALUE         — set a key (idempotent, order-preserving)
    rka cred unset KEY             — remove a key (idempotent)
    rka cred get KEY [--show]      — print '***' by default; --show to unmask
    rka cred env [--format=...]    — emit resolved env (dotenv|json|shell)
    rka cred propagate [--apply]   — dry-run by default; --apply to write
    rka cred check                 — drift detection across all consumers
"""

from __future__ import annotations

import json
import sys

import click

from rka.cli_cred.manifest import (
    Manifest,
    load_manifest,
    load_versions,
    write_default_manifest,
    write_default_versions,
)
from rka.cli_cred.probes import (
    PROBE_FAIL,
    PROBE_PASS,
    PROBE_SKIP,
    ProbeResult,
    run_all_probes,
)
from rka.cli_cred.propagators import all_propagators
from rka.cli_cred.vault import (
    Dotenv,
    creds_path,
    ensure_vault_dir,
    file_mode,
    load_creds,
    save_creds,
    vault_root,
)


@click.group()
def cred():
    """Local-first credential vault (Phase 1 — global creds only).

    Vault location: $XDG_CONFIG_HOME/rka (fallback ~/.config/rka).
    Never lives inside any git repo. File mode 0600 enforced.
    """
    pass


# ----------------------------------------------------------------------
# init
# ----------------------------------------------------------------------


@cred.command("init")
@click.option(
    "--non-interactive",
    is_flag=True,
    help="Skip prompts; only ensures the vault dir + default manifest/versions exist.",
)
def cred_init(non_interactive: bool):
    """Interactive bootstrap — prompts for every key in manifest.global.required + optional.

    Idempotent: existing creds.env values are PRE-FILLED as defaults; manifest.toml
    and versions.toml are only written if missing.
    """
    root = ensure_vault_dir()
    click.echo(f"Vault root: {root}")

    # Write manifest + versions defaults (skip if exist).
    manifest_p = write_default_manifest()
    versions_p = write_default_versions()
    click.echo(f"  manifest: {manifest_p}")
    click.echo(f"  versions: {versions_p}")

    manifest = load_manifest()
    dot = load_creds()
    cpath = creds_path()

    if non_interactive:
        # Just ensure creds.env file exists (empty if first run).
        if not cpath.exists():
            save_creds(dot)
            click.echo(f"  creds.env created at {cpath} (empty)")
        else:
            click.echo(f"  creds.env exists at {cpath}")
        click.echo("Done (non-interactive).")
        return

    click.echo("")
    click.echo("Required credentials:")
    for key in manifest.global_required:
        current = dot.get(key) or ""
        prompt = f"  {key}"
        if current:
            prompt += " [current set; press Enter to keep]"
        value = click.prompt(prompt, default=current, show_default=False, hide_input=True)
        if value:
            dot.set(key, value)

    click.echo("")
    click.echo("Optional credentials (press Enter to skip):")
    for key in manifest.global_optional:
        current = dot.get(key) or ""
        prompt = f"  {key}"
        if current:
            prompt += " [current set; press Enter to keep]"
        value = click.prompt(prompt, default=current, show_default=False, hide_input=True)
        if value:
            dot.set(key, value)

    save_creds(dot)
    mode = file_mode(cpath)
    click.echo("")
    click.echo(f"Wrote {cpath} (mode {oct(mode)})")
    click.echo("Next:")
    click.echo("  rka cred propagate            # dry-run; show what would change")
    click.echo("  rka cred propagate --apply    # write to all consumers")
    click.echo("  rka cred check                # confirm consumers are in sync")


# ----------------------------------------------------------------------
# set / unset / get
# ----------------------------------------------------------------------


@cred.command("set")
@click.argument("key")
@click.argument("value")
def cred_set(key: str, value: str):
    """Set KEY=VALUE in creds.env. Preserves order + comments."""
    dot = load_creds()
    dot.set(key, value)
    save_creds(dot)
    click.echo(f"set {key} (mode {oct(file_mode(creds_path()))})")


@cred.command("unset")
@click.argument("key")
def cred_unset(key: str):
    """Remove KEY from creds.env. Idempotent."""
    dot = load_creds()
    removed = dot.unset(key)
    save_creds(dot)
    if removed:
        click.echo(f"unset {key}")
    else:
        click.echo(f"{key} was not set (no-op)")


@cred.command("get")
@click.argument("key")
@click.option("--show", is_flag=True, help="Print the real value (default: ***)")
def cred_get(key: str, show: bool):
    """Print the value of KEY (masked by default). Exit 1 if missing."""
    dot = load_creds()
    value = dot.get(key)
    if value is None:
        click.echo(f"{key} not set", err=True)
        sys.exit(1)
    if show:
        click.echo(value)
    else:
        click.echo("***")


# ----------------------------------------------------------------------
# env
# ----------------------------------------------------------------------


@cred.command("env")
@click.argument("project", required=False)
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["dotenv", "json", "shell"], case_sensitive=False),
    default="dotenv",
    show_default=True,
)
def cred_env(project: str | None, fmt: str):
    """Emit the resolved env. Phase 1: just global creds.env.

    PROJECT argument is reserved for Phase 2 (per-project addons).
    """
    if project:
        click.echo(
            f"# note: per-project resolution is Phase 2; emitting global creds only",
            err=True,
        )

    dot = load_creds()
    data = dot.to_dict()

    fmt_lower = fmt.lower()
    if fmt_lower == "json":
        click.echo(json.dumps(data, indent=2, sort_keys=True))
        return
    if fmt_lower == "shell":
        for key, value in data.items():
            # Single-quote + escape inner single quotes for safe shell sourcing.
            escaped = value.replace("'", "'\\''")
            click.echo(f"export {key}='{escaped}'")
        return
    # dotenv: re-render via the parsed object so comments are preserved.
    click.echo(dot.render(), nl=False)


# ----------------------------------------------------------------------
# propagate
# ----------------------------------------------------------------------


@cred.command("propagate")
@click.option("--apply", is_flag=True, help="Actually write (default: dry-run)")
@click.option(
    "--api-url",
    default="http://127.0.0.1:9712",
    show_default=True,
    help="rka-server API URL",
)
def cred_propagate(apply: bool, api_url: str):
    """Propagate creds.env to all consumers. Dry-run by default.

    Consumers (Phase 1):
      - claude_desktop          (~/Library/Application Support/Claude/...)
      - claude_code_json        (~/.claude.json)
      - rka_server_rest         (PUT /api/config/zotero)
      - orchestrator_env_file   (orchestrator/.env, ANTHROPIC_API_KEY excluded)
    """
    dot = load_creds()
    creds = dot.to_dict()

    if apply:
        click.echo("propagate: APPLY mode (writing changes)")
    else:
        click.echo("propagate: DRY-RUN (no writes) — pass --apply to commit")
    click.echo("")

    any_changes = False
    rebuild_hints: list[str] = []
    for name, prop in all_propagators():
        if name == "rka_server_rest":
            result = prop(creds, apply=apply, api_url=api_url)
        else:
            result = prop(creds, apply=apply)

        status_marker = {
            "unchanged": "  ",
            "would_change": "* ",
            "applied": "+ ",
            "skipped": "- ",
            "error": "! ",
        }.get(result.status, "? ")
        click.echo(f"{status_marker}{result.consumer:24}  {result.status:13}  {result.summary}")
        if result.changes:
            for key, (old, new) in result.changes.items():
                click.echo(f"        {key}: {old} -> {new}")
        if result.status in ("would_change", "applied"):
            any_changes = True
        if result.needs_rebuild and result.rebuild_hint:
            rebuild_hints.append(f"  {result.consumer}: {result.rebuild_hint}")

    if rebuild_hints:
        click.echo("")
        click.echo("Manual rebuild needed:")
        for hint in rebuild_hints:
            click.echo(hint)
    elif not any_changes:
        click.echo("")
        click.echo("All consumers already in sync.")


# ----------------------------------------------------------------------
# check
# ----------------------------------------------------------------------


@cred.command("check")
@click.argument("project", required=False)
@click.option(
    "--api-url",
    default="http://127.0.0.1:9712",
    show_default=True,
    help="rka-server API URL",
)
def cred_check(project: str | None, api_url: str):
    """Run all drift probes; exit 1 if any FAIL detected.

    PROJECT argument is reserved for Phase 2.
    """
    if project:
        click.echo(
            f"# note: per-project drift is Phase 2; running global probes",
            err=True,
        )

    manifest = load_manifest()
    versions = load_versions()
    dot = load_creds()
    creds = dot.to_dict()

    results = run_all_probes(manifest, versions, creds, api_url=api_url)
    _render_probe_table(results)

    fail_count = sum(1 for r in results if r.status == PROBE_FAIL)
    pass_count = sum(1 for r in results if r.status == PROBE_PASS)
    skip_count = sum(1 for r in results if r.status == PROBE_SKIP)
    click.echo("")
    click.echo(f"summary: {pass_count} pass, {fail_count} fail, {skip_count} skip")
    if fail_count > 0:
        sys.exit(1)


def _render_probe_table(results: list[ProbeResult]) -> None:
    """Plain-ASCII table; portable across terminals."""
    name_w = max(4, max(len(r.name) for r in results))
    status_w = 6
    headers = ("PROBE", "STATUS", "EXPECTED", "FOUND")
    click.echo(
        f"{headers[0]:<{name_w}}  {headers[1]:<{status_w}}  {headers[2]:<30}  {headers[3]}"
    )
    click.echo("-" * (name_w + 2 + status_w + 2 + 30 + 2 + 30))
    for r in results:
        expected = (r.expected or "")[:30]
        found = (r.found or "")[:60]
        click.echo(f"{r.name:<{name_w}}  {r.status:<{status_w}}  {expected:<30}  {found}")
        if r.hint and r.status == PROBE_FAIL:
            click.echo(f"{'':<{name_w}}  {'':<{status_w}}  hint: {r.hint}")
