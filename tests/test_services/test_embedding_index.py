"""Crash/restart and concurrency gates for durable embedding generations."""

from __future__ import annotations

import struct

import pytest

from rka.infra.embeddings import EmbeddingService
from rka.services.embedding_index import (
    EmbeddingGenerationMismatch,
    embedding_space_signature,
    finish_embedding_transition,
    get_embedding_index_state,
    legacy_index_adoption_safe,
    reconcile_embedding_index,
)
from rka.services.embedding_reshape import current_vec_table_dim


class _Backend:
    def __init__(self, model_name: str, dim: int = 768) -> None:
        self._model_name = model_name
        self._dim = dim

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def dim(self) -> int:
        return self._dim

    async def embed(self, text: str, *, is_query: bool = False) -> list[float]:
        return [0.0] * self._dim

    async def embed_batch(
        self,
        texts: list[str],
        *,
        is_query: bool = False,
    ) -> list[list[float]]:
        return [[0.0] * self._dim for _ in texts]


class _FlakyBackend(_Backend):
    def __init__(self) -> None:
        super().__init__("flaky", dim=4)
        self.fail = True

    async def embed(self, text: str, *, is_query: bool = False) -> list[float]:
        if self.fail:
            raise RuntimeError("backend offline")
        return [0.0] * self.dim


def _config(
    model: str,
    *,
    dim: int = 768,
    document_template: str = "{text}",
) -> dict:
    return {
        "backend": "openai_compat",
        "config": {
            "base_url": "http://127.0.0.1:1",
            "model": model,
            "dim": dim,
            "document_template": document_template,
        },
    }


@pytest.mark.asyncio
async def test_runtime_availability_recovers_after_successful_probe() -> None:
    backend = _FlakyBackend()
    service = EmbeddingService(backend=backend)

    with pytest.raises(RuntimeError, match="offline"):
        await service.embed("query")
    assert service.runtime_available is False
    assert service.runtime_error_code == "embedding_backend_unavailable"

    backend.fail = False
    assert await service.embed("query") == [0.0] * 4
    assert service.runtime_available is True
    assert service.runtime_error_code is None


async def _seed_claim(db, claim_id: str = "clm_generation") -> None:
    await db.execute(
        """INSERT INTO journal (id, type, content, source)
           VALUES ('jrn_generation', 'note', 'source', 'pi')"""
    )
    await db.execute(
        """INSERT INTO claims
           (id, source_entry_id, claim_type, content, embedding_pending)
           VALUES (?, 'jrn_generation', 'observation', 'claim text', 0)""",
        [claim_id],
    )


@pytest.mark.asyncio
async def test_empty_legacy_index_is_adopted_without_rebuild(db) -> None:
    cfg = _config("model-a")
    result = await reconcile_embedding_index(
        db,
        space_signature=embedding_space_signature(cfg),
        model_name="model-a",
        dim=768,
    )
    assert result.state.generation == 1
    assert result.state.status == "ready"
    assert result.transitioned is False


@pytest.mark.asyncio
async def test_online_recovery_reshapes_matching_offline_generation(db) -> None:
    """A generation created without sqlite-vec must repair its old schema later."""
    cfg = _config("model-1024", dim=1024)
    db._vec_loaded = False
    try:
        offline = await reconcile_embedding_index(
            db,
            space_signature=embedding_space_signature(cfg),
            model_name="model-1024",
            dim=1024,
        )
    finally:
        db._vec_loaded = True

    assert offline.state.status == "reindexing"
    assert offline.transitioned is True
    assert await current_vec_table_dim(db, "vec_claims") == 768

    resumed = await reconcile_embedding_index(
        db,
        space_signature=embedding_space_signature(cfg),
        model_name="model-1024",
        dim=1024,
    )

    assert resumed.state.generation == offline.state.generation
    assert resumed.state.status == "reindexing"
    assert resumed.resumed is True
    assert all(did_reshape for did_reshape, _pending in resumed.reshape.values())
    assert {
        await current_vec_table_dim(db, table_name)
        for table_name in resumed.reshape
    } == {1024}


