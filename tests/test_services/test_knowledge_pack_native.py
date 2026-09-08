"""Knowledge-pack parity for the native manuscript aggregate."""

from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import zipfile
from pathlib import Path

import pytest

from rka.infra.ids import generate_id
from rka.models.manuscript_native import (
    ManuscriptCheckpointCreate,
    ManuscriptCheckpointResolve,
    ManuscriptClaimVerificationAttestationCreate,
    ManuscriptCreate,
    ManuscriptReferenceManifestReplace,
    ManuscriptReferenceMemberInput,
)
from rka.services.knowledge_pack import (
    PACK_SCHEMA_VERSION,
    KnowledgePackIntegrityError,
    KnowledgePackService,
)
from rka.services.manuscript_native import NativeManuscriptService


_NATIVE_TABLES = (
    "manuscripts",
    "manuscript_reference_members",
    "manuscript_claims",
    "manuscript_claim_versions",
    "manuscript_claim_ratifications",
    "manuscript_units",
    "manuscript_unit_outline_profiles",
    "manuscript_claim_evidence",
    "manuscript_unit_evidence",
    "manuscript_unit_citations",
    "manuscript_claim_units",
    "manuscript_checkpoints",
    "manuscript_claim_verification_attestations",
)


@pytest.mark.asyncio
async def test_v2_pack_conservatively_backfills_legacy_manuscript_identity(
    db_with_project,
) -> None:
    db = db_with_project
    legacy_id = generate_id("journal")
    await db.execute(
        """INSERT INTO journal
           (id, type, content, verbatim_input, source, confidence, importance,
            status, project_id)
           VALUES (?, 'finding', 'Legacy Writer record.',
                   'Imported paper title\n\nImported abstract.',
                   'executor', 'tested', 'normal', 'active', 'proj_default')""",
        [legacy_id],
    )
    for tag in ("manuscript", "venue:USENIX Security", "phase:draft"):
        await db.execute(
            """INSERT INTO tags (tag, entity_type, entity_id, project_id)
               VALUES (?, 'journal', ?, 'proj_default')""",
            [tag, legacy_id],
        )
    await db.commit()
    project = await db.fetchone("SELECT * FROM projects WHERE id = 'proj_default'")
    journal = await db.fetchone(
        "SELECT * FROM journal WHERE id = ?",
        [legacy_id],
    )
    tags = await db.fetchall(
        """SELECT * FROM tags
           WHERE entity_type = 'journal' AND entity_id = ?
           ORDER BY tag""",
        [legacy_id],
    )
    manifest = {
        "pack_format_version": 2,
        "schema_version": 32,
        "project": project,
        "project_state": None,
        "tables": {"journal": [journal], "tags": tags},
        "table_counts": {"journal": 1, "tags": 3},
    }
    payload = io.BytesIO()
    with zipfile.ZipFile(
        payload,
        "w",
        compression=zipfile.ZIP_DEFLATED,
    ) as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
    payload.seek(0)

    result = await KnowledgePackService(db).import_pack(
        payload,
        project_id="proj_legacy_pack",
        project_name="Legacy Pack Import",
    )
    assert result.imported_counts["manuscripts"] == 1
    imported_legacy = await db.fetchone(
        """SELECT id FROM journal
           WHERE project_id = 'proj_legacy_pack'
             AND content = 'Legacy Writer record.'"""
    )
    assert imported_legacy is not None
    manuscript = await db.fetchone(
        """SELECT * FROM manuscripts
           WHERE project_id = 'proj_legacy_pack'"""
    )
    assert manuscript is not None
    assert manuscript["id"] == f"man_{imported_legacy['id'][4:]}"
    assert manuscript["legacy_journal_id"] == imported_legacy["id"]
    assert manuscript["title"] == "Imported paper title"
    assert manuscript["abstract"] == "Imported abstract."
    assert manuscript["venue"] == "USENIX Security"
    assert manuscript["phase"] == "drafting"
    assert (
        await db.fetchall(
            """SELECT * FROM manuscript_claims
           WHERE project_id = 'proj_legacy_pack'"""
        )
        == []
    )


