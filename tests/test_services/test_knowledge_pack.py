"""Tests for project knowledge-pack export/import."""

from __future__ import annotations

import asyncio
import hashlib
import json
import zipfile
from pathlib import Path

import pytest

from rka import __version__
from rka.infra.database import Database
from rka.infra.file_access import FileAccessPolicy
from rka.infra.embeddings import EmbeddingService
from rka.models.claim import ClaimCreate, ClaimScopeCondition, ClaimScopeWrite
from rka.models.decision import DecisionCreate, DecisionOption
from rka.models.journal import JournalEntryCreate
from rka.models.interpretation import InterpretationCandidateCreate, InterpretationTriage
from rka.models.literature import LiteratureCreate
from rka.models.mission import MissionCreate
from rka.models.project import ProjectCreate
from rka.services.artifacts import ArtifactService
from rka.services.claims import ClaimService
from rka.services.decisions import DecisionService
from rka.services.knowledge_pack import KnowledgePackService, PACK_SCHEMA_VERSION
from rka.services.literature import LiteratureService
from rka.services.missions import MissionService
from rka.services.notes import NoteService
from rka.services.interpretation import InterpretationService
from rka.services.project import ProjectService
from rka.services.search import SearchService


class DeterministicEmbeddings(EmbeddingService):
    """Local embedding stub that never downloads or calls a provider."""

    def __init__(self, db: Database):
        super().__init__(model_name="knowledge-pack-test", db=db)

    @staticmethod
    def _vector(text: str) -> list[float]:
        digest = hashlib.sha256(text.encode()).digest()
        return [digest[index % len(digest)] / 255.0 for index in range(768)]

    async def embed(self, text: str) -> list[float]:
        return self._vector(text)

    async def embed_document(self, text: str) -> list[float]:
        return self._vector(text)

    async def embed_batch(self, texts, **kwargs):
        return [self._vector(text) for text in texts]


async def _bind_embedding_generation(db, embeddings):
    from rka.services.embedding_index import reconcile_embedding_index

    state = (await reconcile_embedding_index(
        db, space_signature=embeddings.space_signature,
        model_name=embeddings.model_name, dim=embeddings.dim,
    )).state
    embeddings.bind_index_generation(state.generation, space_signature=state.space_signature)


async def _make_db(path: Path) -> Database:
    db = Database(str(path))
    await db.connect()
    await db.initialize_schema()
    await db.initialize_phase2_schema()
    return db


@pytest.mark.asyncio
async def test_prepare_claim_edges_deduplicates_legacy_memberships(db) -> None:
    service = KnowledgePackService(db)
    rows = [
        {
            "id": "ced_first",
            "project_id": "prj_source",
            "source_claim_id": "clm_one",
            "cluster_id": "ecl_one",
            "relation": "member_of",
        },
        {
            "id": "ced_duplicate",
            "project_id": "prj_source",
            "source_claim_id": "clm_one",
            "cluster_id": "ecl_one",
            "relation": "member_of",
        },
        {
            "id": "ced_supports",
            "project_id": "prj_source",
            "source_claim_id": "clm_one",
            "target_claim_id": "clm_two",
            "relation": "supports",
        },
    ]

    prepared = service._prepare_rows_for_insert(
        "claim_edges",
        rows,
        "prj_target",
    )

    assert [row["id"] for row in prepared] == ["ced_first", "ced_supports"]
    assert {row["project_id"] for row in prepared} == {"prj_target"}


def test_load_manifest_rejects_table_count_mismatch(tmp_path: Path) -> None:
    pack_path = tmp_path / "bad-count.rka-pack.zip"
    manifest = {
        "pack_format_version": PACK_SCHEMA_VERSION,
        "schema_version": 53,
        "project": {"id": "proj_source", "name": "Source"},
        "tables": {"journal": []},
        "table_counts": {"journal": 1},
    }
    with zipfile.ZipFile(pack_path, "w") as archive:
        archive.writestr("manifest.json", json.dumps(manifest))

    with zipfile.ZipFile(pack_path) as archive:
        with pytest.raises(ValueError, match="table count mismatch.*journal"):
            service = KnowledgePackService.__new__(KnowledgePackService)
            service._load_manifest(archive)


@pytest.mark.parametrize(
    ("tables", "table_counts", "message"),
    [
        ({"journal": 1}, {"journal": 0}, "table payloads must be arrays"),
        ({"journal": [1]}, {"journal": 1}, r"journal\[0\] must be an object"),
        ({"journal": []}, {"journal": True}, "non-negative integer"),
        ({"journal": []}, {}, "keys must match tables exactly"),
    ],
)
def test_load_manifest_rejects_invalid_table_count_contract(
    tmp_path: Path,
    tables: dict,
    table_counts: dict,
    message: str,
) -> None:
    pack_path = tmp_path / "bad-table-contract.rka-pack.zip"
    manifest = {
        "pack_format_version": PACK_SCHEMA_VERSION,
        "schema_version": 53,
        "project": {"id": "proj_source", "name": "Source"},
        "tables": tables,
        "table_counts": table_counts,
    }
    with zipfile.ZipFile(pack_path, "w") as archive:
        archive.writestr("manifest.json", json.dumps(manifest))

    with zipfile.ZipFile(pack_path) as archive:
        with pytest.raises(ValueError, match=message):
            service = KnowledgePackService.__new__(KnowledgePackService)
            service._load_manifest(archive)


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"project": "proj_source"}, "missing project metadata"),
        ({"project": {"name": "Source"}}, "requires a non-empty ID"),
        ({"project_state": []}, "project_state must be an object or null"),
        ({"table_counts": None}, rf"format {PACK_SCHEMA_VERSION} requires table_counts"),
    ],
)
def test_load_manifest_rejects_invalid_project_contract(
    tmp_path: Path,
    override: dict,
    message: str,
) -> None:
    pack_path = tmp_path / "bad-project-contract.rka-pack.zip"
    manifest = {
        "pack_format_version": PACK_SCHEMA_VERSION,
        "schema_version": 53,
        "project": {"id": "proj_source", "name": "Source"},
        "project_state": None,
        "tables": {},
        "table_counts": {},
        **override,
    }
    with zipfile.ZipFile(pack_path, "w") as archive:
        archive.writestr("manifest.json", json.dumps(manifest))

    with zipfile.ZipFile(pack_path) as archive:
        with pytest.raises(ValueError, match=message):
            service = KnowledgePackService.__new__(KnowledgePackService)
            service._load_manifest(archive)


@pytest.mark.parametrize(
    ("manifest", "message"),
    [
        ([], "manifest must be an object"),
        (
            {
                "pack_format_version": True,
                "project": {"id": "proj_source", "name": "Source"},
                "tables": {},
            },
            "Unsupported knowledge pack format version",
        ),
        (
            {
                "pack_format_version": 1.0,
                "project": {"id": "proj_source", "name": "Source"},
                "tables": {},
            },
            "Unsupported knowledge pack format version",
        ),
        (
            {
                "pack_format_version": False,
                "schema_version": 7,
                "project": {"id": "proj_source", "name": "Source"},
                "tables": {},
            },
            "Unsupported knowledge pack format version",
        ),
        (
            {
                "pack_format_version": 0,
                "schema_version": 7,
                "project": {"id": "proj_source", "name": "Source"},
                "tables": {},
            },
            "Unsupported knowledge pack format version",
        ),
        (
            {
                "pack_format_version": "",
                "schema_version": 7,
                "project": {"id": "proj_source", "name": "Source"},
                "tables": {},
            },
            "Unsupported knowledge pack format version",
        ),
    ],
)
def test_load_manifest_rejects_invalid_top_level_contract(
    tmp_path: Path,
    manifest: object,
    message: str,
) -> None:
    pack_path = tmp_path / "bad-top-level-contract.rka-pack.zip"
    with zipfile.ZipFile(pack_path, "w") as archive:
        archive.writestr("manifest.json", json.dumps(manifest))

    with zipfile.ZipFile(pack_path) as archive:
        with pytest.raises(ValueError, match=message):
            service = KnowledgePackService.__new__(KnowledgePackService)
            service._load_manifest(archive)


