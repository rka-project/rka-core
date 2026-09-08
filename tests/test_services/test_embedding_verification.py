"""Per-row legacy adoption proofs; isolated SQLite and synthetic vectors only."""

import struct
from dataclasses import asdict

import pytest

from rka.infra.embedding_documents import embedding_content_hash
from rka.services.embedding_index import get_embedding_index_state, reconcile_embedding_index
from rka.services.embedding_verification import iter_stored_document_checks


async def seed_legacy_journal(db, *, stale=False):
    await db.execute("INSERT INTO projects (id, name) VALUES ('proj_second', 'Synthetic second project')")
    for index in range(19):
        entity_id = f"jrn_proof_{index:03d}"
        project_id = "proj_second" if index % 2 else "proj_default"
        content = f"private synthetic document {index}"
        await db.execute(
            "INSERT INTO journal (id, type, content, source, project_id) VALUES (?, 'note', ?, 'pi', ?)",
            [entity_id, content, project_id],
        )
        await db.execute(
            "INSERT INTO vec_journal (id, project_id, embedding) VALUES (?, ?, ?)",
            [entity_id, project_id, struct.pack("768f", *([index / 20] * 768))],
        )
        await db.execute(
            """INSERT INTO embedding_metadata
               (project_id, entity_type, entity_id, content_hash, model_name, dimensions)
               VALUES (?, 'journal', ?, ?, 'synthetic', 768)""",
            [project_id, entity_id, "stale" if stale and index == 10 else embedding_content_hash(content)],
        )
    await db.commit()


async def snapshot(db):
    return (
        await db.fetchall("SELECT * FROM journal ORDER BY id"),
        await db.fetchall("SELECT * FROM embedding_metadata ORDER BY entity_id"),
        await db.fetchall("SELECT id, project_id, embedding FROM vec_journal ORDER BY id"),
        await get_embedding_index_state(db),
    )


@pytest.mark.asyncio
async def test_per_row_checks_are_paged_read_only_and_contain_no_documents(db):
    await seed_legacy_journal(db, stale=True)
    before = await snapshot(db)
    pages = []

    class ReadOnlyPages:
        async def fetchall(self, sql, params):
            assert sql.lstrip().startswith("SELECT")
            assert "LIMIT ?" in sql and "ORDER BY s.id" in sql
            rows = await db.fetchall(sql, params)
            assert len(rows) <= 8
            pages.append(len(rows))
            return rows

    checks = [check async for check in iter_stored_document_checks(
        ReadOnlyPages(), model_name="synthetic", dimensions=768,
    )]
    assert len(checks) == 19 and pages.count(8) == 2 and 3 in pages
    assert sum(check.status == "verified" for check in checks) == 18
    assert [check.entity_id for check in checks if check.status != "verified"] == ["jrn_proof_010"]
    assert "private synthetic document" not in str([asdict(check) for check in checks])
    assert await snapshot(db) == before


@pytest.mark.asyncio
async def test_same_space_adoption_only_invalidates_drifting_row_across_projects(db):
    await seed_legacy_journal(db, stale=True)
    before_sources, before_meta, before_vectors, _ = await snapshot(db)
    result = await reconcile_embedding_index(db, space_signature="synthetic-space", model_name="synthetic", dim=768)
    assert not result.transitioned and result.resumed and result.state.status == "reindexing"
    sources, meta, vectors, _ = await snapshot(db)
    assert sources == before_sources
    assert meta == [row for row in before_meta if row["entity_id"] != "jrn_proof_010"]
    assert vectors == [row for row in before_vectors if row["id"] != "jrn_proof_010"]
    repeated = await reconcile_embedding_index(db, space_signature="synthetic-space", model_name="synthetic", dim=768)
    assert repeated.state.generation == 1 and not repeated.transitioned
    assert (await snapshot(db))[:3] == (sources, meta, vectors)


@pytest.mark.asyncio
async def test_legacy_invalidation_and_state_adoption_roll_back_together(db, monkeypatch):
    await seed_legacy_journal(db, stale=True)
    before = await snapshot(db)
    execute = db.execute

    async def fail_state_insert(sql, params=None):
        if "INSERT INTO embedding_index_state" in sql:
            assert await db.fetchone("SELECT id FROM vec_journal WHERE id='jrn_proof_010'") is None
            raise RuntimeError("synthetic interruption before adoption")
        return await execute(sql, params)

    monkeypatch.setattr(db, "execute", fail_state_insert)
    with pytest.raises(RuntimeError, match="synthetic interruption"):
        await reconcile_embedding_index(db, space_signature="synthetic-space", model_name="synthetic", dim=768)
    assert await snapshot(db) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("identity", ["different-model", "unprovable-policy", "unknown-entity", "unknown-vector"])
