#!/usr/bin/env python3
"""Start and probe the supported RKA Core surfaces in disposable state."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import zipfile
from datetime import timedelta
from pathlib import Path
from typing import TextIO

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


ROOT = Path(__file__).resolve().parents[1]


def _available_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _run_cli(
    cli: list[str], args: list[str], env: dict[str, str], cwd: Path
) -> str:
    result = subprocess.run(
        [*cli, *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
    )
    if result.returncode:
        raise RuntimeError(
            f"{' '.join(args)} failed ({result.returncode})\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result.stdout


def _wait_for_health(base_url: str, server: subprocess.Popen[str]) -> dict:
    deadline = time.monotonic() + 30
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        if server.poll() is not None:
            raise RuntimeError(f"REST server exited early with status {server.returncode}")
        try:
            response = httpx.get(f"{base_url}/api/health", timeout=2)
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, json.JSONDecodeError) as exc:
            last_error = exc
            time.sleep(0.2)
    raise RuntimeError(f"REST health did not become ready: {last_error}")


def _probe_public_rest_contract(base_url: str) -> None:
    """Complete the E2 workflow using HTTP only, with no Core imports."""

    project_id = "prj_public_contract_smoke"
    headers = {"X-RKA-Project": project_id}
    with httpx.Client(base_url=base_url, timeout=10) as client:
        capabilities = client.get(
            "/api/capabilities",
            params={"required_contract": "rka-core/v1"},
        )
        capabilities.raise_for_status()
        if capabilities.json().get("core", {}).get("contract") != "rka-core/v1":
            raise RuntimeError("public capability discovery omitted rka-core/v1")

        project = client.post(
            "/api/projects",
            json={
                "id": project_id,
                "name": "Public contract smoke",
                "description": "Disposable installed-wheel E2 workflow.",
            },
        )
        project.raise_for_status()

        unscoped = client.post(
            "/api/notes",
            json={"content": "Unscoped calls must fail.", "type": "note"},
        )
        if unscoped.status_code != 422:
            raise RuntimeError(
                f"project-scoped public call returned {unscoped.status_code}, expected 422"
            )

        note_input = {
            "content": "The disposable run observed a 12 percent improvement.",
            "type": "note", "source": "executor", "request_id": "startup-note-1",
        }
        note = client.post(
            "/api/notes",
            headers=headers,
            json=note_input,
        )
        note.raise_for_status()
        note_payload = note.json()
        replay = client.post("/api/notes", headers=headers, json=note_input)
        replay.raise_for_status()
        if replay.json() != note_payload:
            raise RuntimeError("public keyed note retry did not replay its creation snapshot")
        receipt = client.get("/api/notes/write-receipts/startup-note-1", headers=headers)
        receipt.raise_for_status()
        if receipt.json().get("entry") != note_payload:
            raise RuntimeError("public note receipt omitted the original creation snapshot")
        note_read = client.get(f"/api/notes/{note_payload['id']}", headers=headers)
        note_read.raise_for_status()
        if note_read.json().get("content") != note_payload["content"]:
            raise RuntimeError("public note read did not preserve exact content")

        rq = client.post(
            "/api/decisions",
            headers=headers,
            json={
                "question": "Does the fixture method improve the measured outcome?",
                "phase": "framing",
                "decided_by": "brain",
                "kind": "research_question",
                "status": "active",
                "related_journal": [note_payload["id"]],
            },
        )
        rq.raise_for_status()
        claim = client.post(
            "/api/claims",
            headers=headers,
            json={
                "source_entry_id": note_payload["id"],
                "claim_type": "evidence",
                "content": "The fixture method improved the measured outcome by 12 percent.",
                "confidence": 0.85,
                "verified": True,
                "evidence_status": "supported",
            },
        )
        claim.raise_for_status()
        cluster = client.post(
            "/api/clusters",
            headers=headers,
            json={
                "research_question_id": rq.json()["id"],
                "label": "Observed improvement",
                "confidence": "moderate",
            },
        )
        cluster.raise_for_status()
        edge_input = {
            "source_claim_id": claim.json()["id"],
            "cluster_id": cluster.json()["id"],
            "relation": "member_of",
            "confidence": 1.0,
        }
        edge = client.post("/api/claims/edges", headers=headers, json=edge_input)
        edge.raise_for_status()
        repeated = client.post("/api/claims/edges", headers=headers, json=edge_input)
        repeated.raise_for_status()
        if repeated.json().get("id") != edge.json().get("id"):
            raise RuntimeError("public natural-key retry created a duplicate claim edge")

        evidence = client.get(
            "/api/assemble-evidence",
            headers=headers,
            params={"research_question_id": rq.json()["id"], "format": "progress_report"},
        )
        evidence.raise_for_status()
        if "12 percent" not in evidence.json().get("content", ""):
            raise RuntimeError("public evidence assembly omitted the supported observation")
        research_map = client.get("/api/research-map", headers=headers)
        research_map.raise_for_status()
        if not any(
            item.get("id") == rq.json()["id"]
            for item in research_map.json().get("research_questions", [])
        ):
            raise RuntimeError("public research map omitted the created research question")
        changes = client.get(
            "/api/changes",
            headers=headers,
            params={"cursor": 0, "limit": 100},
        )
        changes.raise_for_status()
        if int(changes.json().get("next_cursor", 0)) <= 0:
            raise RuntimeError("public change feed did not advance its cursor")


def _assert_writer_export_bundle(path: Path) -> None:
    with zipfile.ZipFile(path) as archive:
        manifest = json.loads(archive.read("manifest.json"))
    if manifest.get("contract") != "rka-legacy-writer-export/v1":
        raise RuntimeError("installed Writer exporter emitted an unsupported contract")
    if manifest.get("table_count") != 29 or len(manifest.get("tables", {})) != 29:
        raise RuntimeError("installed Writer exporter omitted frozen Writer tables")
    if manifest.get("authority", {}).get("authority_switched") is not False:
        raise RuntimeError("Writer compatibility export incorrectly switched authority")
    if not manifest.get("semantic_root_sha256"):
        raise RuntimeError("Writer compatibility export omitted its semantic root")


def _assert_migration_state(db_path: Path, *, require_vec: bool) -> None:
    with sqlite3.connect(db_path) as conn:
        migrations = {
            row[0] for row in conn.execute("SELECT filename FROM schema_migrations")
        }
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
            )
        }

    required_migrations = set()
    if require_vec:
        required_migrations = {
            "002_add_vec_artifacts.sql",
            "010_v2_vec_claims.sql",
        }
    missing_migrations = required_migrations - migrations
    if missing_migrations:
        raise RuntimeError(
            f"migrate omitted vector migrations: {sorted(missing_migrations)}"
        )

    required_tables = {
        "journal_write_receipts",
        "projects",
        "journal",
        "decisions",
        "fts_journal",
        "schema_migrations",
    }
    if require_vec:
        required_tables |= {
            "embedding_metadata",
            "vec_artifacts",
            "vec_claims",
            "vec_journal",
        }
    missing_tables = required_tables - tables
    if missing_tables:
        raise RuntimeError(f"migrate omitted Phase-2 tables: {sorted(missing_tables)}")


def _probe_phase2_file_lock(
    python: str, db_path: Path, env: dict[str, str], cwd: Path
) -> None:
    """Exercise real lock contention through the selected installed package."""
    probe = """