def test_load_manifest_rejects_non_utf8_payload(tmp_path: Path) -> None:
    pack_path = tmp_path / "non-utf8.rka-pack.zip"
    with zipfile.ZipFile(pack_path, "w") as archive:
        archive.writestr("manifest.json", b"\xff\xfe")

    with zipfile.ZipFile(pack_path) as archive:
        with pytest.raises(ValueError, match="not valid JSON"):
            service = KnowledgePackService.__new__(KnowledgePackService)
            service._load_manifest(archive)


def test_scope_hash_remap_preserves_intentionally_stale_scope() -> None:
    stale_hash = "0" * 64
    source = {
        "claims": [
            {"id": "clm_source", "claim_type": "result", "content": "current content"}
        ],
        "claim_scope_versions": [
            {
                "id": "csc_source",
                "claim_id": "clm_source",
                "claim_content_hash": stale_hash,
            }
        ],
    }
    remapped = {
        "claims": [
            {"id": "clm_target", "claim_type": "result", "content": "rewritten content"}
        ],
        "claim_scope_versions": [
            {
                "id": "csc_target",
                "claim_id": "clm_target",
                "claim_content_hash": stale_hash,
            }
        ],
    }

    KnowledgePackService._refresh_remapped_claim_scope_hashes(source, remapped)

    assert remapped["claim_scope_versions"][0]["claim_content_hash"] == stale_hash


def test_remap_rekeys_prose_references_but_preserves_verbatim_originals() -> None:
    service = KnowledgePackService.__new__(KnowledgePackService)
    source_project = "prj_01M100GW2EVPA8T6Q4CSZ5GPFA"
    target_project = "recovery_pack_001"
    source_journal = "jrn_01M147SEKP7F5RQ3351PG3SKD9"
    target_journal = "jrn_01M20000000000000000000000"
    id_map = {source_journal: target_journal}

    mission = service._remap_row(
        "missions",
        {
            "project_id": source_project,
            "checkpoint_triggers": f"Review {source_project}",
            "report": f"Evidence is recorded in {source_journal}",
        },
        id_map=id_map,
        source_project_id=source_project,
        target_project_id=target_project,
    )
    journal = service._remap_row(
        "journal",
        {
            "project_id": source_project,
            "verbatim_input": f"Inspect {source_project} and {source_journal}",
        },
        id_map=id_map,
        source_project_id=source_project,
        target_project_id=target_project,
    )

    assert mission["checkpoint_triggers"] == f"Review {target_project}"
    assert mission["report"] == f"Evidence is recorded in {target_journal}"
    assert journal["project_id"] == target_project
    # A quotation is evidence, not prose owned by the importer. Its embedded
    # IDs must stay literal even while structured IDs and ordinary prose re-key.
    assert journal["verbatim_input"] == (
        f"Inspect {source_project} and {source_journal}"
    )


@pytest.mark.asyncio
async def test_check_integrity_reports_null_cluster_claim_count(db) -> None:
    await db.execute(
        """INSERT INTO evidence_clusters
           (id, label, claim_count, project_id)
           VALUES ('ecl_null_count', 'nullable legacy count', NULL,
                   'proj_default')"""
    )
    await db.commit()

    issues = await KnowledgePackService(db).check_integrity("proj_default")

    mismatch = next(
        issue for issue in issues
        if issue["category"] == "claim_count_mismatch"
    )
    assert mismatch["ids"] == ["ecl_null_count"]
    assert mismatch["severity"] == "warning"


