"""Synthetic #158 upgrade fixture; never use with a research database.

Run ``seed`` in a NEW disposable volume with PYTHONPATH pointing at archived
2.9.0 source (0dc1842ca771962e6e07676b6de9a6abecd74ac9). Run ``verify`` with
the installed current release. Neither operation starts a provider or queues
work: the current API must perform the upgrade and automatic job admission.

Legacy vectors are placeholders with deliberately stale hashes. This forces
real recomputation of all eligible records, not a semantic-vector comparison
or a reproduction of the reporter's unavailable personal database.
"""

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import struct


TABLES = ("journal", "claims", "decisions", "literature", "missions", "artifacts", "figures")
POISON_ID = "jrn_resource_00000"
MARKER = "synthetic-embedding-acceptance.json"
FIXTURE = "issue158-legacy-v1"


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


async def snapshot(db, columns=None):
    if columns is None:
        columns = {
            table: [
                row["name"]
                for row in await db.fetchall(f'PRAGMA table_info("{table}")')
                if row["name"] != "embedding_pending"
            ]
            for table in TABLES
        }
    result = {}
    for table in TABLES:
        projection = ",".join('"' + name.replace('"', '""') + '"' for name in columns[table])
        result[table] = await db.fetchall(f'SELECT {projection} FROM "{table}" ORDER BY id')
    return {
        "columns": columns,
        "rows": {t: len(rows) for t, rows in result.items()},
        "sha256": digest(result),
    }


