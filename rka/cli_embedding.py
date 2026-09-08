"""CLI-only, explicitly scoped, read-only embedding maintenance previews."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import click

from rka.services.embedding_inspection import EmbeddingInspectionError, inspect_embedding_index


@click.group("embedding")
def embedding_admin():
    """Inspect, preview, or perform backed-up offline index recovery."""


def _options(function):
    function = click.option("--json", "json_output", is_flag=True, help="Emit a machine-readable report.")(function)
    function = click.option("--max-rows", type=click.IntRange(1, 100000), default=50000, show_default=True)(function)
    function = click.option("--timeout", "timeout_seconds", type=click.IntRange(1, 60), default=30, show_default=True)(function)
    function = click.option("--db", "db_path", type=click.Path(path_type=Path), help="Database override; default is DATA_DIR/rka.db.")(function)
    return click.option("--data-dir", type=click.Path(path_type=Path), required=True,
                        help="Directory containing the persisted embedding_config.json; no .env/default fallback.")(function)


def _run(*, data_dir, db_path, json_output, max_rows, timeout_seconds, target_config=None, dry_run=False):
    try:
        report = asyncio.run(inspect_embedding_index(
            data_dir=data_dir.expanduser(), db_path=db_path, target_config=target_config,
            dry_run=dry_run, max_rows=max_rows, timeout_seconds=timeout_seconds,
        ))
    except EmbeddingInspectionError as exc:
        if json_output:
            click.echo(json.dumps({"version": 1, "read_only": True, "error": str(exc)}))
            raise click.exceptions.Exit(2) from None
        raise click.ClickException(str(exc)) from None
    if json_output:
        click.echo(json.dumps(report, indent=2, ensure_ascii=True))
    else:
        click.echo(f"Global embedding index: {report['assessment']['status']}")
        click.echo(f"Source rows: {report['totals']['source_rows']}; hash-verified: {report['totals']['hash_verified']}; reusable pairs: {report['totals']['reusable']}")
        if dry_run:
            click.echo(f"Advisory action: {report['plan']['action']}")
        click.echo("Read-only inspection; no provider probe, configuration change or work queued.")
        click.echo("Maintenance ownership not acquired; use the explicit offline recovery commands to execute.")
    if not report["assessment"]["complete"]:
        raise click.exceptions.Exit(2)


@embedding_admin.command("inspect")
@_options
def inspect_command(**kwargs):
    """Inspect current persisted identity, physical tables, coverage and hashes."""
    _run(**kwargs)


@embedding_admin.command("dry-run")
@_options
@click.option("--target-config", type=click.Path(path_type=Path), help="Optional candidate configuration; read only, never saved.")
def dry_run_command(**kwargs):
    """Preview global recovery impact; this command cannot execute a rebuild."""
    _run(**kwargs, dry_run=True)


def _recovery_options(function):
    function = click.option("--peers-stopped", is_flag=True, required=True,
                            help="Confirm ALL API/worker/CLI and old or unrelated database clients are stopped; shared lock-capable storage required.")(function)
    function = click.option("--json", "json_output", is_flag=True)(function)
    function = click.option("--db", "db_path", type=click.Path(path_type=Path))(function)
    return click.option("--data-dir", type=click.Path(path_type=Path), required=True)(function)


def _recover(operation, json_output=False, **kwargs):
    from rka.services.embedding_recovery import recover_embedding_index, EmbeddingRecoveryError
    from rka.infra.runtime_lease import MaintenanceBusy
    try:
        result = asyncio.run(recover_embedding_index(operation=operation, **kwargs))
    except (EmbeddingRecoveryError, EmbeddingInspectionError, MaintenanceBusy) as exc:
        raise click.ClickException(str(exc)) from None
    except Exception:
        raise click.ClickException("offline_recovery_failed; preserve backups and use resume or rollback") from None
    if json_output:
        click.echo(json.dumps(result, indent=2))
    else:
        click.echo(f"Global recovery: {result['status']}; backup: {result['backup_directory']}")
        if result["status"] == "prepared":
            click.echo("Offline transition complete. Restart Core services to run/resume the durable backfill; this is not a ready-index claim.")


@embedding_admin.command("rebuild")
@_recovery_options
@click.option("--target-config", type=click.Path(path_type=Path), help="Validated candidate config for a global index transition.")
def rebuild_command(**kwargs):
    """Back up DB/config, transition offline, and queue the Core worker."""
    _recover("rebuild", **kwargs)


@embedding_admin.command("resume")
@_recovery_options
def resume_command(**kwargs):
    """Resume an interrupted offline transition, without erasing completed work."""
    _recover("resume", **kwargs)


@embedding_admin.command("rollback")
@_recovery_options
def rollback_command(**kwargs):
    """Restore an UNFINISHED offline transition; never undo later research writes."""
    _recover("rollback", **kwargs)
