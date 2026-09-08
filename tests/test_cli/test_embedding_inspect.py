"""Read-only maintenance CLI: synthetic stores, no runtime/provider setup."""

import asyncio
import hashlib
import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from rka.cli import main
from rka.services.embedding_config import EmbeddingConfig, EmbeddingConfigService
from rka.services.embedding_index import embedding_space_signature, reconcile_embedding_index
from tests.test_services.test_embedding_documents import RecordingBackend, seed_seven_documents


async def setup_store(db):
    from rka.infra.embeddings import EmbeddingService
    from rka.services.embedding_backfill import BackfillService, register_job

    config = EmbeddingConfig(
        backend="openai_compat",
        config={"base_url": "http://never-contact.invalid", "model": "synthetic", "dim": 768,
                "api_key": "synthetic-private-token", "document_template": "{text}"},
    )
    EmbeddingConfigService(Path(db.db_path).parent).save_config(config, "system")
    await seed_seven_documents(db)
    service = EmbeddingService(db=db, backend=RecordingBackend())
    result = await BackfillService(db=db, embeddings=service).run_backfill(register_job())
    assert result.state == "complete"
    await reconcile_embedding_index(db, space_signature=embedding_space_signature(config), model_name="synthetic", dim=768)
    return config


async def invoke(db, *args):
    return await asyncio.to_thread(
        CliRunner().invoke, main,
        ["admin", "embedding", *args, "--data-dir", str(Path(db.db_path).parent), "--db", db.db_path, "--json"],
    )


@pytest.mark.asyncio
async def test_inspect_saved_identity_and_all_seven_types_without_mutation(db, monkeypatch):
    from rka.infra import database
    from rka.infra import embedding_backends

    await setup_store(db)
    before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in Path(db.db_path).parent.iterdir() if p.is_file() and not p.name.endswith("-shm")}
    async def forbidden_connect(*args, **kwargs):
        raise AssertionError("ordinary writable database setup must not run")
    def forbidden_backend(*args, **kwargs):
        raise AssertionError("inspection must not construct a provider")
    monkeypatch.setattr(database.Database, "connect", forbidden_connect)
    monkeypatch.setattr(embedding_backends, "make_backend", forbidden_backend)
    monkeypatch.setenv("RKA_EMBEDDING_MODEL", "wrong-env-default")
    unrelated_dir = Path(db.db_path).parent / "env-must-not-be-created"
    monkeypatch.setenv("RKA_DATA_DIR", str(unrelated_dir))
    result = await invoke(db, "inspect")
    assert result.exit_code == 0, (result.output, result.exception)
    report = json.loads(result.output)
    assert report["identity"]["model_name"] == "synthetic"
    assert report["identity"]["source"] == "persisted"
    assert report["assessment"]["status"] == "healthy"
    assert report["totals"]["hash_verified"] == 7
    assert report["scope"] == "global" and len(report["entities"]) == 7
    assert "synthetic-private-token" not in result.output
    assert "never-contact.invalid" not in result.output
    assert {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in Path(db.db_path).parent.iterdir() if p.is_file() and not p.name.endswith("-shm")} == before
    assert not await db.fetchone("SELECT id FROM jobs")
    assert not unrelated_dir.exists()


@pytest.mark.asyncio
async def test_dry_run_drift_plans_selective_repair_but_does_not_execute(db):
    await setup_store(db)
    await db.execute("UPDATE embedding_metadata SET content_hash = 'stale' WHERE entity_type = 'journal'")
    result = await invoke(db, "dry-run")
    assert result.exit_code == 0, (result.output, result.exception)
    report = json.loads(result.output)
    assert report["totals"]["hash_mismatch"] == 1
    assert report["plan"]["action"] == "repair_rows"
    assert report["plan"]["execution_supported"] is False
    assert report["totals"]["hash_verified"] == 6
    assert (await db.fetchone("SELECT content_hash FROM embedding_metadata WHERE entity_type='journal'"))["content_hash"] == "stale"
    assert not await db.fetchone("SELECT id FROM jobs")


