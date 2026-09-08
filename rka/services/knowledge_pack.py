"""Project knowledge-pack export/import service."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import tempfile
import zipfile
from collections import Counter
from pathlib import Path
from typing import Any, BinaryIO

from rka import __version__
from rka.infra.ids import generate_id
from rka.models.knowledge_pack import KnowledgePackImportResult
from rka.models.planning import validate_planning_payload
from rka.models.semantic_patch import SemanticPatchProposalCreate
from rka.services.base import BaseService, _now
from rka.services.outline_integrity import validate_unit_hierarchy
from rka.services.sources import (
    SourceRegistrationError,
    verify_registered_source_artifact,
)

logger = logging.getLogger(__name__)

PACK_SCHEMA_VERSION = 8
PACK_FILE_SUFFIX = ".rka-pack.zip"
_IMPORT_PROJECT_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")

# These tables are deliberately not portable even though they carry a
# ``project_id``.  A pack transports research semantics, not one installation's
# worker/cursor machinery or the diagnostics produced while migrating that
# installation.  Importing semantic rows emits a fresh target-local change
# ledger, while completed validation outcomes remain portable through the
# immutable attestation tables in ``core_data``.
_PORTABILITY_EXCLUSIONS: dict[str, str] = {
    "change_events": (
        "Target-local cursor ledger; import emits fresh events with target-local watermarks."
    ),
    "jobs": (
        "Worker queue state, retries, leases, and pending external work are "
        "installation-local and must be requested again after import."
    ),
    "manuscript_migration_issues": (
        "Diagnostics from the source installation's legacy migration, not "
        "manuscript semantic state."
    ),
    "reference_validation_migration_issues": (
        "Diagnostics from source-installation reference-validation migration, "
        "not portable manuscript semantic state."
    ),
    "manuscript_source_proposals": (
        "Candidate source text, local workspace paths, and recovery state are "
        "installation-local authoring data; export the manuscript files separately."
    ),
    "manuscript_source_events": (
        "Source-file apply and recovery events refer to installation-local paths "
        "and are omitted with their source proposals."
    ),
}


# Mission A T5 (mis_01KR1Z28QW9WYXG4VV8PGYWD8G) introduced a hardcoded
# critical-categories set. Mission B Affordance E
# (mis_01KR209WY4M6WQFEXRH79KC2ZF) replaces that with a per-issue
# `severity` field on check_integrity output (see
# KnowledgePackService._SEVERITY_BY_CATEGORY). The rollback gate in
# import_pack now reads issue["severity"] == "critical" rather than
# matching against this set; the set is preserved for backward-
# compatibility with any external caller importing the symbol, but
# new code SHOULD prefer the severity field.
_CRITICAL_INTEGRITY_CATEGORIES: frozenset[str] = frozenset(
    {
        "orphaned_entity_link_sources",
        "orphaned_entity_link_targets",
        "orphaned_claim_edge_sources",
        "orphaned_claim_edge_targets",
        "orphaned_claim_edge_clusters",
        "claim_scope_pointer_mismatch",
        "claim_scope_revision_chain_invalid",
        "claim_scope_disconfirming_refs_invalid",
        "experiment_plan_head_mismatch",
        "experiment_plan_chain_invalid",
        "experiment_run_plan_invalid",
        "experiment_run_event_head_mismatch",
        "experiment_observation_parent_invalid",
        "evidence_locator_source_invalid",
        "experiment_candidate_source_invalid",
        "claim_evidence_relation_invalid",
        "planning_branch_lineage_invalid",
        "planning_branch_event_head_mismatch",
        "planning_artifact_head_mismatch",
        "planning_artifact_chain_invalid",
        "planning_evidence_binding_invalid",
        "planning_payload_invalid",
        "semantic_patch_manifest_hash_invalid",
        "semantic_patch_proposal_context_invalid",
        "semantic_patch_proposal_event_head_mismatch",
        "semantic_patch_provider_event_chain_invalid",
        "semantic_patch_payload_invalid",
        "outline_hierarchy_invalid",
        "registered_source_artifact_invalid",
        "source_admission_invalid",
        "journal_attribution_history_invalid",
    }
)


class KnowledgePackIntegrityError(RuntimeError):
    """Raised when an imported pack fails the post-insert integrity gate.

    Carries the structured `issues` list (the same shape returned by
    KnowledgePackService.check_integrity) so the caller can surface the
    cause to the operator. The transaction is rolled back before this is
    raised; no partial state is left behind.
    """

    def __init__(self, issues: list[dict]):
        self.issues = issues
        cats = sorted({issue["category"] for issue in issues})
        super().__init__("Pack import rejected: critical integrity issues — " + ", ".join(cats))


# Categorized table registry — all tables with project_id MUST be listed here.
# Export validation will FAIL if any uncategorized table has data.
_TABLE_CATEGORIES: dict[str, list[str]] = {
    "core_data": [
        # MUST export, MUST import — these are the research knowledge
        "literature",
        "decisions",
        "missions",
        "journal",
        "journal_attribution_revisions",
        "checkpoints",
        "interpretation_candidates",
        "interpretation_candidate_hints",
        "interpretation_review_events",
        "registered_sources",
        "source_admissions",
        "experiments",
        "experiment_plan_versions",
        "experiment_runs",
        "experiment_run_events",
        "experiment_observations",
        "evidence_locators",
        "claims",
        "claim_scope_versions",
        "interpretation_promotions",
        "claim_evidence_relations",
        "evidence_clusters",
        "claim_edges",
        "entity_links",
        "tags",
        # Native manuscript aggregate. Immutable histories and typed joins are
        # first-class knowledge and must round-trip without reconstruction.
        "manuscripts",
        "manuscript_planning_branches",
        "manuscript_planning_branch_events",
        "manuscript_planning_artifacts",
        "manuscript_planning_artifact_versions",
        "manuscript_planning_evidence_bindings",
        "manuscript_planning_promotion_events",
        "manuscript_evaluation_events",
        "semantic_patch_context_manifests",
        "semantic_patch_proposals",
        "semantic_patch_proposal_events",
        "semantic_patch_provider_events",
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
        "decision_options",
        "calibration_outcomes",
        "reference_validation_attestations",
        "hooks",
        "brain_notifications",
    ],
    "derived_data": [
        # SHOULD export, can rebuild if missing
        "review_queue",
        "topics",
        "entity_topics",
        "exploration_summaries",
        "context_snapshots",
        "keynodes",
        "graph_views",
        "artifacts",
        "figures",
        "bootstrap_log",
    ],
    "system": [
        # SKIP — managed by infrastructure, not per-project data
        "projects",
        "project_states",
        "project_state",
        "schema_migrations",
        "runtime_schema_upgrades",
        "change_events",
        "jobs",
        "manuscript_migration_issues",
        "reference_validation_migration_issues",
        "manuscript_source_proposals",
        "manuscript_source_events",
        "project_deletion_authorizations",
        "kv_store",
        "embedding_metadata",
        "embedding_index_state",
    ],
    "indexes": [
        # SKIP — rebuilt automatically (vec_*, fts_* detected by prefix)
    ],
    "bulk_logs": [
        # OPTIONAL — export only with include_logs=True
        "audit_log",
        "events",
        "qa_sessions",
        "qa_logs",
        "hook_executions",
    ],
}

# Computed from categories — FK-dependency sorted insert order
_INSERT_ORDER = (
    # core_data (FK-safe order)
    "literature",
    "decisions",
    "decision_options",
    "missions",
    "journal",
    "journal_attribution_revisions",
    "reference_validation_attestations",
    "checkpoints",
    "artifacts",
    "registered_sources",
    "evidence_clusters",
    "experiments",
    "experiment_plan_versions",
    "experiment_runs",
    "experiment_run_events",
    "experiment_observations",
    "interpretation_candidates",
    "interpretation_candidate_hints",
    "interpretation_review_events",
    "claims",
    "claim_scope_versions",
    "interpretation_promotions",
    "claim_evidence_relations",
    "claim_edges",
    "entity_links",
    "source_admissions",
    "tags",
    "calibration_outcomes",
    "hooks",
    "brain_notifications",
    # Native manuscript aggregate (FK-safe order). The legacy journal row is
    # already present before ``manuscripts.legacy_journal_id`` is restored.
    "manuscripts",
    "manuscript_planning_branches",
    "manuscript_planning_artifacts",
    "manuscript_planning_artifact_versions",
    "manuscript_planning_evidence_bindings",
    "manuscript_planning_branch_events",
    "semantic_patch_context_manifests",
    "semantic_patch_proposals",
    "manuscript_planning_promotion_events",
    "manuscript_evaluation_events",
    "semantic_patch_proposal_events",
    "semantic_patch_provider_events",
    "manuscript_reference_members",
    "manuscript_claims",
    "manuscript_claim_versions",
    "manuscript_units",
    "manuscript_unit_outline_profiles",
    "manuscript_claim_evidence",
    "manuscript_unit_evidence",
    "manuscript_unit_citations",
    "manuscript_claim_units",
    "manuscript_claim_ratifications",
    "manuscript_checkpoints",
    "manuscript_claim_verification_attestations",
    # derived_data
    "review_queue",
    "topics",
    "entity_topics",
    "exploration_summaries",
    "context_snapshots",
    "keynodes",
    "graph_views",
    "evidence_locators",
    "figures",
    "bootstrap_log",
    # bulk_logs (when included)
    "audit_log",
    "events",
    "qa_sessions",
    "qa_logs",
    "hook_executions",
)
_IMPORT_INSERT_TABLES = frozenset(("projects", "project_states", *_INSERT_ORDER))
_IMPORT_TRANSIENT_COLUMNS: dict[str, frozenset[str]] = {
    # Bundled-file metadata is consumed by ``_restore_artifact_files`` and is
    # deliberately not a column in the destination artifacts table.
    "artifacts": frozenset({"pack_file"}),
}

# Tables to export by default (core + derived)
_EXPORT_TABLES = _TABLE_CATEGORIES["core_data"] + _TABLE_CATEGORIES["derived_data"]
# All registered table names for validation
_ALL_REGISTERED = {t for tables in _TABLE_CATEGORIES.values() for t in tables}
_SELF_REFERENTIAL_TABLES: dict[str, str | list[str]] = {
    "missions": ["depends_on", "parent_mission_id"],
    "decisions": ["parent_id", "superseded_by"],
    "journal": "supersedes",
    "claim_scope_versions": "supersedes_scope_id",
    "experiment_plan_versions": "supersedes_plan_id",
    "events": "caused_by_event",
    "manuscript_checkpoints": "supersedes_id",
    "manuscript_planning_branches": "parent_branch_id",
    "manuscript_planning_artifact_versions": [
        "supersedes_version_id",
        "derived_from_version_id",
    ],
    "semantic_patch_proposals": "supersedes_proposal_id",
    "topics": "parent_id",
}
_ID_ENTITY_TYPES = {
    "literature": "literature",
    "missions": "mission",
    "decisions": "decision",
    "journal": "journal",
    "journal_attribution_revisions": "journal_attribution_revision",
    "checkpoints": "checkpoint",
    "claims": "claim",
    "claim_scope_versions": "claim_scope",
    "interpretation_candidates": "interpretation_candidate",
    "interpretation_candidate_hints": "interpretation_hint",
    "interpretation_review_events": "interpretation_review",
    "interpretation_promotions": "interpretation_promotion",
    "registered_sources": "registered_source",
    "source_admissions": "source_admission",
    "experiments": "experiment",
    "experiment_plan_versions": "experiment_plan_version",
    "experiment_runs": "experiment_run",
    "experiment_run_events": "experiment_run_event",
    "experiment_observations": "experiment_observation",
    "evidence_locators": "evidence_locator",
    "claim_evidence_relations": "claim_evidence_relation",
    "evidence_clusters": "cluster",
    "claim_edges": "claim_edge",
    "decision_options": "decision_option",
    "calibration_outcomes": "calibration_outcome",
    "reference_validation_attestations": "reference_validation",
    "manuscripts": "manuscript",
    "manuscript_planning_branches": "manuscript_planning_branch",
    "manuscript_planning_branch_events": "manuscript_planning_branch_event",
    "manuscript_planning_artifacts": "manuscript_planning_artifact",
    "manuscript_planning_artifact_versions": "manuscript_planning_artifact_version",
    "manuscript_planning_evidence_bindings": "manuscript_planning_evidence_binding",
    "manuscript_planning_promotion_events": "manuscript_planning_promotion_event",
    "manuscript_evaluation_events": "manuscript_evaluation_event",
    "semantic_patch_context_manifests": "semantic_patch_context_manifest",
    "semantic_patch_proposals": "semantic_patch_proposal",
    "semantic_patch_proposal_events": "semantic_patch_proposal_event",
    "semantic_patch_provider_events": "semantic_patch_provider_event",
    "manuscript_reference_members": "manuscript_reference",
    "manuscript_unit_citations": "manuscript_unit_citation",
    "manuscript_claims": "manuscript_claim",
    "manuscript_claim_ratifications": "manuscript_claim_ratification",
    "manuscript_units": "manuscript_unit",
    "manuscript_checkpoints": "manuscript_checkpoint",
    "manuscript_claim_verification_attestations": "manuscript_verification",
    "hooks": "hook",
    "hook_executions": "hook_execution",
    "brain_notifications": "brain_notification",
    "artifacts": "artifact",
    "figures": "figure",
    "exploration_summaries": "summary",
    "qa_sessions": "qa_session",
    "qa_logs": "qa_log",
    "events": "event",
    "entity_links": "link",
    "review_queue": "review_item",
    "keynodes": "keynode",
    "graph_views": "graphview",
    "topics": "topic",
    "context_snapshots": "context_snapshot",
}
_DIRECT_ID_COLUMNS = {
    "literature": ("id",),
    "missions": ("id", "depends_on", "parent_mission_id", "motivated_by_decision"),
    "decisions": (
        "id",
        "parent_id",
        "superseded_by",
        "recommended_option_id",
        "pi_selected_option_id",
    ),
    "journal": ("id", "related_mission", "supersedes", "superseded_by"),
    "journal_attribution_revisions": ("id", "journal_id"),
    "checkpoints": ("id", "mission_id", "linked_decision_id"),
    "claims": ("id", "source_entry_id", "staleness_resolution_journal_id"),
    "claim_scope_versions": (
        "id",
        "claim_id",
        "source_candidate_id",
        "supersedes_scope_id",
    ),
    "interpretation_candidates": ("id", "source_id", "disposition_target_id"),
    "interpretation_candidate_hints": (
        "id",
        "candidate_id",
        "related_candidate_id",
    ),
    "interpretation_review_events": (
        "id",
        "candidate_id",
        "target_id",
    ),
    "interpretation_promotions": ("id", "candidate_id", "claim_id"),
    "registered_sources": ("id", "artifact_id"),
    "source_admissions": (
        "id",
        "source_id",
        "candidate_id",
        "target_id",
    ),
    "experiments": ("id",),
    "experiment_plan_versions": (
        "id",
        "experiment_id",
        "supersedes_plan_id",
    ),
    "experiment_runs": ("id", "experiment_id"),
    "experiment_run_events": ("id", "run_id"),
    "experiment_observations": ("id", "run_id"),
    "evidence_locators": ("id", "observation_id", "artifact_id"),
    "claim_evidence_relations": (
        "id",
        "claim_id",
        "observation_id",
        "candidate_id",
    ),
    "evidence_clusters": (
        "id",
        "research_question_id",
        "staleness_resolution_journal_id",
    ),
    "claim_edges": ("id", "source_claim_id", "target_claim_id", "cluster_id"),
    "decision_options": ("id", "decision_id", "dominated_by"),
    "calibration_outcomes": ("id", "decision_id"),
    "reference_validation_attestations": (
        "id",
        "manuscript_id",
        "canonical_manuscript_id",
        "legacy_journal_id",
        "literature_id",
        # Migration 036 links an attestation to the local worker job that
        # produced it. Jobs are deliberately excluded, so this FK is cleared
        # during remapping while the immutable completed result is retained.
        "validation_job_id",
    ),
    "manuscripts": ("id", "legacy_journal_id"),
    "manuscript_planning_branches": (
        "id",
        "manuscript_id",
        "context_key",
        "parent_branch_id",
    ),
    "manuscript_planning_branch_events": ("id", "branch_id"),
    "manuscript_planning_artifacts": ("id", "branch_id", "current_version_id"),
    "manuscript_planning_artifact_versions": (
        "id",
        "artifact_id",
        "branch_id",
        "promotion_target_id",
        "supersedes_version_id",
        "derived_from_version_id",
    ),
    "manuscript_planning_evidence_bindings": (
        "id",
        "artifact_version_id",
        "artifact_id",
        "entity_id",
    ),
    "manuscript_planning_promotion_events": (
        "id",
        "branch_id",
        "artifact_id",
        "artifact_version_id",
        "target_id",
        "proposal_id",
        "decision_id",
    ),
    "manuscript_evaluation_events": (
        "id",
        "branch_id",
        "artifact_id",
        "artifact_version_id",
        "target_id",
        "proposal_id",
        "mission_id",
    ),
    "semantic_patch_context_manifests": ("id",),
    "semantic_patch_proposals": (
        "id",
        "context_manifest_id",
        "supersedes_proposal_id",
    ),
    "semantic_patch_proposal_events": ("id", "proposal_id"),
    "semantic_patch_provider_events": ("id", "call_id", "context_manifest_id"),
    "manuscript_reference_members": (
        "id",
        "manuscript_id",
        "literature_id",
    ),
    "manuscript_claims": ("id", "manuscript_id"),
    "manuscript_claim_versions": ("claim_id", "manuscript_id"),
    "manuscript_claim_ratifications": (
        "id",
        "manuscript_id",
        "claim_id",
        "decision_id",
    ),
    "manuscript_units": ("id", "manuscript_id", "artifact_ref"),
    "manuscript_unit_outline_profiles": (
        "unit_id",
        "manuscript_id",
        "parent_unit_id",
    ),
    "manuscript_claim_evidence": (
        "manuscript_id",
        "manuscript_claim_id",
        "evidence_claim_id",
    ),
    "manuscript_unit_evidence": (
        "manuscript_id",
        "unit_id",
        "evidence_claim_id",
    ),
    "manuscript_unit_citations": (
        "id",
        "manuscript_id",
        "unit_id",
        "reference_member_id",
    ),
    "manuscript_claim_units": (
        "manuscript_id",
        "manuscript_claim_id",
        "unit_id",
    ),
    "manuscript_checkpoints": (
        "id",
        "manuscript_id",
        "unit_id",
        "decision_id",
        "supersedes_id",
    ),
    "manuscript_claim_verification_attestations": (
        "id",
        "manuscript_id",
        "claim_id",
    ),
    "hooks": ("id",),
    "hook_executions": ("id", "hook_id"),
    "brain_notifications": ("id", "hook_id"),
    "artifacts": ("id",),
    "figures": ("id", "artifact_id"),
    "exploration_summaries": ("id", "scope_id"),
    "qa_sessions": ("id",),
    "qa_logs": ("id", "session_id"),
    "events": ("id", "entity_id", "caused_by_event", "caused_by_entity"),
    "audit_log": ("entity_id",),
    "tags": ("entity_id",),
    "bootstrap_log": ("entity_id",),
    "entity_links": ("id", "source_id", "target_id"),
    "review_queue": ("id", "item_id"),
    "keynodes": ("id",),
    "graph_views": ("id",),
    "topics": ("id", "parent_id"),
    "context_snapshots": ("id",),
    "entity_topics": ("topic_id", "entity_id"),
}
# Columns with actual FOREIGN KEY constraints in the schema.
# Unresolvable references in these columns must be NULLed to avoid FK errors.
# (Excludes "id" columns — those are PKs, not FK references.)
_FK_COLUMNS: dict[str, set[str]] = {
    "decisions": {"parent_id", "superseded_by"},
    "missions": {"depends_on", "parent_mission_id", "motivated_by_decision"},
    "journal": {"supersedes"},
    "checkpoints": {"mission_id", "linked_decision_id"},
    "claims": {"source_entry_id"},
    "claim_scope_versions": {
        "claim_id",
        "source_candidate_id",
        "supersedes_scope_id",
    },
    "interpretation_candidate_hints": {
        "candidate_id",
        "related_candidate_id",
    },
    "interpretation_review_events": {"candidate_id"},
    "interpretation_promotions": {"candidate_id", "claim_id"},
    "registered_sources": {"artifact_id"},
    "source_admissions": {"source_id", "candidate_id"},
    "experiment_plan_versions": {"experiment_id", "supersedes_plan_id"},
    "experiment_runs": {"experiment_id"},
    "experiment_run_events": {"run_id"},
    "experiment_observations": {"run_id"},
    "evidence_locators": {"observation_id", "artifact_id"},
    "claim_evidence_relations": {
        "claim_id",
        "observation_id",
        "candidate_id",
    },
    "evidence_clusters": {"research_question_id"},
    "claim_edges": {"source_claim_id", "target_claim_id", "cluster_id"},
    "events": {"caused_by_event"},
    "qa_logs": {"session_id"},
    "figures": {"artifact_id"},
    "reference_validation_attestations": {
        "canonical_manuscript_id",
        "legacy_journal_id",
        "literature_id",
        "validation_job_id",
    },
    "manuscripts": {"legacy_journal_id"},
    "manuscript_planning_branches": {"manuscript_id", "parent_branch_id"},
    "manuscript_planning_branch_events": {"branch_id"},
    "manuscript_planning_artifacts": {"branch_id"},
    "manuscript_planning_artifact_versions": {
        "artifact_id",
        "branch_id",
        "supersedes_version_id",
        "derived_from_version_id",
    },
    "manuscript_planning_evidence_bindings": {
        "artifact_version_id",
        "artifact_id",
    },
    "manuscript_planning_promotion_events": {
        "branch_id",
        "artifact_id",
        "artifact_version_id",
        "proposal_id",
        "decision_id",
    },
    "manuscript_evaluation_events": {
        "branch_id",
        "artifact_id",
        "artifact_version_id",
        "proposal_id",
        "mission_id",
    },
    "semantic_patch_proposals": {"context_manifest_id", "supersedes_proposal_id"},
    "semantic_patch_proposal_events": {"proposal_id"},
    "semantic_patch_provider_events": {"context_manifest_id"},
    "manuscript_reference_members": {"manuscript_id", "literature_id"},
    "manuscript_claims": {"manuscript_id"},
    "manuscript_claim_versions": {"claim_id", "manuscript_id"},
    "manuscript_claim_ratifications": {
        "manuscript_id",
        "claim_id",
        "decision_id",
    },
    "manuscript_units": {"manuscript_id"},
    "manuscript_unit_outline_profiles": {
        "unit_id",
        "manuscript_id",
        "parent_unit_id",
    },
    "manuscript_claim_evidence": {
        "manuscript_id",
        "manuscript_claim_id",
        "evidence_claim_id",
    },
    "manuscript_unit_evidence": {
        "manuscript_id",
        "unit_id",
        "evidence_claim_id",
    },
    "manuscript_unit_citations": {
        "manuscript_id",
        "unit_id",
        "reference_member_id",
    },
    "manuscript_claim_units": {
        "manuscript_id",
        "manuscript_claim_id",
        "unit_id",
    },
    "manuscript_checkpoints": {
        "manuscript_id",
        "unit_id",
        "decision_id",
        "supersedes_id",
    },
    "manuscript_claim_verification_attestations": {
        "manuscript_id",
        "claim_id",
    },
    "topics": {"parent_id"},
    "entity_topics": {"topic_id"},
}
# Matches entity-ID-shaped tokens embedded in prose/JSON strings (lowercase
# prefix + ULID-style Crockford-base32 tail). Deliberately permissive: a
# match is only rewritten when it is a key in the pack's id_map, so false
# positives pass through unchanged.
_EMBEDDED_ID_RE = re.compile(r"\b[a-z][a-z_]{1,30}_[0-9A-HJKMNP-TV-Z]{16,32}\b")

# Free-text columns scanned for embedded entity IDs during import re-keying
# (rationale like "Supersedes dec_…", journal content citing other entries).
# Registry-scoped on purpose: a blanket all-string pass would also rewrite
# pack-internal lookup values such as artifact file paths, breaking
# _restore_artifact_files' old-path -> archive mapping.
_PROSE_TEXT_COLUMNS: dict[str, tuple[str, ...]] = {
    # Exact originals and correction reasons/snapshots are evidence, not
    # structured references. Re-keying must never rewrite quoted bytes.
    "journal": ("content", "summary"),
    "decisions": ("question", "rationale", "chosen", "abandonment_reason", "options"),
    "missions": (
        "objective",
        "context",
        "tasks",
        "acceptance_criteria",
        "checkpoint_triggers",
        "report",
    ),
    "literature": ("abstract", "notes"),
    "claims": ("content", "staleness_resolution"),
    "claim_scope_versions": (
        "uncertainty_note",
        "falsifier",
        "falsifier_rationale",
        "reason",
    ),
    "interpretation_candidates": (
        "statement",
        "uncertainty_note",
        "falsifier",
        "disposition_reason",
    ),
    "interpretation_candidate_hints": ("rationale",),
    "interpretation_review_events": ("reason",),
    "source_admissions": ("reason",),
    "interpretation_promotions": ("promotion_reason", "revocation_reason"),
    "experiment_plan_versions": (
        "objective",
        "hypothesis",
        "protocol",
        "reason",
    ),
    "experiment_runs": ("label", "command", "failure_summary"),
    "experiment_run_events": ("reason",),
    "experiment_observations": (
        "name",
        "summary",
        "value_text",
        "uncertainty_note",
    ),
    "evidence_locators": ("label", "locator_value"),
    "claim_evidence_relations": ("review_reason", "revocation_reason"),
    "evidence_clusters": ("label", "synthesis", "staleness_resolution"),
    "checkpoints": ("description",),
    "events": ("summary",),
    "manuscript_claim_versions": ("exact_wording", "allowed_wording"),
    "manuscript_unit_evidence": ("supported_proposition", "warrant"),
    "manuscript_unit_citations": (
        "supported_proposition",
        "comparison_axis",
    ),
    "manuscript_units": (
        "allowed_interpretation",
        "prohibited_interpretation",
    ),
    "manuscript_unit_outline_profiles": (
        "communicative_job",
        "intended_takeaway",
        "transition_from_previous",
        "quick_reader_role",
        "blocker",
    ),
    "manuscript_planning_branches": ("name", "purpose"),
    "manuscript_planning_branch_events": ("reason",),
    "manuscript_planning_artifact_versions": (
        "summary",
        "readiness_notes",
        "reason",
    ),
    "manuscript_planning_evidence_bindings": ("locator_value", "note"),
    "manuscript_planning_promotion_events": ("candidate_key", "reason"),
    "manuscript_evaluation_events": (
        "commitment_key",
        "requirement_key",
        "reason",
    ),
    "semantic_patch_proposals": ("intent", "reason"),
    "semantic_patch_proposal_events": ("reason",),
}

_JSON_ID_COLUMNS = {
    "literature": ("related_decisions",),
    "decisions": ("related_missions", "related_literature", "related_journal"),
    "journal": ("related_decisions", "related_literature"),
    "interpretation_candidates": ("scope_conditions",),
    "claim_scope_versions": (
        "conditions",
        "allowed_extensions",
        "prohibited_extensions",
        "disconfirming_claim_ids",
    ),
    "experiment_plan_versions": (
        "conditions",
        "variables",
        "metrics",
        "baselines",
        "success_criteria",
        "failure_criteria",
    ),
    "experiment_runs": ("config", "environment"),
    "exploration_summaries": ("source_refs",),
    "qa_logs": ("answer_structured", "sources"),
    "events": ("details",),
    "audit_log": ("details",),
    "reference_validation_attestations": ("full_json_payload",),
    "manuscript_claim_versions": (
        "prohibited_wording",
        "conditions",
        "falsification_criteria",
    ),
    # Expanded checkpoint snapshots carry auditable nested identities. Import
    # re-keys their components below and recomputes the digest. Older hash-only
    # snapshots are opaque and therefore cannot be made portable retroactively.
    "manuscript_checkpoints": ("dependency_snapshot",),
    "manuscript_unit_outline_profiles": (
        "evidence_plan",
        "figure_intentions",
        "table_intentions",
        "citation_intentions",
    ),
    "manuscript_planning_branch_events": ("details",),
    "manuscript_planning_artifact_versions": (
        "payload",
        "unresolved_items",
        "readiness_missing",
    ),
    "manuscript_planning_promotion_events": ("details",),
    "manuscript_evaluation_events": ("details",),
    "manuscript_claim_verification_attestations": (
        "dependency_snapshot",
        "full_json_payload",
    ),
    "semantic_patch_context_manifests": (
        "selected_context",
        "resolved_context",
        "target_bases",
        "constraints",
        "omissions",
        "truncation_notes",
    ),
    "semantic_patch_proposals": (
        "operations",
        "target_bases",
        "semantic_diff",
        "validation_findings",
    ),
    "semantic_patch_proposal_events": ("details",),
    "semantic_patch_provider_events": ("details",),
    "context_snapshots": ("entry_ids",),
    "keynodes": ("node_refs",),
    "graph_views": ("nodes", "edges"),
}

# Declared entity-link endpoint types and their authoritative project-scoped
# tables. Integrity checks must match both this type and the edge's project;
# global id existence is insufficient because IDs from another project (or a
# different entity table) would otherwise make a corrupt edge appear valid.
_ENTITY_LINK_ENDPOINT_TABLES: dict[str, str] = {
    "artifact": "artifacts",
    "checkpoint": "checkpoints",
    "claim": "claims",
    "claim_scope": "claim_scope_versions",
    "interpretation_candidate": "interpretation_candidates",
    "interpretation_hint": "interpretation_candidate_hints",
    "interpretation_promotion": "interpretation_promotions",
    "interpretation_review": "interpretation_review_events",
    "experiment": "experiments",
    "experiment_plan_version": "experiment_plan_versions",
    "experiment_run": "experiment_runs",
    "experiment_run_event": "experiment_run_events",
    "experiment_observation": "experiment_observations",
    "evidence_locator": "evidence_locators",
    "claim_evidence_relation": "claim_evidence_relations",
    "claim_edge": "claim_edges",
    "cluster": "evidence_clusters",
    "decision": "decisions",
    "decision_option": "decision_options",
    "event": "events",
    "figure": "figures",
    "journal": "journal",
    "link": "entity_links",
    "literature": "literature",
    "manuscript": "manuscripts",
    "manuscript_planning_branch": "manuscript_planning_branches",
    "manuscript_planning_branch_event": "manuscript_planning_branch_events",
    "manuscript_planning_artifact": "manuscript_planning_artifacts",
    "manuscript_planning_artifact_version": "manuscript_planning_artifact_versions",
    "manuscript_planning_evidence_binding": "manuscript_planning_evidence_bindings",
    "manuscript_planning_promotion_event": "manuscript_planning_promotion_events",
    "manuscript_evaluation_event": "manuscript_evaluation_events",
    "semantic_patch_context_manifest": "semantic_patch_context_manifests",
    "semantic_patch_proposal": "semantic_patch_proposals",
    "semantic_patch_proposal_event": "semantic_patch_proposal_events",
    "semantic_patch_provider_event": "semantic_patch_provider_events",
    "manuscript_checkpoint": "manuscript_checkpoints",
    "manuscript_claim": "manuscript_claims",
    "manuscript_claim_ratification": "manuscript_claim_ratifications",
    "manuscript_claim_verification": ("manuscript_claim_verification_attestations"),
    "manuscript_reference": "manuscript_reference_members",
    "manuscript_unit_citation": "manuscript_unit_citations",
    "manuscript_unit": "manuscript_units",
    "mission": "missions",
    "reference_validation": "reference_validation_attestations",
    "research_question": "decisions",
    "review": "review_queue",
    "topic": "topics",
}

_PLANNING_EVIDENCE_ENTITY_TABLES: dict[str, str] = {
    "journal": "journal",
    "literature": "literature",
    "decision": "decisions",
    "claim": "claims",
    "claim_scope": "claim_scope_versions",
    "cluster": "evidence_clusters",
    "interpretation_candidate": "interpretation_candidates",
    "experiment": "experiments",
    "experiment_plan_version": "experiment_plan_versions",
    "experiment_run": "experiment_runs",
    "experiment_observation": "experiment_observations",
    "evidence_locator": "evidence_locators",
    "artifact": "artifacts",
    "manuscript": "manuscripts",
    "manuscript_claim": "manuscript_claims",
    "manuscript_unit": "manuscript_units",
}


class KnowledgePackService(BaseService):
    """Export and import a full project-scoped knowledge pack."""

    async def export_pack(
        self, project_id: str | None = None, include_logs: bool = False
    ) -> tuple[str, str]:
        """Export one transactionally consistent project snapshot."""
        async with self.db.transaction(write=False):
            return await self._export_pack(project_id, include_logs)

    async def _export_pack(
        self,
        project_id: str | None,
        include_logs: bool,
    ) -> tuple[str, str]:
        """Implementation for :meth:`export_pack` inside its read snapshot."""
        resolved_project_id = self._resolve_project_id(project_id)
        project = await self.db.fetchone(
            "SELECT * FROM projects WHERE id = ?",
            [resolved_project_id],
        )
        if project is None:
            raise ValueError(f"Project '{resolved_project_id}' not found")

        # Dynamic validation: fail if any uncategorized table has data
        await self._validate_registry(resolved_project_id)

        project_state = await self.db.fetchone(
            "SELECT * FROM project_states WHERE project_id = ?",
            [resolved_project_id],
        )

        # Determine which tables to export
        export_tables = list(_EXPORT_TABLES)
        if include_logs:
            export_tables += _TABLE_CATEGORIES["bulk_logs"]

        # Get schema version (latest migration number)
        schema_version = await self._get_schema_version()

        table_counts: dict[str, int] = {}
        manifest: dict[str, Any] = {
            "schema_version": schema_version,
            "pack_format_version": PACK_SCHEMA_VERSION,
            "exported_at": _now(),
            "rka_version": __version__,
            "categories_exported": ["core_data", "derived_data"]
            + (["bulk_logs"] if include_logs else []),
            "project": dict(project),
            "project_state": dict(project_state) if project_state else None,
            "portability": {
                "excluded_tables": dict(sorted(_PORTABILITY_EXCLUSIONS.items())),
                "completed_validation_attestations": "included",
                "validation_job_links_on_import": "cleared",
            },
            "tables": {},
        }

        temp_file = tempfile.NamedTemporaryFile(
            prefix=f"{self._slugify(project['name'] or resolved_project_id)}-",
            suffix=PACK_FILE_SUFFIX,
            delete=False,
        )
        temp_path = Path(temp_file.name)
        temp_file.close()

        try:
            with zipfile.ZipFile(
                temp_path,
                "w",
                compression=zipfile.ZIP_DEFLATED,
            ) as archive:
                for table in export_tables:
                    rows = await self._export_rows_for_table(
                        table,
                        resolved_project_id,
                    )
                    if table == "artifacts":
                        rows = self._attach_artifact_files(rows, archive)
                    manifest["tables"][table] = rows
                    table_counts[table] = len(rows)
                self._validate_portable_child_links(
                    manifest["tables"],
                    project_id=resolved_project_id,
                )
                manifest["table_counts"] = table_counts
                archive.writestr(
                    "manifest.json",
                    json.dumps(manifest, indent=2, sort_keys=True),
                )
        except BaseException:
            temp_path.unlink(missing_ok=True)
            raise

        filename = f"{self._slugify(project['name'] or resolved_project_id)}{PACK_FILE_SUFFIX}"
        return str(temp_path), filename

    async def import_pack(
        self,
        fileobj: BinaryIO,
        project_id: str | None = None,
        project_name: str | None = None,
        defer_indexing: bool = False,
    ) -> KnowledgePackImportResult:
        with zipfile.ZipFile(fileobj) as archive:
            manifest = self._load_manifest(archive)
            source_project = manifest["project"]
            source_state = manifest.get("project_state")
            source_tables: dict[str, list[dict[str, Any]]] = manifest.get("tables", {})
            pack_format = manifest.get("pack_format_version") or manifest.get("schema_version")

            raw_target_project_id = (
                project_id if project_id is not None else source_project.get("id")
            )
            raw_target_project_name = (
                project_name if project_name is not None else source_project.get("name")
            )
            if not isinstance(raw_target_project_id, str):
                raise ValueError("Imported project ID must be a string")
            if not isinstance(raw_target_project_name, str):
                raise ValueError("Imported project name must be a string")

            target_project_id = raw_target_project_id.strip()
            target_project_name = raw_target_project_name.strip()
            target_description = source_project.get("description")
            if not target_project_id:
                raise ValueError("Imported project ID cannot be empty")
            if not target_project_name:
                raise ValueError("Imported project name cannot be empty")
            self._validate_import_project_id(target_project_id)

            await self._assert_target_project_available(target_project_id, target_project_name)
            self._assert_no_duplicate_pack_dois(source_tables)
            self._validate_portable_child_links(
                source_tables,
                project_id=str(source_project["id"]),
            )
            tables = self._remap_tables(
                source_tables,
                source_project_id=source_project["id"],
                target_project_id=target_project_id,
            )
            # Treat the uploaded manifest as untrusted input. Validate every
            # destination key against the live schema before opening the write
            # transaction (or creating artifact staging directories).
            await self._validate_import_rows(tables)

            artifact_root = self._artifact_import_root(target_project_id)
            artifact_project_root = artifact_root.parent
            artifact_storage_root = artifact_project_root.parent
            staging_project_root: Path | None = None
            published_project_root = False

            if tables.get("artifacts"):
                if artifact_project_root.exists():
                    raise ValueError(
                        f"Artifact storage for imported project already exists: {target_project_id}"
                    )
                artifact_storage_root.mkdir(parents=True, exist_ok=True)
                staging_project_root = Path(
                    tempfile.mkdtemp(
                        prefix=".rka-import-",
                        dir=str(artifact_storage_root),
                    )
                ).resolve()

            try:
                async with self.db.transaction():
                    # Defer FK checks until commit — enables forward references
                    # between tables (e.g. decisions.recommended_option_id →
                    # decision_options inserted later in _INSERT_ORDER). All
                    # refs must resolve by commit; genuine orphans still fail.
                    await self.db.execute("PRAGMA defer_foreign_keys = ON")
                    await self._insert_row(
                        "projects",
                        {
                            "id": target_project_id,
                            "name": target_project_name,
                            "description": target_description,
                            "created_by": source_project.get("created_by"),
                            "created_at": source_project.get("created_at") or _now(),
                            "updated_at": source_project.get("updated_at") or _now(),
                        },
                    )

                    state_row = self._build_project_state_row(
                        source_state=source_state,
                        source_project=source_project,
                        target_project_id=target_project_id,
                        target_project_name=target_project_name,
                        target_description=target_description,
                    )
                    await self._insert_row("project_states", state_row)

                    imported_counts: dict[str, int] = {}
                    artifact_files_restored = 0

                    for table in _INSERT_ORDER:
                        rows = [dict(row) for row in tables.get(table, [])]
                        rows = self._prepare_rows_for_insert(table, rows, target_project_id)

                        if table == "artifacts":
                            staging_artifact_root = (
                                staging_project_root / "artifacts"
                                if staging_project_root is not None
                                else artifact_root
                            )
                            rows, restored = self._restore_artifact_files(
                                rows,
                                archive,
                                staging_artifact_root,
                                persisted_root=artifact_root,
                            )
                            artifact_files_restored += restored

                        for row in rows:
                            await self._insert_row(table, row)
                            if table == "claim_scope_versions":
                                await self.db.execute(
                                    """UPDATE claims SET scope_revision = ?
                                       WHERE id = ? AND project_id = ?""",
                                    [
                                        row["revision"],
                                        row["claim_id"],
                                        target_project_id,
                                    ],
                                )

                        imported_counts[table] = len(rows)

                    # Formats 1 and 2 predate the native manuscript aggregate.
                    # Re-run the conservative legacy projection only for the
                    # imported project, inside the same atomic transaction.
                    # This creates identity/metadata only: never claims,
                    # evidence links, ratifications, or checkpoints.
                    if pack_format in (1, 2):
                        imported_counts["manuscripts"] += await self._backfill_legacy_manuscripts(
                            target_project_id
                        )

                    # The integrity gate runs inside the managed transaction so
                    # critical issues roll back the complete imported graph.
                    integrity_issues = await self.check_integrity(
                        target_project_id,
                        verify_managed_files=False,
                    )
                    critical = [i for i in integrity_issues if i.get("severity") == "critical"]
                    if critical:
                        raise KnowledgePackIntegrityError(critical)

                    if staging_project_root is not None:
                        if artifact_project_root.exists():
                            raise ValueError(
                                "Artifact storage for imported project appeared "
                                f"during import: {target_project_id}"
                            )
                        staging_project_root.replace(artifact_project_root)
                        published_project_root = True

                    # Artifact rows point at their final managed paths. Verify
                    # the exact published bytes while the database transaction
                    # is still reversible; failure removes the published tree
                    # in the surrounding exception handler.
                    integrity_issues = await self.check_integrity(
                        target_project_id,
                        verify_managed_files=True,
                    )
                    critical = [
                        issue
                        for issue in integrity_issues
                        if issue.get("severity") == "critical"
                    ]
                    if critical:
                        raise KnowledgePackIntegrityError(critical)
                    if any(i.get("category") == "claim_count_mismatch" for i in integrity_issues):
                        await self._recompute_cluster_claim_counts(target_project_id)
                    # Lexical writes and durable vector intent must not outlive
                    # a failed graph import, or be lost after a successful one.
                    # defer_indexing remains accepted, but never runs inference.
                    lexical_count = await self.index_project(target_project_id)
                    from rka.services.import_indexing import ImportIndexing
                    indexing = await ImportIndexing(self.db).record(
                        target_project_id, self.embeddings, lexical_count=lexical_count,
                    )
            except BaseException:
                if staging_project_root is not None and staging_project_root.exists():
                    shutil.rmtree(staging_project_root, ignore_errors=True)
                if published_project_root and artifact_project_root.exists():
                    shutil.rmtree(artifact_project_root, ignore_errors=True)
                raise

        return KnowledgePackImportResult(
            project_id=target_project_id,
            project_name=target_project_name,
            source_project_id=source_project["id"],
            imported_counts=imported_counts,
            artifact_files_restored=artifact_files_restored,
            integrity_issues=integrity_issues,
            indexing=indexing,
        )

    async def import_status(self, job_id=None):
        from rka.services.import_indexing import ImportIndexing
        return await ImportIndexing(self.db).status(job_id)

    async def _backfill_legacy_manuscripts(self, project_id: str) -> int:
        """Project legacy Writer journals into native manuscript identities.

        This is the import-time counterpart of migration 034 for accepted
        KnowledgePack formats that were created before native manuscripts
        existed.  It intentionally uses the same fail-closed eligibility rules
        and records diagnostics rather than guessing through ambiguity.
        """
        rows = await self.db.fetchall(
            """SELECT j.id, j.project_id, j.verbatim_input, j.status,
                      j.created_at, j.updated_at
               FROM journal AS j
               WHERE j.project_id = ?
                 AND EXISTS (
                     SELECT 1
                     FROM tags AS t
                     WHERE t.entity_type = 'journal'
                       AND t.entity_id = j.id
                       AND lower(t.tag) = 'manuscript'
                 )
               ORDER BY j.id""",
            [project_id],
        )
        created = 0
        for row in rows:
            legacy_id = str(row["id"])
            candidate_id = (
                f"man_{legacy_id[4:]}"
                if legacy_id.startswith("jrn_") and len(legacy_id) > 4
                else None
            )
            tag_rows = await self.db.fetchall(
                """SELECT tag, project_id
                   FROM tags
                   WHERE entity_type = 'journal' AND entity_id = ?
                   ORDER BY lower(tag), tag""",
                [legacy_id],
            )
            manuscript_tags = [
                tag for tag in tag_rows if str(tag.get("tag") or "").casefold() == "manuscript"
            ]
            scoped_manuscript_tags = [
                tag for tag in manuscript_tags if tag.get("project_id") == project_id
            ]
            venue_tags = [
                str(tag.get("tag") or "")[6:].strip()
                for tag in tag_rows
                if tag.get("project_id") == project_id
                and str(tag.get("tag") or "")[:6].casefold() == "venue:"
            ]
            phase_tags = [
                str(tag.get("tag") or "")[6:].strip().casefold()
                for tag in tag_rows
                if tag.get("project_id") == project_id
                and str(tag.get("tag") or "")[:6].casefold() == "phase:"
            ]
            normalized_input = str(row.get("verbatim_input") or "").replace("\r", "")
            title, separator, remainder = normalized_input.partition("\n\n")
            title = title.strip()
            abstract = (remainder.strip() or None) if separator else None

            issues: list[tuple[str, dict[str, Any]]] = []
            if len(manuscript_tags) != len(scoped_manuscript_tags):
                issues.append(
                    (
                        "tag_project_mismatch",
                        {
                            "manuscript_tag_count": len(manuscript_tags),
                            "same_project_tag_count": len(scoped_manuscript_tags),
                        },
                    )
                )
            if candidate_id is None:
                issues.append(
                    (
                        "invalid_legacy_id",
                        {
                            "expected_prefix": "jrn_",
                            "actual_id": legacy_id,
                        },
                    )
                )
            if not title:
                issues.append(
                    (
                        "missing_title",
                        {"source": ("journal.verbatim_input:first_paragraph")},
                    )
                )
            if len(venue_tags) == 0 or (len(venue_tags) == 1 and not venue_tags[0]):
                issues.append(("missing_venue", {"venue_tag_count": len(venue_tags)}))
            elif len(venue_tags) > 1:
                issues.append(("ambiguous_venue", {"venue_tag_count": len(venue_tags)}))
            if len(phase_tags) == 0 or (len(phase_tags) == 1 and not phase_tags[0]):
                issues.append(("missing_phase", {"phase_tag_count": len(phase_tags)}))
            elif len(phase_tags) > 1:
                issues.append(("ambiguous_phase", {"phase_tag_count": len(phase_tags)}))
            elif phase_tags[0] not in {"draft", "drafting", "review", "final"}:
                issues.append(
                    (
                        "unsupported_phase",
                        {
                            "phase": phase_tags[0],
                            "supported": [
                                "draft",
                                "drafting",
                                "review",
                                "final",
                            ],
                        },
                    )
                )
            if row.get("status") not in {"draft", "active"}:
                issues.append(
                    (
                        "inactive_legacy_status",
                        {"status": row.get("status")},
                    )
                )

            bound = await self.db.fetchone(
                """SELECT id
                   FROM manuscripts
                   WHERE legacy_journal_id = ? AND project_id = ?""",
                [legacy_id, project_id],
            )
            collision = (
                await self.db.fetchone(
                    """SELECT id, legacy_journal_id, project_id
                       FROM manuscripts WHERE id = ?""",
                    [candidate_id],
                )
                if candidate_id is not None
                else None
            )
            if bound is None and collision is not None:
                issues.append(
                    (
                        "deterministic_id_conflict",
                        {
                            "candidate_id": candidate_id,
                            "existing_legacy_journal_id": collision.get("legacy_journal_id"),
                            "existing_project_id": collision.get("project_id"),
                        },
                    )
                )

            for reason, details in issues:
                await self.db.execute(
                    """INSERT OR IGNORE INTO manuscript_migration_issues
                       (legacy_journal_id, project_id,
                        canonical_candidate_id, reason, details)
                       VALUES (?, ?, ?, ?, ?)""",
                    [
                        legacy_id,
                        project_id,
                        candidate_id,
                        reason,
                        json.dumps(details, sort_keys=True),
                    ],
                )

            if bound is not None or issues:
                continue
            phase = "drafting" if phase_tags[0] == "draft" else phase_tags[0]
            await self.db.execute(
                """INSERT INTO manuscripts
                   (id, project_id, title, abstract, venue, phase, state,
                    legacy_journal_id, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)""",
                [
                    candidate_id,
                    project_id,
                    title,
                    abstract,
                    venue_tags[0],
                    phase,
                    legacy_id,
                    row["created_at"],
                    row["updated_at"],
                ],
            )
            created += 1
        return created

    async def _export_rows_for_table(self, table: str, project_id: str) -> list[dict[str, Any]]:
        if table == "entity_topics":
            return await self.db.fetchall(
                """SELECT et.*
                   FROM entity_topics AS et
                   JOIN topics AS t ON t.id = et.topic_id
                   WHERE t.project_id = ?
                   ORDER BY et.topic_id, et.entity_type, et.entity_id""",
                [project_id],
            )
        if table == "qa_logs":
            return await self.db.fetchall(
                """SELECT ql.*
                   FROM qa_logs AS ql
                   JOIN qa_sessions AS qs ON qs.id = ql.session_id
                   WHERE qs.project_id = ?
                   ORDER BY ql.id""",
                [project_id],
            )
        # Safety: verify the table exists and has project_id before querying
        try:
            cols = await self.db.fetchall(f"PRAGMA table_info([{table}])")
        except Exception:
            return []
        col_names = [c["name"] for c in cols]
        if not col_names:
            return []  # table doesn't exist
        if "project_id" not in col_names:
            return []  # table has no project_id — skip silently
        primary_key = [
            column["name"]
            for column in sorted(cols, key=lambda column: int(column["pk"] or 0))
            if int(column["pk"] or 0) > 0
        ]
        if primary_key:
            order_by = ", ".join(f"[{column}]" for column in primary_key)
        elif "id" in col_names:
            order_by = "[id]"
        else:
            order_by = "rowid"
        return await self.db.fetchall(
            f"SELECT * FROM [{table}] WHERE project_id = ? ORDER BY {order_by}",
            [project_id],
        )

    def _attach_artifact_files(
        self,
        rows: list[dict[str, Any]],
        archive: zipfile.ZipFile,
    ) -> list[dict[str, Any]]:
        enriched: list[dict[str, Any]] = []
        for row in rows:
            artifact = dict(row)
            filepath = artifact.get("filepath")
            path = Path(filepath) if isinstance(filepath, str) else None
            if path is None or not path.exists() or not path.is_file():
                raise ValueError(
                    f"Artifact '{artifact.get('id') or '<unknown>'}' cannot be "
                    "exported because its registered file is missing"
                )
            safe_name = self._safe_filename(path.name)
            pack_file = f"artifacts/{artifact['id']}/{safe_name}"
            with path.open("rb") as src, archive.open(pack_file, "w") as dst:
                actual_hash = self._copy_and_hash(src, dst)
            self._assert_artifact_content_hash(
                artifact,
                actual_hash,
                operation="export",
            )
            artifact["pack_file"] = pack_file
            enriched.append(artifact)
        return enriched

    def _load_manifest(self, archive: zipfile.ZipFile) -> dict[str, Any]:
        try:
            raw_manifest = archive.read("manifest.json")
        except KeyError as exc:
            raise ValueError("Knowledge pack is missing manifest.json") from exc
        try:
            manifest = json.loads(raw_manifest.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("Knowledge pack manifest is not valid JSON") from exc
        if not isinstance(manifest, dict):
            raise ValueError("Knowledge pack manifest must be an object")

        pack_format = (
            manifest["pack_format_version"]
            if "pack_format_version" in manifest
            else manifest.get("schema_version")
        )
        if (
            isinstance(pack_format, bool)
            or not isinstance(pack_format, int)
            or pack_format not in range(1, PACK_SCHEMA_VERSION + 1)
        ):
            raise ValueError(f"Unsupported knowledge pack format version: {pack_format}")
        project = manifest.get("project")
        if not isinstance(project, dict) or not project:
            raise ValueError("Knowledge pack manifest is missing project metadata")
        if not isinstance(project.get("id"), str) or not project["id"].strip():
            raise ValueError("Knowledge pack project metadata requires a non-empty ID")
        if manifest.get("project_state") is not None and not isinstance(
            manifest["project_state"],
            dict,
        ):
            raise ValueError("Knowledge pack project_state must be an object or null")
        tables = manifest.get("tables")
        if not isinstance(tables, dict):
            raise ValueError("Knowledge pack manifest tables must be an object")
        for table, rows in tables.items():
            if not isinstance(table, str) or not isinstance(rows, list):
                raise ValueError(
                    "Knowledge pack manifest table payloads must be arrays"
                )
            for index, row in enumerate(rows):
                if not isinstance(row, dict):
                    raise ValueError(
                        f"Knowledge pack {table}[{index}] must be an object"
                    )
        table_counts = manifest.get("table_counts")
        if pack_format == PACK_SCHEMA_VERSION and table_counts is None:
            raise ValueError(
                f"Knowledge pack format {PACK_SCHEMA_VERSION} requires table_counts"
            )
        if table_counts is not None:
            if not isinstance(table_counts, dict):
                raise ValueError("Knowledge pack table_counts must be an object")
            if set(table_counts) != set(tables):
                raise ValueError(
                    "Knowledge pack table_counts keys must match tables exactly"
                )
            for table, expected_count in table_counts.items():
                if (
                    isinstance(expected_count, bool)
                    or not isinstance(expected_count, int)
                    or expected_count < 0
                ):
                    raise ValueError(
                        f"Knowledge pack table_counts[{table!r}] must be a non-negative integer"
                    )
                actual_count = len(tables.get(table, []))
                if actual_count != expected_count:
                    raise ValueError(
                        f"Knowledge pack table count mismatch for {table!r}: "
                        f"manifest={expected_count}, payload={actual_count}"
                    )
        return manifest

    async def _assert_target_project_available(self, project_id: str, project_name: str) -> None:
        existing_id = await self.db.fetchone("SELECT id FROM projects WHERE id = ?", [project_id])
        if existing_id:
            raise ValueError(f"Project '{project_id}' already exists")

        existing_name = await self.db.fetchone(
            "SELECT id FROM projects WHERE name = ?", [project_name]
        )
        if existing_name:
            raise ValueError(
                f"Project name '{project_name}' already exists. Choose a different import name."
            )

    def _assert_no_duplicate_pack_dois(self, tables: dict[str, list[dict[str, Any]]]) -> None:
        dois = [str(row["doi"]).strip() for row in tables.get("literature", []) if row.get("doi")]
        duplicate_dois = sorted(doi for doi, count in Counter(dois).items() if count > 1)
        if duplicate_dois:
            sample = ", ".join(duplicate_dois[:5])
            raise ValueError(f"Knowledge pack contains duplicate literature DOI(s): {sample}")

    @staticmethod
    def _validate_portable_child_links(
        tables: dict[str, list[dict[str, Any]]],
        *,
        project_id: str,
    ) -> None:
        """Fail closed when a child row cannot be scoped and re-keyed safely."""
        topic_ids = {
            str(row["id"])
            for row in tables.get("topics", [])
            if row.get("id") and row.get("project_id") == project_id
        }
        endpoint_ids: dict[str, set[str]] = {}
        for entity_type, table in _ENTITY_LINK_ENDPOINT_TABLES.items():
            endpoint_ids[entity_type] = {
                str(row["id"])
                for row in tables.get(table, [])
                if row.get("id")
                and ("project_id" not in row or row.get("project_id") == project_id)
            }
        endpoint_ids["project"] = {project_id}

        for membership in tables.get("entity_topics", []):
            topic_id = str(membership.get("topic_id") or "")
            entity_type = str(membership.get("entity_type") or "")
            entity_id = str(membership.get("entity_id") or "")
            if topic_id not in topic_ids:
                raise ValueError(
                    f"Knowledge pack entity_topics contains a topic outside project {project_id!r}"
                )
            if entity_type not in endpoint_ids:
                raise ValueError(
                    f"Knowledge pack entity_topics contains unsupported entity type {entity_type!r}"
                )
            if entity_id not in endpoint_ids[entity_type]:
                raise ValueError(
                    "Knowledge pack entity_topics contains an entity that is "
                    "outside the project or absent from this pack: "
                    f"{entity_type}:{entity_id}"
                )

        session_ids = {
            str(row["id"])
            for row in tables.get("qa_sessions", [])
            if row.get("id") and row.get("project_id") == project_id
        }
        for log in tables.get("qa_logs", []):
            if str(log.get("session_id") or "") not in session_ids:
                raise ValueError(
                    "Knowledge pack qa_logs contains a session outside the "
                    f"project or absent from this pack: {log.get('session_id')!r}"
                )

        manuscript_units = {
            str(row["id"]): str(row.get("manuscript_id") or "")
            for row in tables.get("manuscript_units", [])
            if row.get("id") and row.get("project_id") == project_id
        }
        for profile in tables.get("manuscript_unit_outline_profiles", []):
            unit_id = str(profile.get("unit_id") or "")
            manuscript_id = str(profile.get("manuscript_id") or "")
            if unit_id not in manuscript_units:
                raise ValueError(
                    "Knowledge pack outline profile contains a unit outside the "
                    f"project or absent from this pack: {unit_id!r}"
                )
            if manuscript_units[unit_id] != manuscript_id:
                raise ValueError(
                    "Knowledge pack outline profile manuscript does not match its unit: "
                    f"{unit_id!r}"
                )
            parent_id = profile.get("parent_unit_id")
            if parent_id is None:
                continue
            parent_id = str(parent_id)
            if parent_id not in manuscript_units:
                raise ValueError(
                    "Knowledge pack outline profile contains a parent outside the "
                    f"project or absent from this pack: {parent_id!r}"
                )
            if manuscript_units[parent_id] != manuscript_id:
                raise ValueError(
                    "Knowledge pack outline profile parent belongs to a different manuscript: "
                    f"{parent_id!r}"
                )

        artifacts = {
            str(row["id"]): row
            for row in tables.get("artifacts", [])
            if row.get("id") and row.get("project_id") == project_id
        }
        for source in tables.get("registered_sources", []):
            if source.get("project_id") != project_id:
                raise ValueError(
                    "Knowledge pack registered source is outside the exported project: "
                    f"{source.get('id')!r}"
                )
        registered_sources = {
            str(row["id"]): row
            for row in tables.get("registered_sources", [])
            if row.get("id") and row.get("project_id") == project_id
        }
        for source_id, source in registered_sources.items():
            artifact = artifacts.get(str(source.get("artifact_id") or ""))
            if artifact is None:
                raise ValueError(
                    f"Knowledge pack registered source {source_id!r} has no project artifact"
                )
            if source.get("content_hash") != artifact.get("content_hash"):
                raise ValueError(
                    f"Knowledge pack registered source {source_id!r} content hash "
                    "does not match its artifact"
                )
            expected_manifest_hash = KnowledgePackService._registered_source_manifest_hash(
                source
            )
            if source.get("manifest_hash") != expected_manifest_hash:
                raise ValueError(
                    f"Knowledge pack registered source {source_id!r} has an invalid manifest hash"
                )

        candidates = {
            str(row["id"]): row
            for row in tables.get("interpretation_candidates", [])
            if row.get("id") and row.get("project_id") == project_id
        }
        target_ids = {
            "journal": {
                str(row["id"])
                for row in tables.get("journal", [])
                if row.get("id") and row.get("project_id") == project_id
            },
            "claim": {
                str(row["id"])
                for row in tables.get("claims", [])
                if row.get("id") and row.get("project_id") == project_id
            },
            "decision": {
                str(row["id"])
                for row in tables.get("decisions", [])
                if row.get("id") and row.get("project_id") == project_id
            },
        }
        provenance_links = {
            (
                str(row.get("source_type") or ""),
                str(row.get("source_id") or ""),
                str(row.get("target_type") or ""),
                str(row.get("target_id") or ""),
            )
            for row in tables.get("entity_links", [])
            if row.get("project_id") == project_id and row.get("link_type") == "derived_from"
        }
        for admission in tables.get("source_admissions", []):
            admission_id = str(admission.get("id") or "")
            if admission.get("project_id") != project_id:
                raise ValueError(
                    "Knowledge pack source admission is outside the exported project: "
                    f"{admission_id!r}"
                )
            source = registered_sources.get(str(admission.get("source_id") or ""))
            candidate = candidates.get(str(admission.get("candidate_id") or ""))
            target_type = str(admission.get("target_type") or "")
            target_id = str(admission.get("target_id") or "")
            if source is None or candidate is None:
                raise ValueError(
                    f"Knowledge pack source admission {admission_id!r} lacks source/candidate"
                )
            if target_id not in target_ids.get(target_type, set()):
                raise ValueError(
                    f"Knowledge pack source admission {admission_id!r} has no project target"
                )
            if (
                candidate.get("source_type") != "artifact"
                or candidate.get("source_id") != source.get("artifact_id")
                or candidate.get("review_status") != "resolved"
                or candidate.get("disposition") != "promoted"
                or candidate.get("disposition_target_type") != target_type
                or candidate.get("disposition_target_id") != target_id
                or int(candidate.get("revision") or 0)
                != int(admission.get("candidate_revision") or -1)
                or admission.get("source_manifest_hash") != source.get("manifest_hash")
                or int(admission.get("grounding_verified") or 0) != 1
            ):
                raise ValueError(
                    f"Knowledge pack source admission {admission_id!r} is inconsistent"
                )
            if (
                target_type,
                target_id,
                "interpretation_candidate",
                str(candidate.get("id") or ""),
            ) not in provenance_links:
                raise ValueError(
                    f"Knowledge pack source admission {admission_id!r} lacks provenance edge"
                )

    @staticmethod
    def _registered_source_manifest_hash(source: dict[str, Any]) -> str:
        raw_provenance = source.get("provenance") or "{}"
        try:
            provenance = (
                json.loads(raw_provenance)
                if isinstance(raw_provenance, str)
                else raw_provenance
            )
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("registered source provenance is not valid JSON") from exc
        if not isinstance(provenance, dict):
            raise ValueError("registered source provenance must be an object")
        payload = {
            "schema_version": "rka.registered-source/v1",
            "source_kind": source.get("source_kind"),
            "content_mode": source.get("content_mode"),
            "title": source.get("title"),
            "stable_locator": source.get("stable_locator"),
            "content_hash": source.get("content_hash"),
            "ownership_kind": source.get("ownership_kind"),
            "ownership_note": source.get("ownership_note"),
            "provenance": provenance,
        }
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _remap_tables(
        self,
        tables: dict[str, list[dict[str, Any]]],
        *,
        source_project_id: str,
        target_project_id: str,
    ) -> dict[str, list[dict[str, Any]]]:
        id_map = self._build_id_map(tables)
        remapped: dict[str, list[dict[str, Any]]] = {}
        for table in tables:
            remapped[table] = [
                self._remap_row(
                    table,
                    row,
                    id_map=id_map,
                    source_project_id=source_project_id,
                    target_project_id=target_project_id,
                )
                for row in tables.get(table, [])
            ]
        self._refresh_remapped_claim_scope_hashes(tables, remapped)
        return remapped

    @staticmethod
    def _refresh_remapped_claim_scope_hashes(
        source_tables: dict[str, list[dict[str, Any]]],
        remapped_tables: dict[str, list[dict[str, Any]]],
    ) -> None:
        """Keep current scope contracts current when claim prose is re-keyed.

        Entity IDs embedded in claim prose are intentionally rewritten during
        import. Recompute a scope hash only when it matched the source claim;
        an intentionally stale historical scope must remain stale.
        """

        source_claims = {
            str(row["id"]): row
            for row in source_tables.get("claims", [])
            if row.get("id")
        }
        remapped_claims = {
            str(row["id"]): row
            for row in remapped_tables.get("claims", [])
            if row.get("id")
        }
        source_scopes = source_tables.get("claim_scope_versions", [])
        remapped_scopes = remapped_tables.get("claim_scope_versions", [])
        for source_scope, remapped_scope in zip(source_scopes, remapped_scopes, strict=True):
            source_claim = source_claims.get(str(source_scope.get("claim_id") or ""))
            remapped_claim = remapped_claims.get(str(remapped_scope.get("claim_id") or ""))
            if source_claim is None or remapped_claim is None:
                continue
            source_hash = KnowledgePackService._claim_scope_content_hash(source_claim)
            if source_scope.get("claim_content_hash") != source_hash:
                continue
            remapped_scope["claim_content_hash"] = (
                KnowledgePackService._claim_scope_content_hash(remapped_claim)
            )

    @staticmethod
    def _claim_scope_content_hash(claim: dict[str, Any]) -> str:
        material = f"{claim.get('claim_type', '')}\0{claim.get('content', '')}".encode()
        return hashlib.sha256(material).hexdigest()

    def _build_id_map(self, tables: dict[str, list[dict[str, Any]]]) -> dict[str, str]:
        id_map: dict[str, str] = {}
        for table, entity_type in _ID_ENTITY_TYPES.items():
            for row in tables.get(table, []):
                row_id = row.get("id")
                if row_id:
                    id_map[str(row_id)] = generate_id(entity_type)
        for row in tables.get("semantic_patch_provider_events", []):
            call_id = row.get("call_id")
            if call_id and str(call_id) not in id_map:
                id_map[str(call_id)] = generate_id("semantic_patch_provider_call")
        return id_map

    def _remap_row(
        self,
        table: str,
        row: dict[str, Any],
        *,
        id_map: dict[str, str],
        source_project_id: str,
        target_project_id: str,
    ) -> dict[str, Any]:
        remapped = dict(row)
        if "project_id" in remapped:
            remapped["project_id"] = target_project_id
        if table == "qa_logs":
            remapped.pop("project_id", None)

        for column in _DIRECT_ID_COLUMNS.get(table, ()):
            if remapped.get(column):
                remapped[column] = self._rewrite_direct_ref(
                    table=table,
                    column=column,
                    value=remapped[column],
                    id_map=id_map,
                    source_project_id=source_project_id,
                    target_project_id=target_project_id,
                    scope_type=remapped.get("scope_type"),
                    entity_type=remapped.get("entity_type"),
                )

        for column in _JSON_ID_COLUMNS.get(table, ()):
            if remapped.get(column):
                if table == "reference_validation_attestations" and column == "full_json_payload":
                    remapped[column] = self._rewrite_reference_validation_payload(
                        remapped[column],
                        id_map=id_map,
                        source_project_id=source_project_id,
                        target_project_id=target_project_id,
                    )
                else:
                    remapped[column] = self._rewrite_json_refs(
                        remapped[column],
                        id_map=id_map,
                        source_project_id=source_project_id,
                        target_project_id=target_project_id,
                    )

        # Final pass: rewrite entity IDs EMBEDDED IN PROSE (rationale text
        # like "Supersedes dec_…", journal content citing other entries,
        # option descriptions). The structured passes above only handle
        # whole-value columns, so re-keying used to sever every textual
        # reference — provenance rot through export/import (eval-v3,
        # 2026-06-11). IDs absent from id_map are left untouched (they may
        # legitimately reference entities outside the pack).
        for column in _PROSE_TEXT_COLUMNS.get(table, ()):
            value = remapped.get(column)
            if isinstance(value, str) and value:
                remapped[column] = _EMBEDDED_ID_RE.sub(
                    lambda match: (
                        target_project_id
                        if match.group(0) == source_project_id
                        else id_map.get(match.group(0), match.group(0))
                    ),
                    value,
                )

        if table == "manuscript_checkpoints" and remapped.get("dependency_snapshot"):
            try:
                checkpoint_snapshot = json.loads(remapped["dependency_snapshot"])
            except (TypeError, json.JSONDecodeError):
                checkpoint_snapshot = None
            if isinstance(checkpoint_snapshot, dict) and isinstance(
                checkpoint_snapshot.get("components"), dict
            ):
                encoded_components = json.dumps(
                    checkpoint_snapshot["components"],
                    sort_keys=True,
                    separators=(",", ":"),
                    default=str,
                ).encode()
                checkpoint_snapshot["sha256"] = hashlib.sha256(
                    encoded_components
                ).hexdigest()
                remapped["dependency_snapshot"] = json.dumps(checkpoint_snapshot)

        if table == "semantic_patch_context_manifests":
            manifest_payload = {
                "schema_version": "rka.context-manifest/v1",
                "project_id": target_project_id,
                "origin": remapped["origin"],
                "provider": remapped.get("provider"),
                "model": remapped.get("model"),
                "boundary": remapped["boundary"],
                "selected_context": json.loads(remapped["selected_context"]),
                "resolved_context": json.loads(remapped["resolved_context"]),
                "target_bases": json.loads(remapped["target_bases"]),
                "constraints": json.loads(remapped["constraints"]),
                "omissions": json.loads(remapped["omissions"]),
                "truncation_notes": json.loads(remapped["truncation_notes"]),
            }
            remapped["manifest_hash"] = hashlib.sha256(
                json.dumps(
                    manifest_payload,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                ).encode("utf-8")
            ).hexdigest()

        return remapped

    def _rewrite_direct_ref(
        self,
        *,
        table: str,
        column: str,
        value: Any,
        id_map: dict[str, str],
        source_project_id: str,
        target_project_id: str,
        scope_type: str | None = None,
        entity_type: str | None = None,
    ) -> Any:
        if not isinstance(value, str):
            return value

        if table == "exploration_summaries" and column == "scope_id":
            if scope_type == "project" and value == source_project_id:
                return target_project_id
            if scope_type in {
                "mission",
                "decision",
                "journal",
                "literature",
                "checkpoint",
                "summary",
            }:
                return id_map.get(value, value)
            return value

        if table in {"events", "audit_log"} and column == "entity_id":
            if entity_type == "project" and value == source_project_id:
                return target_project_id

        if value == source_project_id:
            return target_project_id
        remapped = id_map.get(value)
        if remapped is not None:
            return remapped
        # Value not in id_map — if this column has a FK constraint,
        # the original ID won't exist in the target DB, so NULL it out.
        if column in _FK_COLUMNS.get(table, set()):
            return None
        return value

    def _rewrite_json_refs(
        self,
        value: Any,
        *,
        id_map: dict[str, str],
        source_project_id: str,
        target_project_id: str,
    ) -> Any:
        if not isinstance(value, str):
            return value
        try:
            payload = json.loads(value)
        except json.JSONDecodeError:
            return value
        rewritten = self._rewrite_nested_refs(payload, id_map, source_project_id, target_project_id)
        return json.dumps(rewritten)

    def _rewrite_reference_validation_payload(
        self,
        value: Any,
        *,
        id_map: dict[str, str],
        source_project_id: str,
        target_project_id: str,
    ) -> Any:
        """Re-key semantic ids and make an excluded worker id provenance-only.

        Only ``full_json_payload.result.job_id`` has the worker-job semantics
        defined by the reference validator. Other keys named ``job_id`` are
        left untouched rather than globally rewriting arbitrary validator
        output.
        """
        if not isinstance(value, str):
            return value
        try:
            payload = json.loads(value)
        except json.JSONDecodeError:
            return value

        source_result = payload.get("result") if isinstance(payload, dict) else None
        preserved_source_job_id = None
        preserved_source_project_id = None
        if isinstance(source_result, dict):
            preserved_source_job_id = source_result.get("source_job_id")
            preserved_source_project_id = source_result.get("source_project_id")

        rewritten = self._rewrite_nested_refs(payload, id_map, source_project_id, target_project_id)
        if isinstance(rewritten, dict):
            result = rewritten.get("result")
            if isinstance(result, dict) and "job_id" in result:
                source_job_id = result.pop("job_id")
                result["source_job_id"] = source_job_id
                result["source_project_id"] = source_project_id
            elif isinstance(result, dict) and preserved_source_job_id is not None:
                # A pack may itself have been imported earlier. Keep the
                # original worker provenance stable across another round trip.
                result["source_job_id"] = preserved_source_job_id
                if preserved_source_project_id is not None:
                    result["source_project_id"] = preserved_source_project_id
        return json.dumps(rewritten)

    def _rewrite_nested_refs(
        self,
        value: Any,
        id_map: dict[str, str],
        source_project_id: str,
        target_project_id: str,
    ) -> Any:
        if isinstance(value, str):
            if value == source_project_id:
                return target_project_id
            direct = id_map.get(value)
            if direct is not None:
                return direct
            # JSON intention fields and provider payloads may contain IDs in
            # explanatory strings rather than as whole scalar values. Rewrite
            # only tokens present in this pack's id_map, preserving all other
            # prose byte-for-byte.
            return _EMBEDDED_ID_RE.sub(
                lambda match: id_map.get(match.group(0), match.group(0)),
                value,
            )
        if isinstance(value, list):
            return [
                self._rewrite_nested_refs(item, id_map, source_project_id, target_project_id)
                for item in value
            ]
        if isinstance(value, dict):
            return {
                key: self._rewrite_nested_refs(item, id_map, source_project_id, target_project_id)
                for key, item in value.items()
            }
        return value

    def _build_project_state_row(
        self,
        source_state: dict[str, Any] | None,
        source_project: dict[str, Any],
        target_project_id: str,
        target_project_name: str,
        target_description: str | None,
    ) -> dict[str, Any]:
        phases = None
        if source_state:
            phases = source_state.get("phases_config")
        if not phases:
            phases = json.dumps(
                [
                    "literature",
                    "planning",
                    "data_collection",
                    "implementation",
                    "evaluation",
                    "paper_writing",
                ]
            )
        return {
            "project_id": target_project_id,
            "project_name": target_project_name,
            "project_description": target_description,
            "current_phase": (source_state or {}).get("current_phase"),
            "phases_config": phases,
            "summary": (source_state or {}).get("summary"),
            "blockers": (source_state or {}).get("blockers"),
            "metrics": (source_state or {}).get("metrics"),
            "created_at": (source_state or {}).get("created_at")
            or source_project.get("created_at")
            or _now(),
            "updated_at": (source_state or {}).get("updated_at")
            or source_project.get("updated_at")
            or _now(),
        }

    def _prepare_rows_for_insert(
        self,
        table: str,
        rows: list[dict[str, Any]],
        target_project_id: str,
    ) -> list[dict[str, Any]]:
        prepared = [self._rewrite_project_scope(table, row, target_project_id) for row in rows]

        dependency_keys = _SELF_REFERENTIAL_TABLES.get(table)
        if dependency_keys:
            keys = [dependency_keys] if isinstance(dependency_keys, str) else list(dependency_keys)
            prepared = self._sort_rows_by_dependencies(prepared, keys)

        if table == "audit_log" or table == "bootstrap_log":
            for row in prepared:
                row.pop("id", None)
        if table == "claims":
            # Scope rows are inserted after claims. Rebuild the monotonic head
            # pointer as each immutable version is imported so migration 041's
            # append triggers remain active during untrusted pack ingestion.
            for row in prepared:
                if "scope_revision" in row:
                    row["scope_revision"] = 0
        if table == "claim_edges":
            # Packs exported before migration 052 may contain repeated
            # cluster memberships. Preserve the first edge, matching the
            # migration's repair rule, so old packs remain importable under
            # the new unique membership invariant.
            seen_memberships: set[tuple[str, str, str, str]] = set()
            unique_rows: list[dict[str, Any]] = []
            for row in prepared:
                if row.get("relation") == "member_of":
                    membership = (
                        str(row.get("project_id") or ""),
                        str(row.get("source_claim_id") or ""),
                        str(row.get("cluster_id") or ""),
                        "member_of",
                    )
                    if membership in seen_memberships:
                        continue
                    seen_memberships.add(membership)
                unique_rows.append(row)
            prepared = unique_rows
        return prepared

    def _rewrite_project_scope(
        self,
        table: str,
        row: dict[str, Any],
        target_project_id: str,
    ) -> dict[str, Any]:
        rewritten = dict(row)
        if "project_id" in rewritten:
            rewritten["project_id"] = target_project_id
        if table == "qa_logs":
            rewritten.pop("project_id", None)
        return rewritten

    def _sort_rows_by_dependency(
        self,
        rows: list[dict[str, Any]],
        dependency_key: str,
    ) -> list[dict[str, Any]]:
        return self._sort_rows_by_dependencies(rows, [dependency_key])

    def _sort_rows_by_dependencies(
        self,
        rows: list[dict[str, Any]],
        dependency_keys: list[str],
    ) -> list[dict[str, Any]]:
        remaining = {str(row["id"]): dict(row) for row in rows if row.get("id")}
        ordered: list[dict[str, Any]] = []
        placed: set[str] = set()

        while remaining:
            progressed = False
            for row_id, row in list(remaining.items()):
                dependencies = {
                    str(row[key])
                    for key in dependency_keys
                    if row.get(key) is not None and str(row[key]) in remaining
                }
                if dependencies.issubset(placed):
                    ordered.append(row)
                    placed.add(row_id)
                    del remaining[row_id]
                    progressed = True
            if not progressed:
                raise ValueError(
                    "Cannot import pack because self-references in "
                    f"{', '.join(dependency_keys)} contain a cycle or unresolved reference"
                )

        return ordered

    def _restore_artifact_files(
        self,
        rows: list[dict[str, Any]],
        archive: zipfile.ZipFile,
        artifact_root: Path,
        *,
        persisted_root: Path | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        restored = 0
        if rows:
            artifact_root.mkdir(parents=True, exist_ok=True)

        resolved_artifact_root = artifact_root.resolve()
        resolved_persisted_root = (
            persisted_root.resolve() if persisted_root is not None else resolved_artifact_root
        )
        prepared: list[dict[str, Any]] = []
        for row in rows:
            artifact = dict(row)
            pack_file = artifact.get("pack_file")
            if not isinstance(pack_file, str) or not pack_file:
                raise ValueError(
                    f"Knowledge pack artifact '{artifact.get('id') or '<unknown>'}' "
                    "has no bundled file"
                )
            safe_filename = self._safe_filename(artifact.get("filename") or "artifact.bin")
            destination = (resolved_artifact_root / artifact["id"] / safe_filename).resolve()
            if not destination.is_relative_to(resolved_artifact_root):
                raise ValueError("Unsafe artifact destination in knowledge pack")
            destination.parent.mkdir(parents=True, exist_ok=True)
            try:
                with archive.open(pack_file) as src, destination.open("wb") as dst:
                    actual_hash = self._copy_and_hash(src, dst)
                self._assert_artifact_content_hash(
                    artifact,
                    actual_hash,
                    operation="import",
                )
                persisted_destination = (
                    resolved_persisted_root / artifact["id"] / safe_filename
                ).resolve()
                if not persisted_destination.is_relative_to(resolved_persisted_root):
                    raise ValueError("Unsafe persisted artifact destination in knowledge pack")
                artifact["filepath"] = str(persisted_destination)
                restored += 1
            except KeyError as exc:
                raise ValueError(
                    f"Knowledge pack is missing bundled artifact file '{pack_file}'"
                ) from exc
            artifact.pop("pack_file", None)
            prepared.append(artifact)
        return prepared, restored

    async def _table_columns(self, table: str) -> frozenset[str]:
        """Return the live column allowlist for a fixed import table."""
        if table not in _IMPORT_INSERT_TABLES:
            raise ValueError(f"Knowledge pack targets unsupported table {table!r}")
        cache: dict[str, frozenset[str]] = getattr(
            self,
            "_import_table_columns_cache",
            {},
        )
        if table in cache:
            return cache[table]
        columns = await self.db.fetchall(f"PRAGMA table_info([{table}])")
        names = frozenset(str(column["name"]) for column in columns)
        if not names:
            raise ValueError(f"Knowledge pack target table {table!r} is unavailable")
        cache[table] = names
        self._import_table_columns_cache = cache
        return names

    async def _validate_import_rows(
        self,
        tables: dict[str, list[dict[str, Any]]],
    ) -> None:
        """Reject unsupported tables and row keys before any import writes."""
        unknown_tables = sorted(set(tables) - set(_INSERT_ORDER))
        if unknown_tables:
            raise ValueError(
                "Knowledge pack contains unsupported table(s): " + ", ".join(unknown_tables)
            )

        for table, rows in tables.items():
            allowed = await self._table_columns(table)
            transient = _IMPORT_TRANSIENT_COLUMNS.get(table, frozenset())
            for index, row in enumerate(rows):
                if not isinstance(row, dict):
                    raise ValueError(f"Knowledge pack {table}[{index}] must be an object")
                unknown_columns = sorted(
                    str(column)
                    for column in row
                    if column not in allowed and column not in transient
                )
                if unknown_columns:
                    raise ValueError(
                        f"Knowledge pack {table}[{index}] contains unsupported "
                        "column(s): " + ", ".join(unknown_columns)
                    )

    async def _insert_row(self, table: str, row: dict[str, Any]) -> None:
        """Insert one already-remapped row using schema-approved identifiers."""
        allowed = await self._table_columns(table)
        columns = list(row.keys())
        unknown_columns = [str(column) for column in columns if column not in allowed]
        if unknown_columns:
            raise ValueError(
                f"Knowledge pack row for {table!r} contains unsupported "
                "column(s): " + ", ".join(sorted(unknown_columns))
            )
        if not columns:
            raise ValueError(f"Knowledge pack row for {table!r} cannot be empty")
        placeholders = ", ".join("?" for _ in columns)
        # Identifiers come only from PRAGMA table_info above. Bracket quoting
        # keeps approved names syntactically isolated from uploaded content.
        column_sql = ", ".join(f"[{column}]" for column in columns)
        await self.db.execute(
            f"INSERT INTO [{table}] ({column_sql}) VALUES ({placeholders})",
            [row[column] for column in columns],
        )

    # entity_type -> (source table, columns _sync_indexes reads)
    _INDEXABLE: dict[str, tuple[str, list[str]]] = {
        "journal": ("journal", ["id", "content", "summary"]),
        "decision": ("decisions", ["id", "question", "rationale"]),
        "literature": ("literature", ["id", "title", "abstract", "notes"]),
        "mission": ("missions", ["id", "objective", "context"]),
        "claim": ("claims", ["id", "content"]),
        "cluster": ("evidence_clusters", ["id", "label", "synthesis"]),
        "artifact": (
            "artifacts",
            ["id", "filename", "filetype", "mime", "metadata"],
        ),
        "figure": ("figures", ["id", "caption", "summary", "claims"]),
    }

    async def count_indexable(self, project_id: str) -> int:
        """How many entities `index_project` will visit."""
        total = 0
        for _etype, (table, _cols) in self._INDEXABLE.items():
            row = await self.db.fetchone(
                f"SELECT COUNT(*) AS n FROM {table} WHERE project_id = ?", [project_id]
            )
            total += int((row or {}).get("n") or 0)
        return total

    async def index_project(
        self,
        project_id: str,
        *,
        status: Any | None = None,
    ) -> int:
        """Strict, keyset-paged lexical repair. Vectors belong to the worker.

        Import calls this inside its graph transaction, so any lexical failure
        rolls back the import. Standalone repair serializes each fresh batch
        with source edits. No manifest, model or unbounded fetch is required.
        """
        indexer = BaseService(self.db, project_id=project_id)
        done = 0
        for etype, (table, cols) in self._INDEXABLE.items():
            last_id = ""
            while True:
                async with self.db.transaction():
                    rows = await self.db.fetchall(
                        f"SELECT {', '.join(cols)} FROM {table} WHERE project_id=? AND id>? ORDER BY id LIMIT 128",
                        [project_id, last_id],
                    )
                    if not rows:
                        break
                    for row in rows:
                        await indexer._sync_fts(etype, row["id"], dict(row), strict=True)
                        done += 1
                    last_id = rows[-1]["id"]
                    if status is not None:
                        status.processed = done
        return done

    async def _sync_imported_indexes(
        self,
        tables: dict[str, list[dict[str, Any]]],
        target_project_id: str,
    ) -> None:
        """Compatibility helper: lexical repair only, never inline inference."""
        await self.index_project(target_project_id)

    @staticmethod
    def _copy_and_hash(src: BinaryIO, dst: BinaryIO) -> str:
        """Stream bytes from ``src`` to ``dst`` and return their SHA-256."""
        digest = hashlib.sha256()
        while chunk := src.read(64 * 1024):
            digest.update(chunk)
            dst.write(chunk)
        return digest.hexdigest()

    @staticmethod
    def _assert_artifact_content_hash(
        artifact: dict[str, Any],
        actual_hash: str,
        *,
        operation: str,
    ) -> None:
        """Fail closed when bundled bytes disagree with artifact metadata."""
        expected_hash = artifact.get("content_hash")
        normalized_expected = (
            expected_hash.removeprefix("sha256:").lower() if isinstance(expected_hash, str) else ""
        )
        if normalized_expected != actual_hash:
            artifact_id = artifact.get("id") or "<unknown>"
            raise ValueError(
                f"Artifact '{artifact_id}' content hash mismatch during knowledge pack {operation}"
            )

    async def _recompute_cluster_claim_counts(self, project_id: str) -> None:
        """Project-scoped recompute of evidence_clusters.claim_count from
        claim_edges.relation='member_of'. Mirrors migration 016 but bound to
        a single project so the import path can repair its own stale-derived
        rows without touching other projects.
        """
        await self.db.execute(
            """UPDATE evidence_clusters
               SET claim_count = (
                   SELECT COUNT(DISTINCT source_claim_id) FROM claim_edges
                   WHERE claim_edges.cluster_id = evidence_clusters.id
                     AND claim_edges.relation = 'member_of'
                     AND claim_edges.project_id = evidence_clusters.project_id
               ),
               updated_at = ?
               WHERE project_id = ?""",
            [_now(), project_id],
        )
        await self.db.commit()

    def _artifact_import_root(self, project_id: str) -> Path:
        self._validate_import_project_id(project_id)
        db_dir = Path(self.db.db_path).resolve().parent
        storage_root = (db_dir / "knowledge-packs").resolve()
        artifact_root = (storage_root / project_id / "artifacts").resolve()
        if not artifact_root.is_relative_to(storage_root):
            raise ValueError("Imported project ID escapes knowledge-pack storage")
        return artifact_root

    @staticmethod
    def _validate_import_project_id(project_id: str) -> None:
        """Require one bounded, filesystem-safe path component for imports."""
        if project_id in {".", ".."} or not _IMPORT_PROJECT_ID_RE.fullmatch(project_id):
            raise ValueError(
                "Imported project ID must use only letters, numbers, '.', "
                "'_', or '-' and cannot contain path separators"
            )

    @staticmethod
    def _slugify(value: str) -> str:
        slug = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-.")
        return slug or "knowledge-pack"

    @staticmethod
    def _safe_filename(value: str) -> str:
        return Path(value).name or "artifact.bin"

    async def _validate_registry(self, project_id: str) -> None:
        """Fail if any table with project_id data exists but isn't categorized."""
        all_tables = await self.db.fetchall("SELECT name FROM sqlite_master WHERE type='table'")
        for row in all_tables:
            table_name = row["name"]
            if table_name.startswith(("vec_", "fts_", "sqlite_")):
                continue
            if table_name in _ALL_REGISTERED:
                continue
            # Check if table has project_id column
            cols = await self.db.fetchall(f"PRAGMA table_info({table_name})")
            col_names = [c["name"] for c in cols]
            if "project_id" not in col_names:
                continue
            # Check if table has data for this project
            count = await self.db.fetchone(
                f"SELECT COUNT(*) as c FROM [{table_name}] WHERE project_id = ?",
                [project_id],
            )
            if count and count["c"] > 0:
                raise ValueError(
                    f"Table '{table_name}' has {count['c']} rows for project {project_id} "
                    f"but is not registered in _TABLE_CATEGORIES. "
                    f"Add it to the appropriate category in knowledge_pack.py before exporting."
                )

    async def _get_schema_version(self) -> int:
        """Get the latest migration number as the schema version."""
        row = await self.db.fetchone(
            """SELECT MAX(CAST(SUBSTR(filename, 1, 3) AS INTEGER)) AS ver
               FROM schema_migrations"""
        )
        return int(row["ver"]) if row and row["ver"] is not None else 0

    # Affordance E (Mission B / mis_01KR209WY4M6WQFEXRH79KC2ZF):
    # severity table mapping integrity-issue category → severity level. The
    # Orphan-class categories are critical (rollback on import); the
    # claim_count_mismatch category is warning (commit + recompute).
    # New categories added later default to "warning" — explicit migration
    # to "critical" is required if a future invariant violation should
    # block imports. Mirrors the philosophy of Mission A T5's hardcoded
    # gate (commit 716712c) but keeps severity explicit on each issue
    # so consumers don't need to know the category list.
    _SEVERITY_BY_CATEGORY: dict[str, str] = {
        "orphaned_entity_link_sources": "critical",
        "orphaned_entity_link_targets": "critical",
        "orphaned_claim_edge_sources": "critical",
        "orphaned_claim_edge_targets": "critical",
        "orphaned_claim_edge_clusters": "critical",
        "claim_scope_pointer_mismatch": "critical",
        "claim_scope_revision_chain_invalid": "critical",
        "claim_scope_disconfirming_refs_invalid": "critical",
        "experiment_plan_head_mismatch": "critical",
        "experiment_plan_chain_invalid": "critical",
        "experiment_run_plan_invalid": "critical",
        "experiment_run_event_head_mismatch": "critical",
        "experiment_observation_parent_invalid": "critical",
        "evidence_locator_source_invalid": "critical",
        "experiment_candidate_source_invalid": "critical",
        "claim_evidence_relation_invalid": "critical",
        "planning_branch_lineage_invalid": "critical",
        "planning_branch_event_head_mismatch": "critical",
        "planning_artifact_head_mismatch": "critical",
        "planning_artifact_chain_invalid": "critical",
        "planning_evidence_binding_invalid": "critical",
        "planning_payload_invalid": "critical",
        "semantic_patch_manifest_hash_invalid": "critical",
        "semantic_patch_proposal_context_invalid": "critical",
        "semantic_patch_proposal_event_head_mismatch": "critical",
        "semantic_patch_provider_event_chain_invalid": "critical",
        "semantic_patch_payload_invalid": "critical",
        "outline_hierarchy_invalid": "critical",
        "registered_source_artifact_invalid": "critical",
        "source_admission_invalid": "critical",
        "journal_attribution_history_invalid": "critical",
        "claim_count_mismatch": "warning",
    }

    @classmethod
    def _severity_for(cls, category: str) -> str:
        """Return severity for the given integrity category. Defaults to
        'warning' for unknown categories so new advisory checks land
        non-blocking by default."""
        return cls._SEVERITY_BY_CATEGORY.get(category, "warning")

    async def _outline_hierarchy_integrity_issues(self, project_id: str) -> list[dict]:
        rows = await self.db.fetchall(
            """SELECT u.manuscript_id, u.id AS unit_id, u.local_key,
                      u.status, u.sequence, p.outline_level,
                      p.parent_unit_id, parent.local_key AS parent_unit_key
               FROM manuscript_units AS u
               LEFT JOIN manuscript_unit_outline_profiles AS p
                 ON p.unit_id = u.id
                AND p.manuscript_id = u.manuscript_id
                AND p.project_id = u.project_id
               LEFT JOIN manuscript_units AS parent
                 ON parent.id = p.parent_unit_id
                AND parent.manuscript_id = p.manuscript_id
                AND parent.project_id = p.project_id
               WHERE u.project_id = ?
               ORDER BY u.manuscript_id, u.sequence, u.local_key, u.id""",
            [project_id],
        )
        by_manuscript: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            parent_key = row.get("parent_unit_key")
            if row.get("parent_unit_id") and parent_key is None:
                # Preserve the declared reference so a database created with
                # foreign-key checks disabled still fails closed here.
                parent_key = f"__missing_parent__:{row['parent_unit_id']}"
            by_manuscript.setdefault(str(row["manuscript_id"]), []).append(
                {
                    "local_key": str(row["local_key"]),
                    "status": str(row.get("status") or "planned"),
                    "sequence": int(row.get("sequence") or 0),
                    "outline_level": int(row.get("outline_level") or 4),
                    "parent_unit_key": parent_key,
                }
            )

        failures: list[dict[str, str]] = []
        for manuscript_id, units in by_manuscript.items():
            try:
                validate_unit_hierarchy(units)
            except ValueError as exc:
                failures.append({"manuscript_id": manuscript_id, "reason": str(exc)})
                if len(failures) >= 50:
                    break
        if not failures:
            return []
        category = "outline_hierarchy_invalid"
        return [
            {
                "category": category,
                "severity": self._severity_for(category),
                "count": len(failures),
                "ids": [item["manuscript_id"] for item in failures[:10]],
                "description": "manuscript outlines that violate the canonical hierarchy",
                "failures": failures[:10],
                "fix_action": (
                    "Repair parent/depth/order relationships through a reviewed spine edit"
                ),
            }
        ]

    async def check_integrity(
        self,
        project_id: str | None = None,
        *,
        verify_managed_files: bool = True,
    ) -> list[dict]:
        """Verify knowledge base integrity — check for orphaned edges, missing refs, count mismatches.

        Each issue dict carries a `severity` field (`critical | warning`) so
        consumers can decide rollback vs warn-and-recompute without knowing
        the category list. Affordance E (Mission B).
        """
        pid = project_id or self.project_id
        issues: list[dict] = []
        issues.extend(await self._outline_hierarchy_integrity_issues(pid))

        invalid_attribution = await self.db.fetchall(
            """SELECT j.id FROM journal AS j
               WHERE j.project_id = ? AND (
                 j.attribution_revision != (
                   SELECT COUNT(*) FROM journal_attribution_revisions AS r
                   WHERE r.project_id = j.project_id AND r.journal_id = j.id
                 ) OR (j.attribution_revision > 0 AND NOT EXISTS (
                   SELECT 1 FROM journal_attribution_revisions AS head
                   WHERE head.project_id = j.project_id AND head.journal_id = j.id
                     AND head.revision = j.attribution_revision
                     AND head.after_source IS j.source
                     AND head.after_verbatim_input IS j.verbatim_input
                     AND head.after_capture_mode IS j.capture_mode
                 ))
               )
               UNION
               SELECT r.journal_id AS id FROM journal_attribution_revisions AS r
               WHERE r.project_id = ? AND r.revision > 1 AND NOT EXISTS (
                 SELECT 1 FROM journal_attribution_revisions AS previous
                 WHERE previous.project_id = r.project_id AND previous.journal_id = r.journal_id
                   AND previous.revision = r.expected_revision
                   AND previous.after_source IS r.before_source
                   AND previous.after_verbatim_input IS r.before_verbatim_input
                   AND previous.after_capture_mode IS r.before_capture_mode
               ) LIMIT 50""",
            [pid, pid],
        )
        if invalid_attribution:
            issues.append({
                "category": "journal_attribution_history_invalid", "severity": "critical",
                "count": len(invalid_attribution),
                "ids": [row["id"] for row in invalid_attribution[:10]],
                "description": "Journal attribution revision chain or current head is inconsistent",
                "fix_action": "Restore attribution and immutable history from a trusted pack",
            })

        source_rows = await self.db.fetchall(
            """SELECT source.*, artifact.content_hash AS artifact_content_hash,
                      artifact.id AS resolved_artifact_id,
                      artifact.filepath AS artifact_filepath
               FROM registered_sources AS source
               LEFT JOIN artifacts AS artifact
                 ON artifact.id = source.artifact_id
                AND artifact.project_id = source.project_id
               WHERE source.project_id = ?
               ORDER BY source.id""",
            [pid],
        )
        bad_sources: list[str] = []
        managed_project_root = (
            self._artifact_import_root(pid).parent if verify_managed_files else None
        )
        for source in source_rows:
            try:
                manifest_hash = self._registered_source_manifest_hash(dict(source))
            except ValueError:
                manifest_hash = ""
            invalid = (
                source.get("resolved_artifact_id") is None
                or source.get("content_hash") != source.get("artifact_content_hash")
                or source.get("manifest_hash") != manifest_hash
            )
            if not invalid and verify_managed_files:
                artifact = {
                    "id": source.get("resolved_artifact_id"),
                    "content_hash": source.get("artifact_content_hash"),
                    "filepath": source.get("artifact_filepath"),
                }
                try:
                    verify_registered_source_artifact(
                        source,
                        artifact,
                        project_root=managed_project_root,
                    )
                except SourceRegistrationError:
                    invalid = True
            if invalid:
                bad_sources.append(str(source["id"]))
        if bad_sources:
            cat = "registered_source_artifact_invalid"
            issues.append(
                {
                    "category": cat,
                    "severity": self._severity_for(cat),
                    "count": len(bad_sources),
                    "ids": bad_sources[:10],
                    "description": (
                        "registered sources whose artifact, content hash, or provenance "
                        "manifest is inconsistent"
                    ),
                    "fix_action": "Restore the exact artifact/provenance envelope from a trusted pack",
                }
            )

        bad_admissions = await self.db.fetchall(
            """SELECT admission.id
               FROM source_admissions AS admission
               LEFT JOIN registered_sources AS source
                 ON source.id = admission.source_id
                AND source.project_id = admission.project_id
               LEFT JOIN interpretation_candidates AS candidate
                 ON candidate.id = admission.candidate_id
                AND candidate.project_id = admission.project_id
               WHERE admission.project_id = ?
                 AND (
                    source.id IS NULL
                    OR candidate.id IS NULL
                    OR candidate.source_type <> 'artifact'
                    OR candidate.source_id <> source.artifact_id
                    OR candidate.review_status <> 'resolved'
                    OR candidate.disposition <> 'promoted'
                    OR candidate.disposition_target_type <> admission.target_type
                    OR candidate.disposition_target_id <> admission.target_id
                    OR candidate.revision <> admission.candidate_revision
                    OR admission.source_manifest_hash <> source.manifest_hash
                    OR admission.grounding_verified <> 1
                    OR NOT (
                        (admission.target_type = 'journal' AND EXISTS (
                            SELECT 1 FROM journal AS target
                            WHERE target.id = admission.target_id
                              AND target.project_id = admission.project_id
                        ))
                        OR (admission.target_type = 'claim' AND EXISTS (
                            SELECT 1 FROM claims AS target
                            WHERE target.id = admission.target_id
                              AND target.project_id = admission.project_id
                        ))
                        OR (admission.target_type = 'decision' AND EXISTS (
                            SELECT 1 FROM decisions AS target
                            WHERE target.id = admission.target_id
                              AND target.project_id = admission.project_id
                        ))
                    )
                    OR NOT EXISTS (
                        SELECT 1 FROM entity_links AS link
                        WHERE link.project_id = admission.project_id
                          AND link.source_type = admission.target_type
                          AND link.source_id = admission.target_id
                          AND link.link_type = 'derived_from'
                          AND link.target_type = 'interpretation_candidate'
                          AND link.target_id = admission.candidate_id
                    )
                 )
               ORDER BY admission.id LIMIT 50""",
            [pid],
        )
        if bad_admissions:
            cat = "source_admission_invalid"
            issues.append(
                {
                    "category": cat,
                    "severity": self._severity_for(cat),
                    "count": len(bad_admissions),
                    "ids": [row["id"] for row in bad_admissions[:10]],
                    "description": (
                        "source admissions inconsistent with their source, candidate, "
                        "canonical target, or provenance edge"
                    ),
                    "fix_action": "Restore the immutable admission graph from a trusted pack",
                }
            )

        source_exists = self._typed_entity_link_endpoint_sql(
            type_column="source_type", id_column="source_id"
        )
        target_exists = self._typed_entity_link_endpoint_sql(
            type_column="target_type", id_column="target_id"
        )

        # 1. entity_links whose declared source type/id does not resolve in the
        # edge's own project.
        orphaned_source = await self.db.fetchall(
            f"""SELECT el.id, el.source_id, el.source_type FROM entity_links el
               WHERE el.project_id = ?
               AND NOT ({source_exists})
               LIMIT 50""",
            [pid],
        )
        if orphaned_source:
            cat = "orphaned_entity_link_sources"
            issues.append(
                {
                    "category": cat,
                    "severity": self._severity_for(cat),
                    "count": len(orphaned_source),
                    "ids": [r["id"] for r in orphaned_source[:10]],
                    "description": (
                        "entity_links whose declared source type/id is missing "
                        "from the edge project"
                    ),
                    "fix_action": "Delete orphaned edges or re-import missing entities",
                }
            )

        # 2. Same invariant for target endpoints.
        orphaned_target = await self.db.fetchall(
            f"""SELECT el.id, el.target_id, el.target_type FROM entity_links el
               WHERE el.project_id = ?
               AND NOT ({target_exists})
               LIMIT 50""",
            [pid],
        )
        if orphaned_target:
            cat = "orphaned_entity_link_targets"
            issues.append(
                {
                    "category": cat,
                    "severity": self._severity_for(cat),
                    "count": len(orphaned_target),
                    "ids": [r["id"] for r in orphaned_target[:10]],
                    "description": (
                        "entity_links whose declared target type/id is missing "
                        "from the edge project"
                    ),
                    "fix_action": "Delete orphaned edges or re-import missing entities",
                }
            )

        # 3. claim_edges with orphaned source_claim_id
        orphaned_claims = await self.db.fetchall(
            """SELECT ce.id FROM claim_edges ce
               WHERE ce.project_id = ?
               AND NOT EXISTS (
                   SELECT 1 FROM claims c
                   WHERE c.id = ce.source_claim_id
                     AND c.project_id = ce.project_id
               )
               LIMIT 50""",
            [pid],
        )
        if orphaned_claims:
            cat = "orphaned_claim_edge_sources"
            issues.append(
                {
                    "category": cat,
                    "severity": self._severity_for(cat),
                    "count": len(orphaned_claims),
                    "ids": [r["id"] for r in orphaned_claims[:10]],
                    "description": "claim_edges referencing non-existent claims",
                    "fix_action": "Delete orphaned claim edges",
                }
            )

        # 4. Non-membership claim edges need a same-project target claim.
        orphaned_targets = await self.db.fetchall(
            """SELECT ce.id FROM claim_edges ce
               WHERE ce.project_id = ?
                 AND ce.relation <> 'member_of'
                 AND (
                     (
                         ce.target_claim_id IS NULL
                         AND (
                             ce.relation <> 'contradicts'
                             OR ce.cluster_id IS NULL
                         )
                     )
                     OR NOT EXISTS (
                         SELECT 1 FROM claims c
                         WHERE c.id = ce.target_claim_id
                           AND c.project_id = ce.project_id
                     )
                 )
               LIMIT 50""",
            [pid],
        )
        if orphaned_targets:
            cat = "orphaned_claim_edge_targets"
            issues.append(
                {
                    "category": cat,
                    "severity": self._severity_for(cat),
                    "count": len(orphaned_targets),
                    "ids": [r["id"] for r in orphaned_targets[:10]],
                    "description": (
                        "non-membership claim_edges whose target claim is missing "
                        "from the edge project"
                    ),
                    "fix_action": "Delete orphaned claim edges or restore target claims",
                }
            )

        # 5. Any declared cluster must be same-project; membership requires one.
        orphaned_clusters = await self.db.fetchall(
            """SELECT ce.id FROM claim_edges ce
               WHERE ce.project_id = ?
                 AND (
                     (ce.relation = 'member_of' AND ce.cluster_id IS NULL)
                     OR (
                         ce.cluster_id IS NOT NULL
                         AND NOT EXISTS (
                         SELECT 1 FROM evidence_clusters ec
                         WHERE ec.id = ce.cluster_id
                           AND ec.project_id = ce.project_id
                         )
                     )
                 )
               LIMIT 50""",
            [pid],
        )
        if orphaned_clusters:
            cat = "orphaned_claim_edge_clusters"
            issues.append(
                {
                    "category": cat,
                    "severity": self._severity_for(cat),
                    "count": len(orphaned_clusters),
                    "ids": [r["id"] for r in orphaned_clusters[:10]],
                    "description": "claim_edges referencing non-existent clusters",
                    "fix_action": "Delete orphaned claim edges or re-create clusters",
                }
            )

        # 6. evidence_clusters.claim_count mismatch
        mismatched = await self.db.fetchall(
            """SELECT ec.id, ec.claim_count,
                      (SELECT COUNT(DISTINCT ce.source_claim_id) FROM claim_edges ce
                       WHERE ce.cluster_id = ec.id
                         AND ce.project_id = ec.project_id
                         AND ce.relation = 'member_of') as actual
               FROM evidence_clusters ec
               WHERE ec.project_id = ?
               AND ec.claim_count IS NOT (
                   SELECT COUNT(DISTINCT ce.source_claim_id) FROM claim_edges ce
                   WHERE ce.cluster_id = ec.id
                     AND ce.project_id = ec.project_id
                     AND ce.relation = 'member_of'
               )
               LIMIT 50""",
            [pid],
        )
        if mismatched:
            cat = "claim_count_mismatch"
            issues.append(
                {
                    "category": cat,
                    "severity": self._severity_for(cat),
                    "count": len(mismatched),
                    "ids": [r["id"] for r in mismatched[:10]],
                    "description": "evidence_clusters with claim_count != actual claim_edges count",
                    "fix_action": "Recompute cluster claim counts from unique memberships",
                }
            )

        # 7. Every claim head points to the latest immutable scope version, or
        # remains zero when no contract exists. This also detects a missing
        # imported history row without relying on a mutable ready flag.
        bad_scope_pointers = await self.db.fetchall(
            """SELECT c.id FROM claims AS c
               WHERE c.project_id = ?
                 AND c.scope_revision <> COALESCE((
                     SELECT MAX(scope.revision)
                     FROM claim_scope_versions AS scope
                     WHERE scope.claim_id = c.id
                       AND scope.project_id = c.project_id
                 ), 0)
               LIMIT 50""",
            [pid],
        )
        if bad_scope_pointers:
            cat = "claim_scope_pointer_mismatch"
            issues.append(
                {
                    "category": cat,
                    "severity": self._severity_for(cat),
                    "count": len(bad_scope_pointers),
                    "ids": [row["id"] for row in bad_scope_pointers[:10]],
                    "description": "claims whose scope head does not match immutable history",
                    "fix_action": "Restore the missing history or reset the claim scope through reviewed repair",
                }
            )

        # 8. Revisions must form one explicit predecessor chain per claim.
        broken_scope_chains = await self.db.fetchall(
            """SELECT scope.id FROM claim_scope_versions AS scope
               WHERE scope.project_id = ?
                 AND (
                     (scope.revision = 1 AND scope.supersedes_scope_id IS NOT NULL)
                     OR (
                         scope.revision > 1
                         AND NOT EXISTS (
                             SELECT 1 FROM claim_scope_versions AS previous
                             WHERE previous.claim_id = scope.claim_id
                               AND previous.project_id = scope.project_id
                               AND previous.revision = scope.revision - 1
                               AND previous.id = scope.supersedes_scope_id
                         )
                     )
                 )
               LIMIT 50""",
            [pid],
        )
        if broken_scope_chains:
            cat = "claim_scope_revision_chain_invalid"
            issues.append(
                {
                    "category": cat,
                    "severity": self._severity_for(cat),
                    "count": len(broken_scope_chains),
                    "ids": [row["id"] for row in broken_scope_chains[:10]],
                    "description": "claim scope versions with a missing or incorrect predecessor",
                    "fix_action": "Restore the immutable predecessor chain",
                }
            )

        # 9. JSON-carried disconfirming references remain same-project and
        # cannot point back to the scoped claim itself.
        bad_disconfirming_refs = await self.db.fetchall(
            """SELECT DISTINCT scope.id
               FROM claim_scope_versions AS scope,
                    json_each(scope.disconfirming_claim_ids) AS reference
               WHERE scope.project_id = ?
                 AND (
                     reference.type <> 'text'
                     OR reference.value = scope.claim_id
                     OR NOT EXISTS (
                         SELECT 1 FROM claims AS target
                         WHERE target.id = reference.value
                           AND target.project_id = scope.project_id
                     )
                 )
               LIMIT 50""",
            [pid],
        )
        if bad_disconfirming_refs:
            cat = "claim_scope_disconfirming_refs_invalid"
            issues.append(
                {
                    "category": cat,
                    "severity": self._severity_for(cat),
                    "count": len(bad_disconfirming_refs),
                    "ids": [row["id"] for row in bad_disconfirming_refs[:10]],
                    "description": "claim scope versions with invalid disconfirming claim references",
                    "fix_action": "Restore same-project canonical references or append a corrected scope version",
                }
            )

        # 10. Stable experiment heads must point to the latest immutable plan.
        bad_plan_heads = await self.db.fetchall(
            """SELECT experiment.id
               FROM experiments AS experiment
               WHERE experiment.project_id = ?
                 AND experiment.current_plan_version <> COALESCE((
                     SELECT MAX(plan.version)
                     FROM experiment_plan_versions AS plan
                     WHERE plan.experiment_id = experiment.id
                       AND plan.project_id = experiment.project_id
                 ), 0)
               LIMIT 50""",
            [pid],
        )
        if bad_plan_heads:
            cat = "experiment_plan_head_mismatch"
            issues.append(
                {
                    "category": cat,
                    "severity": self._severity_for(cat),
                    "count": len(bad_plan_heads),
                    "ids": [row["id"] for row in bad_plan_heads[:10]],
                    "description": "experiments whose current plan head does not match immutable history",
                    "fix_action": "Restore the missing plan history or repair the experiment head",
                }
            )

        # 11. Plan versions form one explicit predecessor chain.
        bad_plan_chains = await self.db.fetchall(
            """SELECT plan.id
               FROM experiment_plan_versions AS plan
               WHERE plan.project_id = ?
                 AND (
                     (plan.version = 1 AND plan.supersedes_plan_id IS NOT NULL)
                     OR (
                         plan.version > 1
                         AND NOT EXISTS (
                             SELECT 1 FROM experiment_plan_versions AS previous
                             WHERE previous.experiment_id = plan.experiment_id
                               AND previous.project_id = plan.project_id
                               AND previous.version = plan.version - 1
                               AND previous.id = plan.supersedes_plan_id
                         )
                     )
                 )
               LIMIT 50""",
            [pid],
        )
        if bad_plan_chains:
            cat = "experiment_plan_chain_invalid"
            issues.append(
                {
                    "category": cat,
                    "severity": self._severity_for(cat),
                    "count": len(bad_plan_chains),
                    "ids": [row["id"] for row in bad_plan_chains[:10]],
                    "description": "experiment plan versions with a missing exact predecessor",
                    "fix_action": "Restore the immutable predecessor chain",
                }
            )

        # 12. Every run binds an exact same-project plan version.
        bad_run_plans = await self.db.fetchall(
            """SELECT run.id
               FROM experiment_runs AS run
               WHERE run.project_id = ?
                 AND NOT EXISTS (
                     SELECT 1 FROM experiment_plan_versions AS plan
                     WHERE plan.experiment_id = run.experiment_id
                       AND plan.project_id = run.project_id
                       AND plan.version = run.plan_version
                 )
               LIMIT 50""",
            [pid],
        )
        if bad_run_plans:
            cat = "experiment_run_plan_invalid"
            issues.append(
                {
                    "category": cat,
                    "severity": self._severity_for(cat),
                    "count": len(bad_run_plans),
                    "ids": [row["id"] for row in bad_run_plans[:10]],
                    "description": "experiment runs without their exact plan version",
                    "fix_action": "Restore the plan version used by each run",
                }
            )

        # 13. Run revision heads and append-only event histories agree.
        bad_run_event_heads = await self.db.fetchall(
            """SELECT run.id
               FROM experiment_runs AS run
               WHERE run.project_id = ?
                 AND run.revision <> COALESCE((
                     SELECT MAX(event.run_revision)
                     FROM experiment_run_events AS event
                     WHERE event.run_id = run.id
                       AND event.project_id = run.project_id
                 ), 0)
               LIMIT 50""",
            [pid],
        )
        if bad_run_event_heads:
            cat = "experiment_run_event_head_mismatch"
            issues.append(
                {
                    "category": cat,
                    "severity": self._severity_for(cat),
                    "count": len(bad_run_event_heads),
                    "ids": [row["id"] for row in bad_run_event_heads[:10]],
                    "description": "experiment runs whose revision differs from event history",
                    "fix_action": "Restore the immutable run transition history",
                }
            )

        # 14. Observations retain a same-project run parent.
        bad_observation_parents = await self.db.fetchall(
            """SELECT observation.id
               FROM experiment_observations AS observation
               WHERE observation.project_id = ?
                 AND NOT EXISTS (
                     SELECT 1 FROM experiment_runs AS run
                     WHERE run.id = observation.run_id
                       AND run.project_id = observation.project_id
                 )
               LIMIT 50""",
            [pid],
        )
        if bad_observation_parents:
            cat = "experiment_observation_parent_invalid"
            issues.append(
                {
                    "category": cat,
                    "severity": self._severity_for(cat),
                    "count": len(bad_observation_parents),
                    "ids": [row["id"] for row in bad_observation_parents[:10]],
                    "description": "experiment observations without a same-project run",
                    "fix_action": "Restore the parent run",
                }
            )

        # 15. Exact locators resolve their observation and pinned artifact/hash.
        bad_locator_sources = await self.db.fetchall(
            """SELECT locator.id
               FROM evidence_locators AS locator
               WHERE locator.project_id = ?
                 AND (
                     NOT EXISTS (
                         SELECT 1 FROM experiment_observations AS observation
                         WHERE observation.id = locator.observation_id
                           AND observation.project_id = locator.project_id
                     )
                     OR (
                         locator.source_kind = 'artifact'
                         AND NOT EXISTS (
                             SELECT 1 FROM artifacts AS artifact
                             WHERE artifact.id = locator.artifact_id
                               AND artifact.project_id = locator.project_id
                               AND lower(artifact.content_hash) = lower(locator.content_hash)
                         )
                     )
                 )
               LIMIT 50""",
            [pid],
        )
        if bad_locator_sources:
            cat = "evidence_locator_source_invalid"
            issues.append(
                {
                    "category": cat,
                    "severity": self._severity_for(cat),
                    "count": len(bad_locator_sources),
                    "ids": [row["id"] for row in bad_locator_sources[:10]],
                    "description": "evidence locators with missing or hash-mismatched sources",
                    "fix_action": "Restore the exact observation/artifact source",
                }
            )

        # 16. Observation-backed interpretation candidates resolve locally.
        bad_experiment_candidates = await self.db.fetchall(
            """SELECT candidate.id
               FROM interpretation_candidates AS candidate
               WHERE candidate.project_id = ?
                 AND candidate.source_type = 'experiment_observation'
                 AND NOT EXISTS (
                     SELECT 1 FROM experiment_observations AS observation
                     WHERE observation.id = candidate.source_id
                       AND observation.project_id = candidate.project_id
                 )
               LIMIT 50""",
            [pid],
        )
        if bad_experiment_candidates:
            cat = "experiment_candidate_source_invalid"
            issues.append(
                {
                    "category": cat,
                    "severity": self._severity_for(cat),
                    "count": len(bad_experiment_candidates),
                    "ids": [row["id"] for row in bad_experiment_candidates[:10]],
                    "description": "experiment interpretation candidates without observations",
                    "fix_action": "Restore the observation source",
                }
            )

        # 17. Reviewed claim relations keep claim, observation, and candidate aligned.
        bad_evidence_relations = await self.db.fetchall(
            """SELECT relation.id
               FROM claim_evidence_relations AS relation
               WHERE relation.project_id = ?
                 AND (
                     NOT EXISTS (
                         SELECT 1 FROM claims AS claim
                         WHERE claim.id = relation.claim_id
                           AND claim.project_id = relation.project_id
                     )
                     OR NOT EXISTS (
                         SELECT 1 FROM experiment_observations AS observation
                         WHERE observation.id = relation.observation_id
                           AND observation.project_id = relation.project_id
                     )
                     OR NOT EXISTS (
                         SELECT 1 FROM interpretation_candidates AS candidate
                         WHERE candidate.id = relation.candidate_id
                           AND candidate.project_id = relation.project_id
                           AND candidate.source_type = 'experiment_observation'
                           AND candidate.source_id = relation.observation_id
                           AND (
                               (relation.status = 'active'
                                AND candidate.review_status = 'resolved'
                                AND candidate.disposition = 'classified_evidence'
                                AND candidate.disposition_target_type = 'claim'
                                AND candidate.disposition_target_id = relation.claim_id)
                               OR (relation.status = 'revoked'
                                   AND candidate.review_status = 'pending')
                           )
                     )
                 )
               LIMIT 50""",
            [pid],
        )
        if bad_evidence_relations:
            cat = "claim_evidence_relation_invalid"
            issues.append(
                {
                    "category": cat,
                    "severity": self._severity_for(cat),
                    "count": len(bad_evidence_relations),
                    "ids": [row["id"] for row in bad_evidence_relations[:10]],
                    "description": "claim evidence relations whose reviewed lineage is inconsistent",
                    "fix_action": "Restore the claim-relative interpretation history",
                }
            )

        # 18. Planning branches preserve one context and pin reachable parent
        # and manuscript revisions. A child must never silently drift when its
        # parent or bound manuscript advances after the fork.
        bad_planning_lineage = await self.db.fetchall(
            """SELECT branch.id
               FROM manuscript_planning_branches AS branch
               LEFT JOIN manuscripts AS manuscript
                 ON manuscript.id = branch.manuscript_id
                AND manuscript.project_id = branch.project_id
               LEFT JOIN manuscript_planning_branches AS parent
                 ON parent.id = branch.parent_branch_id
                AND parent.project_id = branch.project_id
               WHERE branch.project_id = ?
                 AND (
                     (branch.manuscript_id IS NULL
                      AND (branch.context_key <> 'project'
                           OR branch.base_manuscript_revision IS NOT NULL))
                     OR (branch.manuscript_id IS NOT NULL
                         AND (manuscript.id IS NULL
                              OR branch.context_key <> branch.manuscript_id
                              OR branch.base_manuscript_revision IS NULL
                              OR branch.base_manuscript_revision > manuscript.revision))
                     OR (branch.parent_branch_id IS NULL
                         AND branch.parent_branch_revision IS NOT NULL)
                     OR (branch.parent_branch_id IS NOT NULL
                         AND (parent.id IS NULL
                              OR parent.context_key <> branch.context_key
                              OR branch.parent_branch_revision IS NULL
                              OR branch.parent_branch_revision > parent.revision
                              OR branch.parent_branch_id = branch.id))
                 )
               LIMIT 50""",
            [pid],
        )
        if bad_planning_lineage:
            cat = "planning_branch_lineage_invalid"
            issues.append(
                {
                    "category": cat,
                    "severity": self._severity_for(cat),
                    "count": len(bad_planning_lineage),
                    "ids": [row["id"] for row in bad_planning_lineage[:10]],
                    "description": "planning branches with invalid frozen ancestry or manuscript context",
                    "fix_action": "Restore the branch parent/context and its pinned revision",
                }
            )

        # 19. Every mutable branch revision has exactly one immutable event.
        bad_planning_event_heads = await self.db.fetchall(
            """SELECT branch.id
               FROM manuscript_planning_branches AS branch
               WHERE branch.project_id = ?
                 AND (
                     branch.revision <> COALESCE((
                         SELECT MAX(event.branch_revision)
                         FROM manuscript_planning_branch_events AS event
                         WHERE event.branch_id = branch.id
                           AND event.project_id = branch.project_id
                     ), 0)
                     OR branch.revision <> (
                         SELECT COUNT(*)
                         FROM manuscript_planning_branch_events AS event
                         WHERE event.branch_id = branch.id
                           AND event.project_id = branch.project_id
                     )
                 )
               LIMIT 50""",
            [pid],
        )
        if bad_planning_event_heads:
            cat = "planning_branch_event_head_mismatch"
            issues.append(
                {
                    "category": cat,
                    "severity": self._severity_for(cat),
                    "count": len(bad_planning_event_heads),
                    "ids": [row["id"] for row in bad_planning_event_heads[:10]],
                    "description": "planning branch heads disagree with immutable event history",
                    "fix_action": "Restore the complete one-event-per-revision history",
                }
            )

        # 20. Stable artifact heads point to the latest immutable version.
        bad_planning_artifact_heads = await self.db.fetchall(
            """SELECT artifact.id
               FROM manuscript_planning_artifacts AS artifact
               WHERE artifact.project_id = ?
                 AND (
                     artifact.current_version <> COALESCE((
                         SELECT MAX(version.version)
                         FROM manuscript_planning_artifact_versions AS version
                         WHERE version.artifact_id = artifact.id
                           AND version.project_id = artifact.project_id
                     ), 0)
                     OR (artifact.current_version = 0
                         AND artifact.current_version_id IS NOT NULL)
                     OR (artifact.current_version > 0
                         AND NOT EXISTS (
                             SELECT 1
                             FROM manuscript_planning_artifact_versions AS version
                             WHERE version.id = artifact.current_version_id
                               AND version.artifact_id = artifact.id
                               AND version.project_id = artifact.project_id
                               AND version.version = artifact.current_version
                         ))
                 )
               LIMIT 50""",
            [pid],
        )
        if bad_planning_artifact_heads:
            cat = "planning_artifact_head_mismatch"
            issues.append(
                {
                    "category": cat,
                    "severity": self._severity_for(cat),
                    "count": len(bad_planning_artifact_heads),
                    "ids": [row["id"] for row in bad_planning_artifact_heads[:10]],
                    "description": "planning artifact heads disagree with immutable version history",
                    "fix_action": "Restore the missing artifact version or exact head pointer",
                }
            )

        # 21. Version predecessor, branch, and branch-event provenance agree.
        bad_planning_chains = await self.db.fetchall(
            """SELECT version.id
               FROM manuscript_planning_artifact_versions AS version
               JOIN manuscript_planning_artifacts AS artifact
                 ON artifact.id = version.artifact_id
                AND artifact.project_id = version.project_id
               WHERE version.project_id = ?
                 AND (
                     version.branch_id <> artifact.branch_id
                     OR (version.version = 1
                         AND version.supersedes_version_id IS NOT NULL)
                     OR (version.version > 1
                         AND NOT EXISTS (
                             SELECT 1
                             FROM manuscript_planning_artifact_versions AS previous
                             WHERE previous.id = version.supersedes_version_id
                               AND previous.artifact_id = version.artifact_id
                               AND previous.project_id = version.project_id
                               AND previous.version = version.version - 1
                         ))
                     OR NOT EXISTS (
                         SELECT 1
                         FROM manuscript_planning_branch_events AS event
                         WHERE event.branch_id = version.branch_id
                           AND event.project_id = version.project_id
                           AND event.branch_revision = version.branch_revision
                           AND event.action = 'artifact_version_appended'
                           AND json_extract(event.details, '$.artifact_id') = version.artifact_id
                           AND json_extract(event.details, '$.artifact_version_id') = version.id
                     )
                 )
               LIMIT 50""",
            [pid],
        )
        if bad_planning_chains:
            cat = "planning_artifact_chain_invalid"
            issues.append(
                {
                    "category": cat,
                    "severity": self._severity_for(cat),
                    "count": len(bad_planning_chains),
                    "ids": [row["id"] for row in bad_planning_chains[:10]],
                    "description": "planning versions have inconsistent predecessor or branch-event lineage",
                    "fix_action": "Restore the exact immutable version and event chain",
                }
            )

        # 22. Evidence bindings resolve by declared type inside the project.
        binding_clauses = []
        for entity_type, table in _PLANNING_EVIDENCE_ENTITY_TABLES.items():
            binding_clauses.append(
                f"(binding.entity_type = '{entity_type}' "
                f"AND EXISTS (SELECT 1 FROM [{table}] AS endpoint "
                "WHERE endpoint.id = binding.entity_id "
                "AND endpoint.project_id = binding.project_id))"
            )
        bad_planning_bindings = await self.db.fetchall(
            f"""SELECT binding.id
                FROM manuscript_planning_evidence_bindings AS binding
                WHERE binding.project_id = ?
                  AND NOT ({" OR ".join(binding_clauses)})
                LIMIT 50""",
            [pid],
        )
        if bad_planning_bindings:
            cat = "planning_evidence_binding_invalid"
            issues.append(
                {
                    "category": cat,
                    "severity": self._severity_for(cat),
                    "count": len(bad_planning_bindings),
                    "ids": [row["id"] for row in bad_planning_bindings[:10]],
                    "description": "planning evidence bindings do not resolve in their project",
                    "fix_action": "Restore the typed evidence entity or remove the invalid imported binding",
                }
            )

        # 23. JSON validity is insufficient: each stage has a closed payload
        # contract. This Python check makes pack import reject structurally
        # plausible but semantically invalid deliberation state.
        planning_payload_rows = await self.db.fetchall(
            """SELECT version.id, artifact.stage_type, version.payload
               FROM manuscript_planning_artifact_versions AS version
               JOIN manuscript_planning_artifacts AS artifact
                 ON artifact.id = version.artifact_id
                AND artifact.project_id = version.project_id
               WHERE version.project_id = ?
               ORDER BY version.id""",
            [pid],
        )
        bad_planning_payload_ids: list[str] = []
        for row in planning_payload_rows:
            try:
                validate_planning_payload(row["stage_type"], json.loads(row["payload"]))
            except (TypeError, ValueError, json.JSONDecodeError):
                bad_planning_payload_ids.append(row["id"])
        if bad_planning_payload_ids:
            cat = "planning_payload_invalid"
            issues.append(
                {
                    "category": cat,
                    "severity": self._severity_for(cat),
                    "count": len(bad_planning_payload_ids),
                    "ids": bad_planning_payload_ids[:10],
                    "description": "planning artifact payloads violate their closed stage schema",
                    "fix_action": "Restore a payload that validates against the declared planning stage",
                }
            )

        # 24. Context manifests remain exact after export/import re-keying.
        manifest_rows = await self.db.fetchall(
            """SELECT * FROM semantic_patch_context_manifests
               WHERE project_id = ? ORDER BY id""",
            [pid],
        )
        bad_manifest_hashes: list[str] = []
        for row in manifest_rows:
            try:
                payload = {
                    "schema_version": "rka.context-manifest/v1",
                    "project_id": pid,
                    "origin": row["origin"],
                    "provider": row.get("provider"),
                    "model": row.get("model"),
                    "boundary": row["boundary"],
                    "selected_context": json.loads(row["selected_context"]),
                    "resolved_context": json.loads(row["resolved_context"]),
                    "target_bases": json.loads(row["target_bases"]),
                    "constraints": json.loads(row["constraints"]),
                    "omissions": json.loads(row["omissions"]),
                    "truncation_notes": json.loads(row["truncation_notes"]),
                }
                expected_hash = hashlib.sha256(
                    json.dumps(
                        payload,
                        sort_keys=True,
                        separators=(",", ":"),
                        ensure_ascii=False,
                    ).encode("utf-8")
                ).hexdigest()
            except (TypeError, ValueError, json.JSONDecodeError, KeyError):
                expected_hash = ""
            if row.get("manifest_hash") != expected_hash:
                bad_manifest_hashes.append(str(row["id"]))
        if bad_manifest_hashes:
            cat = "semantic_patch_manifest_hash_invalid"
            issues.append(
                {
                    "category": cat,
                    "severity": self._severity_for(cat),
                    "count": len(bad_manifest_hashes),
                    "ids": bad_manifest_hashes[:10],
                    "description": "semantic patch context manifest hashes do not match content",
                    "fix_action": "Restore the exact immutable context manifest",
                }
            )

        # 25. AI proposals bind to a matching provider/model disclosure.
        bad_manifest_context = await self.db.fetchall(
            """SELECT id FROM semantic_patch_context_manifests
               WHERE project_id = ?
                 AND (origin NOT IN ('host_agent', 'lm_studio')
                      OR provider IS NULL OR length(trim(provider)) = 0
                      OR model IS NULL OR length(trim(model)) = 0
                      OR (origin = 'host_agent' AND boundary <> 'host_conversation')
                      OR (origin = 'lm_studio' AND boundary <> 'local_loopback'))
               LIMIT 50""",
            [pid],
        )
        bad_proposal_context = await self.db.fetchall(
            """SELECT proposal.id
               FROM semantic_patch_proposals AS proposal
               LEFT JOIN semantic_patch_context_manifests AS manifest
                 ON manifest.id = proposal.context_manifest_id
                AND manifest.project_id = proposal.project_id
               WHERE proposal.project_id = ?
                 AND (
                     (proposal.origin = 'human' AND proposal.context_manifest_id IS NOT NULL)
                     OR (proposal.origin <> 'human'
                         AND (manifest.id IS NULL
                              OR manifest.origin <> proposal.origin
                              OR manifest.provider <> proposal.provider
                              OR manifest.model <> proposal.model
                              OR manifest.boundary <> proposal.boundary))
                 )
               LIMIT 50""",
            [pid],
        )
        bad_context_ids = [row["id"] for row in bad_manifest_context]
        bad_context_ids.extend(row["id"] for row in bad_proposal_context)
        if bad_context_ids:
            cat = "semantic_patch_proposal_context_invalid"
            issues.append(
                {
                    "category": cat,
                    "severity": self._severity_for(cat),
                    "count": len(bad_context_ids),
                    "ids": bad_context_ids[:10],
                    "description": "AI proposals do not match their immutable provider manifest",
                    "fix_action": "Restore the proposal and its exact provider context",
                }
            )

        # 26. Proposal heads agree with a complete immutable event history.
        bad_proposal_heads = await self.db.fetchall(
            """SELECT proposal.id
               FROM semantic_patch_proposals AS proposal
               WHERE proposal.project_id = ?
                 AND (
                     proposal.revision <> COALESCE((
                         SELECT MAX(event.proposal_revision)
                         FROM semantic_patch_proposal_events AS event
                         WHERE event.proposal_id = proposal.id
                           AND event.project_id = proposal.project_id
                     ), 0)
                     OR proposal.revision <> (
                         SELECT COUNT(*) FROM semantic_patch_proposal_events AS event
                         WHERE event.proposal_id = proposal.id
                           AND event.project_id = proposal.project_id
                     )
                     OR NOT EXISTS (
                         SELECT 1 FROM semantic_patch_proposal_events AS event
                         WHERE event.proposal_id = proposal.id
                           AND event.project_id = proposal.project_id
                           AND event.proposal_revision = proposal.revision
                           AND event.action = proposal.status
                     )
                 )
               LIMIT 50""",
            [pid],
        )
        if bad_proposal_heads:
            cat = "semantic_patch_proposal_event_head_mismatch"
            issues.append(
                {
                    "category": cat,
                    "severity": self._severity_for(cat),
                    "count": len(bad_proposal_heads),
                    "ids": [row["id"] for row in bad_proposal_heads[:10]],
                    "description": "semantic proposal heads disagree with immutable events",
                    "fix_action": "Restore the complete one-event-per-revision history",
                }
            )

        # 27. Provider calls start once, terminate at most once, keep one
        # boundary, and successfully close every persisted AI proposal.
        bad_provider_calls = await self.db.fetchall(
            """SELECT event.call_id
               FROM semantic_patch_provider_events AS event
               WHERE event.project_id = ?
               GROUP BY event.call_id
               HAVING SUM(event.event = 'started') <> 1
                   OR SUM(event.event IN ('succeeded', 'failed')) > 1
                   OR COUNT(DISTINCT event.context_manifest_id) <> 1
                   OR COUNT(DISTINCT event.provider) <> 1
                   OR COUNT(DISTINCT event.model) <> 1
                   OR COUNT(DISTINCT event.boundary) <> 1
               LIMIT 50""",
            [pid],
        )
        bad_provider_boundaries = await self.db.fetchall(
            """SELECT event.id
               FROM semantic_patch_provider_events AS event
               LEFT JOIN semantic_patch_context_manifests AS manifest
                 ON manifest.id = event.context_manifest_id
                AND manifest.project_id = event.project_id
               WHERE event.project_id = ?
                 AND (manifest.id IS NULL
                      OR event.provider <> manifest.provider
                      OR event.model <> manifest.model
                      OR event.boundary <> manifest.boundary)
               LIMIT 50""",
            [pid],
        )
        bad_provider_proposals = await self.db.fetchall(
            """SELECT proposal.id
               FROM semantic_patch_proposals AS proposal
               WHERE proposal.project_id = ? AND proposal.origin <> 'human'
                 AND NOT EXISTS (
                     SELECT 1 FROM semantic_patch_provider_events AS event
                     WHERE event.project_id = proposal.project_id
                       AND event.context_manifest_id = proposal.context_manifest_id
                       AND event.event = 'succeeded'
                       AND json_extract(event.details, '$.proposal_id') = proposal.id
                 )
               LIMIT 50""",
            [pid],
        )
        bad_provider_ids = [row["call_id"] for row in bad_provider_calls]
        bad_provider_ids.extend(row["id"] for row in bad_provider_boundaries)
        bad_provider_ids.extend(row["id"] for row in bad_provider_proposals)
        if bad_provider_ids:
            cat = "semantic_patch_provider_event_chain_invalid"
            issues.append(
                {
                    "category": cat,
                    "severity": self._severity_for(cat),
                    "count": len(bad_provider_ids),
                    "ids": bad_provider_ids[:10],
                    "description": "semantic provider-call history is incomplete or ambiguous",
                    "fix_action": "Restore the exact provider start and terminal event chain",
                }
            )

        # 28. JSON-valid operation arrays must also satisfy the closed semantic
        # proposal model and its per-origin invariants.
        proposal_rows = await self.db.fetchall(
            """SELECT * FROM semantic_patch_proposals
               WHERE project_id = ? ORDER BY id""",
            [pid],
        )
        bad_proposal_payloads: list[str] = []
        for row in proposal_rows:
            try:
                SemanticPatchProposalCreate.model_validate(
                    {
                        "origin": row["origin"],
                        "intent": row["intent"],
                        "reason": row["reason"],
                        "created_by": row["created_by"],
                        "operations": json.loads(row["operations"]),
                        "provider": row.get("provider"),
                        "model": row.get("model"),
                        "boundary": row["boundary"],
                        "context_manifest_id": row.get("context_manifest_id"),
                        "supersedes_proposal_id": row.get("supersedes_proposal_id"),
                    }
                )
            except (TypeError, ValueError, json.JSONDecodeError, KeyError):
                bad_proposal_payloads.append(str(row["id"]))
        if bad_proposal_payloads:
            cat = "semantic_patch_payload_invalid"
            issues.append(
                {
                    "category": cat,
                    "severity": self._severity_for(cat),
                    "count": len(bad_proposal_payloads),
                    "ids": bad_proposal_payloads[:10],
                    "description": "semantic proposal operations violate their closed schema",
                    "fix_action": "Restore a proposal that validates against semantic patch v1",
                }
            )

        issues.extend(await self._index_integrity_issues(pid))

        return issues

    # Index checks are database-wide: an orphan's project is gone, which is
    # precisely why it is an orphan. Vector tables now carry filterable
    # project/type metadata, while FTS tables still rely on source-id reconciliation.
    _INDEX_SOURCES: tuple[tuple[str, tuple[str, ...]], ...] = (
        ("journal", ("vec_journal", "fts_journal")),
        ("decisions", ("vec_decisions", "fts_decisions")),
        ("literature", ("vec_literature", "fts_literature")),
        ("missions", ("vec_missions", "fts_missions")),
        ("claims", ("vec_claims", "fts_claims")),
        ("evidence_clusters", ("fts_clusters",)),
        ("artifacts", ("vec_artifacts",)),
        ("figures", ("vec_artifacts",)),
    )

    async def _index_integrity_issues(self, project_id: str) -> list[dict]:
        """Index rows with no entity, and entities with no project.

        Neither condition was detectable before: `check_integrity` reported
        zero issues on an instance carrying 808 orphaned vector rows, 2913
        orphaned FTS rows, and 35 journal entries whose project had been
        deleted — rows that exist in the database and cannot be reached by
        any product surface.

        Reported, not repaired. `rka admin reindex` rebuilds FTS, an
        embedding backfill rebuilds vectors, and re-homing stranded entities
        is a judgement call about someone's research content, not a sweep.
        """
        issues: list[dict] = []

        orphan_vec, orphan_fts = [], []
        unreadable: list[str] = []
        for source, index_tables in self._INDEX_SOURCES:
            for index_table in index_tables:
                if index_table == "vec_artifacts":
                    if not getattr(self.db, "vec_available", False):
                        unreadable.append(index_table)
                        continue
                    entity_type = "figure" if source == "figures" else "artifact"
                    try:
                        rows = await self.db.fetchall(
                            f"""SELECT id FROM vec_artifacts
                                WHERE entity_type = ?
                                  AND id NOT IN (SELECT id FROM {source})
                                LIMIT 50""",
                            [entity_type],
                        )
                    except Exception:
                        unreadable.append(index_table)
                        continue
                    orphan_vec.extend(r["id"] for r in rows)
                    continue

                # `vec_*` is a virtual table and needs the sqlite-vec module
                # loaded to read. Its `_rowids` shadow is a plain table with
                # the same ids, so the check works whether or not the
                # extension is present — an integrity check that cannot look
                # must not report "no problem".
                target = (
                    f"{index_table}_rowids"
                    if index_table.startswith("vec_")
                    else index_table
                )
                try:
                    rows = await self.db.fetchall(
                        f"""SELECT id FROM {target}
                            WHERE id NOT IN (SELECT id FROM {source})
                            LIMIT 50""",
                    )
                except Exception:
                    # A pre-migration database without this index table.
                    unreadable.append(target)
                    continue
                bucket = orphan_vec if index_table.startswith("vec_") else orphan_fts
                bucket.extend(r["id"] for r in rows)

        if getattr(self.db, "vec_available", False):
            # The E1.2 structural migration preserves source-less vectors with
            # explicit sentinel metadata rather than deleting evidence.
            # Surface those rows even when their shared-table entity_type is
            # also unknown, so preservation does not make them invisible.
            for index_table in (
                "vec_journal",
                "vec_decisions",
                "vec_literature",
                "vec_missions",
                "vec_claims",
                "vec_artifacts",
            ):
                type_clause = (
                    " OR entity_type NOT IN ('artifact', 'figure')"
                    if index_table == "vec_artifacts"
                    else ""
                )
                try:
                    rows = await self.db.fetchall(
                        f"""SELECT id FROM {index_table}
                            WHERE project_id = ?{type_clause}
                            LIMIT 50""",
                        ["__orphan__"],
                    )
                except Exception:
                    if index_table not in unreadable:
                        unreadable.append(index_table)
                    continue
                orphan_vec.extend(r["id"] for r in rows)

        for cat, found, what, fix in (
            ("orphaned_vector_rows", orphan_vec, "vectors", "an embedding backfill"),
            ("orphaned_fts_rows", orphan_fts, "FTS rows", "`rka admin reindex`"),
        ):
            if found:
                found = list(dict.fromkeys(found))
                issues.append({
                    "category": cat,
                    "severity": self._severity_for(cat),
                    "count": len(found),
                    "ids": found[:10],
                    "description": (
                        f"{what} whose entity no longer exists; they occupy "
                        "index storage and indicate incomplete cleanup"
                    ),
                    "fix_action": f"Delete them, then repopulate with {fix}",
                })

        if unreadable:
            cat = "index_check_incomplete"
            issues.append({
                "category": cat,
                "severity": self._severity_for(cat),
                "count": len(unreadable),
                "ids": unreadable[:10],
                "description": (
                    "index tables this check could not read, so their "
                    "contents are unverified rather than known-good"
                ),
                "fix_action": "Apply pending migrations, then re-run",
            })

        stranded: list[str] = []
        for source, _ in self._INDEX_SOURCES:
            try:
                rows = await self.db.fetchall(
                    f"""SELECT id FROM {source}
                        WHERE project_id IS NOT NULL
                          AND project_id NOT IN (SELECT id FROM projects)
                        LIMIT 50""",
                )
            except Exception:
                continue
            stranded.extend(r["id"] for r in rows)

        if stranded:
            cat = "stranded_entities"
            issues.append({
                "category": cat,
                "severity": self._severity_for(cat),
                "count": len(stranded),
                "ids": stranded[:10],
                "description": (
                    "entities whose project row is gone; they exist in the "
                    "database and no API path can read them"
                ),
                "fix_action": (
                    "Re-home them to a live project, or recreate the projects "
                    "row. Do not purge — this is real content"
                ),
            })

        return issues

    @staticmethod
    def _typed_entity_link_endpoint_sql(*, type_column: str, id_column: str) -> str:
        """Return a trusted SQL predicate for a typed, project-local endpoint."""
        clauses = []
        for entity_type, table in _ENTITY_LINK_ENDPOINT_TABLES.items():
            clauses.append(
                f"(el.{type_column} = '{entity_type}' "
                f"AND EXISTS (SELECT 1 FROM [{table}] AS endpoint "
                f"WHERE endpoint.id = el.{id_column} "
                "AND endpoint.project_id = el.project_id))"
            )
        clauses.append(
            f"(el.{type_column} = 'project' "
            f"AND el.{id_column} = el.project_id "
            "AND EXISTS (SELECT 1 FROM projects AS endpoint "
            f"WHERE endpoint.id = el.{id_column}))"
        )
        return " OR ".join(clauses)
