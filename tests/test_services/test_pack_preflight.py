"""I6: reject source corruption before rewriting, preserve arbitrary JSON text."""

import io
import json
import zipfile

import pytest

from rka.models.checkpoint import CheckpointCreate
from rka.models.claim import ClaimCreate
from rka.models.journal import JournalEntryCreate
from rka.models.mission import MissionCreate
from rka.services.checkpoints import CheckpointService
from rka.services.claims import ClaimService
from rka.services.knowledge_pack import KnowledgePackService, KnowledgePackIntegrityError

from rka.services.missions import MissionService
from rka.services.notes import NoteService
from rka.services.researcher_tools import ResearcherToolsService

PACK_MANIFEST_PATH = "manifest.json"


async def pack(db):
    note = await NoteService(db).create(
        JournalEntryCreate(content="portable source", source="executor")
    )
    claim = await ClaimService(db).create(
        ClaimCreate(source_entry_id=note.id, content="portable finding", claim_type="evidence")
    )
    mission = await MissionService(db).create(
        MissionCreate(objective="portable mission", phase="implementation")
    )
    checkpoint = await CheckpointService(db).create(
        CheckpointCreate(mission_id=mission.id, type="clarification", description="review")
    )
    path, _ = await KnowledgePackService(db).export_pack()
    with zipfile.ZipFile(path) as source:
        entries = {name: source.read(name) for name in source.namelist()}
    return entries, note, claim, mission, checkpoint


def rewrite(entries, mutate):
    entries = dict(entries)
    manifest = json.loads(entries[PACK_MANIFEST_PATH])
    mutate(manifest)
    entries[PACK_MANIFEST_PATH] = json.dumps(manifest).encode()
    result = io.BytesIO()
    with zipfile.ZipFile(result, "w") as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    result.seek(0)
    return result


@pytest.mark.parametrize(
    "table,column",
    [("checkpoints", "mission_id"), ("claims", "source_entry_id"), ("journal", "related_mission")],
)
async def test_required_dangling_reference_has_structured_issue_and_no_project(db, table, column):
    entries, *_ = await pack(db)
    raw = rewrite(
        entries, lambda manifest: manifest["tables"][table][0].update({column: "missing_reference"})
    )
    with pytest.raises(KnowledgePackIntegrityError) as err:
        await KnowledgePackService(db).import_pack(raw, "prj_rejected", "Rejected")
    assert any(item["table"] == table and item["column"] == column for item in err.value.issues)
    assert not await db.fetchone("SELECT id FROM projects WHERE id = 'prj_rejected'")


async def test_wrong_project_source_record_rejected(db):
    entries, *_ = await pack(db)
    raw = rewrite(
        entries, lambda manifest: manifest["tables"]["claims"][0].update(project_id="prj_foreign")
    )
    with pytest.raises(KnowledgePackIntegrityError) as err:
        await KnowledgePackService(db).import_pack(raw, "prj_rejected", "Rejected")
    assert any(item["category"] == "source_project_mismatch" for item in err.value.issues)


async def test_review_verdict_pack_roundtrip(db):
    entries, note, claim, *_ = await pack(db)
    svc = ResearcherToolsService(db)
    await svc.flag_stale(claim.id, "outdated", propagate=False)
    await svc.resolve_stale(claim.id, "historical", "reviewed", "pi", note.id)
    path, _ = await KnowledgePackService(db).export_pack()
    with open(path, "rb") as source:
        result = await KnowledgePackService(db).import_pack(
            source, "prj_reviewed_import", "Reviewed Import"
        )
    stored = await ClaimService(db, project_id=result.project_id).list()
    assert stored[0].staleness_verdict == "historical"
    assert not stored[0].currentness["is_current"]
    assert stored[0].staleness_resolution_journal_id != note.id
    assert stored[0].staleness_resolution == "reviewed"


def test_arbitrary_nested_json_strings_not_rewritten():
    service = KnowledgePackService(None)
    original = {
        "claim_id": "clm_old",
        "note": "clm_old",
        "nested": {"label": "clm_old", "comment": "mentions clm_old"},
        "opaque": ["clm_old"],
        "project_id": "prj_old",
    }
    rewritten = service._rewrite_nested_refs(original, {"clm_old": "clm_new"}, "prj_old", "prj_new")
    assert rewritten == {**original, "claim_id": "clm_new", "project_id": "prj_new"}


