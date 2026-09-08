"""RKA CLI — init, serve, mcp, status."""

from __future__ import annotations

import asyncio
import sqlite3
import sys
from pathlib import Path

import click

from rka import __version__


def _configure_console_output() -> None:
    """Keep CLI output alive when a legacy console cannot encode Unicode.

    UTF-8 terminals are unchanged. On streams such as a redirected Windows
    cp1252 console, unsupported characters are rendered as ASCII escape
    sequences instead of aborting the server before startup.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(errors="backslashreplace")
        except (OSError, ValueError):
            # Test runners and embedded hosts may expose a closed, detached,
            # or otherwise non-reconfigurable text stream.
            pass


@click.group()
@click.version_option(version=__version__)
def main():
    """Research Knowledge Agent — AI-assisted research orchestration."""
    _configure_console_output()


# Register the cred-vault subcommand group (Phase 1 — local-first creds).
from rka.cli_cred import cred as _cred_group  # noqa: E402

main.add_command(_cred_group)


@main.command()
@click.argument("name")
@click.option("--description", "-d", default=None, help="Project description")
@click.option("--dir", "directory", default=".", help="Project directory")
def init(name: str, description: str | None, directory: str):
    """Initialize a new RKA project."""
    from rka.infra.database import Database
    from rka.services.project import ProjectService

    project_dir = Path(directory).resolve()
    db_path = project_dir / "rka.db"

    async def _init():
        db = Database(str(db_path))
        await db.connect()
        await db.initialize_schema()

        svc = ProjectService(db)
        state = await svc.initialize(name, description)
        await db.close()
        return state

    state = asyncio.run(_init())
    click.echo(f"✅ Initialized RKA project: {state.project_name}")
    click.echo(f"   Database: {db_path}")
    click.echo(f"   Phase: {state.current_phase}")
    click.echo("\nRun 'rka serve' to start the API server.")

    # Create .env file if it doesn't exist
    env_path = project_dir / ".env"
    if not env_path.exists():
        env_path.write_text(
            f"# RKA Configuration\n"
            f"RKA_PROJECT_DIR={project_dir}\n"
            f"RKA_DB_PATH=rka.db\n"
            f"RKA_HOST=127.0.0.1\n"
            f"RKA_PORT=9712\n"
            f"\n"
            f"# LLM (Phase 2 — uncomment when ready)\n"
            f"# RKA_LLM_MODEL=<provider/model>\n"
            f"# RKA_LLM_API_BASE=<your_openai_compatible_endpoint>\n"
            f"# RKA_LLM_ENABLED=true\n"
            f"# RKA_EMBEDDINGS_ENABLED=true\n"
        )
        click.echo("   Created .env file")


@main.command()
@click.option("--host", default=None, help="Override host")
@click.option("--port", default=None, type=int, help="Override port")
@click.option("--reload", "do_reload", is_flag=True, help="Enable auto-reload for development")
def serve(host: str | None, port: int | None, do_reload: bool):
    """Start the RKA API server."""
    import uvicorn
    from rka.config import RKAConfig

    config = RKAConfig()
    h = host or config.host
    p = port or config.port

    click.echo(f"🚀 Starting RKA server at http://{h}:{p}")
    click.echo(f"   API docs: http://{h}:{p}/docs")
    click.echo(f"   Database: {config.database_url}")

    uvicorn.run(
        "rka.api.app:create_app",
        factory=True,
        host=h,
        port=p,
        reload=do_reload,
        log_level="info",
    )


@main.command()
@click.option(
    "--transport",
    type=click.Choice(["stdio", "http"], case_sensitive=False),
    default=None,
    help="Transport mode: stdio (default, for Claude Desktop / Claude Code) or http (Streamable HTTP, dev/remote access).",
)
@click.option("--host", default="127.0.0.1", help="Host for HTTP transport only.")
@click.option("--port", default=9713, type=int, help="Port for HTTP transport only. Default 9713 (avoids REST API port 9712).")
def mcp(transport: str | None, host: str, port: int):
    """Start the MCP server.

    Defaults to stdio transport (Claude Desktop spawns this as a subprocess).
    HTTP transport is opt-in via --transport http or RKA_MCP_TRANSPORT=http
    for dev, remote access, or mitmproxy-based debugging.
    """
    import os
    from rka.mcp.server import mcp as mcp_server

    # Resolve effective transport: CLI flag > env var > stdio default.
    effective = (transport or os.environ.get("RKA_MCP_TRANSPORT") or "stdio").lower()

    if effective == "http":
        mcp_server.settings.host = host
        mcp_server.settings.port = port
        click.echo(
            f"🚀 Starting MCP server on Streamable HTTP at http://{host}:{port}"
            f"{mcp_server.settings.streamable_http_path}"
        )
        mcp_server.run(transport="streamable-http")
    else:
        # stdio — the default for Claude Desktop / Claude Code subprocess integration.
        mcp_server.run()


@main.command()
@click.option("--poll-interval", default=None, type=float, help="Override worker poll interval")
@click.option("--lease-seconds", default=None, type=int, help="Override job lease duration")
@click.option("--max-attempts", default=None, type=int, help="Override max attempts per job")
@click.option("--once", is_flag=True, help="Process at most one available job and exit")
def worker(
    poll_interval: float | None,
    lease_seconds: int | None,
    max_attempts: int | None,
    once: bool,
):
    """Run the background enrichment worker."""
    from rka.config import RKAConfig
    from rka.infra.database import Database
    from rka.services.worker import EnrichmentWorker

    config = RKAConfig()

    async def _worker():
        db = Database(config.database_url)
        await db.connect()
        try:
            await db.initialize_schema()
            await db.initialize_phase2_schema()
            # v2.5.8 (mis_01KS3E4S33B13EGR2NWRQM2QG4 T2; Brain-ratified
            # exemption-extension): use EnrichmentWorker.boot() so the
            # worker reads persisted /data/embedding_config.json rather
            # than env-only. Falls back to env automatically when config
            # missing or corrupt.
            runner = EnrichmentWorker.boot(
                db=db,
                data_dir=config.data_dir,
                embeddings_enabled=config.embeddings_enabled,
                env_fallback_model=config.embedding_model,
                poll_interval=poll_interval or config.job_poll_interval,
                lease_seconds=lease_seconds or config.job_lease_seconds,
                max_attempts=max_attempts or config.job_max_attempts,
            )

            if once:
                handled = await runner.run_once()
                click.echo("Processed 1 job." if handled else "No jobs available.")
                return

            click.echo(f"Starting worker for {config.database_url}")
            await runner.run_forever()
        finally:
            await db.close()

    try:
        asyncio.run(_worker())
    except KeyboardInterrupt:
        click.echo("Worker stopped.")


@main.command()
def status():
    """Show current project status."""
    from rka.config import RKAConfig
    from rka.infra.database import Database
    from rka.services.project import ProjectService
    from rka.services.missions import MissionService
    from rka.services.checkpoints import CheckpointService

    config = RKAConfig()

    async def _status():
        db = Database(config.database_url)
        await db.connect()

        proj_svc = ProjectService(db)
        state = await proj_svc.get()
        if state is None:
            click.echo("❌ Project not initialized. Run `rka init <name>` first.")
            await db.close()
            return

        mis_svc = MissionService(db)
        active = await mis_svc.list(status="active", limit=1)

        chk_svc = CheckpointService(db)
        open_chks = await chk_svc.list(status="open")

        click.echo(f"📋 Project: {state.project_name}")
        click.echo(f"   Phase: {state.current_phase or 'not set'}")
        if state.summary:
            click.echo(f"   Summary: {state.summary[:120]}")
        if state.blockers:
            click.echo(f"   ⚠️  Blockers: {state.blockers}")

        if active:
            m = active[0]
            click.echo(f"\n▶  Active Mission: {m.id}")
            click.echo(f"   {m.objective[:100]}")

        if open_chks:
            click.echo(f"\n🔔 Open Checkpoints: {len(open_chks)}")
            for chk in open_chks[:5]:
                icon = "🔴" if chk.blocking else "🟡"
                click.echo(f"   {icon} {chk.id}: {chk.description[:80]}")

        await db.close()

    asyncio.run(_status())


@main.command()
@click.option("--output", "-o", default=None, help="Output file path")
def backup(output: str | None):
    """Create a consistent, integrity-checked database backup."""
    import sqlite3

    from rka.config import RKAConfig
    from rka.infra.sqlite_backup import backup_sqlite_database

    config = RKAConfig()
    src = Path(config.database_url)
    if not src.exists():
        click.echo("❌ No database found.")
        return

    from datetime import datetime
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dst = Path(output) if output else src.parent / f"rka_backup_{timestamp}.db"
    try:
        result = backup_sqlite_database(src, dst)
    except (OSError, sqlite3.DatabaseError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"✅ Backed up to {result.path}")
    click.echo(f"   SHA-256: {result.sha256}")
    if result.foreign_key_violations:
        click.echo(
            "⚠️  Backup preserved "
            f"{result.foreign_key_violations} pre-existing foreign-key violation(s)."
        )


@main.command("export-writer")
@click.option("--project-id", required=True, help="Exact RKA project ID to export.")
@click.option(
    "--output",
    "-o",
    type=click.Path(path_type=Path, dir_okay=False, resolve_path=True),
    required=True,
    help="Destination .rka-writer-export.zip file.",
)
def export_writer(project_id: str, output: Path):
    """Export frozen legacy Writer state through a disposable backup."""
    import sqlite3
    import tempfile

    from rka.config import RKAConfig
    from rka.infra.sqlite_backup import backup_sqlite_database
    from rka.services.legacy_writer_export import (
        LegacyWriterExportError,
        export_legacy_writer_bundle,
    )

    try:
        source = Path(RKAConfig().database_url)
        if not source.is_file():
            raise LegacyWriterExportError(f"SQLite database not found: {source}")
        with tempfile.TemporaryDirectory(prefix="rka-writer-export-") as directory:
            snapshot = Path(directory) / "source-backup.db"
            backup_sqlite_database(source, snapshot)
            result = export_legacy_writer_bundle(snapshot, project_id, output)
    except (LegacyWriterExportError, OSError, sqlite3.DatabaseError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc

    click.echo(f"✅ Exported legacy Writer state to {result.path}")
    click.echo(f"   Project: {result.project_id}")
    click.echo(f"   Tables: {result.table_count}; rows: {result.row_count}")
    click.echo(f"   Tables SHA-256: {result.tables_sha256}")
    click.echo(f"   Semantic root: {result.semantic_root_sha256}")
    click.echo(f"   Bundle SHA-256: {result.sha256}")
    click.echo("   Authority: rka-writer staging only (not switched)")


@main.command()
def migrate():
    """Run pending database migrations."""
    from rka.config import RKAConfig
    from rka.infra.database import Database

    config = RKAConfig()

    async def _migrate():
        db = Database(config.database_url)
        await db.connect()
        try:
            # Core uses both the base schema and Phase-2 FTS/vector schema.
            # The latter also replays migrations that are intentionally
            # deferred until sqlite-vec or Phase-2 tables are available.
            await db.initialize_schema()
            await db.initialize_phase2_schema()
            # Return the result of one final idempotent sweep. Initialization
            # above may already have applied every pending migration.
            return await db.run_migrations()
        finally:
            await db.close()

    count = asyncio.run(_migrate())
    click.echo(
        "Migration initialization complete (base + Phase 2); "
        f"final sweep applied {count} migration(s)."
    )


@main.command("start-all")
@click.option("--host", default="127.0.0.1", help="API server host")
@click.option("--port", default=9712, type=int, help="API server port")
@click.option("--foreground", is_flag=True, help="Run in foreground (don't daemonize)")
def start_all(host: str, port: int, foreground: bool):
    """Start RKA server + worker as background processes (Dockerless mode).

    Data is stored at ~/.rka/ (override via RKA_DATA_DIR). This is the
    Dockerless alternative to `docker compose up -d`.

    Use `rka stop-all` to shut down. PID files are written to the data
    directory so stop-all can find the processes.
    """
    import os
    import subprocess
    import sys
    import time
    from rka.config import RKAConfig

    config = RKAConfig()
    data_dir = config.data_dir
    data_dir.mkdir(parents=True, exist_ok=True)
    pid_dir = data_dir / "pids"
    pid_dir.mkdir(exist_ok=True)

    db_path = data_dir / "rka.db"
    env = {
        **os.environ,
        "RKA_DATA_DIR": str(data_dir),
        "RKA_DB_PATH": str(db_path),
        "RKA_HOST": host,
        "RKA_PORT": str(port),
    }

    rka_bin = sys.executable
    rka_module = [rka_bin, "-m", "rka.cli"]

    # Check if already running
    for name in ("serve", "worker"):
        pf = pid_dir / f"{name}.pid"
        if pf.exists():
            pid = int(pf.read_text().strip())
            try:
                os.kill(pid, 0)
                click.echo(f"⚠️  {name} already running (pid {pid}). Use `rka stop-all` first.")
                return
            except OSError:
                pf.unlink()  # stale pid file

    if foreground:
        click.echo(f"Starting RKA in foreground (data: {data_dir}, db: {db_path})")
        click.echo(f"Server: http://{host}:{port}")
        click.echo("Press Ctrl+C to stop.\n")
        # Run serve in foreground; worker is not started in foreground mode
        # (use a separate terminal or `rka worker` in another shell).
        os.environ.update(env)
        import uvicorn
        uvicorn.run(
            "rka.api.app:create_app",
            factory=True,
            host=host,
            port=port,
            log_level="info",
        )
        return

    # Background mode: start serve + worker as subprocesses
    click.echo(f"Starting RKA (data: {data_dir})")

    serve_proc = subprocess.Popen(
        [*rka_module, "serve", "--host", host, "--port", str(port)],
        env=env,
        stdout=open(data_dir / "serve.log", "a"),
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    (pid_dir / "serve.pid").write_text(str(serve_proc.pid))

    worker_proc = subprocess.Popen(
        [*rka_module, "worker"],
        env=env,
        stdout=open(data_dir / "worker.log", "a"),
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    (pid_dir / "worker.pid").write_text(str(worker_proc.pid))

    # Wait briefly and check both processes started
    time.sleep(1.0)
    for name, proc in [("serve", serve_proc), ("worker", worker_proc)]:
        if proc.poll() is not None:
            click.echo(f"❌ {name} exited immediately (code {proc.returncode}). Check {data_dir}/{name}.log")
            return

    click.echo(f"   Server: http://{host}:{port} (pid {serve_proc.pid})")
    click.echo(f"   Worker: pid {worker_proc.pid}")
    click.echo(f"   Logs: {data_dir}/serve.log, {data_dir}/worker.log")
    click.echo("   Stop: rka stop-all")


@main.command("stop-all")
def stop_all():
    """Stop RKA background processes started by start-all."""
    import os
    import signal
    from rka.config import RKAConfig

    config = RKAConfig()
    pid_dir = config.data_dir / "pids"
    if not pid_dir.is_dir():
        click.echo("No PID files found. Nothing to stop.")
        return

    stopped = 0
    for name in ("serve", "worker"):
        pf = pid_dir / f"{name}.pid"
        if not pf.exists():
            continue
        pid = int(pf.read_text().strip())
        try:
            os.kill(pid, signal.SIGTERM)
            click.echo(f"   Stopped {name} (pid {pid})")
            stopped += 1
        except OSError:
            click.echo(f"   {name} (pid {pid}) already gone")
        pf.unlink(missing_ok=True)

    if stopped:
        click.echo(f"Stopped {stopped} process(es).")
    else:
        click.echo("No running processes found.")


@main.group()
def bootstrap():
    """Workspace bootstrap — scan and ingest research files."""
    pass


@main.command()
def backfill():
    """Backfill entity_links from legacy JSON arrays in existing entries."""
    from rka.config import RKAConfig
    from rka.infra.database import Database
    from rka.services.backfill import backfill_entity_links

    config = RKAConfig()

    async def _backfill():
        db = Database(config.database_url)
        await db.connect()
        await db.initialize_schema()
        counts = await backfill_entity_links(db)
        await db.close()
        return counts

    counts = asyncio.run(_backfill())
    click.echo("Entity links backfill complete:")
    for source, count in counts.items():
        click.echo(f"  {source}: {count} links created")
    click.echo(f"  Total: {sum(counts.values())}")


@main.command("backfill-embeddings")
@click.option("--project", "project_id", default="proj_default", help="Project id to backfill")
@click.option("--batch-size", default=50, show_default=True, type=click.IntRange(1, 128), help="Maximum rows per batch (worker resource limits may lower it)")
@click.option("--figures/--no-figures", default=True, help="Backfill figure embeddings")
@click.option("--artifacts/--no-artifacts", default=True, help="Backfill artifact embeddings")
@click.option("--claims/--no-claims", default=True, help="Backfill claim embeddings")
@click.option("--force", is_flag=True, help="Re-embed even if metadata is current")
def backfill_embeddings_cmd(
    project_id: str,
    batch_size: int,
    figures: bool,
    artifacts: bool,
    claims: bool,
    force: bool,
):
    """Queue project-scoped artifact, figure and claim embeddings for the worker."""
    from rka.config import RKAConfig
    from rka.infra.database import Database
    from rka.services.embedding_config import EmbeddingConfigService
    from rka.services.worker import EnrichmentWorker
    from rka.services.backfill import backfill_embeddings

    config = RKAConfig()
    if not any((artifacts, figures, claims)):
        raise click.ClickException("Select at least one embedding entity type.")
    if not config.embeddings_enabled:
        raise click.ClickException("Embeddings are disabled; enable the configured backend before queueing.")
    if not EmbeddingConfigService(config_dir=config.data_dir).config_path.is_file():
        raise click.ClickException("No persisted embedding configuration; test and save it through Settings first.")
    if not Path(config.database_url).is_file():
        raise click.ClickException("No initialized RKA database; start the API with this data directory first.")

    async def _run():
        db = Database(config.database_url)
        await db.connect()
        try:
            # No CLI schema reshape, default-model fallback or inference.
            runner = EnrichmentWorker.boot(db=db, data_dir=config.data_dir)
            await runner._refresh_embeddings_before_job()
            return await backfill_embeddings(
                db, runner.embeddings, project_id=project_id, batch_size=batch_size,
                include_artifacts=artifacts, include_figures=figures,
                include_claims=claims, force=force,
            )
        finally:
            await db.close()

    try:
        job = asyncio.run(_run())
    except (ValueError, RuntimeError, sqlite3.Error) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"Embedding backfill queued for {project_id}: {job['id']}")
    click.echo("Run rka worker with the same data directory; this command did not perform inference.")
    click.echo(f"Status: /api/config/embedding/backfill/status?job_id={job['id']}")


@bootstrap.command("scan")
@click.argument("folder")
@click.option("--ignore", "-i", multiple=True, help="Additional ignore patterns")
@click.option("--no-llm", is_flag=True, help="Disable LLM-enhanced classification")
@click.option("--json-output", is_flag=True, help="Output raw JSON manifest")
def bootstrap_scan(folder: str, ignore: tuple, no_llm: bool, json_output: bool):
    """Scan an operator-authorized folder (RKA_SERVER_FILE_ROOTS)."""
    from rka.config import RKAConfig
    from rka.infra.database import Database
    from rka.infra.llm import LLMClient
    from rka.services.workspace import WorkspaceService
    from rka.services.notes import NoteService
    from rka.services.literature import LiteratureService
    from rka.services.academic import AcademicImportService

    config = RKAConfig()
    file_policy = config.file_access_policy
    file_policy.directory(folder)

    async def _scan():
        db = Database(config.database_url)
        await db.connect()
        try:
            await db.initialize_schema()
            llm = LLMClient(config) if config.llm_enabled and not no_llm else None
            note_svc = NoteService(db, llm=llm)
            lit_svc = LiteratureService(db, llm=llm)
            academic_svc = AcademicImportService(lit_svc, note_service=note_svc, file_policy=file_policy)
            ws_svc = WorkspaceService(db, academic_svc, note_svc, lit_svc, llm=llm, file_policy=file_policy)
            return await ws_svc.scan(
                folder_path=folder,
                ignore_patterns=list(ignore),
                use_llm=not no_llm,
            )
        finally:
            await db.close()

    manifest = asyncio.run(_scan())

    if json_output:
        click.echo(manifest.model_dump_json(indent=2))
        return

    click.echo(f"📂 Scanned: {manifest.root_path}")
    click.echo(f"   Scan ID: {manifest.scan_id}")
    click.echo(f"   Files: {manifest.total_files_found} found, {manifest.total_files_scanned} scanned")
    click.echo(f"   Categories: {manifest.summary.by_category}")
    click.echo(f"   Targets: {manifest.summary.by_target}")

    if manifest.summary.duplicate_count:
        click.echo(f"   ⚠️  Duplicates: {manifest.summary.duplicate_count}")
    if manifest.summary.llm_classified_count:
        click.echo(f"   🤖 LLM-classified: {manifest.summary.llm_classified_count}")

    click.echo(f"\nFiles ({len(manifest.files)}):")
    for f in manifest.files:
        dup = " [DUP]" if f.is_duplicate else ""
        llm_tag = " [LLM]" if f.llm_classified else ""
        click.echo(f"  {f.relative_path} [{f.category.value}→{f.proposed_type}]{dup}{llm_tag}")

    if manifest.warnings:
        click.echo("\n⚠️  Warnings:")
        for w in manifest.warnings:
            click.echo(f"  - {w}")

    click.echo(f"\nRun 'rka bootstrap ingest {folder}' to ingest these files.")


@bootstrap.command("ingest")
@click.argument("folder")
@click.option("--phase", "-p", default=None, help="Research phase for all entries")
@click.option("--tags", "-t", multiple=True, help="Tags to add to all entries")
@click.option("--skip", "-s", multiple=True, help="Relative paths to skip")
@click.option("--no-llm", is_flag=True, help="Disable LLM-enhanced classification")
@click.option("--dry-run", is_flag=True, help="Preview without creating entries")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt")
def bootstrap_ingest(
    folder: str, phase: str | None, tags: tuple,
    skip: tuple, no_llm: bool, dry_run: bool, yes: bool,
):
    """Ingest an operator-authorized folder (RKA_SERVER_FILE_ROOTS)."""
    from rka.config import RKAConfig
    from rka.infra.database import Database
    from rka.infra.llm import LLMClient
    from rka.services.workspace import WorkspaceService
    from rka.services.notes import NoteService
    from rka.services.literature import LiteratureService
    from rka.services.academic import AcademicImportService
    from rka.models.workspace import WorkspaceIngestRequest

    config = RKAConfig()
    file_policy = config.file_access_policy
    file_policy.directory(folder)

    async def _ingest():
        db = Database(config.database_url)
        await db.connect()
        try:
            await db.initialize_schema()
            llm = LLMClient(config) if config.llm_enabled and not no_llm else None
            note_svc = NoteService(db, llm=llm)
            lit_svc = LiteratureService(db, llm=llm)
            academic_svc = AcademicImportService(lit_svc, note_service=note_svc, file_policy=file_policy)
            ws_svc = WorkspaceService(db, academic_svc, note_svc, lit_svc, llm=llm, file_policy=file_policy)
            manifest = await ws_svc.scan(folder_path=folder, ignore_patterns=[], use_llm=not no_llm)
            request = WorkspaceIngestRequest(
                manifest=manifest,
                skip_files=list(skip),
                override_tags=list(tags),
                phase=phase,
                source="pi",
                dry_run=dry_run,
            )
            result = await ws_svc.ingest(request)
            return manifest, result
        finally:
            await db.close()

    # Confirmation
    if not dry_run and not yes:
        if not click.confirm(f"Ingest files from {folder}?"):
            click.echo("Cancelled.")
            return

    manifest, result = asyncio.run(_ingest())

    prefix = "🔍 DRY RUN — " if dry_run else "✅ "
    click.echo(f"{prefix}Bootstrap complete")
    click.echo(f"   Scan ID: {manifest.scan_id}")
    click.echo(f"   Processed: {result.total_processed}")
    click.echo(f"   Created: {result.total_created}")
    click.echo(f"   Skipped: {result.total_skipped}")
    click.echo(f"   Errors: {result.total_errors}")

    for item in result.results:
        if item.error and not item.success:
            click.echo(f"  ❌ {item.relative_path}: {item.error}")
        elif item.entity_ids:
            click.echo(f"  ✓ {item.relative_path} → {item.entity_count} entries")


@main.command("periodic-hooks")
@click.option(
    "--project-id",
    "project_ids",
    multiple=True,
    help="Project IDs to fire 'periodic' hooks for. Repeat for multiple, "
         "or omit to fire across every project in the database.",
)
def periodic_hooks(project_ids: tuple[str, ...]):
    """Fire 'periodic' hooks once across one or more projects.

    Intended to be invoked by cron or a scheduler at the cadence the PI/Brain
    chooses (hourly, daily). Each invocation fires the periodic event once;
    handler config inside individual hooks decides what to do.

    Mission 2 v1: simple cron-driven invocation. v1.1 may add per-hook
    interval scheduling inside the dispatcher.
    """
    from datetime import datetime, timezone
    from rka.config import RKAConfig
    from rka.infra.database import Database
    from rka.services.hook_dispatcher import HookDispatcher

    config = RKAConfig()

    async def _run() -> None:
        db = Database(config.database_url)
        await db.connect()
        try:
            targets = list(project_ids)
            if not targets:
                rows = await db.fetchall("SELECT id FROM projects")
                targets = [r["id"] for r in rows]
            if not targets:
                click.echo("No projects found; nothing to fire.")
                return
            dispatcher = HookDispatcher(db)
            now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            total = 0
            for pid in targets:
                ids = await dispatcher.fire(
                    event="periodic",
                    payload={"project_id": pid, "now": now},
                    project_id=pid,
                )
                click.echo(f"  {pid}: fired {len(ids)} hook execution(s)")
                total += len(ids)
            click.echo(f"Done. {total} executions across {len(targets)} project(s).")
        finally:
            await db.close()

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# admin subgroup (v2.7.0.6) — one-shot maintenance commands.
#
# These intentionally are NOT exposed via MCP: admin operations require
# shell-level intent (per the v2.7.0.6 design ratification). The PI cockpit
# cannot accidentally fire them through `rka_execute`.
# ---------------------------------------------------------------------------


@main.group()
def admin():
    """Admin/maintenance commands (CLI-only, not exposed via MCP)."""
    pass


from rka.cli_embedding import embedding_admin as _embedding_admin  # noqa: E402

admin.add_command(_embedding_admin)


@admin.command("list-orphan-supersedes")
@click.option(
    "--project", "project_id", required=True,
    help="Project id (prj_...) to inspect.",
)
@click.option(
    "--json", "json_output", is_flag=True,
    help="Emit JSON instead of human-readable text.",
)
def admin_list_orphan_supersedes(project_id: str, json_output: bool):
    """List decisions whose status='superseded' but superseded_by IS NULL.

    These are the v2.7.0.4-era cockpit-workaround orphans: the PI flipped
    the status manually but the atomic supersede side effects
    (superseded_by FK, supersedes entity link, scope_version bump,
    staleness cascade) never ran. Use the output to build the
    `--map old_id=new_id` arguments for `rka admin repair-supersedes`.
    """
    import json as _json
    from rka.config import RKAConfig
    from rka.infra.database import Database
    from rka.services.admin_repair import list_orphan_supersedes

    config = RKAConfig()

    async def _run():
        db = Database(config.database_url)
        await db.connect()
        try:
            return await list_orphan_supersedes(db, project_id)
        finally:
            await db.close()

    rows = asyncio.run(_run())
    if json_output:
        click.echo(_json.dumps(rows, indent=2, default=str))
        return
    if not rows:
        click.echo(f"No orphan supersedes in project {project_id}.")
        return
    click.echo(
        f"=== {len(rows)} orphan superseded decisions in {project_id} ==="
    )
    for r in rows:
        click.echo(
            f"\n  {r['id']}"
            f"\n    question:      {(r.get('question') or '')[:80]}"
            f"\n    phase:         {r.get('phase') or '(empty)'}"
            f"\n    decided_by:    {r.get('decided_by')}"
            f"\n    chosen:        {(r.get('chosen') or '')[:60]}"
            f"\n    scope_version: {r.get('scope_version')}"
            f"\n    updated_at:    {r.get('updated_at')}"
        )
    click.echo(
        "\nNext step: identify each orphan's replacement decision id and run"
        f"\n  rka admin repair-supersedes --project={project_id} "
        "--map=<old>=<new> [--map=...] [--apply]\n"
        "(--apply is required to mutate; default is dry-run.)"
    )


@admin.command("repair-supersedes")
@click.option(
    "--project", "project_id", required=True,
    help="Project id (prj_...) the orphan pairs live in.",
)
@click.option(
    "--map", "mappings", multiple=True, required=True,
    metavar="old_id=new_id",
    help=(
        "Pair to repair, formatted old_decision_id=new_decision_id. "
        "Repeat the option for multiple pairs."
    ),
)
@click.option(
    "--apply", "do_apply", is_flag=True,
    help=(
        "Required for mutation. Without it, the command runs in dry-run "
        "mode and prints the WOULD/ALREADY plan with NO DB writes."
    ),
)
@click.option(
    "--actor",
    type=click.Choice(["pi", "brain", "executor", "system"]),
    default="pi", show_default=True,
    help="Actor recorded on the backfilled event + entity_links rows.",
)
@click.option(
    "--json", "json_output", is_flag=True,
    help="Emit JSON instead of human-readable text.",
)
def admin_repair_supersedes(
    project_id: str,
    mappings: tuple[str, ...],
    do_apply: bool,
    actor: str,
    json_output: bool,
):
    """Backfill the missing supersede side effects for orphan decisions.

    For each (old, new) pair the command replays the canonical supersede
    sequence WITHOUT creating a new decision row (the new row already
    exists from the cockpit workaround):

      1. bump new.scope_version to old.scope_version + 1
      2. set old.superseded_by = new.id
      3. insert entity_links row (link_type='supersedes', new -> old)
      4. cascade staleness on claims/clusters sourced from old-linked
         journal entries
      5. insert a re_distill_review row + emit decision_superseded event

    Each pair is wrapped in its own transaction — a mid-pair failure
    rolls back that pair without partial state. Idempotent: a re-run
    that finds a pair already partially repaired shows ALREADY markers
    for the already-satisfied steps.
    """
    from rka.config import RKAConfig
    from rka.infra.database import Database
    from rka.services.admin_repair import (
        render_pair_reports,
        repair_orphan_supersedes,
    )

    pairs: dict[str, str] = {}
    for raw in mappings:
        if "=" not in raw:
            raise click.UsageError(
                f"--map value {raw!r} must be old_id=new_id"
            )
        old_id, _, new_id = raw.partition("=")
        old_id, new_id = old_id.strip(), new_id.strip()
        if not old_id or not new_id:
            raise click.UsageError(
                f"--map value {raw!r} has an empty side"
            )
        if old_id in pairs:
            raise click.UsageError(
                f"old_decision_id {old_id!r} listed more than once in --map"
            )
        pairs[old_id] = new_id

    config = RKAConfig()
    dry_run = not do_apply

    async def _run():
        db = Database(config.database_url)
        await db.connect()
        try:
            return await repair_orphan_supersedes(
                db, project_id=project_id, mapping=pairs,
                dry_run=dry_run, actor=actor,
            )
        finally:
            await db.close()

    reports = asyncio.run(_run())
    click.echo(render_pair_reports(
        reports, dry_run=dry_run, json_output=json_output,
    ))
    # Exit non-zero if any pair rolled back, so CI / scripts see the
    # failure signal explicitly.
    if any(r.rolled_back for r in reports):
        raise SystemExit(1)


@admin.command("reindex")
@click.option(
    "--project", "project_id", default=None,
    help="Only rebuild this project's rows. Omit to rebuild ALL projects.",
)
@click.option(
    "--types", "types_csv", default=None,
    help="Comma-separated entity types to rebuild "
         "(journal,decision,literature,mission,claim,cluster). Default: all.",
)
@click.option(
    "--json", "json_output", is_flag=True,
    help="Emit JSON instead of human-readable text.",
)
def admin_reindex(project_id: str | None, types_csv: str | None, json_output: bool):
    """Rebuild the FTS search indexes from their source tables.

    Recovery path for search-index drift: the FTS5 indexes are maintained
    in application code, so a write-path slip (a missing sync, a swallowed
    failure, a partial import) can silently desync the index from the
    source rows. This command rebuilds them deterministically. Safe to run
    any time — it DELETEs and re-INSERTs FTS rows only (never touches source
    data). Scope to one project with --project; otherwise rebuilds globally.
    """
    import json as _json
    from rka.config import RKAConfig
    from rka.infra.database import Database
    from rka.services.reindex import reindex_fts

    entity_types = (
        [t.strip() for t in types_csv.split(",") if t.strip()] if types_csv else None
    )
    config = RKAConfig()

    async def _run():
        db = Database(config.database_url)
        await db.connect()
        try:
            return await reindex_fts(db, project_id=project_id, entity_types=entity_types)
        finally:
            await db.close()

    report = asyncio.run(_run())

    if json_output:
        click.echo(_json.dumps({
            "results": report.results,
            "failures": report.failures,
            "total_reindexed": report.total_reindexed,
            "ok": report.ok,
        }, indent=2))
    else:
        scope = f"project {project_id}" if project_id else "ALL projects"
        click.echo(f"=== rka admin reindex — FTS rebuild ({scope}) ===")
        for etype, count in report.results.items():
            click.echo(f"  [+] {etype}: {count} rows reindexed")
        for etype, err in report.failures.items():
            click.echo(f"  [!] {etype}: FAILED — {err}")
        click.echo(f"Total: {report.total_reindexed} rows reindexed.")
        if not report.ok:
            click.echo("Some tables failed — see [!] lines above.")
    if not report.ok:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