def _spine(
    evidence_id: str,
    artifact_id: str,
    *,
    empirical_wording: str,
) -> dict:
    return {
        "claims": [
            {
                "claim_id": "C1",
                "claim_type": "empirical",
                "status": "active",
                "text": empirical_wording,
                "allowed_wording": empirical_wording,
                "prohibited_wording": ["The system eliminates every latency source."],
                "conditions": ["Measured testbed configuration only."],
                "falsification_criteria": ["The measured direction does not reproduce."],
                "evidence_ids": [evidence_id],
                "qualifier_ids": [],
                "counterevidence_ids": [],
                "unit_links": [{"unit_key": "R1", "relationship": "tests"}],
            },
            {
                "claim_id": "C2",
                "claim_type": "methodological",
                "status": "candidate",
                "text": "The workflow separates support from qualifiers.",
                "allowed_wording": ("The workflow records support and qualifiers separately."),
                "prohibited_wording": ["The workflow proves that every result is correct."],
                "evidence_ids": [],
                "qualifier_ids": [evidence_id],
                "counterevidence_ids": [],
                "unit_links": [{"unit_key": "M1", "relationship": "advances"}],
            },
        ],
        "units": [
            {
                "unit_id": "R1",
                "kind": "result",
                "location": "sections/results.tex#latency",
                "parent_unit_key": "M1",
                "outline_level": 3,
                "unit_role": "result",
                "rhetorical_move": "present_result",
                "communicative_job": "Report the bounded latency result.",
                "intended_takeaway": "The measured configuration reduced latency.",
                "evidence_plan": [f"Bind the measured observation {evidence_id}."],
                "figure_intentions": [f"Render artifact {artifact_id} with support {evidence_id}."],
                "citation_intentions": [f"Cite the source of {evidence_id}."],
                "artifact_ref": artifact_id,
                "allowed_interpretation": ("Latency was lower under the measured configuration."),
                "prohibited_interpretation": ("Latency is lower under every configuration."),
                "evidence_ids": [evidence_id],
                "evidence": {
                    "support": [{
                        "evidence_claim_id": evidence_id,
                        "supported_proposition": "Latency was lower in the measured testbed.",
                        "warrant": "The observation records the exact bounded contrast.",
                    }],
                    "qualifier": [],
                    "counterevidence": [],
                },
                "sequence": 2,
            },
            {
                "unit_id": "M1",
                "kind": "method",
                "location": "sections/method.tex#provenance",
                "outline_level": 2,
                "unit_role": "argument_block",
                "rhetorical_move": "describe_method",
                "communicative_job": "Explain the provenance-aware workflow.",
                "intended_takeaway": "Support and qualifiers remain distinct.",
                "evidence_plan": [f"Use qualifier {evidence_id}."],
                "table_intentions": [f"Summarize evidence role for {evidence_id}."],
                "qualifier_ids": [evidence_id],
                "sequence": 1,
            },
        ],
    }