async def test_original_manifest_hash_rejected_before_remap(db):
    from rka.models.manuscript_native import ManuscriptCreate
    from rka.models.semantic_patch import ContextManifestCreate
    from rka.services.manuscript_native import NativeManuscriptService
    from rka.services.semantic_patch import SemanticPatchService

    manuscript = await NativeManuscriptService(db).create(
        ManuscriptCreate(title="source digest"), actor="pi"
    )
    await SemanticPatchService(db).create_context_manifest(
        ContextManifestCreate(
            origin="host_agent",
            provider="test",
            model="test",
            boundary="host_conversation",
            targets=[{"target_type": "manuscript", "target_id": manuscript.id}],
        )
    )
    entries, *_ = await pack(db)
    raw = rewrite(
        entries,
        lambda manifest: manifest["tables"]["semantic_patch_context_manifests"][0].update(
            manifest_hash="0" * 64
        ),
    )
    with pytest.raises(KnowledgePackIntegrityError) as err:
        await KnowledgePackService(db).import_pack(raw, "prj_bad_digest", "Bad digest")
    assert any(item["category"] == "source_manifest_hash_invalid" for item in err.value.issues)
    assert not await db.fetchone("SELECT id FROM projects WHERE id = 'prj_bad_digest'")


@pytest.mark.parametrize(
    "table,column,row",
    [
        (
            "decisions",
            "recommended_option_id",
            {"id": "dec_missing_option", "recommended_option_id": "opt_missing"},
        ),
        ("brain_notifications", "hook_id", {"id": "bnt_missing_hook", "hook_id": "hook_missing"}),
        ("checkpoints", "mission_id", {"id": "chk_null_parent", "mission_id": None}),
    ],
)
async def test_schema_catalog_catches_overlooked_foreign_keys(db, table, column, row):
    entries, *_ = await pack(db)

    def mutate(manifest):
        manifest["tables"].setdefault(table, []).append(
            {**row, "project_id": manifest["project"]["id"]}
        )
        manifest["table_counts"][table] = len(manifest["tables"][table])

    with pytest.raises(KnowledgePackIntegrityError) as err:
        await KnowledgePackService(db).import_pack(
            rewrite(entries, mutate), "prj_missing_fk", "Missing FK"
        )
    assert any(
        item["category"] == "missing_pack_reference"
        and item["table"] == table
        and item["column"] == column
        for item in err.value.issues
    )
    assert not await db.fetchone("SELECT id FROM projects WHERE id = 'prj_missing_fk'")


async def test_directive_dependencies_roundtrip_and_type_validation(db):
    from rka.models.decision import DecisionCreate
    from rka.services.decisions import DecisionService
    from rka.services.lifecycle import DirectiveDependencyService

    dec = await DecisionService(db).create(
        DecisionCreate(
            question="portable dependency", chosen="yes", phase="design", decided_by="brain"
        )
    )
    note = await NoteService(db).create(
        JournalEntryCreate(content="conditional", type="directive", source="executor")
    )
    await DirectiveDependencyService(db).record(note.id, dec.id, "pi", "only for this choice")
    entries, *_ = await pack(db)
    imported = await KnowledgePackService(db).import_pack(
        rewrite(entries, lambda _: None), "prj_deps_import", "Dependencies"
    )
    dependency = await db.fetchone(
        "SELECT * FROM directive_dependencies WHERE project_id = ?", [imported.project_id]
    )
    assert dependency["directive_id"] != note.id and dependency["decision_id"] != dec.id
    assert dependency["reason"] == "only for this choice"
    assert dependency["declared_by"] == "pi"

    def mutate(manifest):
        next(row for row in manifest["tables"]["journal"] if row["id"] == note.id)["type"] = "note"

    with pytest.raises(KnowledgePackIntegrityError) as err:
        await KnowledgePackService(db).import_pack(
            rewrite(entries, mutate), "prj_bad_dependency", "Bad dependency"
        )
    assert any(item["category"] == "invalid_directive_dependency" for item in err.value.issues)


async def test_legacy_pack_without_review_columns_imports_current(db):
    from rka.services.currentness import RESOLUTION_FIELDS

    entries, *_ = await pack(db)

    def mutate(manifest):
        manifest["tables"]["journal"][0]["related_decisions"] = "null"
        for row in manifest["tables"]["claims"]:
            for field in RESOLUTION_FIELDS:
                row.pop(field, None)

    result = await KnowledgePackService(db).import_pack(
        rewrite(entries, mutate), "prj_old_review_schema", "Legacy review schema"
    )
    entity = (await ClaimService(db, project_id=result.project_id).list())[0]
    assert entity.currentness["is_current"] and entity.staleness_verdict is None


def test_expanded_original_dependency_digest_cannot_be_repaired_by_remapping():
    import hashlib
    from rka.services.pack_integrity import original_hash_issues

    components = {"claim_id": "clm_old", "version": 1}
    digest = hashlib.sha256(
        json.dumps(components, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()
    row = {
        "id": "mck_old",
        "dependency_snapshot": json.dumps({"components": components, "sha256": digest}),
    }
    assert not original_hash_issues({"manuscript_checkpoints": [row]}, "proj_default")
    components["version"] = 2
    row["dependency_snapshot"] = json.dumps({"components": components, "sha256": digest})
    assert (
        original_hash_issues({"manuscript_checkpoints": [row]}, "proj_default")[0]["category"]
        == "source_dependency_hash_invalid"
    )