def test_missing_input_does_not_initialize_directory(tmp_path):
    missing = tmp_path / "never-created"
    result = CliRunner().invoke(main, ["admin", "embedding", "inspect", "--data-dir", str(missing), "--json"])
    assert result.exit_code != 0
    assert json.loads(result.output)["error"] == "persisted_config_missing"
    assert not missing.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("change,action,preserve", [
    ({"query_template": "query: {text}"}, "none", 7),
    ({"document_template": "document: {text}"}, "rebuild_space", 0),
    ({"model": "another-model"}, "rebuild_space", 0),
    ({"embedding_space_id": "different-artifact-revision"}, "rebuild_space", 0),
    ({"dim": 384}, "offline_rebuild", 0),
])
async def test_candidate_config_previews_global_impact_without_saving(db, change, action, preserve):
    current = await setup_store(db)
    data_dir = Path(db.db_path).parent
    config_before = (data_dir / "embedding_config.json").read_bytes()
    target = data_dir / "candidate.json"
    target.write_text(current.model_copy(update={"config": {**current.config, **change}}).model_dump_json())
    result = await invoke(db, "dry-run", "--target-config", str(target))
    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report["plan"]["action"] == action
    assert report["plan"]["preservable_pairs"] == preserve
    assert report["plan"]["execution_supported"] is False
    assert (data_dir / "embedding_config.json").read_bytes() == config_before
    assert (await db.fetchone("SELECT count(*) AS n FROM embedding_metadata"))["n"] == 7
    assert (await db.fetchone("SELECT dimensions FROM embedding_index_state"))["dimensions"] == 768


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [
    '{"backend":"secret-token-in-error","config":{"api_key":"private-key"}}',
    '{"backend":"openai_compat","config":{"model":"test","dim":false}}',
    '{"backend":"openai_compat","config":{"model":"test","dim":0}}',
    '{"backend":"openai_compat","config":{"model":"test","dim":"768"}}',
    '{"backend":"openai_compat","config":{"model":"test","base_url":"private-url"}}',
    '{"backend":"openai_compat","config":{"model":"test","dim":768,"document_template":"private-template"}}',
    'private malformed config',
    'x' * 65537,
], ids=[
    "unknown-backend", "boolean-dimension", "zero-dimension", "string-dimension",
    "missing-dimension", "invalid-document-template", "malformed-json", "oversized-config",
])
async def test_bad_config_is_explicit_and_errors_do_not_echo_private_fields(db, payload):
    await setup_store(db)
    path = Path(db.db_path).parent / "embedding_config.json"
    path.write_text(payload)
    result = await invoke(db, "inspect")
    assert result.exit_code == 2
    assert "error" in json.loads(result.output)
    assert not any(value in result.output for value in ("secret-token", "private", "x" * 20))
    assert path.read_text() == payload


@pytest.mark.asyncio
async def test_legacy_hash_drift_and_missing_coverage_are_reported_separately(db):
    await setup_store(db)
    await db.execute("DELETE FROM embedding_index_state")
    await db.execute("UPDATE embedding_metadata SET content_hash='stale' WHERE entity_type='journal'")
    await db.execute("INSERT INTO journal (id, type, content, source, project_id) VALUES ('jrn_missing', 'note', 'not indexed', 'pi', 'proj_default')")
    result = await invoke(db, "dry-run")
    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report["plan"]["action"] == "adopt_legacy"
    assert report["totals"]["metadata_missing"] == 1
    assert report["totals"]["hash_mismatch"] == 1
    assert report["plan"]["rows_to_embed"] == 2
    assert report["plan"]["preservable_pairs"] == 6
    assert not await db.fetchone("SELECT generation FROM embedding_index_state")


@pytest.mark.asyncio
async def test_unknown_legacy_document_policy_never_adopts_hash_matches(db):
    current = await setup_store(db)
    await db.execute("DELETE FROM embedding_index_state")
    current.config["document_template"] = "private prefix: {text}"
    EmbeddingConfigService(Path(db.db_path).parent).save_config(current, "system")
    result = await invoke(db, "dry-run")
    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report["totals"]["hash_verified"] == 7
    assert report["totals"]["reusable"] == 0
    assert report["plan"]["action"] == "rebuild_space"
    assert "private prefix" not in result.output


@pytest.mark.asyncio
async def test_no_vec_extension_is_incomplete_not_healthy(db, monkeypatch):
    from rka.infra import readonly_sqlite
    await setup_store(db)
    monkeypatch.setattr(readonly_sqlite, "_load_vec", lambda conn: False)
    result = await invoke(db, "dry-run")
    assert result.exit_code == 2, result.output
    report = json.loads(result.output)
    assert report["assessment"] == {"complete": False, "status": "incomplete"}
    assert report["totals"]["hash_verified"] == 7
    assert report["plan"]["action"] == "blocked"
    assert all(v["rows"] is None for v in report["physical_tables"].values())


