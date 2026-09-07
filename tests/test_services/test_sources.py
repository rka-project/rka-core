"""Safe source registration, explicit admission, and pack portability."""

from __future__ import annotations

import base64
import hashlib
import json
import zipfile
from pathlib import Path

import pytest
from pydantic import ValidationError

from rka.infra.ids import generate_id
from rka.infra.file_access import FileAccessPolicy, FileAccessError
from rka.models.interpretation import (
    InterpretationCandidateCreate,
    InterpretationTriage,
)
from rka.models.sources import RegisterSourceRequest, SourceAdmissionCreate
from rka.models.project import ProjectCreate
from rka.services.interpretation import (
    InterpretationConflictError,
    InterpretationNotFoundError,
    InterpretationService,
)
from rka.services.knowledge_pack import KnowledgePackService
from rka.services.sources import SourceRegistrationError, SourceService
from rka.services.project import ProjectService


PROJECT = "proj_default"


async def _candidate(db, artifact_id: str, statement: str = "Reviewed statement."):
    return await InterpretationService(db, project_id=PROJECT).create(
        InterpretationCandidateCreate(
            source_type="artifact",
            source_id=artifact_id,
            locator_kind="record",
            locator_value="full_record",
            statement=statement,
            epistemic_kind="reported_fact",
            created_by="executor",
            extraction_tool="pytest",
        )
    )


async def _target_journal(db, entry_id: str = "jrn_source_target", project_id: str = PROJECT):
    await db.execute(
        """INSERT INTO journal
           (id, project_id, type, content, source, confidence)
           VALUES (?, ?, 'note', 'Reviewed canonical summary.', 'pi', 'tested')""",
        [entry_id, project_id],
    )
    return entry_id


def _paste(text: str = "exact supplied bytes", **updates) -> RegisterSourceRequest:
    values = {
        "source_kind": "pasted_text",
        "pasted_text": text,
        "title": "Lab note",
        "ownership_kind": "researcher",
        "provenance": {"collection": "local lab notebook"},
        "registered_by": "pi",
    }
    values.update(updates)
    return RegisterSourceRequest(**values)


@pytest.mark.asyncio
async def test_registration_is_hash_verified_idempotent_and_noncanonical(db, tmp_path: Path) -> None:
    service = SourceService(db, project_id=PROJECT, storage_root=tmp_path / "packs")
    before = {
        table: (await db.fetchone(f"SELECT COUNT(*) AS n FROM {table}"))["n"]
        for table in ("journal", "claims", "decisions")
    }

    first = await service.register(_paste())
    retry = await service.register(_paste())

    assert first.duplicate is False
    assert retry.duplicate is True
    assert retry.source.id == first.source.id
    assert first.source.content_hash == hashlib.sha256(b"exact supplied bytes").hexdigest()
    assert first.source.content_mode == "bytes"
    detail = await service.get(first.source.id)
    assert detail is not None
    artifact_path = Path(detail.artifact["filepath"])
    assert artifact_path.read_bytes() == b"exact supplied bytes"
    assert artifact_path.stat().st_mode & 0o777 == 0o600
    assert detail.admissions == []
    assert detail.interpretation_candidate_count == 0
    after = {
        table: (await db.fetchone(f"SELECT COUNT(*) AS n FROM {table}"))["n"]
        for table in ("journal", "claims", "decisions")
    }
    assert after == before
    assert (await db.fetchone("SELECT COUNT(*) AS n FROM artifacts"))["n"] == 1
    assert (await db.fetchone("SELECT COUNT(*) AS n FROM registered_sources"))["n"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["missing", "corrupt"])
async def test_duplicate_registration_fails_closed_for_damaged_managed_bytes(
    db,
    tmp_path: Path,
    damage: str,
) -> None:
    service = SourceService(db, project_id=PROJECT, storage_root=tmp_path / "packs")
    first = await service.register(_paste())
    detail = await service.get(first.source.id)
    assert detail is not None
    artifact_path = Path(detail.artifact["filepath"])
    if damage == "missing":
        artifact_path.unlink()
    else:
        artifact_path.write_bytes(b"corrupt replacement")

    with pytest.raises(SourceRegistrationError, match="managed artifact"):
        await service.register(_paste())


@pytest.mark.asyncio
async def test_base64_registration_preserves_exact_binary_bytes(db, tmp_path: Path) -> None:
    payload = b"\x00binary\xffsource"
    service = SourceService(db, project_id=PROJECT, storage_root=tmp_path / "packs")
    result = await service.register(
        RegisterSourceRequest(
            source_kind="file",
            content_base64=base64.b64encode(payload).decode("ascii"),
            filename="capture.bin",
            registered_by="executor",
        )
    )
    detail = await service.get(result.source.id)
    assert detail is not None
    assert Path(detail.artifact["filepath"]).read_bytes() == payload
    assert result.source.content_hash == hashlib.sha256(payload).hexdigest()