@pytest.mark.asyncio
async def test_knowledge_pack_round_trip_imports_into_same_db_with_remapped_ids_and_artifacts(tmp_path: Path):
    db = await _make_db(tmp_path / "round-trip.db")
    artifact_path = tmp_path / "reference.txt"
    artifact_path.write_text("artifact payload", encoding="utf-8")

    try:
        project_svc = ProjectService(db)
        await project_svc.create_project(
            ProjectCreate(id="proj_export", name="Export Source", description="pack source"),
            actor="system",
        )
        note_svc = NoteService(db, project_id="proj_export")
        decision_svc = DecisionService(db, project_id="proj_export")
        literature_svc = LiteratureService(db, project_id="proj_export")
        artifact_svc = ArtifactService(db, project_id="proj_export", file_policy=FileAccessPolicy([tmp_path]))

        decision = await decision_svc.create(
            DecisionCreate(
                question="Use background probe for local LLM startup?",
                options=[
                    DecisionOption(label="block startup", description="wait for readiness"),
                    DecisionOption(label="background probe", description="serve immediately"),
                ],
                chosen="background probe",
                rationale="Keeps the API responsive while the local model warms up.",
                decided_by="pi",
                phase="validation",
                tags=["llm", "startup"],
            ),
            actor="pi",
        )
        literature = await literature_svc.create(
            LiteratureCreate(
                title="Background probing for local inference",
                doi="10.1234/rka-pack-roundtrip",
                abstract="A paper about background startup checks.",
                related_decisions=[decision.id],
                added_by="web_ui",
                tags=["llm"],
            ),
            actor="web_ui",
        )
        note = await note_svc.create(
            JournalEntryCreate(
                content="The imported knowledge pack should restore this note and keep it searchable.",
                type="finding",
                source="web_ui",
                phase="validation",
                related_decisions=[decision.id],
                related_literature=[literature.id],
                tags=["export", "import"],
            ),
            actor="web_ui",
        )
        artifact = await artifact_svc.register(
            filepath=str(artifact_path),
            filename="reference.txt",
            created_by="web_ui",
            metadata={"kind": "test"},
        )

        export_svc = KnowledgePackService(db, project_id="proj_export")
        pack_path, _ = await export_svc.export_pack()

        import_svc = KnowledgePackService(db)
        with open(pack_path, "rb") as pack_file:
            result = await import_svc.import_pack(
                pack_file,
                project_id="proj_imported",
                project_name="Imported Copy",
            )

        imported_note = await db.fetchone(
            "SELECT * FROM journal WHERE project_id = ? ORDER BY created_at DESC LIMIT 1",
            ["proj_imported"],
        )
        imported_decision = await db.fetchone(
            "SELECT * FROM decisions WHERE project_id = ? ORDER BY created_at DESC LIMIT 1",
            ["proj_imported"],
        )
        imported_literature = await db.fetchone(
            "SELECT * FROM literature WHERE project_id = ? ORDER BY created_at DESC LIMIT 1",
            ["proj_imported"],
        )
        imported_artifact = await db.fetchone(
            "SELECT * FROM artifacts WHERE project_id = ? ORDER BY created_at DESC LIMIT 1",
            ["proj_imported"],
        )
        imported_doi_rows = await db.fetchall(
            "SELECT id, project_id FROM literature WHERE doi = ? ORDER BY project_id",
            ["10.1234/rka-pack-roundtrip"],
        )
        fts_rows = await db.fetchall(
            "SELECT id FROM fts_journal WHERE fts_journal MATCH ? ORDER BY id",
            ["searchable"],
        )

        assert result.project_id == "proj_imported"
        assert result.project_name == "Imported Copy"
        assert result.source_project_id == "proj_export"
        assert result.imported_counts["journal"] == 1
        assert result.imported_counts["decisions"] == 1
        assert result.imported_counts["literature"] == 1
        assert result.imported_counts["artifacts"] == 1
        assert result.artifact_files_restored == 1

        assert imported_note is not None
        assert imported_decision is not None
        assert imported_literature is not None
        assert imported_artifact is not None

        assert imported_note["id"] != note.id
        assert imported_decision["id"] != decision.id
        assert imported_literature["id"] != literature.id
        assert imported_artifact["id"] != artifact["id"]

        assert json.loads(imported_note["related_decisions"]) == [imported_decision["id"]]
        assert json.loads(imported_note["related_literature"]) == [imported_literature["id"]]
        assert json.loads(imported_literature["related_decisions"]) == [imported_decision["id"]]

        assert imported_literature["doi"] == "10.1234/rka-pack-roundtrip"
        assert [(row["project_id"], row["id"]) for row in imported_doi_rows] == [
            ("proj_export", literature.id),
            ("proj_imported", imported_literature["id"]),
        ]

        assert Path(imported_artifact["filepath"]).exists()
        assert Path(imported_artifact["filepath"]).read_text(encoding="utf-8") == "artifact payload"
        assert sorted(row["id"] for row in fts_rows) == sorted([note.id, imported_note["id"]])
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_knowledge_pack_round_trip_preserves_interpretation_promotion_lineage(
    tmp_path: Path,
) -> None:
    db = await _make_db(tmp_path / "interpretation-round-trip.db")
    try:
        await ProjectService(db).create_project(
            ProjectCreate(id="proj_interpretation_export", name="Interpretation Export"),
            actor="system",
        )
        note = await NoteService(db, project_id="proj_interpretation_export").create(
            JournalEntryCreate(
                content="The isolated evaluation measured 42 ms under the configured workload.",
                type="log",
                source="executor",
                confidence="tested",
            ),
            actor="executor",
        )
        staging = InterpretationService(db, project_id="proj_interpretation_export")
        candidate = await staging.create(
            InterpretationCandidateCreate(
                source_type="journal",
                source_id=note.id,
                locator_kind="record",
                locator_value="full_record",
                statement="The isolated evaluation measured 42 ms.",
                epistemic_kind="observation",
                scope_conditions=["configured workload"],
                uncertainty="low",
                falsifier="A repeated evaluation does not reproduce the measurement.",
                proposed_claim_type="result",
                created_by="executor",
                extraction_tool="pytest",
            )
        )
        reviewing = await staging.triage(
            candidate.id,
            InterpretationTriage(
                action="start_review",
                expected_revision=candidate.revision,
                actor="brain",
            ),
        )
        promoted = await staging.triage(
            candidate.id,
            InterpretationTriage(
                action="promote",
                expected_revision=reviewing.revision,
                actor="brain",
                reason="Checked the exact journal record and its stated scope.",
                grounding_verified=True,
                claim_confidence=0.82,
            ),
        )

        pack_path, _ = await KnowledgePackService(
            db,
            project_id="proj_interpretation_export",
        ).export_pack()
        with open(pack_path, "rb") as pack_file:
            result = await KnowledgePackService(db).import_pack(
                pack_file,
                project_id="proj_interpretation_import",
                project_name="Interpretation Import",
            )

        imported_candidate_row = await db.fetchone(
            "SELECT id FROM interpretation_candidates WHERE project_id = ?",
            [result.project_id],
        )
        assert imported_candidate_row is not None
        imported = await InterpretationService(
            db,
            project_id=result.project_id,
        ).get_detail(imported_candidate_row["id"])
        assert imported is not None
        assert imported.id != candidate.id
        assert imported.source_id != note.id
        assert imported.review_status == "resolved"
        assert imported.disposition == "promoted"
        assert imported.active_claim_id is not None
        assert imported.active_claim_id != promoted.active_claim_id
        assert imported.scope_conditions == ["configured workload"]
        assert [event.action for event in imported.review_events] == [
            "created",
            "start_review",
            "promote",
        ]
        assert imported.promotions[0].claim_id == imported.active_claim_id
        assert imported.promotions[0].status == "active"
        imported_claim = await db.fetchone(
            "SELECT * FROM claims WHERE id = ? AND project_id = ?",
            [imported.active_claim_id, result.project_id],
        )
        assert imported_claim["source_entry_id"] == imported.source_id
        assert imported_claim["verified"] == 1
        assert imported_claim["evidence_status"] == "unassessed"
        assert imported_claim["scope_revision"] == 1
        imported_scope = await ClaimService(
            db,
            project_id=result.project_id,
        ).get_scope_history(imported.active_claim_id)
        assert imported_scope is not None
        assert imported_scope.scope_readiness == "incomplete"
        assert imported_scope.current is not None
        assert imported_scope.current.source_candidate_id == imported.id
        assert imported_scope.current.conditions[0].value == "configured workload"
        assert imported_scope.current.falsifier_status == "applicable"
        assert await db.fetchall("PRAGMA foreign_key_check") == []
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_pack_rekey_keeps_current_claim_scope_hash_current(tmp_path: Path) -> None:
    db = await _make_db(tmp_path / "scope-hash-rekey.db")
    try:
        await ProjectService(db).create_project(
            ProjectCreate(id="proj_scope_source", name="Scope Source"),
            actor="system",
        )
        note = await NoteService(db, project_id="proj_scope_source").create(
            JournalEntryCreate(
                content="Measured the effect in the isolated test environment.",
                type="finding",
                source="executor",
                confidence="tested",
            ),
            actor="executor",
        )
        claims = ClaimService(db, project_id="proj_scope_source")
        claim = await claims.create(
            ClaimCreate(
                source_entry_id=note.id,
                claim_type="result",
                content=f"The result recorded in {note.id} holds in the isolated test.",
                confidence=0.8,
                verified=True,
            ),
            actor="executor",
        )
        source_scope = await claims.append_scope(
            claim.id,
            ClaimScopeWrite(
                expected_revision=0,
                actor="brain",
                reason="Reviewed against the source entry.",
                conditions=[
                    ClaimScopeCondition(
                        kind="environment",
                        key="test_environment",
                        operator="equals",
                        value="isolated",
                    )
                ],
                uncertainty="low",
                extension_policy="exact_only",
                prohibited_extensions=["production deployment"],
                falsifier_status="applicable",
                falsifier="A repeated isolated test does not reproduce the result.",
                review_status="reviewed",
            ),
        )
        assert source_scope.scope_readiness == "ready"

        pack_path, _ = await KnowledgePackService(
            db,
            project_id="proj_scope_source",
        ).export_pack()
        with Path(pack_path).open("rb") as pack_file:
            await KnowledgePackService(db).import_pack(
                pack_file,
                project_id="proj_scope_target",
                project_name="Scope Target",
                defer_indexing=True,
            )

        imported_claim_row = await db.fetchone(
            "SELECT id, content FROM claims WHERE project_id = ?",
            ["proj_scope_target"],
        )
        imported_note_row = await db.fetchone(
            "SELECT id FROM journal WHERE project_id = ?",
            ["proj_scope_target"],
        )
        assert imported_claim_row is not None
        assert imported_note_row is not None
        assert note.id not in imported_claim_row["content"]
        assert imported_note_row["id"] in imported_claim_row["content"]

        imported_scope = await ClaimService(
            db,
            project_id="proj_scope_target",
        ).get_scope_history(imported_claim_row["id"])
        assert imported_scope is not None
        assert imported_scope.scope_readiness == "ready"
        assert all(
            finding.code != "CLAIM_SCOPE_STALE"
            for finding in imported_scope.findings
        )
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_knowledge_pack_export_rejects_stale_artifact_file(
    tmp_path: Path,
) -> None:
    db = await _make_db(tmp_path / "stale-artifact-export.db")
    artifact_path = tmp_path / "stale.txt"
    artifact_path.write_bytes(b"registered bytes")

    try:
        await ProjectService(db).create_project(
            ProjectCreate(id="proj_stale_artifact", name="Stale Artifact"),
            actor="system",
        )
        artifact = await ArtifactService(
            db,
            project_id="proj_stale_artifact",
            file_policy=FileAccessPolicy([tmp_path]),
        ).register(
            filepath=str(artifact_path),
            created_by="system",
        )
        artifact_path.write_bytes(b"bytes changed after registration")

        with pytest.raises(
            ValueError,
            match=rf"Artifact '{artifact['id']}' content hash mismatch.*export",
        ):
            await KnowledgePackService(
                db,
                project_id="proj_stale_artifact",
            ).export_pack()
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_knowledge_pack_export_rejects_missing_artifact_file(
    tmp_path: Path,
) -> None:
    db = await _make_db(tmp_path / "missing-artifact-export.db")
    artifact_path = tmp_path / "missing-after-registration.txt"
    artifact_path.write_bytes(b"registered bytes")
    try:
        await ProjectService(db).create_project(
            ProjectCreate(id="proj_missing_export", name="Missing Export"),
            actor="system",
        )
        artifact = await ArtifactService(
            db,
            project_id="proj_missing_export",
            file_policy=FileAccessPolicy([tmp_path]),
        ).register(filepath=str(artifact_path), created_by="system")
        artifact_path.unlink()

        with pytest.raises(
            ValueError,
            match=rf"Artifact '{artifact['id']}'.*registered file is missing",
        ):
            await KnowledgePackService(
                db,
                project_id="proj_missing_export",
            ).export_pack()
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_knowledge_pack_import_rejects_corrupted_bundled_artifact(
    tmp_path: Path,
) -> None:
    db = await _make_db(tmp_path / "corrupted-artifact-import.db")
    artifact_path = tmp_path / "source.txt"
    original_bytes = b"artifact bytes recorded by ArtifactService"
    artifact_path.write_bytes(original_bytes)

    try:
        await ProjectService(db).create_project(
            ProjectCreate(id="proj_artifact_source", name="Artifact Source"),
            actor="system",
        )
        artifact = await ArtifactService(
            db,
            project_id="proj_artifact_source",
            file_policy=FileAccessPolicy([tmp_path]),
        ).register(
            filepath=str(artifact_path),
            created_by="system",
        )
        pack_path, _ = await KnowledgePackService(
            db,
            project_id="proj_artifact_source",
        ).export_pack()

        corrupted_pack = tmp_path / "corrupted.rka-pack.zip"
        with zipfile.ZipFile(pack_path) as source, zipfile.ZipFile(
            corrupted_pack,
            "w",
            compression=zipfile.ZIP_DEFLATED,
        ) as destination:
            for member in source.infolist():
                payload = source.read(member.filename)
                if member.filename.startswith("artifacts/"):
                    payload = b"corrupted but internally valid zip bytes"
                destination.writestr(member, payload)

        with corrupted_pack.open("rb") as pack_file:
            with pytest.raises(
                ValueError,
                match=r"Artifact '.*' content hash mismatch.*import",
            ):
                await KnowledgePackService(db).import_pack(
                    pack_file,
                    project_id="proj_artifact_corrupted",
                    project_name="Corrupted Artifact Copy",
                )

        assert artifact["duplicate"] is False
        assert hashlib.sha256(original_bytes).hexdigest() == (
            await db.fetchone(
                "SELECT content_hash FROM artifacts WHERE id = ?",
                [artifact["id"]],
            )
        )["content_hash"]
        assert await db.fetchone(
            "SELECT id FROM projects WHERE id = ?",
            ["proj_artifact_corrupted"],
        ) is None
        storage_root = tmp_path / "knowledge-packs"
        assert not (storage_root / "proj_artifact_corrupted").exists()
        assert list(storage_root.glob(".rka-import-*")) == []
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_knowledge_pack_import_with_mission_motivated_by_decision(tmp_path: Path):
    """Import must succeed when missions reference decisions via motivated_by_decision FK."""
    db = await _make_db(tmp_path / "mission-fk.db")

    try:
        project_svc = ProjectService(db)
        await project_svc.create_project(
            ProjectCreate(id="proj_src", name="Source", description="src"),
            actor="system",
        )
        decision_svc = DecisionService(db, project_id="proj_src")
        mission_svc = MissionService(db, project_id="proj_src")

        decision = await decision_svc.create(
            DecisionCreate(
                question="Which broker to use?",
                decided_by="pi",
                phase="design",
            ),
            actor="pi",
        )
        mission = await mission_svc.create(
            MissionCreate(
                phase="design",
                objective="Evaluate broker options",
                motivated_by_decision=decision.id,
            ),
            actor="executor",
        )

        export_svc = KnowledgePackService(db, project_id="proj_src")
        pack_path, _ = await export_svc.export_pack()

        import_svc = KnowledgePackService(db)
        with open(pack_path, "rb") as pack_file:
            result = await import_svc.import_pack(
                pack_file,
                project_id="proj_dst",
                project_name="Destination",
            )

        assert result.imported_counts["missions"] == 1
        assert result.imported_counts["decisions"] == 1

        imported_mission = await db.fetchone(
            "SELECT * FROM missions WHERE project_id = ?", ["proj_dst"]
        )
        imported_decision = await db.fetchone(
            "SELECT * FROM decisions WHERE project_id = ? LIMIT 1", ["proj_dst"]
        )

        assert imported_mission is not None
        assert imported_decision is not None
        assert imported_mission["id"] != mission.id
        assert imported_mission["motivated_by_decision"] == imported_decision["id"]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_knowledge_pack_import_remaps_decision_related_journal(tmp_path: Path):
    """Import must remap decision.related_journal JSON references."""
    db = await _make_db(tmp_path / "dec-journal.db")

    try:
        project_svc = ProjectService(db)
        await project_svc.create_project(
            ProjectCreate(id="proj_src", name="Source", description="src"),
            actor="system",
        )
        decision_svc = DecisionService(db, project_id="proj_src")
        note_svc = NoteService(db, project_id="proj_src")

        note = await note_svc.create(
            JournalEntryCreate(content="Background analysis.", type="note"),
            actor="executor",
        )
        await decision_svc.create(
            DecisionCreate(
                question="Which approach?",
                decided_by="brain",
                phase="design",
                related_journal=[note.id],
            ),
            actor="brain",
        )

        export_svc = KnowledgePackService(db, project_id="proj_src")
        pack_path, _ = await export_svc.export_pack()

        import_svc = KnowledgePackService(db)
        with open(pack_path, "rb") as pack_file:
            await import_svc.import_pack(
                pack_file,
                project_id="proj_dst",
                project_name="Destination",
            )

        imported_decision = await db.fetchone(
            "SELECT * FROM decisions WHERE project_id = ?", ["proj_dst"]
        )
        imported_note = await db.fetchone(
            "SELECT * FROM journal WHERE project_id = ?", ["proj_dst"]
        )

        assert imported_decision is not None
        assert imported_note is not None
        related_journal = json.loads(imported_decision["related_journal"])
        assert related_journal == [imported_note["id"]]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_knowledge_pack_import_rejects_duplicate_target_project_name(tmp_path: Path):
    db = await _make_db(tmp_path / "target-name.db")

    try:
        project_svc = ProjectService(db)
        await project_svc.create_project(
            ProjectCreate(id="proj_export", name="Export Source", description="pack source"),
            actor="system",
        )
        note_svc = NoteService(db, project_id="proj_export")
        await note_svc.create(
            JournalEntryCreate(content="Imported project names must stay unique.", type="finding"),
            actor="pi",
        )

        export_svc = KnowledgePackService(db, project_id="proj_export")
        pack_path, _ = await export_svc.export_pack()

        with open(pack_path, "rb") as pack_file:
            with pytest.raises(ValueError, match="Project name 'Export Source' already exists"):
                await KnowledgePackService(db).import_pack(
                    pack_file,
                    project_id="proj_clone",
                    project_name="Export Source",
                )
    finally:
        await db.close()