@pytest.mark.asyncio
async def test_orphan_legacy_vector_forces_clean_rebuild(db) -> None:
    cfg = _config("model-a")
    blob = struct.pack("768f", *([0.0] * 768))
    await db.execute(
        "INSERT INTO vec_claims (id, project_id, embedding) VALUES (?, ?, ?)",
        ["clm_orphan", "proj_default", blob],
    )

    result = await reconcile_embedding_index(
        db,
        space_signature=embedding_space_signature(cfg),
        model_name="model-a",
        dim=768,
    )

    assert result.transitioned is True
    assert result.state.status == "reindexing"
    assert await db.fetchone("SELECT id FROM vec_claims LIMIT 1") is None


@pytest.mark.asyncio
async def test_legacy_vector_metadata_pair_without_source_forces_rebuild(db) -> None:
    cfg = _config("model-a")
    blob = struct.pack("768f", *([0.0] * 768))
    await db.execute(
        "INSERT INTO vec_claims (id, project_id, embedding) VALUES (?, ?, ?)",
        ["clm_ghost", "proj_default", blob],
    )
    await db.execute(
        """INSERT INTO embedding_metadata
           (project_id, entity_type, entity_id, content_hash, model_name, dimensions)
           VALUES ('proj_default', 'claim', 'clm_ghost', 'hash', 'model-a', 768)"""
    )

    result = await reconcile_embedding_index(
        db,
        space_signature=embedding_space_signature(cfg),
        model_name="model-a",
        dim=768,
    )

    assert result.transitioned is True
    assert await db.fetchone(
        "SELECT id FROM vec_claims WHERE id = 'clm_ghost'"
    ) is None
    assert await db.fetchone(
        "SELECT entity_id FROM embedding_metadata WHERE entity_id = 'clm_ghost'"
    ) is None


@pytest.mark.asyncio
async def test_stale_hash_legacy_pair_is_invalidated_during_adoption(
    db,
) -> None:
    cfg = _config("model-a")
    await _seed_claim(db)
    blob = struct.pack("768f", *([0.0] * 768))
    await db.execute(
        "INSERT INTO vec_claims (id, project_id, embedding) VALUES (?, ?, ?)",
        ["clm_generation", "proj_default", blob],
    )
    await db.execute(
        """INSERT INTO embedding_metadata
           (project_id, entity_type, entity_id, content_hash, model_name, dimensions)
           VALUES ('proj_default', 'claim', 'clm_generation', 'hash', 'model-a', 768)"""
    )

    result = await reconcile_embedding_index(
        db,
        space_signature=embedding_space_signature(cfg),
        model_name="model-a",
        dim=768,
    )

    assert result.transitioned is False
    assert result.resumed is True
    assert result.state.status == "reindexing"
    assert await db.fetchone(
        "SELECT id FROM vec_claims WHERE id = 'clm_generation'"
    ) is None
    assert await db.fetchone(
        "SELECT entity_id FROM embedding_metadata WHERE entity_id = 'clm_generation'"
    ) is None
    assert (await db.fetchone("SELECT embedding_pending FROM claims WHERE id = 'clm_generation'"))[
        "embedding_pending"
    ] == 1


@pytest.mark.asyncio
async def test_hash_verified_partial_legacy_index_is_preserved_for_repair(db) -> None:
    cfg = _config("model-a")
    await _seed_claim(db)
    blob = struct.pack("768f", *([0.0] * 768))
    await db.execute(
        "INSERT INTO vec_claims (id, project_id, embedding) VALUES (?, ?, ?)",
        ["clm_generation", "proj_default", blob],
    )
    await db.execute(
        """INSERT INTO embedding_metadata
           (project_id, entity_type, entity_id, content_hash, model_name, dimensions)
           VALUES ('proj_default', 'claim', 'clm_generation', ?, 'model-a', 768)""",
        [EmbeddingService.content_hash("claim text")],
    )

    result = await reconcile_embedding_index(
        db,
        space_signature=embedding_space_signature(cfg),
        model_name="model-a",
        dim=768,
    )

    assert result.transitioned is False
    assert result.resumed is True
    assert result.state.status == "reindexing"
    assert await db.fetchone(
        "SELECT id FROM vec_claims WHERE id = 'clm_generation'"
    ) == {"id": "clm_generation"}


def test_custom_document_contract_cannot_adopt_legacy_metadata() -> None:
    assert legacy_index_adoption_safe(_config("model-a")) is True
    assert (
        legacy_index_adoption_safe(
            _config("model-a", document_template="Document: {text}")
        )
        is False
    )