@pytest.mark.asyncio
@pytest.mark.parametrize("filename", ["..", "nested/source.bin", "x" * 256])
async def test_registration_rejects_unsafe_filename_without_orphan_directory(
    db,
    tmp_path: Path,
    filename: str,
) -> None:
    storage_root = tmp_path / "packs"
    service = SourceService(db, project_id=PROJECT, storage_root=storage_root)

    with pytest.raises(SourceRegistrationError, match="filename"):
        await service.register(
            RegisterSourceRequest(
                source_kind="file",
                content_base64=base64.b64encode(b"payload").decode("ascii"),
                filename=filename,
                registered_by="executor",
            )
        )

    registered_root = storage_root / PROJECT / "registered-sources"
    assert not registered_root.exists() or list(registered_root.iterdir()) == []
    assert (await db.fetchone("SELECT COUNT(*) AS n FROM artifacts"))["n"] == 0
    assert (await db.fetchone("SELECT COUNT(*) AS n FROM registered_sources"))["n"] == 0


@pytest.mark.asyncio
async def test_same_bytes_with_different_provenance_remain_distinct(db, tmp_path: Path) -> None:
    service = SourceService(db, project_id=PROJECT, storage_root=tmp_path / "packs")
    first = await service.register(_paste(provenance={"run": 1}))
    second = await service.register(_paste(provenance={"run": 2}))

    assert first.source.content_hash == second.source.content_hash
    assert first.source.manifest_hash != second.source.manifest_hash
    assert first.source.id != second.source.id
    assert first.source.artifact_id != second.source.artifact_id


@pytest.mark.asyncio
async def test_registration_rejects_non_json_provenance(db, tmp_path: Path) -> None:
    service = SourceService(db, project_id=PROJECT, storage_root=tmp_path / "packs")

    with pytest.raises(SourceRegistrationError, match="JSON-serializable"):
        await service.register(_paste(provenance={"invalid": float("nan")}))

    assert (await db.fetchone("SELECT COUNT(*) AS n FROM artifacts"))["n"] == 0
    assert (await db.fetchone("SELECT COUNT(*) AS n FROM registered_sources"))["n"] == 0


@pytest.mark.asyncio
async def test_locator_registration_preserves_manifest_without_fetching(db, tmp_path: Path) -> None:
    service = SourceService(db, project_id=PROJECT, storage_root=tmp_path / "packs")
    result = await service.register(
        RegisterSourceRequest(
            source_kind="repository",
            stable_locator="https://github.com/example/research/tree/abc123",
            title="Pinned repository",
            ownership_kind="third_party",
            provenance={"commit": "abc123"},
            registered_by="executor",
        )
    )
    detail = await service.get(result.source.id)
    assert detail is not None
    descriptor = json.loads(Path(detail.artifact["filepath"]).read_text())
    assert result.source.content_mode == "locator_manifest"
    assert descriptor["stable_locator"].endswith("/abc123")
    assert descriptor["provenance"] == {"commit": "abc123"}


@pytest.mark.asyncio
async def test_file_registration_rejects_unsafe_or_unverified_inputs(db, tmp_path: Path) -> None:
    payload = tmp_path / "payload.bin"
    payload.write_bytes(b"12345")
    symlink = tmp_path / "payload-link.bin"
    symlink.symlink_to(payload)
    service = SourceService(
        db,
        project_id=PROJECT,
        storage_root=tmp_path / "packs",
        max_bytes=4,
        file_policy=FileAccessPolicy([tmp_path]),
    )

    with pytest.raises(FileAccessError, match="symlink"):
        await service.register(
            RegisterSourceRequest(
                source_kind="file", filepath=str(symlink), registered_by="pi"
            )
        )
    with pytest.raises(FileAccessError, match="maximum size"):
        await service.register(
            RegisterSourceRequest(
                source_kind="file", filepath=str(payload), registered_by="pi"
            )
        )
    with pytest.raises(SourceRegistrationError, match="valid canonical base64"):
        await service.register(
            RegisterSourceRequest(
                source_kind="file",
                content_base64="!!!!",
                filename="payload.bin",
                registered_by="pi",
            )
        )
    with pytest.raises(SourceRegistrationError, match="maximum size"):
        await service.register(
            RegisterSourceRequest(
                source_kind="file",
                content_base64=base64.b64encode(b"12345").decode("ascii"),
                filename="payload.bin",
                registered_by="pi",
            )
        )

    hash_service = SourceService(db, project_id=PROJECT, storage_root=tmp_path / "packs2", file_policy=FileAccessPolicy([tmp_path]))
    with pytest.raises(SourceRegistrationError, match="expected_content_hash"):
        await hash_service.register(
            RegisterSourceRequest(
                source_kind="file",
                filepath=str(payload),
                registered_by="pi",
                expected_content_hash="0" * 64,
            )
        )
    assert (await db.fetchone("SELECT COUNT(*) AS n FROM registered_sources"))["n"] == 0


