#!/usr/bin/env python3
"""Generate public synthetic fixtures with pinned old Core code, then upgrade.

No user database, home configuration, ports, embedding provider or Docker is
used. Historical schemas and write services run in separate subprocesses;
dependencies come from the current reviewed Core environment (not old locks).
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import json
import os
import shutil
import sqlite3
import struct
import subprocess
import sys
import tarfile
import tempfile
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASELINES = {
    "2.8.1": "5c7da7125dd6c0996b7e9a740c3fea35dbdcdfe0",
    # Last 2.9.0 source before the Core/Writer split; issue #158 reports
    # upgrading 2.9.0 to 3.0.0. The reporter's exact 2.9.0 SHA is unknown.
    "2.9.0": "0dc1842ca771962e6e07676b6de9a6abecd74ac9",
}
FIXTURE_HASHES = {
    "2.8.1": "0536a12277478af44ff5f8efc660eaf6f8c008458649f6e89353f9a7f8060927",
    "2.9.0": "de066793548e0c8dbfb232b1dbbf422eaea3ae03a2eaf93ca4e23d19efcd3910",
}
PROJECT = "prj_synthetic_upgrade"
TABLES = (
    "projects",
    "project_states",
    "journal",
    "decisions",
    "claims",
    "entity_links",
    "tags",
    "events",
    "audit_log",
)
STAMP = "2026-01-01T00:00:00Z"


def canonical_hash(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


@contextmanager
def connect(path):
    import sqlite_vec

    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.enable_load_extension(True)
    sqlite_vec.load(db)
    db.enable_load_extension(False)
    try:
        yield db
    finally:
        db.close()


def rows(db, table):
    return [dict(row) for row in db.execute(f'SELECT * FROM "{table}" ORDER BY rowid')]


def snapshot(path):
    with connect(path) as db:
        result = {table: rows(db, table) for table in TABLES}
        result["migrations"] = [
            row[0] for row in db.execute("SELECT filename FROM schema_migrations ORDER BY filename")
        ]
        result["vectors"] = [
            (row[0], hashlib.sha256(row[1]).hexdigest())
            for row in db.execute("SELECT id, embedding FROM vec_claims ORDER BY id")
        ]
        result["metadata"] = rows(db, "embedding_metadata")
        result["foreign_keys"] = [tuple(row) for row in db.execute("PRAGMA foreign_key_check")]
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        return result


async def seed(path):
    # Fixed IDs only in this synthetic process, before importing old services.
    import itertools
    import rka.infra.ids as ids

    counter = itertools.count(1)
    ids.generate_id = lambda kind: (
        f"{ids._PREFIXES.get(kind, kind[:3])}_synthetic_{next(counter):06d}"
    )
    from rka.infra.database import Database
    from rka.models.project import ProjectCreate
    from rka.models.journal import JournalEntryCreate
    from rka.models.decision import DecisionCreate
    from rka.models.claim import ClaimCreate
    from rka.services.project import ProjectService
    from rka.services.notes import NoteService
    from rka.services.decisions import DecisionService
    from rka.services.claims import ClaimService

    assert not path.exists(), "Refuse to overwrite a fixture"
    db = Database(str(path))
    await db.connect()
    try:
        await db.initialize_schema()
        await db.initialize_phase2_schema()
        assert db._vec_loaded, "Real sqlite-vec required; do not silently skip"
        await ProjectService(db).create_project(
            ProjectCreate(id=PROJECT, name="Synthetic upgrade fixture")
        )
        notes = NoteService(db, project_id=PROJECT)
        original = await notes.create(
            JournalEntryCreate(
                content="Synthetic amber provenance observation.\n原始记录。",
                source="pi",
                type="note",
                verbatim_input="Original synthetic quotation.\r\n",
                tags=["synthetic", "upgrade"],
                confidence="tested",
            )
        )
        await notes.create(
            JournalEntryCreate(
                content="Synthetic amber replacement observation.",
                source="executor",
                type="note",
                supersedes=original.id,
            )
        )
        await notes.create(
            JournalEntryCreate(
                content="Synthetic directive: keep the dataset local.",
                source="pi",
                type="directive",
            )
        )
        await DecisionService(db, project_id=PROJECT).create(
            DecisionCreate(
                question="Synthetic amber decision?",
                phase="evaluation",
                decided_by="pi",
                rationale="Synthetic rationale.",
                related_journal=[original.id],
            )
        )
        claim = await ClaimService(db, project_id=PROJECT).create(
            ClaimCreate(
                content="Synthetic amber evidence.",
                source_entry_id=original.id,
                claim_type="evidence",
                confidence=0.75,
            )
        )
        # Deliberately synthetic vectors, not model inference or a semantic
        # accuracy test. Exercise preservation of a populated legacy vec index.
        vector = struct.pack("<768f", 1.0, *([0.0] * 767))
        vector_columns = {row["name"] for row in await db.fetchall("PRAGMA table_info(vec_claims)")}
        if "project_id" in vector_columns:
            await db.execute(
                "INSERT INTO vec_claims(id, project_id, embedding) VALUES (?, ?, ?)",
                [claim.id, PROJECT, vector],
            )
        else:
            await db.execute(
                "INSERT INTO vec_claims(id, embedding) VALUES (?, ?)", [claim.id, vector]
            )
        await db.execute(
            """INSERT INTO embedding_metadata
               (project_id, entity_type, entity_id, content_hash, model_name, dimensions)
               VALUES (?, 'claim', ?, ?, 'nomic-ai/nomic-embed-text-v1.5', 768)""",
            [PROJECT, claim.id, hashlib.sha256(claim.content.encode()).hexdigest()],
        )
        # Canonical timestamps make the fixture's logical data hash repeatable.
        # This normalization is fixture generation, never a production migration.
        for table in TABLES + ("embedding_metadata", "schema_migrations"):
            columns = await db.fetchall(f'PRAGMA table_info("{table}")')
            for column in columns:
                name = column["name"]
                if name in {"created_at", "updated_at", "applied_at", "timestamp"}:
                    await db.execute(
                        f'UPDATE "{table}" SET "{name}" = ? WHERE "{name}" IS NOT NULL', [STAMP]
                    )
        await db.commit()
    finally:
        await db.close()


def schema(path):
    """Compare actual columns, constraints, indexes and triggers, not ledger counts."""
    with connect(path) as db:
        result = {}
        for item in db.execute("SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY name"):
            kind, name, table, sql = item
            if name.startswith(("sqlite_", "fts_", "vec_")):
                continue
            if kind == "table":
                result[name] = {
                    "columns": [tuple(row) for row in db.execute(f'PRAGMA table_info("{name}")')],
                    "foreign_keys": sorted(
                        tuple(row) for row in db.execute(f'PRAGMA foreign_key_list("{name}")')
                    ),
                    "sql": " ".join((sql or "").replace('"', "").split()),
                }
            elif kind in {"index", "trigger"}:
                result[name] = {
                    "type": kind,
                    "table": table,
                    "sql": " ".join((sql or "").replace('"', "").split()),
                }
        return result


async def upgrade(path):
    from rka.infra.database import Database

    db = Database(str(path))
    await db.connect()
    try:
        await db.initialize_schema()
        await db.initialize_phase2_schema()
        assert db._vec_loaded
        assert await db.run_migrations() == 0
        assert not await db.fetchall("PRAGMA foreign_key_check")
        assert (
            await db.fetchone("SELECT id FROM fts_journal WHERE fts_journal MATCH 'amber'")
            is not None
        )
        for table in ("fts_claims", "fts_decisions"):
            assert await db.fetchone(f"SELECT id FROM {table} WHERE {table} MATCH 'amber'")
        vector = struct.pack("<768f", 1.0, *([0.0] * 767))
        matches = await db.fetchall(
            "SELECT id FROM vec_claims WHERE embedding MATCH ? AND project_id = ? AND k = 1",
            [vector, PROJECT],
        )
        claim = await db.fetchone("SELECT id FROM claims WHERE project_id = ?", [PROJECT])
        assert [row["id"] for row in matches] == [claim["id"]]
        assert not await db.fetchall(
            "SELECT id FROM vec_claims WHERE embedding MATCH ? AND project_id = ? AND k = 1",
            [vector, "proj_default"],
        )
    finally:
        await db.close()


async def fresh(path):
    from rka.infra.database import Database

    db = Database(str(path))
    await db.connect()
    try:
        await db.initialize_schema()
        await db.initialize_phase2_schema()
    finally:
        await db.close()


def historical_process(source, command, path, env):
    subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "--historical-source",
            str(source),
            "--mode",
            command,
            "--database",
            str(path),
        ],
        env=env,
        cwd=source,
        check=True,
        timeout=120,
    )


def verify(root, report_dir):
    sys.path.insert(0, str(root))
    from rka.infra.sqlite_backup import backup_sqlite_database

    reports = []
    with tempfile.TemporaryDirectory(prefix="rka-historical-upgrade-") as temp:
        temporary = Path(temp)
        env = {
            **os.environ,
            "RKA_DATA_DIR": str(temporary / "data"),
            "XDG_CONFIG_HOME": str(temporary / "config"),
            "RKA_EMBEDDINGS_ENABLED": "false",
            "RKA_LLM_ENABLED": "false",
            "RKA_API_URL": "http://127.0.0.1:1",
            "HF_HUB_OFFLINE": "1",
        }
        fresh_db = temporary / "fresh.db"
        asyncio.run(fresh(fresh_db))
        expected_schema = schema(fresh_db)
        for version, commit in BASELINES.items():
            source = temporary / version
            source.mkdir()
            archive = subprocess.run(
                ["git", "-C", str(root), "archive", commit, "rka"], check=True, capture_output=True
            ).stdout
            with tarfile.open(fileobj=io.BytesIO(archive)) as bundle:
                bundle.extractall(source, filter="data")
            original = source / "synthetic.db"
            historical_process(source, "seed", original, env)
            before = snapshot(original)
            assert canonical_hash(before) == FIXTURE_HASHES[version], (
                f"{version}: synthetic fixture changed; review its provenance before accepting a new hash"
            )
            assert not before["foreign_keys"]
            backup = source / "backup.db"
            target = source / "upgraded.db"
            backup_sqlite_database(original, backup)
            backup_sqlite_database(backup, target)
            asyncio.run(upgrade(target))
            after = snapshot(target)
            for table in TABLES:
                projected = [
                    {key: row[key] for key in old}
                    for old, row in zip(before[table], after[table], strict=True)
                ]
                assert projected == before[table], (
                    f"{version}: historical values changed in {table}"
                )
            assert before["vectors"] == after["vectors"]
            for old, row in zip(before["metadata"], after["metadata"], strict=True):
                assert {key: row[key] for key in old} == old
            actual_schema = schema(target)
            differences = sorted(
                key
                for key in expected_schema.keys() | actual_schema.keys()
                if expected_schema.get(key) != actual_schema.get(key)
            )
            # Historical event-table rebuilds omit these two optimization
            # indexes on a first boot. schema.sql restores them on a later
            # boot. Pin the only allowed difference, including its exact SQL;
            # no missing constraint, FK, trigger or other index is tolerated.
            expected_restart_indexes = {
                "idx_events_caused_by": "CREATE INDEX idx_events_caused_by ON events(caused_by_event)",
                "idx_events_phase": "CREATE INDEX idx_events_phase ON events(phase)",
            }
            assert set(differences) == set(expected_restart_indexes), (
                f"{version}: unexpected fresh/upgraded schema differences: {differences}"
            )
            for name, sql in expected_restart_indexes.items():
                assert name not in expected_schema
                assert actual_schema[name] == {"type": "index", "table": "events", "sql": sql}
            restarted_fresh = source / "restarted-fresh.db"
            backup_sqlite_database(fresh_db, restarted_fresh)
            asyncio.run(fresh(restarted_fresh))
            assert schema(restarted_fresh) == actual_schema
            # Compare every migration name and the exact post-upgrade row state
            # after another full startup, not just run_migrations's return count.
            assert after["migrations"] == snapshot(fresh_db)["migrations"]
            asyncio.run(upgrade(target))
            assert snapshot(target) == after, f"{version}: non-idempotent second startup"
            recovery_report = source / "recovery.json"
            subprocess.run(
                [
                    sys.executable,
                    str(root / "scripts/core_recovery_smoke.py"),
                    "--source-db",
                    str(target),
                    "--project-id",
                    PROJECT,
                    "--report",
                    str(recovery_report),
                ],
                check=True,
                env=env,
                cwd=root,
                timeout=180,
            )
            recovery = json.loads(recovery_report.read_text())
            assert recovery["passed"]
            restored = source / "restored.db"
            shutil.copyfile(backup, restored)
            assert restored.read_bytes() == backup.read_bytes()
            # Reopen the restored database with the pinned old runtime.
            historical_process(source, "reopen", restored, env)
            assert snapshot(restored) == before, f"{version}: old-runtime rollback changed data"
            assert snapshot(original) == before, "Source fixture was modified during upgrade"
            report = {
                "version": version,
                "historical_commit": commit,
                "passed": True,
                "fixture_logical_sha256": canonical_hash(before),
                "fixture_file_sha256": hashlib.sha256(original.read_bytes()).hexdigest(),
                "source_archive_sha256": hashlib.sha256(archive).hexdigest(),
                "rows": {table: len(before[table]) for table in TABLES},
                "old_migrations": before["migrations"],
                "added_migrations": sorted(set(after["migrations"]) - set(before["migrations"])),
                "schema_differences": differences,
                "legacy_vectors_preserved": len(before["vectors"]),
                "idempotent": True,
                "old_runtime_restore": True,
                "pack_round_trip": recovery["passed"],
            }
            reports.append(report)
            print(f"Historical {version} upgrade / recovery passed.", flush=True)
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "historical-upgrades.json").write_text(
        json.dumps(
            {"schema": "rka-historical-upgrades/v1", "passed": True, "fixtures": reports}, indent=2
        )
        + "\n"
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report-dir", type=Path)
    parser.add_argument("--historical-source", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--database", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--mode", choices=["seed", "reopen"], help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.historical_source:
        sys.path.insert(0, str(args.historical_source))
        import rka

        assert Path(rka.__file__).resolve().is_relative_to(args.historical_source.resolve())
        assert rka.__version__ in BASELINES
        if args.mode == "seed":
            asyncio.run(seed(args.database))
        else:
            asyncio.run(fresh(args.database))
        return
    if not args.report_dir:
        parser.error("--report-dir is required")
    verify(ROOT, args.report_dir)


if __name__ == "__main__":
    main()
