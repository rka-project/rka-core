"""Document-recipe parity and coverage, with synthetic data and no provider."""

import hashlib
import json
from unittest.mock import AsyncMock

import pytest

from rka.services.base import BaseService
from rka.services.artifacts import ArtifactService
from rka.services.claims import ClaimService
from rka.services.decisions import DecisionService
from rka.services.embedding_backfill import _ENTITY_BACKFILL_CONFIGS
from rka.services.embedding_index import EmbeddingIndexState, _active_index_has_full_coverage
from rka.services.literature import LiteratureService
from rka.services.missions import MissionService
from rka.services.notes import NoteService


STANDARD_CASES = [
    ("claim", ClaimService, {"content": " \t alpha  beta\n "}, "alpha  beta"),
    ("journal", NoteService, {"content": " alpha  beta \n", "summary": "\t gamma "}, "alpha  beta gamma"),
    ("decision", DecisionService, {"question": " alpha  beta \n", "rationale": "\t gamma "}, "alpha  beta gamma"),
    ("literature", LiteratureService, {"title": " alpha  beta \n", "abstract": "\t gamma "}, "alpha  beta gamma"),
    ("mission", MissionService, {"objective": " alpha  beta \n", "context": "\t gamma "}, "alpha  beta gamma"),
]


@pytest.mark.parametrize("entity_type,service_class,row,expected", STANDARD_CASES)
@pytest.mark.asyncio
async def test_ordinary_worker_and_backfill_document_parity(entity_type, service_class, row, expected):
    original = dict(row)
    database = AsyncMock()
    database.fetchone.return_value = row
    embeddings = AsyncMock()
    service = service_class(database, embeddings=embeddings)
    await service._sync_embedding(entity_type, "synthetic_id", row)
    ordinary_text = embeddings.embed_and_store.call_args.args[2]
    await service.process_embedding_job("synthetic_id")
    worker_text = embeddings.embed_and_store.call_args.args[2]
    backfill_text = _ENTITY_BACKFILL_CONFIGS[entity_type].compose_text(row)
    assert ordinary_text == worker_text == backfill_text == expected
    assert row == original, "assembling derived text must not rewrite the source"


@pytest.mark.asyncio
async def test_parked_cluster_still_has_no_embedding():
    embeddings = AsyncMock()
    await BaseService(AsyncMock(), embeddings=embeddings)._sync_embedding(
        "cluster", "clu_synthetic", {"content": "not an embedding entity"}
    )
    embeddings.embed_and_store.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("empty_text", ["\t\n", "\u2003\u00a0", ""])
async def test_coverage_uses_recipe_not_sqlite_whitespace(db, empty_text):
    for index in range(19):
        await db.execute(
            "INSERT INTO journal (id, type, content, source, project_id) VALUES (?, 'note', ?, 'pi', 'proj_default')",
            [f"jrn_empty_{index:03d}", empty_text],
        )
    state = EmbeddingIndexState(1, "synthetic", "synthetic", 768, "reindexing")
    assert await _active_index_has_full_coverage(db, state)
    await db.execute(
        "INSERT INTO journal (id, type, content, source, project_id) VALUES ('jrn_z_nonempty', 'note', 'needed', 'pi', 'proj_default')"
    )
    assert not await _active_index_has_full_coverage(db, state)
    assert await _active_index_has_full_coverage(db, state, entity_types=("claim",))


@pytest.mark.asyncio
@pytest.mark.parametrize("claims", [None, "[]", "{}", "null", "invalid json", '[{"other": "value"}]'])
async def test_empty_figure_claims_do_not_block_readiness(db, claims):
    await db.execute("INSERT INTO figures (id, claims, project_id) VALUES ('fig_empty', ?, 'proj_default')", [claims])
    state = EmbeddingIndexState(1, "synthetic", "synthetic", 768, "reindexing")
    assert _ENTITY_BACKFILL_CONFIGS["figure"].compose_text({"claims": claims}) == ""
    assert await _active_index_has_full_coverage(db, state)