@pytest.mark.asyncio
async def test_registration_is_project_scoped(db, tmp_path: Path) -> None:
    await db.execute(
        "INSERT INTO projects (id, name, created_by) VALUES ('prj_other', 'Other', 'pi')"
    )
    first_service = SourceService(db, project_id=PROJECT, storage_root=tmp_path / "packs")
    other_service = SourceService(db, project_id="prj_other", storage_root=tmp_path / "packs")
    first = await first_service.register(_paste())
    other = await other_service.register(_paste())

    assert await other_service.get(first.source.id) is None
    assert first.source.id != other.source.id
    assert first.source.manifest_hash == other.source.manifest_hash


@pytest.mark.asyncio
async def test_admission_requires_exact_source_revision_target_and_grounding(db, tmp_path: Path) -> None:
    service = SourceService(db, project_id=PROJECT, storage_root=tmp_path / "packs")
    first = await service.register(_paste("first"))
    second = await service.register(_paste("second", provenance={"run": 2}))
    candidate = await _candidate(db, first.source.artifact_id)
    await _target_journal(db)

    with pytest.raises(ValidationError, match="Input should be True"):
        SourceAdmissionCreate(
            candidate_id=candidate.id,
            expected_revision=1,
            target_type="journal",
            target_id="jrn_source_target",
            actor="pi",
            reason="not checked",
            grounding_verified=False,
        )
    with pytest.raises(InterpretationConflictError, match="revision"):
        await service.admit(
            first.source.id,
            SourceAdmissionCreate(
                candidate_id=candidate.id,
                expected_revision=2,
                target_type="journal",
                target_id="jrn_source_target",
                actor="pi",
                reason="reviewed",
                grounding_verified=True,
            ),
        )
    with pytest.raises(SourceRegistrationError, match="must be grounded"):
        await service.admit(
            second.source.id,
            SourceAdmissionCreate(
                candidate_id=candidate.id,
                expected_revision=1,
                target_type="journal",
                target_id="jrn_source_target",
                actor="pi",
                reason="reviewed",
                grounding_verified=True,
            ),
        )
    with pytest.raises(InterpretationNotFoundError, match="target"):
        await service.admit(
            first.source.id,
            SourceAdmissionCreate(
                candidate_id=candidate.id,
                expected_revision=1,
                target_type="claim",
                target_id="clm_missing",
                actor="pi",
                reason="reviewed",
                grounding_verified=True,
            ),
        )


@pytest.mark.asyncio
async def test_successful_admission_is_audited_idempotent_and_final(db, tmp_path: Path) -> None:
    service = SourceService(db, project_id=PROJECT, storage_root=tmp_path / "packs")
    source = await service.register(_paste())
    candidate = await _candidate(db, source.source.artifact_id)
    target_id = await _target_journal(db)
    request = SourceAdmissionCreate(
        candidate_id=candidate.id,
        expected_revision=1,
        target_type="journal",
        target_id=target_id,
        actor="pi",
        reason="Verified against the exact registered bytes.",
        grounding_verified=True,
    )

    admission = await service.admit(source.source.id, request)
    retry = await service.admit(source.source.id, request)
    assert retry.id == admission.id
    resolved = await InterpretationService(db, project_id=PROJECT).get(candidate.id)
    assert resolved is not None
    assert resolved.revision == 2
    assert resolved.review_status == "resolved"
    assert resolved.disposition_target_id == target_id
    link = await db.fetchone(
        """SELECT id FROM entity_links
           WHERE project_id = ? AND source_type = 'journal' AND source_id = ?
             AND link_type = 'derived_from'
             AND target_type = 'interpretation_candidate' AND target_id = ?""",
        [PROJECT, target_id, candidate.id],
    )
    assert link is not None
    event = await db.fetchone(
        """SELECT * FROM interpretation_review_events
           WHERE project_id = ? AND candidate_id = ? AND action = 'promote'""",
        [PROJECT, candidate.id],
    )
    assert event["candidate_revision"] == 2

    with pytest.raises(InterpretationConflictError, match="final"):
        await InterpretationService(db, project_id=PROJECT).triage(
            candidate.id,
            InterpretationTriage(
                action="reopen",
                expected_revision=2,
                actor="pi",
                reason="try to reopen immutable admission",
            ),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["missing", "corrupt"])
