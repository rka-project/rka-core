"""Tests for Phase 1 artifact/figure embeddings and backfill."""

from __future__ import annotations

import hashlib

import pytest
import pytest_asyncio

from rka.infra.database import Database
from rka.infra.embeddings import EmbeddingService
from rka.services.artifacts import ArtifactService
from rka.services.backfill import backfill_embeddings
from rka.services.search import SearchService


class DummyEmbeddings(EmbeddingService):
    """Deterministic local embedding stub for service tests."""

    def __init__(self, db: Database):
        super().__init__(model_name="dummy-embed", db=db)

    def _vector(self, text: str) -> list[float]:
        digest = hashlib.sha256(text.encode()).digest()
        return [digest[i % len(digest)] / 255.0 for i in range(self.dim)]

    async def embed(self, text: str) -> list[float]:
        return self._vector(text)

    async def embed_document(self, text: str) -> list[float]:
        return self._vector(text)

    async def embed_batch(self, texts, **kwargs):
        return [self._vector(text) for text in texts]


async def _ensure_project(db: Database, project_id: str, name: str) -> None:
    await db.execute(
        "INSERT OR IGNORE INTO projects (id, name, description, created_by) VALUES (?, ?, ?, ?)",
        [project_id, name, f"{name} description", "system"],
    )
    await db.execute(
        """INSERT OR IGNORE INTO project_states
           (project_id, project_name, project_description, phases_config, created_at, updated_at)
           VALUES (?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%SZ','now'), strftime('%Y-%m-%dT%H:%M:%SZ','now'))""",
        [project_id, name, f"{name} description", "[]"],
    )
    await db.commit()


@pytest_asyncio.fixture
async def dummy_embeddings(db: Database) -> DummyEmbeddings:
    return DummyEmbeddings(db=db)


class TestProjectScopedEmbeddingMetadata:
    @pytest.mark.asyncio
    async def test_embedding_metadata_is_project_scoped(
        self,
        db: Database,
        dummy_embeddings: DummyEmbeddings,
    ):
        await _ensure_project(db, "proj_alpha", "Alpha")
        await _ensure_project(db, "proj_beta", "Beta")

        await dummy_embeddings.store_embedding(
            "figure",
            "figure_alpha",
            "alpha figure text",
            project_id="proj_alpha",
        )
        await dummy_embeddings.store_embedding(
            "figure",
            "figure_beta",
            "beta figure text",
            project_id="proj_beta",
        )

        rows = await db.fetchall(
            """SELECT project_id, entity_type, entity_id
               FROM embedding_metadata
               WHERE entity_type = 'figure'
               ORDER BY project_id, entity_id""",
        )
        assert rows == [
            {"project_id": "proj_alpha", "entity_type": "figure", "entity_id": "figure_alpha"},
            {"project_id": "proj_beta", "entity_type": "figure", "entity_id": "figure_beta"},
        ]


class TestArtifactFigureSearch:
    @pytest.mark.asyncio
    async def test_artifact_and_figure_embeddings_are_searchable_by_project(
        self,
        db: Database,
        dummy_embeddings: DummyEmbeddings,
        tmp_path,
    ):
        await _ensure_project(db, "proj_alpha", "Alpha")
        await _ensure_project(db, "proj_beta", "Beta")

        from rka.infra.file_access import FileAccessPolicy
        alpha_artifacts = ArtifactService(db=db, llm=None, embeddings=dummy_embeddings, project_id="proj_alpha", file_policy=FileAccessPolicy([tmp_path]))
        beta_search = SearchService(db=db, embeddings=dummy_embeddings, project_id="proj_beta")
        alpha_search = SearchService(db=db, embeddings=dummy_embeddings, project_id="proj_alpha")

        image_path = tmp_path / "packet-loss.png"
        image_path.write_bytes(b"fake-png-data")

        artifact = await alpha_artifacts.register(
            filepath=str(image_path),
            created_by="system",
            metadata={"topic": "packet loss"},
        )
        figure = await alpha_artifacts._store_figure(
            artifact_id=artifact["id"],
            page=1,
            caption="Packet loss over time",
            caption_confidence=0.9,
            summary="Packet loss drops sharply after tuning.",
            claims=[{"claim": "Packet loss decreases after tuning", "confidence": 0.9}],
        )

        artifact_hits = await alpha_search.search("packet-loss.png", entity_types=["artifact"], limit=10)
        assert any(hit.entity_id == artifact["id"] for hit in artifact_hits)

        figure_hits = await alpha_search.search("packet loss", entity_types=["figure"], limit=10)
        assert any(hit.entity_id == figure["id"] for hit in figure_hits)

        beta_hits = await beta_search.search("packet loss", entity_types=["figure"], limit=10)
        assert all(hit.entity_id != figure["id"] for hit in beta_hits)

        if db.vec_available:
            stored = await db.fetchall(
                "SELECT id FROM vec_artifacts WHERE id IN (?, ?) ORDER BY id",
                [artifact["id"], figure["id"]],
            )
            assert [row["id"] for row in stored] == sorted([artifact["id"], figure["id"]])


class TestEmbeddingBackfill:
    @pytest.mark.asyncio
    async def test_backfill_embeddings_indexes_existing_artifacts_and_figures(
        self,
        db: Database,
        dummy_embeddings: DummyEmbeddings,
        tmp_path,
    ):
        await _ensure_project(db, "proj_alpha", "Alpha")
        artifact_path = tmp_path / "existing-figure.png"
        artifact_path.write_bytes(b"existing-data")

        await db.execute(
            """INSERT INTO artifacts
               (id, filename, filepath, filetype, mime, extraction_status, metadata, project_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                "artifact_existing",
                artifact_path.name,
                str(artifact_path),
                "png",
                "image/png",
                "complete",
                '{"topic":"latency"}',
                "proj_alpha",
            ],
        )
        await db.execute(
            """INSERT INTO figures
               (id, artifact_id, page, caption, summary, claims, project_id)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            [
                "figure_existing",
                "artifact_existing",
                2,
                "Latency by configuration",
                "Latency improves for the tuned configuration.",
                '[{"claim":"Latency improves after tuning","confidence":0.8}]',
                "proj_alpha",
            ],
        )
        await db.commit()

        from rka.services.embedding_index import reconcile_embedding_index
        from rka.services.embedding_jobs import EmbeddingJobs
        from rka.services.worker import EnrichmentWorker
        state = (await reconcile_embedding_index(
            db, space_signature=dummy_embeddings.space_signature,
            model_name=dummy_embeddings.model_name, dim=dummy_embeddings.dim,
        )).state
        dummy_embeddings.bind_index_generation(state.generation, space_signature=state.space_signature)
        job = await backfill_embeddings(
            db,
            dummy_embeddings,
            project_id="proj_alpha",
            batch_size=10,
            force=True,
        )
        assert job["status"] == "pending" and job["payload"]["project_id"] == "proj_alpha"
        assert job["payload"]["entity_types"] == ["claim", "artifact", "figure"]
        assert await EnrichmentWorker(db=db, embeddings=dummy_embeddings).run_once()
        status = await EmbeddingJobs(db).status(job["id"])
        assert status["state"] == "complete" and status["processed"] == 2

        rows = await db.fetchall(
            """SELECT entity_type, entity_id
               FROM embedding_metadata
               WHERE project_id = 'proj_alpha'
               ORDER BY entity_type, entity_id""",
        )
        assert rows == [
            {"entity_type": "artifact", "entity_id": "artifact_existing"},
            {"entity_type": "figure", "entity_id": "figure_existing"},
        ]