@pytest.mark.asyncio
async def test_artifact_and_figure_builders_keep_backfill_parity():
    from rka.infra.embedding_documents import compose_document, embedding_content_hash
    from rka.infra.embeddings import EmbeddingService

    embeddings = AsyncMock()
    service = ArtifactService(AsyncMock(), embeddings=embeddings)
    artifact = {"filename": "Synthetic.pdf", "filetype": "pdf", "mime": "application/pdf", "metadata": {"z": 2, "a": "合成"}}
    await service._embed_artifact("art_synthetic", **artifact)
    expected_artifact = 'Synthetic.pdf\nfiletype: pdf\nmime: application/pdf\nmetadata: {"a": "\\u5408\\u6210", "z": 2}'
    assert embeddings.embed_and_store.call_args.args[2] == expected_artifact
    figure = {"caption": " caption ", "summary": "summary", "claims": [{"claim": f" claim {i} "} for i in range(7)]}
    await service._embed_figure("fig_synthetic", **figure)
    expected_figure = "caption:  caption \nsummary: summary\nclaims: claim 0; claim 1; claim 2; claim 3; claim 4"
    assert embeddings.embed_and_store.call_args.args[2] == expected_figure
    for entity_type, row, expected, json_field in [
        ("artifact", artifact, expected_artifact, "metadata"),
        ("figure", figure, expected_figure, "claims"),
    ]:
        stored = {**row, json_field: json.dumps(row[json_field])}
        assert compose_document(entity_type, stored) == expected
        assert _ENTITY_BACKFILL_CONFIGS[entity_type].compose_text(stored) == expected
        digest = hashlib.sha256(expected.encode("utf-8")).hexdigest()[:16]
        assert embedding_content_hash(expected) == EmbeddingService.content_hash(expected) == digest
        assert embedding_content_hash(expected.encode("utf-8")) == digest


@pytest.mark.asyncio
async def test_invalid_figure_is_not_mistaken_for_empty(db):
    await db.execute("INSERT INTO figures (id, claims, project_id) VALUES ('fig_invalid', '[{\"claim\": 7}]', 'proj_default')")
    state = EmbeddingIndexState(1, "synthetic", "synthetic", 768, "reindexing")
    assert not await _active_index_has_full_coverage(db, state)


@pytest.mark.asyncio
async def test_artifact_metadata_alone_requires_coverage(db):
    await db.execute(
        "INSERT INTO artifacts (id, filename, filepath, metadata, project_id) VALUES ('art_metadata', '', 'synthetic', '{\"key\": 1}', 'proj_default')"
    )
    state = EmbeddingIndexState(1, "synthetic", "synthetic", 768, "reindexing")
    assert not await _active_index_has_full_coverage(db, state)
    assert await _active_index_has_full_coverage(db, state, project_id="proj_other")


class RecordingBackend:
    model_name = "synthetic"
    dim = 768

    def __init__(self):
        self.calls = []

    async def embed(self, text, *, is_query=False):
        assert not is_query
        self.calls.append(text)
        return [0.5] * self.dim

    async def embed_batch(self, texts, *, is_query=False):
        return [await self.embed(text, is_query=is_query) for text in texts]


async def seed_seven_documents(db):
    await db.execute("INSERT INTO journal (id, type, content, summary, source, project_id) VALUES ('doc_journal', 'note', '  alpha  beta ', ' gamma ', 'pi', 'proj_default')")
    await db.execute("INSERT INTO claims (id, source_entry_id, claim_type, content, project_id) VALUES ('doc_claim', 'doc_journal', 'observation', '  claim text  ', 'proj_default')")
    await db.execute("INSERT INTO decisions (id, phase, question, rationale, decided_by, project_id) VALUES ('doc_decision', 'test', ' question ', ' rationale ', 'pi', 'proj_default')")
    await db.execute("INSERT INTO literature (id, title, abstract, project_id) VALUES ('doc_literature', ' title ', ' abstract ', 'proj_default')")
    await db.execute("INSERT INTO missions (id, phase, objective, context, project_id) VALUES ('doc_mission', 'test', ' objective ', ' context ', 'proj_default')")
    await db.execute("INSERT INTO artifacts (id, filename, filepath, metadata, project_id) VALUES ('doc_artifact', 'synthetic.pdf', 'not-read', '{\"b\": 2, \"a\": 1}', 'proj_default')")
    await db.execute("INSERT INTO figures (id, artifact_id, caption, claims, project_id) VALUES ('doc_figure', 'doc_artifact', ' caption ', '[{\"claim\": \" figure claim \"}]', 'proj_default')")
    await db.commit()


