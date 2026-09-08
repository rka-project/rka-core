"""ULID-based ID generation with type prefixes."""

from __future__ import annotations

from ulid import ULID

# Prefix mapping: entity type → 3-char prefix
_PREFIXES = {
    "decision": "dec",
    "literature": "lit",
    "journal": "jrn",
    "journal_attribution_revision": "jar",
    "mission": "mis",
    "checkpoint": "chk",
    "event": "evt",
    "project": "prj",
    "scan": "scn",
    "link": "lnk",
    "artifact": "art",
    "registered_source": "src",
    "source_admission": "sad",
    "figure": "fig",
    "summary": "sum",
    "qa_session": "qas",
    "qa_log": "qal",
    "keynode": "knd",
    "graphview": "gvw",
    "claim": "clm",
    "claim_scope": "csc",
    "cluster": "ecl",
    "claim_edge": "ced",
    "interpretation_candidate": "icd",
    "interpretation_hint": "ich",
    "interpretation_review": "icv",
    "interpretation_promotion": "ipm",
    "experiment": "exp",
    "experiment_plan_version": "epv",
    "experiment_run": "run",
    "experiment_run_event": "rue",
    "experiment_observation": "obs",
    "evidence_locator": "elc",
    "claim_evidence_relation": "evr",
    "decision_option": "dop",
    "calibration_outcome": "cao",
    "hook": "hk",
    "hook_execution": "hkx",
    "brain_notification": "bnt",
    "topic": "top",
    "entity_topic": "etp",
    "context_snapshot": "ctx",
    "review": "rev",
    "reference_validation": "rvd",
    "manuscript": "man",
    "manuscript_claim": "mcl",
    "manuscript_claim_ratification": "mra",
    "manuscript_unit": "mun",
    "manuscript_checkpoint": "mck",
    "manuscript_verification": "mva",
    "manuscript_reference": "mrf",
    "manuscript_unit_citation": "muc",
    "manuscript_planning_branch": "mpb",
    "manuscript_planning_branch_event": "pbe",
    "manuscript_planning_artifact": "pla",
    "manuscript_planning_artifact_version": "plv",
    "manuscript_planning_evidence_binding": "plb",
    "manuscript_planning_promotion_event": "ppe",
    "manuscript_evaluation_event": "eva",
    "semantic_patch_context_manifest": "pcm",
    "semantic_patch_proposal": "spp",
    "semantic_patch_proposal_event": "spe",
    "semantic_patch_provider_call": "spc",
    "semantic_patch_provider_event": "pce",
    "manuscript_source_proposal": "msp",
    "manuscript_source_event": "mse",
}


def generate_id(entity_type: str) -> str:
    """Generate a prefixed ULID for the given entity type.

    Format: {prefix}_{ulid}
    Example: dec_01HXYZ...

    ULIDs are sortable by creation time and globally unique.
    """
    prefix = _PREFIXES.get(entity_type, entity_type[:3])
    return f"{prefix}_{ULID()}"