async def _seed_native_manuscript(db) -> dict[str, str]:
    project_id = "proj_default"
    legacy_id = generate_id("journal")
    source_journal_id = generate_id("journal")
    evidence_id = generate_id("claim")
    artifact_id = generate_id("artifact")
    artifact_path = Path(db.db_path).resolve().parent / f"{artifact_id}.csv"
    artifact_bytes = b"latency_ms\n14\n"
    artifact_path.write_bytes(artifact_bytes)
    artifact_hash = hashlib.sha256(artifact_bytes).hexdigest()
    retired_literature_id = generate_id("literature")
    active_literature_id = generate_id("literature")

    await db.execute(
        """INSERT INTO journal
           (id, type, content, source, confidence, importance, status, project_id)
           VALUES (?, 'finding', 'Legacy manuscript identity.',
                   'executor', 'tested', 'normal', 'active', ?)""",
        [legacy_id, project_id],
    )
    await db.execute(
        """INSERT INTO tags (tag, entity_type, entity_id, project_id)
           VALUES ('manuscript', 'journal', ?, ?)""",
        [legacy_id, project_id],
    )
    await db.execute(
        """INSERT INTO journal
           (id, type, content, source, confidence, importance, status, project_id)
           VALUES (?, 'finding', 'Measured 14 percent lower latency.',
                   'executor', 'tested', 'high', 'active', ?)""",
        [source_journal_id, project_id],
    )
    await db.execute(
        """INSERT INTO claims
           (id, source_entry_id, claim_type, content, confidence, verified,
            evidence_status, stale, project_id)
           VALUES (?, ?, 'result', 'Latency was 14 percent lower.',
                   0.9, 1, 'supported', 0, ?)""",
        [evidence_id, source_journal_id, project_id],
    )
    await db.execute(
        """UPDATE claims
           SET staleness_reviewed_at = '2026-08-17T12:00:00Z',
               staleness_verdict = 'historical',
               staleness_resolution = ?,
               staleness_resolution_journal_id = ?,
               staleness_resolved_by = 'pi'
           WHERE id = ? AND project_id = ?""",
        [
            f"Bounded by review note {source_journal_id}.",
            source_journal_id,
            evidence_id,
            project_id,
        ],
    )
    await db.execute(
        """INSERT INTO artifacts
           (id, filename, filepath, filetype, file_size, content_hash,
            extraction_status, project_id)
           VALUES (?, 'latency.csv', ?, 'csv', ?, ?, 'complete', ?)""",
        [
            artifact_id,
            str(artifact_path),
            len(artifact_bytes),
            artifact_hash,
            project_id,
        ],
    )
    await db.execute(
        """INSERT INTO literature
           (id, title, authors, year, doi, status, added_by, project_id)
           VALUES (?, 'Earlier citation', '["A. Author"]', 2024,
                   '10.1000/earlier', 'cited', 'pi', ?)""",
        [retired_literature_id, project_id],
    )
    await db.execute(
        """INSERT INTO literature
           (id, title, authors, year, doi, status, added_by, project_id)
           VALUES (?, 'Current citation', '["B. Author"]', 2025,
                   '10.1000/current', 'cited', 'pi', ?)""",
        [active_literature_id, project_id],
    )
    await db.commit()

    service = NativeManuscriptService(db, project_id=project_id)
    manuscript = await service.create(
        ManuscriptCreate(
            title="Portable native manuscript",
            venue="USENIX Security",
            legacy_journal_id=legacy_id,
        )
    )
    await service.upsert_argument_spine(
        manuscript.id,
        expected_revision=1,
        spine=_spine(
            evidence_id,
            artifact_id,
            empirical_wording="The system reduced latency.",
        ),
    )
    context = await service.upsert_argument_spine(
        manuscript.id,
        expected_revision=2,
        spine=_spine(
            evidence_id,
            artifact_id,
            empirical_wording="Latency was lower in the measured testbed.",
        ),
    )
    claims = {row["local_key"]: row for row in context["claims"]}
    units = {row["local_key"]: row for row in context["units"]}
    retired_manifest = await service.replace_reference_manifest(
        manuscript.id,
        ManuscriptReferenceManifestReplace(
            expected_revision=3,
            members=[
                ManuscriptReferenceMemberInput(
                    citation_key="earlier2024",
                    literature_id=retired_literature_id,
                )
            ],
        ),
    )
    retired_reference_id = retired_manifest["members"][0]["id"]
    active_manifest = await service.replace_reference_manifest(
        manuscript.id,
        ManuscriptReferenceManifestReplace(
            expected_revision=4,
            members=[
                ManuscriptReferenceMemberInput(
                    citation_key="current2025",
                    literature_id=active_literature_id,
                )
            ],
        ),
    )
    active_reference_id = active_manifest["members"][0]["id"]

    ratifying_decision_id = generate_id("decision")
    matching_but_unbound_decision_id = generate_id("decision")
    await db.execute(
        """INSERT INTO decisions
           (id, phase, question, chosen, rationale, decided_by, status, project_id)
           VALUES (?, 'paper_writing', 'Ratify C1?', ?, 'PI selected C1.',
                   'pi', 'active', ?)""",
        [
            ratifying_decision_id,
            claims["C1"]["exact_wording"],
            project_id,
        ],
    )
    await db.execute(
        """INSERT INTO decisions
           (id, phase, question, chosen, rationale, decided_by, status, project_id)
           VALUES (?, 'paper_writing', 'Consider C2?', ?, 'Not bound to C2.',
                   'pi', 'active', ?)""",
        [
            matching_but_unbound_decision_id,
            claims["C2"]["exact_wording"],
            project_id,
        ],
    )
    await db.commit()
    ratification = await service.ratify_claim(
        manuscript.id,
        local_key="C1",
        decision_id=ratifying_decision_id,
        expected_revision=5,
        ratified_at="2026-07-23T10:00:00Z",
    )
    first_checkpoint = await service.create_checkpoint(
        ManuscriptCheckpointCreate(
            manuscript_id=manuscript.id,
            kind="venue",
        ),
        expected_revision=6,
    )
    await service.resolve_checkpoint(
        first_checkpoint.id,
        ManuscriptCheckpointResolve(
            decision_id=matching_but_unbound_decision_id,
            status="rejected",
            resolved_at="2026-07-23T10:00:30Z",
        ),
        expected_revision=7,
    )
    replacement_checkpoint = await service.create_checkpoint(
        ManuscriptCheckpointCreate(
            manuscript_id=manuscript.id,
            kind="venue",
            supersedes_id=first_checkpoint.id,
        ),
        expected_revision=8,
    )
    outline_checkpoint = await service.create_checkpoint(
        ManuscriptCheckpointCreate(
            manuscript_id=manuscript.id,
            kind="outline",
        ),
        expected_revision=9,
    )
    verification = await service.record_verification_attestation(
        ManuscriptClaimVerificationAttestationCreate(
            manuscript_id=manuscript.id,
            claim_id=claims["C1"]["id"],
            claim_version=2,
            overall_verdict="pass",
            grounding_verdict="pass",
            evidence_verdict="pass",
            contradiction_verdict="pass",
            currency_verdict="warn",
            ratification_verdict="pass",
            unit_coverage_verdict="pass",
            changelog_cursor="source-cursor-41",
            dependency_snapshot={
                "manuscript_id": manuscript.id,
                "claim_id": claims["C1"]["id"],
                "evidence_ids": [evidence_id],
            },
            full_json_payload={
                "decision_id": ratifying_decision_id,
                "unit_id": units["R1"]["id"],
                "unratified_claim_id": claims["C2"]["id"],
            },
            validator_version="native-test/v1",
            started_at="2026-07-23T10:01:00Z",
            completed_at="2026-07-23T10:01:01Z",
        ),
        expected_revision=10,
    )

    validation_job_id = generate_id("job")
    validation_id = generate_id("reference_validation")
    citation_use_id = generate_id("manuscript_unit_citation")
    await db.execute(
        """INSERT INTO jobs
           (id, job_type, project_id, entity_type, entity_id, payload)
           VALUES (?, 'reference_validate', ?, 'manuscript', ?, ?)""",
        [
            validation_job_id,
            project_id,
            manuscript.id,
            json.dumps({"manuscript_id": manuscript.id}, sort_keys=True),
        ],
    )
    await db.execute(
        """INSERT INTO reference_validation_attestations
           (id, project_id, manuscript_id, canonical_manuscript_id,
            legacy_journal_id, literature_id, validation_job_id,
            input_title, input_authors, status,
            retraction_check_enabled, retraction_checked, sources_tried,
            sources_confirmed, notes, stage_trace, full_json_payload,
            pipeline_version, started_at, completed_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'Current citation', '[]',
                   'VERIFIED', 1, 1,
                   '["crossref"]', '["crossref"]', '[]',
                   '{"D":{"completed":true}}', ?, 'reference-test/v1',
                   '2026-07-23T10:02:00Z', '2026-07-23T10:02:01Z')""",
        [
            validation_id,
            project_id,
            manuscript.id,
            manuscript.id,
            legacy_id,
            active_literature_id,
            validation_job_id,
            json.dumps(
                {
                    "manuscript_id": manuscript.id,
                    "claim_id": claims["C1"]["id"],
                    "unit_id": units["R1"]["id"],
                    "job_id": "domain-specific-non-worker-value",
                    "metadata": {"job_id": "nested-non-worker-value"},
                    "result": {
                        "job_id": validation_job_id,
                        "status": "VERIFIED",
                    },
                },
                sort_keys=True,
            ),
        ],
    )
    await db.execute(
        """INSERT INTO manuscript_unit_citations
           (id, manuscript_id, project_id, unit_id, reference_member_id,
            citation_role, supported_proposition, verification_state,
            comparison_axis)
           VALUES (?, ?, ?, ?, ?, 'baseline',
                   'The result compares against the established baseline.',
                   'verified', 'latency')""",
        [
            citation_use_id,
            manuscript.id,
            project_id,
            units["R1"]["id"],
            active_reference_id,
        ],
    )
    await db.execute(
        """INSERT INTO manuscript_migration_issues
           (legacy_journal_id, project_id, canonical_candidate_id,
            reason, details)
           VALUES (?, ?, ?, 'deterministic_id_conflict', '{}')""",
        [legacy_id, project_id, manuscript.id],
    )
    await db.commit()

    reference_set_decision_id = generate_id("decision")
    await db.execute(
        """INSERT INTO decisions
           (id, phase, question, chosen, rationale, decided_by, status, project_id)
           VALUES (?, 'paper_writing', 'Approve reference set?',
                   'Approve current2025', 'PI approved the verified citation set.',
                   'pi', 'active', ?)""",
        [reference_set_decision_id, project_id],
    )
    await db.commit()
    reference_set_checkpoint = await service.create_checkpoint(
        ManuscriptCheckpointCreate(
            manuscript_id=manuscript.id,
            kind="reference_set",
        ),
        expected_revision=11,
    )
    await service.resolve_checkpoint(
        reference_set_checkpoint.id,
        ManuscriptCheckpointResolve(
            decision_id=reference_set_decision_id,
            status="resolved",
            resolved_at="2026-07-23T10:03:00Z",
        ),
        expected_revision=12,
    )
    outline_decision_id = generate_id("decision")
    await db.execute(
        """INSERT INTO decisions
           (id, phase, question, chosen, rationale, decided_by, status, project_id)
           VALUES (?, 'paper_writing', 'Approve outline?', 'Approve current outline',
                   'PI approved the typed outline dependencies.',
                   'pi', 'active', ?)""",
        [outline_decision_id, project_id],
    )
    await db.commit()
    await service.resolve_checkpoint(
        outline_checkpoint.id,
        ManuscriptCheckpointResolve(
            decision_id=outline_decision_id,
            status="resolved",
            resolved_at="2026-07-23T10:04:00Z",
        ),
        expected_revision=13,
    )

    return {
        "manuscript_id": manuscript.id,
        "legacy_id": legacy_id,
        "evidence_id": evidence_id,
        "artifact_id": artifact_id,
        "c1_id": claims["C1"]["id"],
        "c2_id": claims["C2"]["id"],
        "r1_id": units["R1"]["id"],
        "ratifying_decision_id": ratifying_decision_id,
        "unbound_decision_id": matching_but_unbound_decision_id,
        "ratification_id": ratification.id,
        "first_checkpoint_id": first_checkpoint.id,
        "replacement_checkpoint_id": replacement_checkpoint.id,
        "verification_id": verification.id,
        "validation_id": validation_id,
        "validation_job_id": validation_job_id,
        "retired_literature_id": retired_literature_id,
        "active_literature_id": active_literature_id,
        "retired_reference_id": retired_reference_id,
        "active_reference_id": active_reference_id,
        "citation_use_id": citation_use_id,
        "reference_set_checkpoint_id": reference_set_checkpoint.id,
        "outline_checkpoint_id": outline_checkpoint.id,
    }