@pytest.mark.asyncio
async def test_seven_types_store_identical_hashes_via_ordinary_worker_and_bulk(db):
    from rka.infra.embedding_documents import DOCUMENT_SPECS, compose_document
    from rka.infra.embeddings import EmbeddingService
    from rka.services.embedding_backfill import BackfillService, register_job
    from rka.services.embedding_verification import iter_stored_document_checks

    await seed_seven_documents(db)
    backend = RecordingBackend()
    embeddings = EmbeddingService(db=db, backend=backend)
    ordinary = BaseService(db, embeddings=embeddings)
    artifacts = ArtifactService(db, embeddings=embeddings)
    originals = {}
    for entity_type, spec in DOCUMENT_SPECS.items():
        row = await db.fetchone(f"SELECT * FROM {spec.source_table} WHERE id = ?", [f"doc_{entity_type}"])
        originals[entity_type] = {field: row[field] for field in spec.fields}
        if entity_type == "artifact":
            await artifacts._embed_artifact(f"doc_{entity_type}", **originals[entity_type])
        elif entity_type == "figure":
            await artifacts._embed_figure(f"doc_{entity_type}", **originals[entity_type])
        else:
            await ordinary._sync_embedding(entity_type, f"doc_{entity_type}", row)
    original_hashes = await db.fetchall("SELECT entity_type, entity_id, content_hash FROM embedding_metadata ORDER BY entity_type")
    assert len(original_hashes) == 7 and len(backend.calls) == 7
    for entity_type, service_class, _, _ in STANDARD_CASES:
        await service_class(db, embeddings=embeddings).process_embedding_job(f"doc_{entity_type}")
    assert len(backend.calls) == 7, "same text/hash must skip repeat inference"

    # Remove only this fixture's derived index, then exercise actual bulk writes.
    for table in {spec.vec_table for spec in DOCUMENT_SPECS.values()}:
        await db.execute(f"DELETE FROM {table}")
    await db.execute("DELETE FROM embedding_metadata")
    await db.commit()
    result = await BackfillService(db=db, embeddings=embeddings).run_backfill(register_job())
    assert result.state == "complete" and result.processed == 7
    assert await db.fetchall("SELECT entity_type, entity_id, content_hash FROM embedding_metadata ORDER BY entity_type") == original_hashes
    checks = [check async for check in iter_stored_document_checks(db, model_name="synthetic", dimensions=768)]
    assert len(checks) == 7 and all(check.status == "verified" for check in checks)
    for entity_type, spec in DOCUMENT_SPECS.items():
        row = await db.fetchone(f"SELECT * FROM {spec.source_table} WHERE id = ?", [f"doc_{entity_type}"])
        assert {field: row[field] for field in spec.fields} == originals[entity_type]
        assert backend.calls.count(compose_document(entity_type, row)) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("source_id", ["", None])
async def test_coverage_does_not_skip_unusual_legacy_ids(db, source_id):
    if source_id is None:
        # Current change-event triggers reject NULL IDs on insert. Inject an
        # unaddressable legacy/corrupt row at the read boundary instead.
        db = AsyncMock()
        db.fetchall.return_value = [{"id": None}]
    else:
        await db.execute(
            "INSERT INTO journal (id, type, content, source, project_id) VALUES (?, 'note', 'unindexed', 'pi', 'proj_default')",
            [source_id],
        )
    state = EmbeddingIndexState(1, "synthetic", "synthetic", 768, "reindexing")
    assert not await _active_index_has_full_coverage(db, state)