@pytest.mark.asyncio
async def test_same_dim_space_transition_clears_claim_vector_and_metadata(db) -> None:
    cfg_a = _config("model-a")
    initial = await reconcile_embedding_index(
        db,
        space_signature=embedding_space_signature(cfg_a),
        model_name="model-a",
        dim=768,
    )
    await _seed_claim(db)
    blob = struct.pack("768f", *([0.0] * 768))
    await db.execute(
        "INSERT INTO vec_claims (id, project_id, embedding) VALUES (?, ?, ?)",
        ["clm_generation", "proj_default", blob],
    )
    await db.execute(
        """INSERT INTO embedding_metadata
           (project_id, entity_type, entity_id, content_hash, model_name, dimensions)
           VALUES ('proj_default', 'claim', 'clm_generation', 'hash', 'model-a', 768)"""
    )

    cfg_b = _config("model-b")
    changed = await reconcile_embedding_index(
        db,
        space_signature=embedding_space_signature(cfg_b),
        model_name="model-b",
        dim=768,
    )

    assert changed.transitioned is True
    assert changed.state.generation == initial.state.generation + 1
    assert changed.state.status == "reindexing"
    assert await db.fetchone("SELECT id FROM vec_claims LIMIT 1") is None
    assert await db.fetchone("SELECT entity_id FROM embedding_metadata LIMIT 1") is None
    assert await db.fetchone(
        "SELECT embedding_pending FROM claims WHERE id = 'clm_generation'"
    ) == {"embedding_pending": 1}


@pytest.mark.asyncio
async def test_stale_generation_write_is_rejected(db) -> None:
    cfg_a = _config("model-a")
    first = await reconcile_embedding_index(
        db,
        space_signature=embedding_space_signature(cfg_a),
        model_name="model-a",
        dim=768,
    )
    stale = EmbeddingService(db=db, backend=_Backend("model-a"))
    stale.space_signature = first.state.space_signature
    stale.bind_index_generation(first.state.generation)
    await _seed_claim(db)

    cfg_b = _config("model-b")
    await reconcile_embedding_index(
        db,
        space_signature=embedding_space_signature(cfg_b),
        model_name="model-b",
        dim=768,
    )

    with pytest.raises(EmbeddingGenerationMismatch):
        await stale.store_embedding(
            "claim",
            "clm_generation",
            "claim text",
            embedding=[0.0] * 768,
        )
    assert await db.fetchone("SELECT id FROM vec_claims LIMIT 1") is None


@pytest.mark.asyncio
async def test_restart_resumes_generation_without_clearing_partial_progress(db) -> None:
    cfg_a = _config("model-a")
    await reconcile_embedding_index(
        db,
        space_signature=embedding_space_signature(cfg_a),
        model_name="model-a",
        dim=768,
    )
    cfg_b = _config("model-b")
    changed = await reconcile_embedding_index(
        db,
        space_signature=embedding_space_signature(cfg_b),
        model_name="model-b",
        dim=768,
    )
    await _seed_claim(db)
    service = EmbeddingService(db=db, backend=_Backend("model-b"))
    service.space_signature = changed.state.space_signature
    service.bind_index_generation(changed.state.generation)
    await service.store_embedding(
        "claim",
        "clm_generation",
        "claim text",
        embedding=[0.0] * 768,
    )

    resumed = await reconcile_embedding_index(
        db,
        space_signature=embedding_space_signature(cfg_b),
        model_name="model-b",
        dim=768,
    )
    assert resumed.resumed is True
    assert resumed.state.generation == changed.state.generation
    assert await db.fetchone(
        "SELECT id FROM vec_claims WHERE id = 'clm_generation'"
    ) == {"id": "clm_generation"}


@pytest.mark.asyncio
async def test_finish_refuses_ready_while_eligible_entity_is_pending(db) -> None:
    cfg_a = _config("model-a")
    await reconcile_embedding_index(
        db,
        space_signature=embedding_space_signature(cfg_a),
        model_name="model-a",
        dim=768,
    )
    cfg_b = _config("model-b")
    changed = await reconcile_embedding_index(
        db,
        space_signature=embedding_space_signature(cfg_b),
        model_name="model-b",
        dim=768,
    )
    await _seed_claim(db)

    assert await finish_embedding_transition(
        db,
        generation=changed.state.generation,
        success=True,
    )
    state = await get_embedding_index_state(db)
    assert state is not None
    assert state.status == "failed"