async def run(args):
    import rka
    from rka.infra.database import Database

    folder = Path(args.data_dir)
    path = folder / "rka.db"
    marker = folder / MARKER
    if args.operation == "seed":
        if (
            args.legacy_root is None
            or Path(rka.__file__).resolve().parent != Path(args.legacy_root).resolve() / "rka"
        ):
            raise RuntimeError("seed requires explicit archived source and matching imported rka")
        protected = (
            path,
            marker,
            folder / "embedding_config.json",
            folder / "synthetic-bge-config.json",
        )
        if any(p.exists() or p.is_symlink() for p in protected):
            raise RuntimeError("seed requires a NEW disposable store")
        if rka.__version__ != "2.9.0":
            raise RuntimeError("seed requires historical Core version 2.9.0")
        folder.mkdir(parents=True, exist_ok=True)
        baseline = None
    else:
        baseline = json.loads(marker.read_text())
        if baseline.get("fixture") != FIXTURE or not path.is_file():
            raise RuntimeError("refusing a store without the exact synthetic fixture marker")

    db = Database(str(path))
    await db.connect()
    try:
        if args.operation == "seed":
            from rka.services.embedding_config import EmbeddingConfig, EmbeddingConfigService

            await db.initialize_schema()
            await db.initialize_phase2_schema()
            assert db._vec_loaded, "real sqlite-vec is required"
            assert not await db.fetchone(
                "SELECT name FROM sqlite_master WHERE name='embedding_index_state'"
            )
            text = (
                "Synthetic research observation: measured local temperature and compared independent sensor readings. "
                * 12
            )[:1100]
            async with db.transaction():
                for i in range(1747):
                    content = "x" * 20535 if i == 0 else f"Synthetic observation {i}. {text}"
                    await db.execute(
                        "INSERT INTO journal (id,type,content,source,project_id) VALUES (?, 'note', ?, 'pi', 'proj_default')",
                        [f"jrn_resource_{i:05d}", content],
                    )
                for i in range(687):
                    await db.execute(
                        "INSERT INTO claims (id,source_entry_id,claim_type,content,project_id) VALUES (?, 'jrn_resource_00001', 'observation', ?, 'proj_default')",
                        [f"clm_resource_{i:05d}", text],
                    )
                for i in range(507):
                    await db.execute(
                        "INSERT INTO decisions (id,phase,question,decided_by,project_id) VALUES (?, 'test', ?, 'pi', 'proj_default')",
                        [f"dec_resource_{i:05d}", text],
                    )
                for i in range(527):
                    await db.execute(
                        "INSERT INTO literature (id,title,abstract,project_id) VALUES (?, 'Synthetic reference', ?, 'proj_default')",
                        [f"lit_resource_{i:05d}", text],
                    )
                for i in range(145):
                    await db.execute(
                        "INSERT INTO missions (id,phase,objective,project_id) VALUES (?, 'test', ?, 'proj_default')",
                        [f"mis_resource_{i:05d}", text],
                    )
                vector = struct.pack("<768f", 1.0, *([0.0] * 767))
                for kind, count in (("claim", 687), ("journal", 16)):
                    table = "vec_claims" if kind == "claim" else "vec_journal"
                    for i in range(count):
                        entity_id = (
                            f"clm_resource_{i:05d}"
                            if kind == "claim"
                            else f"jrn_resource_{i + 1:05d}"
                        )
                        await db.execute(
                            f"INSERT INTO {table}(id,project_id,embedding) VALUES (?, 'proj_default', ?)",
                            [entity_id, vector],
                        )
                        await db.execute(
                            "INSERT INTO embedding_metadata(project_id,entity_type,entity_id,content_hash,model_name,dimensions) VALUES ('proj_default',?,?,?,'nomic-ai/nomic-embed-text-v1.5',768)",
                            [kind, entity_id, "0" * 64],
                        )
            config = EmbeddingConfig(
                backend="fastembed",
                config={
                    "model_name": "nomic-ai/nomic-embed-text-v1.5",
                    "dim": 768,
                    "cache_dir": str(folder / "model-cache"),
                    "threads": 2,
                },
            )
            EmbeddingConfigService(folder).save_config(config, "system")
            baseline = {
                "synthetic": True,
                "fixture": FIXTURE,
                "legacy_source": str(rka.__file__),
                "canonical": await snapshot(db),
                "legacy_metadata_rows": 703,
            }
            assert (await db.fetchone("SELECT count(*) n FROM embedding_metadata"))["n"] == 703
            marker.write_text(json.dumps(baseline, indent=2))
            candidate = {
                "backend": "fastembed",
                "config": {
                    "model_name": "BAAI/bge-small-en-v1.5",
                    "dim": 384,
                    "cache_dir": str(folder / "model-cache"),
                    "threads": 2,
                },
            }
            (folder / "synthetic-bge-config.json").write_text(json.dumps(candidate))
            print(json.dumps(baseline), flush=True)
        else:
            from rka.services.embedding_inspection import inspect_embedding_index

            actual = await snapshot(db, baseline["canonical"]["columns"])
            assert actual == baseline["canonical"], "canonical research records changed"
            assert (await db.fetchone("SELECT content FROM journal WHERE id=?", [POISON_ID]))[
                "content"
            ] == "x" * 20535
            assert not await db.fetchone(
                "SELECT 1 FROM embedding_metadata WHERE entity_id=?", [POISON_ID]
            )
            assert (await db.fetchone("PRAGMA integrity_check"))["integrity_check"] == "ok"
            assert not await db.fetchall("PRAGMA foreign_key_check")
            metadata = await db.fetchone("SELECT count(*) n FROM embedding_metadata")
            state = await db.fetchone(
                "SELECT generation,status,dimensions FROM embedding_index_state"
            )
            if args.expected_metadata is not None:
                assert metadata["n"] == args.expected_metadata, metadata
            if args.expected_dimensions is not None:
                assert state["dimensions"] == args.expected_dimensions, state
                assert not await db.fetchone(
                    "SELECT 1 FROM embedding_metadata WHERE dimensions<>?",
                    [args.expected_dimensions],
                )
            inspection = await inspect_embedding_index(data_dir=folder)
            assert inspection["generation_matches_config"]
            assert inspection["structural"]["coherent"], inspection["structural"]
            assert inspection["totals"]["reusable"] == metadata["n"], inspection["totals"]
            assert state["status"] != "ready", "unembedded poison must not certify global readiness"
            print(
                json.dumps(
                    {
                        "canonical": actual,
                        "metadata": metadata,
                        "state": state,
                        "poison_unchanged_and_unembedded": True,
                        "integrity": "ok",
                        "inspection_totals": inspection["totals"],
                        "physical_pairs_coherent": True,
                        "rka_source": str(rka.__file__),
                    }
                ),
                flush=True,
            )
    finally:
        await db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["seed", "verify"])
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--legacy-root")
    parser.add_argument("--expected-metadata", type=int)
    parser.add_argument("--expected-dimensions", type=int)
    asyncio.run(run(parser.parse_args()))