async def test_unprovable_identity_never_reuses_legacy_rows(db, identity):
    await seed_legacy_journal(db)
    if identity == "unknown-entity":
        await db.execute(
            """INSERT INTO embedding_metadata
               (project_id, entity_type, entity_id, content_hash, model_name, dimensions)
               VALUES ('proj_default', 'unknown', 'unknown_id', 'irrelevant', 'synthetic', 768)"""
        )
    if identity == "unknown-vector":
        await db.execute(
            "INSERT INTO vec_artifacts (id, project_id, entity_type, embedding) VALUES ('unknown_id', 'proj_default', 'unknown', ?)",
            [struct.pack("768f", *([0.0] * 768))],
        )
    result = await reconcile_embedding_index(
        db, space_signature="new-space",
        model_name="different" if identity == "different-model" else "synthetic", dim=768,
        allow_legacy_adoption=identity != "unprovable-policy",
    )
    assert result.transitioned and result.state.status == "reindexing"
    assert not await db.fetchall("SELECT id FROM vec_journal")
    assert not await db.fetchall("SELECT entity_id FROM embedding_metadata")
    assert not await db.fetchall("SELECT id FROM vec_artifacts")
    assert len(await db.fetchall("SELECT id FROM journal")) == 19


@pytest.mark.asyncio
@pytest.mark.parametrize("entity_type", ["claim", "journal", "decision", "literature", "mission", "artifact", "figure"])
async def test_each_entity_can_be_invalidated_without_discarding_other_types(db, entity_type):
    from rka.infra.embedding_documents import DOCUMENT_SPECS
    from rka.infra.embeddings import EmbeddingService
    from rka.services.embedding_backfill import BackfillService, register_job
    from tests.test_services.test_embedding_documents import RecordingBackend, seed_seven_documents

    await seed_seven_documents(db)
    backend = RecordingBackend()
    service = EmbeddingService(db=db, backend=backend)
    initial = await BackfillService(db=db, embeddings=service).run_backfill(register_job())
    assert initial.processed == 7 and initial.state == "complete"
    before_vectors = {
        table: await db.fetchall(f"SELECT id, project_id, embedding FROM {table} ORDER BY id")
        for table in {spec.vec_table for spec in DOCUMENT_SPECS.values()}
    }
    await db.execute("UPDATE embedding_metadata SET content_hash = 'stale' WHERE entity_type = ?", [entity_type])
    await db.commit()
    result = await reconcile_embedding_index(db, space_signature=service.space_signature, model_name="synthetic", dim=768)
    assert not result.transitioned and result.state.status == "reindexing"
    assert len(backend.calls) == 7, "adoption must not call a provider"
    for table, rows in before_vectors.items():
        assert await db.fetchall(f"SELECT id, project_id, embedding FROM {table} ORDER BY id") == [
            row for row in rows if row["id"] != f"doc_{entity_type}"
        ]
    assert len(await db.fetchall("SELECT entity_id FROM embedding_metadata")) == 6
    # A pre-adoption service is deliberately fenced out until it binds the
    # adopted generation, just as the durable worker does on refresh.
    service.bind_index_generation(result.state.generation)
    repaired = await BackfillService(db=db, embeddings=service).run_backfill(register_job())
    assert repaired.processed == 1 and repaired.state == "complete"
    assert len(backend.calls) == 8


@pytest.mark.asyncio
@pytest.mark.parametrize("claims,expected_status", [('[]', 'empty_document'), ('[{"claim": 7}]', 'invalid_document')])
async def test_unusable_figure_checks_never_return_source_or_exception_text(db, claims, expected_status):
    await db.execute("INSERT INTO figures (id, claims, project_id) VALUES ('fig_unusable', ?, 'proj_default')", [claims])
    await db.execute(
        """INSERT INTO embedding_metadata
           (project_id, entity_type, entity_id, content_hash, model_name, dimensions)
           VALUES ('proj_default', 'figure', 'fig_unusable', ?, 'synthetic', 768)""",
        [embedding_content_hash("")],
    )
    checks = [check async for check in iter_stored_document_checks(db, model_name="synthetic", dimensions=768)]
    assert [asdict(check) for check in checks] == [{
        "entity_type": "figure", "entity_id": "fig_unusable",
        "project_id": "proj_default", "status": expected_status,
    }]


@pytest.mark.asyncio
async def test_empty_legacy_id_is_checked_not_silently_trusted(db):
    await db.execute("INSERT INTO journal (id, type, content, source, project_id) VALUES ('', 'note', 'source', 'pi', 'proj_default')")
    await db.execute(
        """INSERT INTO embedding_metadata
           (project_id, entity_type, entity_id, content_hash, model_name, dimensions)
           VALUES ('proj_default', 'journal', '', 'stale', 'synthetic', 768)"""
    )
    checks = [check async for check in iter_stored_document_checks(db, model_name="synthetic", dimensions=768)]
    assert len(checks) == 1 and checks[0].entity_id == "" and checks[0].status == "hash_mismatch"