@pytest.mark.asyncio
async def test_native_manuscript_round_trip_preserves_history_without_synthesis(
    db_with_project,
) -> None:
    db = db_with_project
    source = await _seed_native_manuscript(db)
    source_cursor = await db.fetchone(
        """SELECT max(cursor) AS cursor
           FROM change_events WHERE project_id = 'proj_default'"""
    )
    assert source_cursor and source_cursor["cursor"] is not None
    source_context = await NativeManuscriptService(db, project_id="proj_default").get_context(
        source["manuscript_id"]
    )
    source_reference_checkpoint = next(
        checkpoint
        for checkpoint in source_context["checkpoints"]
        if checkpoint["id"] == source["reference_set_checkpoint_id"]
    )
    assert source_reference_checkpoint["dependency_current"] is True

    pack_path, _ = await KnowledgePackService(db, project_id="proj_default").export_pack()
    with zipfile.ZipFile(pack_path) as archive:
        manifest = json.loads(archive.read("manifest.json"))

    assert manifest["pack_format_version"] == PACK_SCHEMA_VERSION == 8
    assert manifest["portability"] == {
        "completed_validation_attestations": "included",
        "excluded_tables": {
            "change_events": (
                "Target-local cursor ledger; import emits fresh events with "
                "target-local watermarks."
            ),
            "jobs": (
                "Worker queue state, retries, leases, and pending external "
                "work are installation-local and must be requested again "
                "after import."
            ),
            "manuscript_migration_issues": (
                "Diagnostics from the source installation's legacy migration, "
                "not manuscript semantic state."
            ),
            "manuscript_source_events": (
                "Source-file apply and recovery events refer to installation-local "
                "paths and are omitted with their source proposals."
            ),
            "manuscript_source_proposals": (
                "Candidate source text, local workspace paths, and recovery state "
                "are installation-local authoring data; export the manuscript files "
                "separately."
            ),
            "reference_validation_migration_issues": (
                "Diagnostics from source-installation reference-validation "
                "migration, not portable manuscript semantic state."
            ),
        },
        "validation_job_links_on_import": "cleared",
    }
    assert (
        not {
            "change_events",
            "jobs",
            "manuscript_migration_issues",
            "manuscript_source_events",
            "manuscript_source_proposals",
            "reference_validation_migration_issues",
        }
        & manifest["tables"].keys()
    )
    for table in _NATIVE_TABLES:
        assert manifest["table_counts"][table] > 0

    version_keys = [
        (row["claim_id"], row["version"]) for row in manifest["tables"]["manuscript_claim_versions"]
    ]
    assert version_keys == sorted(version_keys)

    with open(pack_path, "rb") as pack_file:
        result = await KnowledgePackService(db).import_pack(
            pack_file,
            project_id="proj_native_import",
            project_name="Native Manuscript Import",
        )

    for table in _NATIVE_TABLES:
        assert result.imported_counts[table] == manifest["table_counts"][table]

    imported_manuscript = await db.fetchone(
        """SELECT * FROM manuscripts
           WHERE project_id = 'proj_native_import'"""
    )
    assert imported_manuscript is not None
    assert imported_manuscript["id"] != source["manuscript_id"]
    assert imported_manuscript["revision"] == 14

    imported_legacy = await db.fetchone(
        """SELECT id FROM journal
           WHERE project_id = 'proj_native_import'
             AND content = 'Legacy manuscript identity.'"""
    )
    assert imported_legacy is not None
    assert imported_manuscript["legacy_journal_id"] == imported_legacy["id"]

    imported_claim_rows = await db.fetchall(
        """SELECT * FROM manuscript_claims
           WHERE project_id = 'proj_native_import'
           ORDER BY local_key"""
    )
    imported_claims = {row["local_key"]: row for row in imported_claim_rows}
    assert list(imported_claims) == ["C1", "C2"]
    assert imported_claims["C1"]["id"] != source["c1_id"]
    assert imported_claims["C2"]["id"] != source["c2_id"]

    imported_versions = await db.fetchall(
        """SELECT claim_id, version, exact_wording, conditions,
                  falsification_criteria
           FROM manuscript_claim_versions
           WHERE project_id = 'proj_native_import'
           ORDER BY claim_id, version"""
    )
    versions_by_claim: dict[str, list[dict]] = {}
    for row in imported_versions:
        versions_by_claim.setdefault(row["claim_id"], []).append(row)
    assert [row["version"] for row in versions_by_claim[imported_claims["C1"]["id"]]] == [
        1,
        2,
    ]
    assert [row["exact_wording"] for row in versions_by_claim[imported_claims["C1"]["id"]]] == [
        "The system reduced latency.",
        "Latency was lower in the measured testbed.",
    ]
    assert json.loads(versions_by_claim[imported_claims["C1"]["id"]][-1]["conditions"]) == [
        "Measured testbed configuration only."
    ]

    imported_ratifications = await db.fetchall(
        """SELECT r.*, c.local_key
           FROM manuscript_claim_ratifications AS r
           JOIN manuscript_claims AS c ON c.id = r.claim_id
           WHERE r.project_id = 'proj_native_import'"""
    )
    assert len(imported_ratifications) == 1
    assert imported_ratifications[0]["local_key"] == "C1"
    assert imported_ratifications[0]["id"] != source["ratification_id"]
    assert not await db.fetchone(
        """SELECT 1
           FROM manuscript_claim_ratifications
           WHERE project_id = 'proj_native_import'
             AND claim_id = ?""",
        [imported_claims["C2"]["id"]],
    )

    imported_units = {
        row["local_key"]: row
        for row in await db.fetchall(
            """SELECT * FROM manuscript_units
               WHERE project_id = 'proj_native_import'"""
        )
    }
    imported_artifact = await db.fetchone(
        """SELECT id FROM artifacts
           WHERE project_id = 'proj_native_import'"""
    )
    assert imported_artifact is not None
    assert imported_units["R1"]["artifact_ref"] == imported_artifact["id"]
    imported_profiles = {
        row["unit_id"]: row
        for row in await db.fetchall(
            """SELECT * FROM manuscript_unit_outline_profiles
               WHERE project_id = 'proj_native_import'"""
        )
    }
    assert (
        imported_profiles[imported_units["R1"]["id"]]["parent_unit_id"]
        == (imported_units["M1"]["id"])
    )
    assert imported_profiles[imported_units["R1"]["id"]]["outline_level"] == 3
    assert imported_profiles[imported_units["R1"]["id"]]["unit_role"] == "result"
    assert imported_profiles[imported_units["R1"]["id"]]["rhetorical_move"] == (
        "present_result"
    )
    imported_evidence = await db.fetchone(
        """SELECT * FROM claims
           WHERE project_id = 'proj_native_import'"""
    )
    assert imported_evidence is not None
    imported_source_journal = await db.fetchone(
        """SELECT id FROM journal
           WHERE project_id = 'proj_native_import'
             AND content = 'Measured 14 percent lower latency.'"""
    )
    assert imported_source_journal is not None
    assert imported_evidence["staleness_reviewed_at"] == "2026-08-17T12:00:00Z"
    assert imported_evidence["staleness_verdict"] == "historical"
    assert imported_evidence["staleness_resolved_by"] == "pi"
    assert imported_evidence["staleness_resolution_journal_id"] == imported_source_journal["id"]
    assert imported_evidence["staleness_resolution"] == (
        f"Bounded by review note {imported_source_journal['id']}."
    )
    assert json.loads(imported_profiles[imported_units["R1"]["id"]]["figure_intentions"]) == [
        f"Render artifact {imported_artifact['id']} with support {imported_evidence['id']}."
    ]
    assert json.loads(imported_profiles[imported_units["R1"]["id"]]["citation_intentions"]) == [
        f"Cite the source of {imported_evidence['id']}."
    ]
    assert json.loads(imported_profiles[imported_units["M1"]["id"]]["table_intentions"]) == [
        f"Summarize evidence role for {imported_evidence['id']}."
    ]
    claim_support = await db.fetchone(
        """SELECT evidence_claim_id, role
           FROM manuscript_claim_evidence
           WHERE project_id = 'proj_native_import'
             AND manuscript_claim_id = ?
             AND claim_version = 2""",
        [imported_claims["C1"]["id"]],
    )
    assert claim_support == {
        "evidence_claim_id": imported_evidence["id"],
        "role": "support",
    }
    unit_support = await db.fetchone(
        """SELECT evidence_claim_id, role, supported_proposition, warrant
           FROM manuscript_unit_evidence
           WHERE project_id = 'proj_native_import'
             AND unit_id = ?""",
        [imported_units["R1"]["id"]],
    )
    assert unit_support == {
        "evidence_claim_id": imported_evidence["id"],
        "role": "support",
        "supported_proposition": "Latency was lower in the measured testbed.",
        "warrant": "The observation records the exact bounded contrast.",
    }
    claim_unit = await db.fetchone(
        """SELECT unit_id, relationship
           FROM manuscript_claim_units
           WHERE project_id = 'proj_native_import'
             AND manuscript_claim_id = ?
             AND claim_version = 2""",
        [imported_claims["C1"]["id"]],
    )
    assert claim_unit == {
        "unit_id": imported_units["R1"]["id"],
        "relationship": "tests",
    }

    checkpoints = await db.fetchall(
        """SELECT * FROM manuscript_checkpoints
           WHERE project_id = 'proj_native_import'
           ORDER BY created_at, id"""
    )
    venue_checkpoints = [checkpoint for checkpoint in checkpoints if checkpoint["kind"] == "venue"]
    assert len(venue_checkpoints) == 2
    first_imported = next(
        checkpoint for checkpoint in venue_checkpoints if checkpoint["status"] == "superseded"
    )
    replacement_imported = next(
        checkpoint for checkpoint in venue_checkpoints if checkpoint["status"] == "pending"
    )
    assert replacement_imported["supersedes_id"] == first_imported["id"]
    assert first_imported["approved_choice"] == ("The workflow separates support from qualifiers.")
    outline = next(checkpoint for checkpoint in checkpoints if checkpoint["kind"] == "outline")
    assert outline["supersedes_id"] is None
    imported_reference_checkpoint = next(
        checkpoint for checkpoint in checkpoints if checkpoint["kind"] == "reference_set"
    )
    assert imported_reference_checkpoint["status"] == "resolved"

    imported_decision = await db.fetchone(
        """SELECT id FROM decisions
           WHERE project_id = 'proj_native_import'
             AND question = 'Ratify C1?'"""
    )
    assert imported_decision is not None
    imported_verification = await db.fetchone(
        """SELECT * FROM manuscript_claim_verification_attestations
           WHERE project_id = 'proj_native_import'"""
    )
    assert imported_verification is not None
    assert imported_verification["id"] != source["verification_id"]
    assert imported_verification["changelog_cursor"] == "source-cursor-41"
    assert json.loads(imported_verification["dependency_snapshot"]) == {
        "claim_id": imported_claims["C1"]["id"],
        "evidence_ids": [imported_evidence["id"]],
        "manuscript_id": imported_manuscript["id"],
    }
    assert json.loads(imported_verification["full_json_payload"]) == {
        "decision_id": imported_decision["id"],
        "unit_id": imported_units["R1"]["id"],
        "unratified_claim_id": imported_claims["C2"]["id"],
    }

    imported_reference_validation = await db.fetchone(
        """SELECT * FROM reference_validation_attestations
           WHERE project_id = 'proj_native_import'"""
    )
    assert imported_reference_validation is not None
    assert imported_reference_validation["id"] != source["validation_id"]
    assert imported_reference_validation["manuscript_id"] == imported_manuscript["id"]
    assert imported_reference_validation["canonical_manuscript_id"] == imported_manuscript["id"]
    assert imported_reference_validation["legacy_journal_id"] == imported_legacy["id"]
    assert imported_reference_validation["validation_job_id"] is None
    imported_literature = {
        row["title"]: row["id"]
        for row in await db.fetchall(
            """SELECT id, title FROM literature
               WHERE project_id = 'proj_native_import'"""
        )
    }
    imported_reference_members = await db.fetchall(
        """SELECT id, citation_key, literature_id, state, retired_at
           FROM manuscript_reference_members
           WHERE project_id = 'proj_native_import'
           ORDER BY state, citation_key"""
    )
    assert len(imported_reference_members) == 2
    imported_by_state = {row["state"]: row for row in imported_reference_members}
    assert imported_by_state["active"]["id"] != source["active_reference_id"]
    assert imported_by_state["active"]["citation_key"] == "current2025"
    assert imported_by_state["active"]["literature_id"] == imported_literature["Current citation"]
    assert imported_by_state["active"]["retired_at"] is None
    assert imported_by_state["retired"]["id"] != source["retired_reference_id"]
    assert imported_by_state["retired"]["citation_key"] == "earlier2024"
    assert imported_by_state["retired"]["literature_id"] == imported_literature["Earlier citation"]
    assert imported_by_state["retired"]["retired_at"] is not None
    imported_citation_use = await db.fetchone(
        """SELECT id, unit_id, reference_member_id, citation_role,
                  supported_proposition, verification_state, comparison_axis
           FROM manuscript_unit_citations
           WHERE project_id = 'proj_native_import'"""
    )
    assert imported_citation_use is not None
    assert {key: value for key, value in imported_citation_use.items() if key != "id"} == {
        "unit_id": imported_units["R1"]["id"],
        "reference_member_id": imported_by_state["active"]["id"],
        "citation_role": "baseline",
        "supported_proposition": (
            "The result compares against the established baseline."
        ),
        "verification_state": "verified",
        "comparison_axis": "latency",
    }
    assert imported_citation_use["id"] != source["citation_use_id"]
    assert imported_reference_validation["literature_id"] == imported_literature["Current citation"]
    assert json.loads(imported_reference_validation["full_json_payload"]) == {
        "claim_id": imported_claims["C1"]["id"],
        "job_id": "domain-specific-non-worker-value",
        "manuscript_id": imported_manuscript["id"],
        "metadata": {"job_id": "nested-non-worker-value"},
        "result": {
            "source_job_id": source["validation_job_id"],
            "source_project_id": "proj_default",
            "status": "VERIFIED",
        },
        "unit_id": imported_units["R1"]["id"],
    }
    # E-series imports persist a completed lexical-index receipt. They must
    # not restore source worker jobs or schedule synthesis/verification.
    imported_jobs = await db.fetchall(
        "SELECT job_type, status, payload FROM jobs WHERE project_id = 'proj_native_import'"
    )
    assert len(imported_jobs) == 1
    assert imported_jobs[0]["job_type"] == "pack_import"
    assert imported_jobs[0]["status"] == "completed"
    assert json.loads(imported_jobs[0]["payload"])["embedding_job_id"] is None
    assert not await db.fetchone(
        """SELECT 1 FROM manuscript_migration_issues
           WHERE project_id = 'proj_native_import'"""
    )
    assert not await db.fetchone(
        """SELECT 1 FROM reference_validation_migration_issues
           WHERE project_id = 'proj_native_import'"""
    )
    imported_context = await NativeManuscriptService(
        db, project_id="proj_native_import"
    ).get_context(imported_manuscript["id"])
    imported_reference_checkpoint = next(
        checkpoint
        for checkpoint in imported_context["checkpoints"]
        if checkpoint["kind"] == "reference_set"
    )
    assert imported_reference_checkpoint["dependency_current"] is True
    imported_outline_checkpoint = next(
        checkpoint
        for checkpoint in imported_context["checkpoints"]
        if checkpoint["kind"] == "outline"
    )
    assert imported_outline_checkpoint["status"] == "resolved"
    current_outline_dependencies = await NativeManuscriptService(
        db, project_id="proj_native_import"
    )._checkpoint_dependency_snapshot(
        imported_manuscript["id"], kind="outline", unit_id=None
    )
    assert imported_outline_checkpoint["dependency_snapshot"] == (
        current_outline_dependencies
    )
    assert imported_outline_checkpoint["dependency_current"] is True

    target_cursor = await db.fetchone(
        """SELECT min(cursor) AS cursor
           FROM change_events WHERE project_id = 'proj_native_import'"""
    )
    assert target_cursor and target_cursor["cursor"] > source_cursor["cursor"]

    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        await db.execute(
            """UPDATE manuscript_claim_versions
               SET exact_wording = 'Rewritten after import.'
               WHERE claim_id = ? AND version = 2""",
            [imported_claims["C1"]["id"]],
        )
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        await db.execute(
            """DELETE FROM manuscript_claim_ratifications
               WHERE project_id = 'proj_native_import'"""
        )
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        await db.execute(
            """UPDATE manuscript_claim_verification_attestations
               SET overall_verdict = 'warn'
               WHERE project_id = 'proj_native_import'"""
        )