async def test_admission_rejects_damaged_managed_bytes_without_side_effects(
    db,
    tmp_path: Path,
    damage: str,
) -> None:
    service = SourceService(db, project_id=PROJECT, storage_root=tmp_path / "packs")
    source = await service.register(_paste("admission source bytes"))
    detail = await service.get(source.source.id)
    assert detail is not None
    artifact_path = Path(detail.artifact["filepath"])
    candidate = await _candidate(db, source.source.artifact_id)
    target_id = await _target_journal(db, entry_id="jrn_damaged_source_target")
    events_before = await db.fetchall(
        "SELECT * FROM interpretation_review_events WHERE candidate_id = ?",
        [candidate.id],
    )
    if damage == "missing":
        artifact_path.unlink()
    else:
        artifact_path.write_bytes(b"corrupt")

    with pytest.raises(SourceRegistrationError, match="managed artifact"):
        await service.admit(
            source.source.id,
            SourceAdmissionCreate(
                candidate_id=candidate.id,
                expected_revision=1,
                target_type="journal",
                target_id=target_id,
                actor="pi",
                reason="must not admit corrupt bytes",
                grounding_verified=True,
            ),
        )

    unchanged = await InterpretationService(db, project_id=PROJECT).get(candidate.id)
    assert unchanged is not None
    assert unchanged.revision == 1
    assert unchanged.review_status == "pending"
    assert unchanged.disposition is None
    assert await db.fetchall("SELECT * FROM source_admissions") == []
    assert await db.fetchall(
        "SELECT * FROM interpretation_review_events WHERE candidate_id = ?",
        [candidate.id],
    ) == events_before


@pytest.mark.asyncio
async def test_registered_source_and_admission_round_trip_in_pack(db, tmp_path: Path) -> None:
    service = SourceService(db, project_id=PROJECT)
    source = await service.register(_paste("portable exact bytes"))
    candidate = await _candidate(db, source.source.artifact_id)
    target_id = await _target_journal(db, entry_id=generate_id("journal"))
    admission_reason = (
        f"Reviewed {source.source.id} via {candidate.id} for {target_id}."
    )
    await service.admit(
        source.source.id,
        SourceAdmissionCreate(
            candidate_id=candidate.id,
            expected_revision=1,
            target_type="journal",
            target_id=target_id,
            actor="pi",
            reason=admission_reason,
            grounding_verified=True,
        ),
    )

    pack_path, _ = await KnowledgePackService(db, project_id=PROJECT).export_pack()
    try:
        with Path(pack_path).open("rb") as source_pack:
            await KnowledgePackService(db).import_pack(
                source_pack,
                project_id="prj_source_import",
                project_name="Imported sources",
            )
    finally:
        Path(pack_path).unlink(missing_ok=True)

    imported_source = await db.fetchone(
        "SELECT * FROM registered_sources WHERE project_id = 'prj_source_import'"
    )
    imported_admission = await db.fetchone(
        "SELECT * FROM source_admissions WHERE project_id = 'prj_source_import'"
    )
    imported_review_event = await db.fetchone(
        """SELECT * FROM interpretation_review_events
           WHERE project_id = 'prj_source_import' AND action = 'promote'"""
    )
    imported_artifact = await db.fetchone(
        """SELECT artifact.* FROM artifacts AS artifact
           JOIN registered_sources AS source ON source.artifact_id = artifact.id
           WHERE source.project_id = 'prj_source_import'"""
    )
    assert imported_source["content_hash"] == source.source.content_hash
    assert imported_source["manifest_hash"] == source.source.manifest_hash
    assert json.loads(imported_source["provenance"])["collection"] == "local lab notebook"
    assert imported_admission["source_manifest_hash"] == imported_source["manifest_hash"]
    remapped_reason = (
        f"Reviewed {imported_source['id']} via {imported_admission['candidate_id']} "
        f"for {imported_admission['target_id']}."
    )
    assert imported_admission["reason"] == remapped_reason
    assert imported_review_event["reason"] == remapped_reason
    assert source.source.id not in remapped_reason
    assert candidate.id not in remapped_reason
    assert target_id not in remapped_reason
    assert Path(imported_artifact["filepath"]).read_bytes() == b"portable exact bytes"
    critical = [
        issue
        for issue in await KnowledgePackService(db).check_integrity("prj_source_import")
        if issue["severity"] == "critical"
    ]
    assert critical == []


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["missing", "corrupt"])
async def test_integrity_reports_damaged_registered_source_bytes(
    db,
    damage: str,
) -> None:
    service = SourceService(db, project_id=PROJECT)
    source = await service.register(_paste(f"integrity-{damage}"))
    detail = await service.get(source.source.id)
    assert detail is not None
    artifact_path = Path(detail.artifact["filepath"])
    if damage == "missing":
        artifact_path.unlink()
    else:
        artifact_path.write_bytes(b"tampered")

    issues = await KnowledgePackService(db).check_integrity(PROJECT)
    issue = next(
        item
        for item in issues
        if item["category"] == "registered_source_artifact_invalid"
    )
    assert issue["severity"] == "critical"
    assert source.source.id in issue["ids"]