# Defect 3 (mis_01KR1Z28QW9WYXG4VV8PGYWD8G T5):
# _sync_imported_indexes now iterates claims and evidence_clusters; the import
# transaction now runs check_integrity before commit and rolls back on
# critical issues; the success path repairs non-critical claim_count drift.


from rka.services.knowledge_pack import (  # noqa: E402
    KnowledgePackIntegrityError,
    PACK_SCHEMA_VERSION,
)


def _write_synthetic_pack(
    pack_path: Path,
    *,
    source_project_id: str,
    source_project_name: str,
    tables: dict[str, list[dict]],
) -> None:
    """Build a minimal pack zip with the supplied tables payload.

    Used by integrity-gate tests that need to inject malformed rows the
    export path would never produce on its own.
    """
    manifest = {
        "pack_format_version": PACK_SCHEMA_VERSION,
        "schema_version": 21,  # DB migration number; advisory only
        "project": {
            "id": source_project_id,
            "name": source_project_name,
            "description": "synthetic pack",
            "created_by": "system",
        },
        "project_state": None,
        "tables": tables,
        "table_counts": {k: len(v) for k, v in tables.items()},
    }
    with zipfile.ZipFile(pack_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest, indent=2, sort_keys=True))


@pytest.mark.asyncio
async def test_import_deduplicates_legacy_memberships_and_repairs_count(
    tmp_path: Path,
) -> None:
    pack_path = tmp_path / "legacy-memberships.rka-pack.zip"
    source_project_id = "proj_legacy_memberships_src"
    _write_synthetic_pack(
        pack_path,
        source_project_id=source_project_id,
        source_project_name="Legacy Membership Source",
        tables={
            "journal": [
                {
                    "id": "jrn_legacy_membership",
                    "type": "finding",
                    "content": "Legacy source evidence.",
                    "source": "executor",
                    "project_id": source_project_id,
                }
            ],
            "claims": [
                {
                    "id": "clm_legacy_membership",
                    "source_entry_id": "jrn_legacy_membership",
                    "claim_type": "evidence",
                    "content": "One claim was assigned twice.",
                    "project_id": source_project_id,
                }
            ],
            "evidence_clusters": [
                {
                    "id": "ecl_legacy_membership",
                    "label": "Legacy membership cluster",
                    "claim_count": 2,
                    "project_id": source_project_id,
                }
            ],
            "claim_edges": [
                {
                    "id": "ced_legacy_first",
                    "source_claim_id": "clm_legacy_membership",
                    "cluster_id": "ecl_legacy_membership",
                    "relation": "member_of",
                    "confidence": 0.25,
                    "project_id": source_project_id,
                },
                {
                    "id": "ced_legacy_duplicate",
                    "source_claim_id": "clm_legacy_membership",
                    "cluster_id": "ecl_legacy_membership",
                    "relation": "member_of",
                    "confidence": 0.75,
                    "project_id": source_project_id,
                },
            ],
        },
    )
    db = await _make_db(tmp_path / "legacy-memberships.db")
    try:
        with pack_path.open("rb") as pack_file:
            result = await KnowledgePackService(db).import_pack(
                pack_file,
                project_id="proj_legacy_memberships_dst",
                project_name="Legacy Membership Destination",
                defer_indexing=True,
            )

        assert result.imported_counts["claim_edges"] == 1
        assert any(
            issue["category"] == "claim_count_mismatch"
            for issue in result.integrity_issues
        )
        assert await db.fetchone(
            """SELECT COUNT(*) AS edges, MIN(confidence) AS confidence
               FROM claim_edges
               WHERE project_id = 'proj_legacy_memberships_dst'
                 AND relation = 'member_of'"""
        ) == {"edges": 1, "confidence": 0.25}
        assert await db.fetchone(
            """SELECT claim_count FROM evidence_clusters
               WHERE project_id = 'proj_legacy_memberships_dst'"""
        ) == {"claim_count": 1}
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_import_embedding_sync_uses_explicit_target_project_scope(
    tmp_path: Path,
) -> None:
    pack_path = tmp_path / "embedding-scope.rka-pack.zip"
    _write_synthetic_pack(
        pack_path,
        source_project_id="proj_embedding_source",
        source_project_name="Embedding Source",
        tables={
            "journal": [
                {
                    "id": "jrn_embedding_source",
                    "type": "finding",
                    "content": "Imported evidence must use the target scope.",
                    "source": "executor",
                    "project_id": "proj_embedding_source",
                }
            ]
        },
    )

    db = await _make_db(tmp_path / "embedding-scope.db")
    embeddings = DeterministicEmbeddings(db)
    await _bind_embedding_generation(db, embeddings)
    service = KnowledgePackService(
        db,
        embeddings=embeddings,
        project_id="proj_unrelated_service_default",
    )
    try:
        with pack_path.open("rb") as pack_file:
            result = await service.import_pack(
                pack_file,
                project_id="proj_embedding_target",
                project_name="Embedding Target",
            )

        assert service.project_id == "proj_unrelated_service_default"
        from rka.services.worker import EnrichmentWorker
        assert not await db.fetchone("SELECT entity_id FROM embedding_metadata")
        assert await EnrichmentWorker(db=db, embeddings=embeddings).run_once()
        assert (await service.import_status(result.indexing["job_id"]))["state"] == "complete"
        imported = await db.fetchone(
            "SELECT id FROM journal WHERE project_id = ?",
            ["proj_embedding_target"],
        )
        assert imported is not None
        metadata = await db.fetchall("SELECT entity_type, entity_id, project_id FROM embedding_metadata")
        assert metadata == [{"entity_type": "journal", "entity_id": imported["id"], "project_id": "proj_embedding_target"}]
    finally:
        await db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("defer_indexing", [False, True], ids=["inline", "background"])
