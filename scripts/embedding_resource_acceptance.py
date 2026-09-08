"""Opt-in real-model acceptance on an explicitly disposable data directory.

Run seed/worker/status in isolated containers with a NEW named volume. Never
point this script at an existing research store. No host ports are necessary.
"""

import argparse
import asyncio
import json
import os
from pathlib import Path
import threading
import time

from rka.infra.database import Database
from rka.infra.embeddings import EmbeddingService
from rka.services.embedding_config import EmbeddingConfig, EmbeddingConfigService
from rka.services.embedding_index import reconcile_embedding_index, embedding_space_signature
from rka.services.embedding_jobs import EmbeddingJobs
from rka.services.worker import EnrichmentWorker


def cgroup_memory():
    root = Path("/sys/fs/cgroup")
    if not (root / "memory.peak").exists():
        return None
    return {"peak_bytes": int((root / "memory.peak").read_text()),
            "events": dict(line.split() for line in (root / "memory.events").read_text().splitlines())}


async def run(args):
    folder = Path(args.data_dir)
    path = folder / "rka.db"
    marker = folder / "synthetic-embedding-acceptance.json"
    if args.operation == "seed":
        if path.exists() or marker.exists():
            raise RuntimeError("seed requires a new disposable store")
        folder.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps({"synthetic": True, "version": 1}))
    elif not marker.is_file():
        raise RuntimeError("refusing a store without synthetic acceptance marker")
    db = Database(str(path))
    await db.connect()
    try:
        await db.initialize_schema()
        await db.initialize_phase2_schema()
        if args.operation == "seed":
            config = EmbeddingConfig(backend="fastembed", config={
                "model_name": "nomic-ai/nomic-embed-text-v1.5", "dim": 768,
                "cache_dir": str(folder / "model-cache"), "threads": 2,
            })
            EmbeddingConfigService(folder).save_config(config, "system")
            # #158-sized row distribution and ~4 MB composed corpus. One long
            # poison row must be rejected without changing its canonical text.
            text = ("Synthetic research observation: measured local temperature and compared independent sensor readings. " * 12)[:1100]
            async with db.transaction():
                for index in range(1747):
                    content = "x" * 20535 if index == 0 else f"Synthetic observation {index}. {text}"
                    await db.execute("INSERT INTO journal (id,type,content,source,project_id) VALUES (?, 'note', ?, 'pi', 'proj_default')", [f"jrn_resource_{index:05d}", content])
                for index in range(687):
                    await db.execute("INSERT INTO claims (id,source_entry_id,claim_type,content,project_id) VALUES (?, 'jrn_resource_00001', 'observation', ?, 'proj_default')", [f"clm_resource_{index:05d}", text])
                for index in range(507):
                    await db.execute("INSERT INTO decisions (id,phase,question,decided_by,project_id) VALUES (?, 'test', ?, 'pi', 'proj_default')", [f"dec_resource_{index:05d}", text])
                for index in range(527):
                    await db.execute("INSERT INTO literature (id,title,abstract,project_id) VALUES (?, 'Synthetic reference', ?, 'proj_default')", [f"lit_resource_{index:05d}", text])
                for index in range(145):
                    await db.execute("INSERT INTO missions (id,phase,objective,project_id) VALUES (?, 'test', ?, 'proj_default')", [f"mis_resource_{index:05d}", text])
            state = await reconcile_embedding_index(db, space_signature=embedding_space_signature(config), model_name=config.config["model_name"], dim=768)
            service = EmbeddingService.from_config(config.model_dump(), db=db)
            service.bind_index_generation(state.state.generation)
            await EmbeddingJobs(db).request(service)
            print(json.dumps({"synthetic_rows": 3613, "poison_rows": 1}), flush=True)
        elif args.operation == "worker":
            stop = threading.Event()
            peak = [0]
            def sample():
                while not stop.wait(0.05):
                    total = 0
                    for status in Path("/proc").glob("[0-9]*/status"):
                        try:
                            for line in status.read_text().splitlines():
                                if line.startswith("VmRSS:"):
                                    total += int(line.split()[1]) * 1024
                        except (OSError, ValueError):
                            pass
                    peak[0] = max(peak[0], total)
            sampler = threading.Thread(target=sample, daemon=True)
            sampler.start()
            started = time.monotonic()
            try:
                worker = EnrichmentWorker.boot(db=db, data_dir=folder, lease_seconds=15, worker_id=f"acceptance-{os.getpid()}")
                await worker.run_once()
            finally:
                stop.set()
                sampler.join()
                result = {"peak_process_tree_rss_bytes": peak[0], "elapsed_seconds": time.monotonic() - started,
                          "metadata_rows": (await db.fetchone("SELECT count(*) n FROM embedding_metadata"))["n"],
                          "cgroup_memory": cgroup_memory()}
                (folder / "worker-result.json").write_text(json.dumps(result))
                print(json.dumps(result), flush=True)
        elif args.operation == "boundary":
            # The old 8 KiB input is rejected before loading the model. Exercise
            # one query and the full two-document native padding ceiling.
            from rka.infra.embedding_backends.fastembed import FastEmbedBackend
            backend = FastEmbedBackend(cache_dir=str(folder / "model-cache"))
            started = time.monotonic()
            from rka.infra.embedding_resources import EmbeddingInputLimit
            try:
                await backend.embed("a " * 4085, is_query=True)
            except EmbeddingInputLimit:
                pass
            else:
                raise AssertionError("old unsafe boundary must be rejected")
            vector = await backend.embed("a " * 1017, is_query=True)
            batch = await backend.embed_batch(["a " * 1015, "b " * 1015])
            print(json.dumps({"dimensions": len(vector), "batch_dimensions": [len(v) for v in batch],
                              "old_boundary_rejected": True, "input_bytes": 2034, "elapsed_seconds": time.monotonic() - started,
                              "cgroup_memory": cgroup_memory()}), flush=True)
        else:
            print(json.dumps({"metadata": await db.fetchone("SELECT count(*) n FROM embedding_metadata"),
                              "state": await db.fetchone("SELECT generation,status FROM embedding_index_state"),
                              "jobs": await db.fetchall("SELECT status,attempts,last_error FROM jobs WHERE job_type LIKE 'embedding_backfill%' ORDER BY rowid")}), flush=True)
    finally:
        await db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["seed", "worker", "status", "boundary"])
    parser.add_argument("--data-dir", required=True)
    asyncio.run(run(parser.parse_args()))