@pytest.mark.asyncio
async def test_config_changed_during_snapshot_is_rejected(db, monkeypatch):
    from rka.services import embedding_inspection
    await setup_store(db)
    original = embedding_inspection._inspect_snapshot
    async def changed(*args, **kwargs):
        report = await original(*args, **kwargs)
        (Path(db.db_path).parent / "embedding_config.json").write_text('{"backend":"fastembed","config":{}}')
        return report
    monkeypatch.setattr(embedding_inspection, "_inspect_snapshot", changed)
    result = await invoke(db, "inspect")
    assert result.exit_code == 2
    assert json.loads(result.output)["error"] == "config_changed_during_inspection"


@pytest.mark.asyncio
async def test_row_budget_fails_closed_without_partial_ready_report(db):
    await setup_store(db)
    result = await invoke(db, "inspect", "--max-rows", "3")
    assert result.exit_code == 2
    assert json.loads(result.output)["error"] == "inspection_budget_exceeded"
    assert "healthy" not in result.output


@pytest.mark.asyncio
async def test_unknown_metadata_or_orphan_vector_blocks_adoption(db):
    import struct
    await setup_store(db)
    await db.execute("DELETE FROM embedding_index_state")
    await db.execute("INSERT INTO embedding_metadata (project_id, entity_type, entity_id, content_hash, model_name, dimensions) VALUES ('proj_default','unknown','bad-id','stale','synthetic',768)")
    await db.execute("INSERT INTO vec_artifacts (id, entity_type, project_id, embedding) VALUES ('orphan','unknown','proj_default',?)", [struct.pack("768f", *([0.5] * 768))])
    result = await invoke(db, "dry-run")
    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report["structural"]["unknown_metadata"] == 1
    assert report["structural"]["orphan_vectors"] == 1
    assert report["plan"]["action"] == "rebuild_space"


@pytest.mark.asyncio
async def test_corrupt_database_fails_without_repair_or_raw_error(db):
    await setup_store(db)
    corrupt = Path(db.db_path).parent / "corrupt.db"
    corrupt.write_bytes(b"private database contents")
    # Use an explicit --db instead of invoke's normal fixture database.
    result = await asyncio.to_thread(CliRunner().invoke, main, ["admin", "embedding", "inspect", "--data-dir", str(corrupt.parent), "--db", str(corrupt), "--json"])
    assert result.exit_code == 2
    assert "private" not in result.output and "Traceback" not in result.output
    assert corrupt.read_bytes() == b"private database contents"


@pytest.mark.asyncio
async def test_second_project_scope_and_cross_project_corruption(db):
    await setup_store(db)
    await db.execute("INSERT INTO projects (id,name) VALUES ('proj_second','Synthetic second')")
    await db.execute("UPDATE journal SET project_id='proj_second' WHERE id='doc_journal'")
    result = await invoke(db, "dry-run")
    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report["scope"] == "global"
    assert report["totals"]["source_rows"] == 7
    assert report["totals"]["reusable"] == 6
    assert report["structural"]["orphan_metadata"] == 1
    assert report["plan"]["action"] == "rebuild_space"
    assert (await db.fetchone("SELECT project_id FROM journal WHERE id='doc_journal'"))["project_id"] == "proj_second"


@pytest.mark.asyncio
async def test_empty_and_invalid_documents_remain_distinct(db):
    await setup_store(db)
    await db.execute("INSERT INTO figures (id,claims,project_id) VALUES ('fig_empty','[]','proj_default')")
    await db.execute("INSERT INTO figures (id,claims,project_id) VALUES ('fig_invalid','[{\"claim\":7}]','proj_default')")
    result = await invoke(db, "dry-run")
    report = json.loads(result.output)
    assert result.exit_code == 0, result.output
    assert report["totals"]["empty"] == 1 and report["totals"]["invalid_document"] == 1
    assert report["plan"]["action"] == "resolve_source_errors"


@pytest.mark.asyncio
async def test_missing_vector_for_empty_document_is_structurally_invalid(db):
    await setup_store(db)
    await db.execute("UPDATE figures SET caption=NULL, claims='[]' WHERE id='doc_figure'")
    await db.execute("DELETE FROM vec_artifacts WHERE id='doc_figure'")
    result = await invoke(db, "dry-run")
    report = json.loads(result.output)
    assert result.exit_code == 0, result.output
    assert report["structural"]["missing_vectors"] == 1
    assert not report["structural"]["coherent"]