async def test_pack_import_rebuilds_searchable_artifact_and_figure_vectors(
    tmp_path: Path,
    defer_indexing: bool,
) -> None:
    db = await _make_db(tmp_path / "artifact-figure-indexes.db")
    artifact_path = tmp_path / "packet-loss.png"
    artifact_path.write_bytes(b"deterministic image fixture")
    try:
        await ProjectService(db).create_project(
            ProjectCreate(id="proj_media_source", name="Media Source"),
            actor="system",
        )
        source_artifacts = ArtifactService(db, project_id="proj_media_source", file_policy=FileAccessPolicy([tmp_path]))
        artifact = await source_artifacts.register(
            filepath=str(artifact_path),
            created_by="system",
            metadata={"topic": "packet loss"},
        )
        figure = await source_artifacts._store_figure(
            artifact_id=artifact["id"],
            page=1,
            caption="Packet loss over time",
            caption_confidence=0.95,
            summary="Packet loss decreases after tuning.",
            claims=[{"claim": "Tuning reduces packet loss", "confidence": 0.9}],
        )
        embeddings = DeterministicEmbeddings(db)
        await _bind_embedding_generation(db, embeddings)
        background_indexer = KnowledgePackService(db, embeddings=embeddings)
        assert await background_indexer.count_indexable("proj_media_source") == 2
        assert await background_indexer.index_project("proj_media_source") == 2
        from rka.services.embedding_jobs import EmbeddingJobs
        from rka.services.worker import EnrichmentWorker
        await EmbeddingJobs(db).request(embeddings, project_id="proj_media_source")
        assert await EnrichmentWorker(db=db, embeddings=embeddings).run_once()
        pack_path, _ = await KnowledgePackService(
            db,
            project_id="proj_media_source",
        ).export_pack()

        importer = KnowledgePackService(db, embeddings=embeddings)
        with Path(pack_path).open("rb") as pack_file:
            await importer.import_pack(
                pack_file,
                project_id="proj_media_target",
                project_name="Media Target",
                defer_indexing=defer_indexing,
            )

        # Both compatibility values leave vectors to the durable worker.
        assert await db.fetchall(
            "SELECT id FROM vec_artifacts WHERE project_id = ?", ["proj_media_target"],
        ) == []
        assert await importer.index_project("proj_media_target") == 2
        assert await EnrichmentWorker(db=db, embeddings=embeddings).run_once()

        imported_artifact = await db.fetchone(
            "SELECT id FROM artifacts WHERE project_id = ?",
            ["proj_media_target"],
        )
        imported_figure = await db.fetchone(
            "SELECT id FROM figures WHERE project_id = ?",
            ["proj_media_target"],
        )
        assert imported_artifact is not None
        assert imported_figure is not None
        assert imported_artifact["id"] != artifact["id"]
        assert imported_figure["id"] != figure["id"]

        assert await importer.count_indexable("proj_media_target") == 2
        vector_rows = await db.fetchall(
            """SELECT id, project_id, entity_type
               FROM vec_artifacts
               WHERE project_id = ?
               ORDER BY entity_type""",
            ["proj_media_target"],
        )
        assert vector_rows == [
            {
                "id": imported_artifact["id"],
                "project_id": "proj_media_target",
                "entity_type": "artifact",
            },
            {
                "id": imported_figure["id"],
                "project_id": "proj_media_target",
                "entity_type": "figure",
            },
        ]

        search = SearchService(
            db,
            embeddings=embeddings,
            project_id="proj_media_target",
        )
        artifact_hits = await search.search(
            "packet-loss.png",
            entity_types=["artifact"],
            limit=5,
        )
        figure_hits = await search.search(
            "packet loss after tuning",
            entity_types=["figure"],
            limit=5,
        )
        assert [hit.entity_id for hit in artifact_hits] == [imported_artifact["id"]]
        assert [hit.entity_id for hit in figure_hits] == [imported_figure["id"]]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_knowledge_pack_import_rejects_path_like_project_id_without_touching_sibling(
    tmp_path: Path,
) -> None:
    victim = tmp_path / "victim"
    victim.mkdir()
    sentinel = victim / "keep.txt"
    sentinel.write_text("do not delete", encoding="utf-8")
    pack_path = tmp_path / "path-traversal.rka-pack.zip"
    _write_synthetic_pack(
        pack_path,
        source_project_id="../victim",
        source_project_name="Traversal Source",
        tables={
            "artifacts": [
                {
                    "id": "art_traversal",
                    "filename": "payload.txt",
                    "filepath": "/source/payload.txt",
                    "pack_file": "artifacts/art_traversal/payload.txt",
                    "project_id": "../victim",
                }
            ]
        },
    )

    db = await _make_db(tmp_path / "path-traversal.db")
    try:
        with pack_path.open("rb") as pack_file:
            with pytest.raises(ValueError, match="cannot contain path separators"):
                await KnowledgePackService(db).import_pack(pack_file)

        assert sentinel.read_text(encoding="utf-8") == "do not delete"
        assert not (tmp_path / "knowledge-packs").exists()
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_knowledge_pack_import_rejects_artifact_without_bundled_file(
    tmp_path: Path,
) -> None:
    source_machine_path = tmp_path / "source-machine-artifact.txt"
    source_machine_path.write_bytes(b"local bytes must not satisfy a portable pack")
    pack_path = tmp_path / "artifact-without-bundle.rka-pack.zip"
    _write_synthetic_pack(
        pack_path,
        source_project_id="proj_unbundled_artifact_src",
        source_project_name="Unbundled Artifact Source",
        tables={
            "artifacts": [
                {
                    "id": "art_unbundled",
                    "filename": source_machine_path.name,
                    "filepath": str(source_machine_path),
                    "content_hash": hashlib.sha256(
                        source_machine_path.read_bytes()
                    ).hexdigest(),
                    "pack_file": None,
                    "extraction_status": "complete",
                    "project_id": "proj_unbundled_artifact_src",
                }
            ]
        },
    )

    db = await _make_db(tmp_path / "artifact-without-bundle.db")
    try:
        with pack_path.open("rb") as pack_file:
            with pytest.raises(ValueError, match="has no bundled file"):
                await KnowledgePackService(db).import_pack(
                    pack_file,
                    project_id="proj_unbundled_artifact_dst",
                    project_name="Unbundled Artifact Destination",
                )

        assert await db.fetchone(
            "SELECT id FROM projects WHERE id = ?",
            ["proj_unbundled_artifact_dst"],
        ) is None
        storage_root = tmp_path / "knowledge-packs"
        assert not (storage_root / "proj_unbundled_artifact_dst").exists()
        assert list(storage_root.glob(".rka-import-*")) == []
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_knowledge_pack_failed_artifact_restore_removes_only_staging_root(
    tmp_path: Path,
) -> None:
    pack_path = tmp_path / "missing-artifact.rka-pack.zip"
    _write_synthetic_pack(
        pack_path,
        source_project_id="proj_missing_artifact_src",
        source_project_name="Missing Artifact Source",
        tables={
            "artifacts": [
                {
                    "id": "art_missing",
                    "filename": "missing.txt",
                    "filepath": "/source/missing.txt",
                    "pack_file": "artifacts/art_missing/missing.txt",
                    "project_id": "proj_missing_artifact_src",
                }
            ]
        },
    )

    db = await _make_db(tmp_path / "missing-artifact.db")
    try:
        with pack_path.open("rb") as pack_file:
            with pytest.raises(ValueError, match="missing bundled artifact"):
                await KnowledgePackService(db).import_pack(
                    pack_file,
                    project_id="proj_missing_artifact_dst",
                    project_name="Missing Artifact Destination",
                )

        storage_root = tmp_path / "knowledge-packs"
        assert not (storage_root / "proj_missing_artifact_dst").exists()
        assert list(storage_root.glob(".rka-import-*")) == []
        assert await db.fetchone(
            "SELECT id FROM projects WHERE id = ?",
            ["proj_missing_artifact_dst"],
        ) is None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_knowledge_pack_import_preserves_preexisting_artifact_directory(
    tmp_path: Path,
) -> None:
    storage_root = tmp_path / "knowledge-packs"
    existing_root = storage_root / "proj_existing_files"
    existing_root.mkdir(parents=True)
    sentinel = existing_root / "keep.txt"
    sentinel.write_text("preexisting", encoding="utf-8")
    pack_path = tmp_path / "preexisting-artifact-root.rka-pack.zip"
    _write_synthetic_pack(
        pack_path,
        source_project_id="proj_existing_files_src",
        source_project_name="Existing Files Source",
        tables={
            "artifacts": [
                {
                    "id": "art_existing",
                    "filename": "payload.txt",
                    "filepath": "/source/payload.txt",
                    "pack_file": "artifacts/art_existing/payload.txt",
                    "project_id": "proj_existing_files_src",
                }
            ]
        },
    )
    with zipfile.ZipFile(pack_path, "a") as archive:
        archive.writestr("artifacts/art_existing/payload.txt", "payload")

    db = await _make_db(tmp_path / "preexisting-artifact-root.db")
    try:
        with pack_path.open("rb") as pack_file:
            with pytest.raises(ValueError, match="already exists"):
                await KnowledgePackService(db).import_pack(
                    pack_file,
                    project_id="proj_existing_files",
                    project_name="Existing Files Destination",
                )

        assert sentinel.read_text(encoding="utf-8") == "preexisting"
        assert list(storage_root.glob(".rka-import-*")) == []
        assert await db.fetchone(
            "SELECT id FROM projects WHERE id = ?",
            ["proj_existing_files"],
        ) is None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_knowledge_pack_import_indexes_imported_claims_and_clusters(tmp_path: Path):
    """T5: post-import, claim and cluster rows are in their FTS tables without a
    manual reindex. Pre-v2.3.4 _sync_imported_indexes stopped at missions, so
    imported claims/clusters were silently invisible to search.
    """
    db = await _make_db(tmp_path / "claim-cluster-fts.db")
    try:
        project_svc = ProjectService(db)
        await project_svc.create_project(
            ProjectCreate(id="proj_pack_src", name="Pack Source", description="s"),
            actor="system",
        )

        # Seed a journal entry as the FK target for the claim's source_entry_id.
        note_svc = NoteService(db, project_id="proj_pack_src")
        seed = await note_svc.create(
            JournalEntryCreate(content="seed entry for claim source.", type="note"),
            actor="pi",
        )

        # Insert one cluster + one claim directly so they show up in the export.
        await db.execute(
            """INSERT INTO evidence_clusters (id, label, synthesis, project_id)
               VALUES (?, ?, ?, ?)""",
            ["ecl_t5_a", "Latency cluster",
             "Synthesis: tuned configuration consistently lowers tail latency.",
             "proj_pack_src"],
        )
        await db.execute(
            """INSERT INTO claims
               (id, source_entry_id, claim_type, content, confidence, project_id)
               VALUES (?, ?, ?, ?, ?, ?)""",
            ["clm_t5_a", seed.id, "observation",
             "Tuned configuration reduces 99th-percentile latency by 18 percent.",
             0.85, "proj_pack_src"],
        )
        await db.commit()

        export_svc = KnowledgePackService(db, project_id="proj_pack_src")
        pack_path, _ = await export_svc.export_pack()

        with open(pack_path, "rb") as pack_file:
            await KnowledgePackService(db).import_pack(
                pack_file, project_id="proj_pack_dst", project_name="Pack Dest",
            )

        # FTS hit on the imported claim by content keyword.
        claim_hits = await db.fetchall(
            "SELECT id FROM fts_claims WHERE fts_claims MATCH ?", ["latency"],
        )
        # FTS hit on the imported cluster by label keyword.
        cluster_hits = await db.fetchall(
            "SELECT id FROM fts_clusters WHERE fts_clusters MATCH ?", ["Latency"],
        )

        # Both source and imported (remapped) IDs must be FTS-visible.
        all_imported_claim_ids = await db.fetchall(
            "SELECT id FROM claims WHERE project_id = ?", ["proj_pack_dst"],
        )
        all_imported_cluster_ids = await db.fetchall(
            "SELECT id FROM evidence_clusters WHERE project_id = ?", ["proj_pack_dst"],
        )
        imported_claim_ids = {r["id"] for r in all_imported_claim_ids}
        imported_cluster_ids = {r["id"] for r in all_imported_cluster_ids}

        fts_claim_ids = {r["id"] for r in claim_hits}
        fts_cluster_ids = {r["id"] for r in cluster_hits}

        assert imported_claim_ids and imported_claim_ids.issubset(fts_claim_ids), (
            f"Imported claims missing from fts_claims: imported={imported_claim_ids} "
            f"fts={fts_claim_ids}"
        )
        assert imported_cluster_ids and imported_cluster_ids.issubset(fts_cluster_ids), (
            f"Imported clusters missing from fts_clusters: imported={imported_cluster_ids} "
            f"fts={fts_cluster_ids}"
        )
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_knowledge_pack_import_rolls_back_on_orphan_entity_link(tmp_path: Path):
    """T5: a pack carrying an entity_link whose target_id points at a
    non-existent entity must roll back (orphaned_entity_link_targets is one of
    the four critical-integrity categories) and leave NO rows for the target
    project. Pre-v2.3.4 the integrity check was advisory-only; partial state
    landed.
    """
    pack_path = tmp_path / "synthetic-orphan.rka-pack.zip"
    _write_synthetic_pack(
        pack_path,
        source_project_id="proj_orphan_src",
        source_project_name="Orphan Source",
        tables={
            "journal": [{
                "id": "jrn_t5_orphan", "type": "note",
                "content": "Will be rolled back.", "source": "pi",
                "confidence": "tested", "status": "active",
                "project_id": "proj_orphan_src",
            }],
            # entity_link references a decision_id that doesn't exist in the
            # pack — orphan target by construction.
            "entity_links": [{
                "id": "lnk_t5_orphan",
                "source_type": "journal", "source_id": "jrn_t5_orphan",
                "link_type": "references",
                "target_type": "decision", "target_id": "dec_does_not_exist",
                "project_id": "proj_orphan_src",
            }],
        },
    )

    db = await _make_db(tmp_path / "orphan-rollback.db")
    try:
        with open(pack_path, "rb") as pack_file:
            with pytest.raises(KnowledgePackIntegrityError) as excinfo:
                await KnowledgePackService(db).import_pack(
                    pack_file,
                    project_id="proj_orphan_dst",
                    project_name="Orphan Dest",
                )
        # The error carries the structured findings.
        cats = {issue["category"] for issue in excinfo.value.issues}
        assert "orphaned_entity_link_targets" in cats

        # Critical: no rows for the target project survived. Check across
        # projects, journal, and entity_links.
        assert (await db.fetchall(
            "SELECT id FROM projects WHERE id = ?", ["proj_orphan_dst"],
        )) == []
        assert (await db.fetchall(
            "SELECT id FROM journal WHERE project_id = ?", ["proj_orphan_dst"],
        )) == []
        assert (await db.fetchall(
            "SELECT id FROM entity_links WHERE project_id = ?", ["proj_orphan_dst"],
        )) == []
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_knowledge_pack_import_recomputes_stale_claim_count(tmp_path: Path):
    """T5 (Brain success-path addition): when the post-insert integrity check
    reports a non-critical claim_count_mismatch, the import commits and the
    success path runs a project-scoped recompute so the row lands with the
    correct derived count instead of the stale value the pack carried.
    """
    pack_path = tmp_path / "synthetic-stale-count.rka-pack.zip"
    _write_synthetic_pack(
        pack_path,
        source_project_id="proj_stale_src",
        source_project_name="Stale Count Source",
        tables={
            "journal": [{
                "id": "jrn_t5_stale", "type": "note",
                "content": "seed", "source": "pi",
                "confidence": "tested", "status": "active",
                "project_id": "proj_stale_src",
            }],
            "claims": [{
                "id": "clm_t5_stale_1", "source_entry_id": "jrn_t5_stale",
                "claim_type": "observation",
                "content": "single member claim",
                "confidence": 0.7, "project_id": "proj_stale_src",
            }],
            "evidence_clusters": [{
                "id": "ecl_t5_stale", "label": "Stale-count cluster",
                # Pack carries claim_count = 99 (deliberately wrong).
                "claim_count": 99,
                "project_id": "proj_stale_src",
            }],
            "claim_edges": [{
                "id": "clmedge_t5_stale_member", "source_claim_id": "clm_t5_stale_1",
                "target_claim_id": None, "cluster_id": "ecl_t5_stale",
                "relation": "member_of", "confidence": 0.9,
                "project_id": "proj_stale_src",
            }],
        },
    )

    db = await _make_db(tmp_path / "stale-count.db")
    try:
        with open(pack_path, "rb") as pack_file:
            result = await KnowledgePackService(db).import_pack(
                pack_file,
                project_id="proj_stale_dst",
                project_name="Stale Count Dest",
            )

        # Issue surfaced (proves the gate ran) and import committed (success
        # path returned a result).
        issue_cats = {i["category"] for i in result.integrity_issues}
        assert "claim_count_mismatch" in issue_cats

        rows = await db.fetchall(
            "SELECT id, claim_count FROM evidence_clusters WHERE project_id = ?",
            ["proj_stale_dst"],
        )
        assert len(rows) == 1
        assert rows[0]["claim_count"] == 1, (
            "Success-path recompute should have repaired the stale claim_count "
            f"(99 → 1 member_of edge); got {rows[0]['claim_count']}."
        )
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_export_uses_one_snapshot_under_concurrent_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "snapshot-export.db"
    db = await _make_db(db_path)
    writer = Database(str(db_path))
    await writer.connect()
    try:
        await ProjectService(db).create_project(
            ProjectCreate(id="proj_snapshot", name="Snapshot Source"),
            actor="system",
        )
        await db.execute(
            """INSERT INTO manuscripts
               (id, project_id, title, venue)
               VALUES ('man_snapshot', 'proj_snapshot',
                       'Snapshot manuscript', 'Test Venue')"""
        )
        await db.commit()

        service = KnowledgePackService(db, project_id="proj_snapshot")
        literature_read = asyncio.Event()
        resume_export = asyncio.Event()
        original_export_rows = service._export_rows_for_table

        async def pause_after_literature(table: str, project_id: str):
            rows = await original_export_rows(table, project_id)
            if table == "literature":
                literature_read.set()
                await resume_export.wait()
            return rows

        monkeypatch.setattr(
            service,
            "_export_rows_for_table",
            pause_after_literature,
        )
        export_task = asyncio.create_task(service.export_pack())
        await literature_read.wait()
        async with writer.transaction():
            await writer.execute(
                """INSERT INTO literature (id, title, project_id)
                   VALUES ('lit_snapshot_late', 'Late literature',
                           'proj_snapshot')"""
            )
            await writer.execute(
                """INSERT INTO manuscript_reference_members
                   (id, manuscript_id, project_id, citation_key,
                    literature_id)
                   VALUES ('mrf_snapshot_late', 'man_snapshot',
                           'proj_snapshot', 'late2026',
                           'lit_snapshot_late')"""
            )
        resume_export.set()
        pack_path, _ = await export_task

        with zipfile.ZipFile(pack_path) as archive:
            manifest = json.loads(archive.read("manifest.json"))
        assert manifest["rka_version"] == __version__
        literature_ids = {
            row["id"] for row in manifest["tables"]["literature"]
        }
        member_ids = {
            row["id"]
            for row in manifest["tables"]["manuscript_reference_members"]
        }
        assert "lit_snapshot_late" not in literature_ids
        assert "mrf_snapshot_late" not in member_ids

        with open(pack_path, "rb") as pack_file:
            await KnowledgePackService(db).import_pack(
                pack_file,
                project_id="proj_snapshot_copy",
                project_name="Snapshot Copy",
            )
    finally:
        await writer.close()
        await db.close()


