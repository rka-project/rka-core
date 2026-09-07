"""Legacy CLI only queues against persisted config and its requested project."""

import asyncio
from pathlib import Path

from click.testing import CliRunner
import pytest

from rka.cli import main
from rka.services.embedding_config import EmbeddingConfig, EmbeddingConfigService
from tests.test_services.test_durable_embedding_backfill import setup_index


async def invoke(db, monkeypatch, args, *, saved=True):
    service, calls = await setup_index(db, count=0)
    data_dir = Path(db.db_path).parent
    monkeypatch.setenv("RKA_DATA_DIR", str(data_dir))
    monkeypatch.setenv("RKA_DB_PATH", db.db_path)
    monkeypatch.setenv("RKA_EMBEDDINGS_ENABLED", "true")
    monkeypatch.setenv("RKA_EMBEDDING_MODEL", "wrong-env-default")
    if saved:
        cfg = EmbeddingConfigService(config_dir=data_dir).save_config(
            EmbeddingConfig(
                backend="openai_compat",
                config={
                    "base_url": "http://isolated.test",
                    "model": "synthetic",
                    "dim": 2,
                },
            ),
            actor="system",
        )
        from rka.services.embedding_index import (
            embedding_space_signature,
            reconcile_embedding_index,
        )

        signature = embedding_space_signature(cfg, dimensions=2)
        state = (
            await reconcile_embedding_index(
                db,
                space_signature=signature,
                model_name=service.model_name,
                dim=2,
            )
        ).state
        service.bind_index_generation(state.generation, space_signature=signature)
    result = await asyncio.to_thread(CliRunner().invoke, main, ["backfill-embeddings", *args])
    return result, service, calls


@pytest.mark.asyncio
async def test_cli_loads_saved_model_and_queues_force_without_inference(db, monkeypatch):
    from rka.services.jobs import JobQueue

    result, _, calls = await invoke(
        db, monkeypatch, ["--force", "--no-figures", "--no-artifacts", "--batch-size", "1"]
    )
    assert result.exit_code == 0, (result.output, result.exception)
    assert "queued" in result.output and "complete" not in result.output
    row = await db.fetchone("SELECT id FROM jobs WHERE job_type='embedding_backfill_v2'")
    job = await JobQueue(db).get(row["id"])
    assert job["payload"]["model_name"] == "synthetic"
    assert job["payload"]["entity_types"] == ["claim"]
    assert job["payload"]["project_id"] == "proj_default"
    assert job["payload"]["check_hashes"] is True
    assert job["payload"]["version"] == 2
    assert job["payload"]["force"] and job["payload"]["batch_size"] == 1
    assert job["status"] == "pending" and calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "args",
    [
        ["--project", "unknown"],
        ["--batch-size", "0"],
        ["--batch-size", "129"],
        ["--no-artifacts", "--no-figures", "--no-claims"],
    ],
)
async def test_invalid_cli_request_does_not_enqueue(db, monkeypatch, args):
    result, _, calls = await invoke(db, monkeypatch, args)
    assert result.exit_code != 0
    assert not await db.fetchone("SELECT id FROM jobs")
    assert calls == []


@pytest.mark.asyncio
async def test_missing_saved_config_cannot_fall_back_to_env(db, monkeypatch):
    result, _, calls = await invoke(db, monkeypatch, [], saved=False)
    assert result.exit_code != 0 and "persisted" in result.output
    assert not await db.fetchone("SELECT id FROM jobs")
    assert calls == []