@pytest.mark.asyncio
async def test_generation_document_identity_drift_is_not_reusable(db):
    current = await setup_store(db)
    current.config["document_template"] = "new: {text}"
    EmbeddingConfigService(Path(db.db_path).parent).save_config(current, "system")
    result = await invoke(db, "dry-run")
    report = json.loads(result.output)
    assert result.exit_code == 0, result.output
    assert not report["generation_matches_config"]
    assert report["totals"]["hash_verified"] == 7 and report["totals"]["reusable"] == 0
    assert report["plan"]["action"] == "rebuild_space"


@pytest.mark.asyncio
async def test_failed_generation_reports_resume_not_healthy(db):
    await setup_store(db)
    await db.execute("UPDATE embedding_index_state SET status='failed', last_error='private failure detail'")
    result = await invoke(db, "dry-run")
    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report["assessment"]["status"] == "needs_attention"
    assert report["plan"]["action"] == "resume_generation"
    assert "private failure detail" not in result.output


@pytest.mark.asyncio
async def test_old_schema_is_not_migrated(db):
    import sqlite3
    await setup_store(db)
    old = Path(db.db_path).parent / "old.db"
    with sqlite3.connect(old) as conn:
        conn.execute("CREATE TABLE journal (id TEXT, content TEXT)")
    before = old.read_bytes()
    result = await asyncio.to_thread(CliRunner().invoke, main, ["admin", "embedding", "inspect", "--data-dir", str(old.parent), "--db", str(old), "--json"])
    assert result.exit_code == 2
    assert json.loads(result.output)["error"] == "source_schema_unsupported"
    assert old.read_bytes() == before


@pytest.mark.asyncio
async def test_deadline_failure_is_not_a_partial_healthy_result(db):
    from rka.services.embedding_inspection import EmbeddingInspectionError, inspect_embedding_index
    await setup_store(db)
    with pytest.raises(EmbeddingInspectionError, match="budget_exceeded"):
        await inspect_embedding_index(data_dir=Path(db.db_path).parent, db_path=Path(db.db_path), timeout_seconds=1e-9)


@pytest.mark.asyncio
async def test_text_output_is_explicit_about_non_execution(db):
    await setup_store(db)
    result = await asyncio.to_thread(CliRunner().invoke, main, ["admin", "embedding", "dry-run", "--data-dir", str(Path(db.db_path).parent), "--db", db.db_path])
    assert result.exit_code == 0, result.output
    assert "Advisory action: none" in result.output
    assert "Maintenance ownership not acquired" in result.output
    assert "explicit offline recovery commands" in result.output
    assert "no provider probe" in result.output


@pytest.mark.asyncio
async def test_fastembed_identity_does_not_require_model_runtime(db, monkeypatch):
    await setup_store(db)
    config = EmbeddingConfig(backend="fastembed", config={"model_name": "synthetic"})
    EmbeddingConfigService(Path(db.db_path).parent).save_config(config, "system")
    await db.execute("UPDATE embedding_index_state SET space_signature=?", [embedding_space_signature(config, dimensions=768)])
    from rka.infra.embedding_backends.fastembed import FastEmbedBackend
    def forbidden(*args, **kwargs):
        raise AssertionError("must not construct FastEmbed")
    monkeypatch.setattr(FastEmbedBackend, "__init__", forbidden)
    result = await invoke(db, "inspect")
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["assessment"]["status"] == "healthy"


@pytest.mark.asyncio
async def test_malformed_generation_is_sanitized(db):
    await setup_store(db)
    await db.execute("UPDATE embedding_index_state SET model_name=?", [b"private blob"])
    result = await invoke(db, "inspect")
    assert result.exit_code == 2
    assert json.loads(result.output)["error"] == "generation_record_invalid"
    assert "private" not in result.output


@pytest.mark.asyncio
async def test_source_view_is_not_evaluated(db):
    await setup_store(db)
    await db.execute("ALTER TABLE figures RENAME TO real_figures")
    await db.execute("CREATE VIEW figures AS SELECT load_extension('private-path') AS id")
    result = await invoke(db, "inspect")
    assert result.exit_code == 2
    assert json.loads(result.output)["error"] == "source_schema_unsupported"
    assert "private-path" not in result.output