import asyncio
import sys
from rka.infra.database import Database

async def main():
    first = Database(sys.argv[1])
    second = Database(sys.argv[1])
    async with first._phase2_schema_lock():
        try:
            async with second._phase2_schema_lock():
                raise RuntimeError("contended Phase-2 sidecar lock was entered")
        except TimeoutError:
            pass
    async with second._phase2_schema_lock():
        pass

asyncio.run(main())
"""
    probe_env = env.copy()
    probe_env["RKA_MIGRATION_LOCK_TIMEOUT_MS"] = "25"
    probe_env["RKA_PHASE2_LOCK_PATH"] = str(db_path.with_suffix(".lock"))
    _run_cli([python], ["-c", probe, str(db_path)], probe_env, cwd)


async def _probe_mcp(
    python: str, cwd: Path, env: dict[str, str], errlog: TextIO
) -> None:
    params = StdioServerParameters(
        command=python,
        args=["-m", "rka", "mcp"],
        cwd=cwd,
        env=env,
    )
    async with stdio_client(params, errlog=errlog) as (read, write):
        async with ClientSession(
            read,
            write,
            read_timeout_seconds=timedelta(seconds=15),
        ) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = {tool.name for tool in tools.tools}
            required = {
                "rka_query",
                "rka_execute",
                "rka_describe",
                "rka_load_tools",
                "rka_help",
            }
            missing = required - names
            if missing:
                raise RuntimeError(
                    f"MCP startup omitted required tools: {sorted(missing)}"
                )

            result = await session.call_tool(
                "rka_query",
                {"args": {"operation": "health"}},
            )
            if result.isError:
                raise RuntimeError(f"MCP health call failed: {result.content}")
            rendered = "\n".join(
                block.text for block in result.content if hasattr(block, "text")
            )
            payload = json.loads(rendered)
            if payload.get("status") not in {"ok", "healthy"}:
                raise RuntimeError(f"unexpected MCP health result: {rendered}")

            # Read the REST-created receipt through the actual stdio transport.
            receipt_result = await session.call_tool("rka_query", {"args": {
                "operation": "note_write_receipt", "project_id": "prj_public_contract_smoke",
                "request_id": "startup-note-1",
            }})
            if receipt_result.isError:
                raise RuntimeError(f"MCP receipt read failed: {receipt_result.content}")
            receipt = json.loads("\n".join(
                block.text for block in receipt_result.content if hasattr(block, "text")
            ))
            if (receipt.get("request_id") != "startup-note-1"
                    or receipt.get("project_id") != "prj_public_contract_smoke"
                    or receipt.get("journal_id") != receipt.get("entry", {}).get("id")):
                raise RuntimeError("MCP receipt omitted scoped creation identity")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--require-web",
        action="store_true",
        help="Fail unless the built web dashboard is served from /.",
    )
    parser.add_argument(
        "--require-vec",
        action="store_true",
        help="Fail unless sqlite-vec and the Phase-2 vector schema are available.",
    )
    parser.add_argument(
        "--python",
        default=sys.executable,
        help="Python interpreter whose installed RKA package should be tested.",
    )
    parser.add_argument(
        "--cwd",
        type=Path,
        default=ROOT,
        help="Working directory for all tested RKA subprocesses.",
    )
    args = parser.parse_args()

    # Do not resolve the interpreter symlink: a venv's ``python`` commonly
    # points at the base interpreter, and resolving it would silently discard
    # the isolated environment we intend to test.
    runtime_python = str(Path(args.python).expanduser().absolute())
    runtime_cwd = args.cwd.expanduser().resolve()
    runtime_cwd.mkdir(parents=True, exist_ok=True)
    cli = [runtime_python, "-m", "rka"]

    with tempfile.TemporaryDirectory(prefix="rka-core-smoke-") as temp_dir:
        data_dir = Path(temp_dir)
        port = _available_port()
        base_url = f"http://127.0.0.1:{port}"
        env = os.environ.copy()
        # A clean-wheel smoke must not import RKA from a source checkout or
        # inherit a caller's project-local database selection.
        env.pop("PYTHONPATH", None)
        env.pop("RKA_DB_PATH", None)
        env.pop("RKA_PROJECT_DIR", None)
        env.update(
            {
                "RKA_DATA_DIR": str(data_dir),
                "RKA_LLM_ENABLED": "false",
                # Avoid a model download while still proving sqlite-vec loads.
                "RKA_EMBEDDINGS_ENABLED": "false",
                "RKA_API_URL": base_url,
                "PYTHONUNBUFFERED": "1",
            }
        )

        db_path = data_dir / "rka.db"
        version_output = _run_cli(cli, ["--version"], env, runtime_cwd)
        migration_output = _run_cli(cli, ["migrate"], env, runtime_cwd)
        if not db_path.is_file():
            raise RuntimeError(f"default database was not created under data_dir: {db_path}")
        stray_db = runtime_cwd / "rka.db"
        if stray_db != db_path and stray_db.exists():
            raise RuntimeError(f"RKA created a cwd-relative database: {stray_db}")
        _assert_migration_state(db_path, require_vec=args.require_vec)
        _probe_phase2_file_lock(
            runtime_python,
            data_dir / "phase2-lock-probe.db",
            env,
            runtime_cwd,
        )
        worker_output = _run_cli(cli, ["worker", "--once"], env, runtime_cwd)

        log_path = data_dir / "server.log"
        mcp_log_path = data_dir / "mcp.log"
        with (
            log_path.open("w+", encoding="utf-8") as log,
            mcp_log_path.open("w+", encoding="utf-8") as mcp_log,
        ):
            server = subprocess.Popen(
                [*cli, "serve", "--host", "127.0.0.1", "--port", str(port)],
                cwd=runtime_cwd,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                text=True,
            )
            try:
                health = _wait_for_health(base_url, server)
                if health.get("status") != "ok":
                    raise RuntimeError(f"unexpected REST health payload: {health}")
                if args.require_vec and health.get("vec_available") is not True:
                    raise RuntimeError(f"sqlite-vec unavailable: {health}")
                _probe_public_rest_contract(base_url)
                writer_bundle = data_dir / "public-contract.rka-writer-export.zip"
                _run_cli(
                    cli,
                    [
                        "export-writer",
                        "--project-id",
                        "prj_public_contract_smoke",
                        "--output",
                        str(writer_bundle),
                    ],
                    env,
                    runtime_cwd,
                )
                _assert_writer_export_bundle(writer_bundle)

                web_index = ROOT / "web" / "dist" / "index.html"
                if args.require_web and not web_index.is_file():
                    raise RuntimeError("--require-web was set but web/dist/index.html is absent")
                if args.require_web:
                    dashboard = httpx.get(f"{base_url}/", timeout=5)
                    dashboard.raise_for_status()
                    if "text/html" not in dashboard.headers.get("content-type", ""):
                        raise RuntimeError("web dashboard did not return HTML")

                    asset_match = re.search(r'src="(/assets/[^"]+\.js)"', dashboard.text)
                    if not asset_match:
                        raise RuntimeError("web dashboard HTML references no JavaScript asset")
                    asset = httpx.get(f"{base_url}{asset_match.group(1)}", timeout=5)
                    asset.raise_for_status()
                    content_type = asset.headers.get("content-type", "")
                    if "javascript" not in content_type:
                        raise RuntimeError(
                            f"web dashboard asset has unexpected content type: {content_type}"
                        )

                    brand_icon = httpx.get(
                        f"{base_url}/brand/rka-project-plugin-app-icon.svg",
                        timeout=5,
                    )
                    brand_icon.raise_for_status()
                    brand_content_type = brand_icon.headers.get("content-type", "")
                    if "image/svg+xml" not in brand_content_type:
                        raise RuntimeError(
                            "web brand icon has unexpected content type: "
                            f"{brand_content_type}"
                        )
                    if not brand_icon.text.lstrip().startswith("<svg"):
                        raise RuntimeError(
                            "web brand icon did not return SVG content; "
                            "the SPA fallback may have masked a missing asset"
                        )

                asyncio.run(
                    asyncio.wait_for(
                        _probe_mcp(runtime_python, runtime_cwd, env, mcp_log),
                        timeout=30,
                    )
                )
            except Exception:
                log.flush()
                log.seek(0)
                print(log.read(), file=sys.stderr)
                mcp_log.flush()
                mcp_log.seek(0)
                print(mcp_log.read(), file=sys.stderr)
                raise
            finally:
                if server.poll() is None:
                    server.terminate()
                    try:
                        server.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        server.kill()
                        server.wait(timeout=5)

        print(
            "Core startup smoke passed: installed entry point, migrations, "
            "Phase-2 file locking, public REST workflow, MCP, worker"
            + (", sqlite-vec" if args.require_vec else "")
            + (", and web dashboard." if args.require_web else ".")
        )
        print(version_output.strip())
        print(migration_output.strip())
        print(worker_output.strip())


if __name__ == "__main__":
    main()