def _rewrite_pack_manifest(source: str, destination: Path, mutate) -> None:
    with zipfile.ZipFile(source) as archive:
        entries = {name: archive.read(name) for name in archive.namelist()}
    manifest = json.loads(entries["manifest.json"])
    mutate(manifest["tables"])
    manifest["table_counts"] = {
        table: len(rows) for table, rows in manifest["tables"].items()
    }
    entries["manifest.json"] = json.dumps(manifest, indent=2, sort_keys=True).encode()
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, payload in entries.items():
            archive.writestr(name, payload)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "corruption",
    ["cycle", "depth", "removed_parent", "parent_order", "subtree_contiguity"],
)
async def test_pack_import_rolls_back_outline_hierarchy_corruption(
    db_with_project,
    tmp_path: Path,
    corruption: str,
) -> None:
    db = db_with_project
    await _seed_native_manuscript(db)
    pack_path, _ = await KnowledgePackService(db, project_id="proj_default").export_pack()
    corrupt_path = tmp_path / f"outline-{corruption}.rka-pack.zip"

    def corrupt(tables: dict[str, list[dict]]) -> None:
        units = {row["local_key"]: row for row in tables["manuscript_units"]}
        profiles = {row["unit_id"]: row for row in tables["manuscript_unit_outline_profiles"]}
        parent = units["M1"]
        child = units["R1"]
        if corruption == "cycle":
            profiles[parent["id"]]["parent_unit_id"] = child["id"]
        elif corruption == "depth":
            profiles[child["id"]]["outline_level"] = profiles[parent["id"]]["outline_level"]
        elif corruption == "removed_parent":
            parent["status"] = "removed"
        elif corruption == "parent_order":
            parent["sequence"] = 20
            child["sequence"] = 10
        else:
            sibling = dict(parent)
            sibling["id"] = generate_id("manuscript_unit")
            sibling["local_key"] = "OTHER"
            sibling["location"] = "sections/other.tex"
            sibling["sequence"] = 20
            tables["manuscript_units"].append(sibling)
            sibling_profile = dict(profiles[parent["id"]])
            sibling_profile["unit_id"] = sibling["id"]
            sibling_profile["parent_unit_id"] = None
            tables["manuscript_unit_outline_profiles"].append(sibling_profile)
            parent["sequence"] = 10
            child["sequence"] = 30

    _rewrite_pack_manifest(pack_path, corrupt_path, corrupt)
    target_project = f"proj_outline_{corruption}"
    with corrupt_path.open("rb") as pack_file:
        with pytest.raises(KnowledgePackIntegrityError) as excinfo:
            await KnowledgePackService(db).import_pack(
                pack_file,
                project_id=target_project,
                project_name=f"Outline corruption {corruption}",
            )
    assert {issue["category"] for issue in excinfo.value.issues} == {"outline_hierarchy_invalid"}
    assert await db.fetchone("SELECT id FROM projects WHERE id = ?", [target_project]) is None