@pytest.mark.asyncio
async def test_topics_and_context_snapshots_rekey_on_same_database_copy(
    tmp_path: Path,
) -> None:
    db = await _make_db(tmp_path / "derived-rekey.db")
    try:
        await ProjectService(db).create_project(
            ProjectCreate(id="proj_derived_source", name="Derived Source"),
            actor="system",
        )
        await db.execute(
            """INSERT INTO journal
               (id, type, content, source, project_id)
               VALUES ('jrn_derived_source', 'note', 'source', 'executor',
                       'proj_derived_source')"""
        )
        await db.execute(
            """INSERT INTO claims
               (id, source_entry_id, claim_type, content, confidence, project_id)
               VALUES ('clm_derived_source', 'jrn_derived_source',
                       'observation', 'derived claim', 0.8,
                       'proj_derived_source')"""
        )
        await db.execute(
            """INSERT INTO topics (id, name, project_id)
               VALUES ('top_derived_parent', 'parent',
                       'proj_derived_source')"""
        )
        await db.execute(
            """INSERT INTO topics (id, name, parent_id, project_id)
               VALUES ('top_derived_child', 'child', 'top_derived_parent',
                       'proj_derived_source')"""
        )
        await db.execute(
            """INSERT INTO context_snapshots
               (id, entry_ids, query, project_id)
               VALUES ('ctx_derived_source', '["jrn_derived_source"]',
                       'source query', 'proj_derived_source')"""
        )
        await db.execute(
            """INSERT INTO entity_topics
               (topic_id, entity_type, entity_id, assigned_by)
               VALUES ('top_derived_child', 'claim', 'clm_derived_source',
                       'brain')"""
        )
        await db.commit()

        pack_path, _ = await KnowledgePackService(
            db,
            project_id="proj_derived_source",
        ).export_pack()
        with open(pack_path, "rb") as pack_file:
            await KnowledgePackService(db).import_pack(
                pack_file,
                project_id="proj_derived_copy",
                project_name="Derived Copy",
            )

        imported_journal = await db.fetchone(
            """SELECT id FROM journal
               WHERE project_id = 'proj_derived_copy'"""
        )
        imported_topics = await db.fetchall(
            """SELECT id, name, parent_id FROM topics
               WHERE project_id = 'proj_derived_copy'
               ORDER BY name"""
        )
        imported_context = await db.fetchone(
            """SELECT id, entry_ids FROM context_snapshots
               WHERE project_id = 'proj_derived_copy'"""
        )
        imported_claim = await db.fetchone(
            """SELECT id FROM claims
               WHERE project_id = 'proj_derived_copy'"""
        )
        assert imported_journal is not None
        assert imported_context is not None
        assert imported_claim is not None
        assert imported_context["id"] != "ctx_derived_source"
        assert json.loads(imported_context["entry_ids"]) == [
            imported_journal["id"]
        ]
        topics_by_name = {row["name"]: row for row in imported_topics}
        assert topics_by_name["parent"]["id"] != "top_derived_parent"
        assert topics_by_name["child"]["id"] != "top_derived_child"
        assert (
            topics_by_name["child"]["parent_id"]
            == topics_by_name["parent"]["id"]
        )
        imported_membership = await db.fetchone(
            """SELECT topic_id, entity_type, entity_id, assigned_by
               FROM entity_topics
               WHERE topic_id = ?""",
            [topics_by_name["child"]["id"]],
        )
        assert imported_membership == {
            "topic_id": topics_by_name["child"]["id"],
            "entity_type": "claim",
            "entity_id": imported_claim["id"],
            "assigned_by": "brain",
        }
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_export_rejects_cross_project_topic_membership(
    tmp_path: Path,
) -> None:
    db = await _make_db(tmp_path / "foreign-topic-membership.db")
    try:
        projects = ProjectService(db)
        await projects.create_project(
            ProjectCreate(id="proj_topic_owner", name="Topic Owner"),
            actor="system",
        )
        await projects.create_project(
            ProjectCreate(id="proj_foreign_entity", name="Foreign Entity"),
            actor="system",
        )
        await db.execute(
            """INSERT INTO topics (id, name, project_id)
               VALUES ('top_owner', 'owner topic', 'proj_topic_owner')"""
        )
        await db.execute(
            """INSERT INTO journal
               (id, type, content, source, project_id)
               VALUES ('jrn_foreign_topic_target', 'note', 'foreign',
                       'executor', 'proj_foreign_entity')"""
        )
        await db.execute(
            """INSERT INTO entity_topics
               (topic_id, entity_type, entity_id, assigned_by)
               VALUES ('top_owner', 'journal', 'jrn_foreign_topic_target',
                       'brain')"""
        )
        await db.commit()

        with pytest.raises(ValueError, match="outside the project or absent"):
            await KnowledgePackService(
                db,
                project_id="proj_topic_owner",
            ).export_pack()
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_include_logs_round_trip_preserves_hook_executions(
    tmp_path: Path,
) -> None:
    db = await _make_db(tmp_path / "hook-log-pack.db")
    try:
        await ProjectService(db).create_project(
            ProjectCreate(id="proj_hook_log", name="Hook Log Source"),
            actor="system",
        )
        await db.execute(
            """INSERT INTO hooks
               (id, event, project_id, handler_type, handler_config, name)
               VALUES ('hk_log_source', 'session_start', 'proj_hook_log',
                       'sql', '{}', 'log hook')"""
        )
        await db.execute(
            """INSERT INTO hook_executions
               (id, hook_id, project_id, status, payload)
               VALUES ('hkx_log_source', 'hk_log_source', 'proj_hook_log',
                       'success', '{"event":"session_start"}')"""
        )
        await db.execute(
            """INSERT INTO qa_sessions (id, project_id, title)
               VALUES ('qas_log_source', 'proj_hook_log', 'Portable QA')"""
        )
        await db.execute(
            """INSERT INTO qa_logs
               (id, session_id, question, answer, answer_structured, sources)
               VALUES ('qal_log_source', 'qas_log_source', 'What ran?',
                       'The hook ran.',
                       '{"hook_id":"hk_log_source"}',
                       '["hk_log_source"]')"""
        )
        await db.commit()

        pack_path, _ = await KnowledgePackService(
            db,
            project_id="proj_hook_log",
        ).export_pack(include_logs=True)
        with open(pack_path, "rb") as pack_file:
            result = await KnowledgePackService(db).import_pack(
                pack_file,
                project_id="proj_hook_log_copy",
                project_name="Hook Log Copy",
            )
        imported_hook = await db.fetchone(
            """SELECT id FROM hooks
               WHERE project_id = 'proj_hook_log_copy'"""
        )
        imported_execution = await db.fetchone(
            """SELECT id, hook_id, payload FROM hook_executions
               WHERE project_id = 'proj_hook_log_copy'"""
        )
        imported_session = await db.fetchone(
            """SELECT id FROM qa_sessions
               WHERE project_id = 'proj_hook_log_copy'"""
        )
        imported_log = await db.fetchone(
            """SELECT id, session_id, answer_structured, sources
               FROM qa_logs WHERE session_id = ?""",
            [imported_session["id"] if imported_session else ""],
        )
        assert result.imported_counts["hook_executions"] == 1
        assert result.imported_counts["qa_logs"] == 1
        assert imported_hook is not None
        assert imported_execution is not None
        assert imported_session is not None
        assert imported_log is not None
        assert imported_execution["id"] != "hkx_log_source"
        assert imported_execution["hook_id"] == imported_hook["id"]
        assert json.loads(imported_execution["payload"]) == {
            "event": "session_start"
        }
        assert imported_log["id"] != "qal_log_source"
        assert imported_log["session_id"] == imported_session["id"]
        assert json.loads(imported_log["answer_structured"]) == {
            "hook_id": imported_hook["id"]
        }
        assert json.loads(imported_log["sources"]) == [imported_hook["id"]]
    finally:
        await db.close()