@pytest.mark.asyncio
async def test_pack_import_rejects_tampered_registered_source_without_side_effects(
    db, tmp_path: Path
) -> None:
    service = SourceService(db, project_id=PROJECT)
    source = await service.register(_paste("tamper-evident bytes"))
    candidate = await _candidate(db, source.source.artifact_id)
    target_id = await _target_journal(db, entry_id="jrn_tamper_source_target")
    await service.admit(
        source.source.id,
        SourceAdmissionCreate(
            candidate_id=candidate.id,
            expected_revision=1,
            target_type="journal",
            target_id=target_id,
            actor="pi",
            reason="Reviewed before export.",
            grounding_verified=True,
        ),
    )

    pack_path, _ = await KnowledgePackService(db, project_id=PROJECT).export_pack()
    tampered_path = tmp_path / "tampered-source.rka-pack.zip"
    try:
        with zipfile.ZipFile(pack_path) as source_pack:
            entries = {
                member.filename: source_pack.read(member.filename)
                for member in source_pack.infolist()
            }
        manifest = json.loads(entries["manifest.json"])
        manifest["tables"]["registered_sources"][0]["manifest_hash"] = "0" * 64
        entries["manifest.json"] = json.dumps(
            manifest, indent=2, sort_keys=True
        ).encode()
        with zipfile.ZipFile(
            tampered_path, "w", compression=zipfile.ZIP_DEFLATED
        ) as destination:
            for name, payload in entries.items():
                destination.writestr(name, payload)

        with tampered_path.open("rb") as source_pack:
            with pytest.raises(ValueError, match="invalid manifest hash"):
                await KnowledgePackService(db).import_pack(
                    source_pack,
                    project_id="prj_tampered_source",
                    project_name="Tampered source",
                )
    finally:
        Path(pack_path).unlink(missing_ok=True)

    assert await db.fetchone(
        "SELECT id FROM projects WHERE id = 'prj_tampered_source'"
    ) is None
    imported_storage = (
        Path(db.db_path).resolve().parent
        / "knowledge-packs"
        / "prj_tampered_source"
    )
    assert not imported_storage.exists()


@pytest.mark.asyncio
async def test_project_deletion_removes_source_rows_and_managed_bytes(db) -> None:
    project_id = "prj_delete_registered_sources"
    projects = ProjectService(db)
    await projects.create_project(
        ProjectCreate(id=project_id, name="Delete registered sources"),
        actor="pi",
    )
    source_service = SourceService(db, project_id=project_id)
    source = await source_service.register(_paste())
    candidate = await InterpretationService(db, project_id=project_id).create(
        InterpretationCandidateCreate(
            source_type="artifact",
            source_id=source.source.artifact_id,
            locator_kind="record",
            locator_value="full_record",
            statement="Reviewed statement.",
            epistemic_kind="reported_fact",
            created_by="executor",
            extraction_tool="pytest",
        )
    )
    target_id = await _target_journal(
        db, entry_id="jrn_delete_registered_source", project_id=project_id
    )
    await source_service.admit(
        source.source.id,
        SourceAdmissionCreate(
            candidate_id=candidate.id,
            expected_revision=1,
            target_type="journal",
            target_id=target_id,
            actor="pi",
            reason="Reviewed before project deletion.",
            grounding_verified=True,
        ),
    )
    project_dir = Path(db.db_path).resolve().parent / "knowledge-packs" / project_id
    assert project_dir.exists()

    await projects.delete_project(project_id, confirm=True)

    assert not project_dir.exists()
    assert await db.fetchall(
        "SELECT * FROM registered_sources WHERE project_id = ?", [project_id]
    ) == []
    assert await db.fetchall(
        "SELECT * FROM source_admissions WHERE project_id = ?", [project_id]
    ) == []
    assert await db.fetchall("PRAGMA foreign_key_check") == []