@pytest.mark.asyncio
async def test_pack_import_rejects_outline_parent_absent_from_pack(
    db_with_project,
    tmp_path: Path,
) -> None:
    db = db_with_project
    await _seed_native_manuscript(db)
    pack_path, _ = await KnowledgePackService(db, project_id="proj_default").export_pack()
    corrupt_path = tmp_path / "outline-missing-parent.rka-pack.zip"

    def corrupt(tables: dict[str, list[dict]]) -> None:
        units = {row["local_key"]: row for row in tables["manuscript_units"]}
        profiles = {row["unit_id"]: row for row in tables["manuscript_unit_outline_profiles"]}
        profiles[units["R1"]["id"]]["parent_unit_id"] = generate_id("manuscript_unit")

    _rewrite_pack_manifest(pack_path, corrupt_path, corrupt)
    with corrupt_path.open("rb") as pack_file:
        from rka.services.knowledge_pack import KnowledgePackIntegrityError
        with pytest.raises(KnowledgePackIntegrityError) as error:
            await KnowledgePackService(db).import_pack(
                pack_file,
                project_id="proj_outline_missing_parent",
                project_name="Outline missing parent",
            )
        assert any(item.get("column") == "parent_unit_id" for item in error.value.issues)
    assert (
        await db.fetchone("SELECT id FROM projects WHERE id = 'proj_outline_missing_parent'")
        is None
    )
