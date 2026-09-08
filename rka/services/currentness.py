"""Shared lightweight currency projections and review SQL (trusted constants)."""

from datetime import datetime, timezone

from rka.services.entity_resolver import _currentness

RESOLUTION_FIELDS = (
    "staleness_reviewed_at",
    "staleness_verdict",
    "staleness_resolution",
    "staleness_resolution_journal_id",
    "staleness_resolved_by",
)
CURRENCY_COLUMNS = {
    "decision": ("status", "superseded_by"),
    "journal": ("status", "confidence", "superseded_by"),
    "claim": ("stale", "staleness", "staleness_verdict", "valid_from", "valid_until"),
    "cluster": ("needs_reprocessing", "staleness", "staleness_verdict", "synthesis_valid_until"),
    "mission": ("status",),
    "literature": ("status",),
    "checkpoint": ("status",),
    "review": ("status",),
}


def currentness(record):
    return _currentness(record, as_of=datetime.now(timezone.utc))


def review_projection(record):
    return {
        **{field: record.get(field) for field in RESOLUTION_FIELDS},
        "staleness": record.get("staleness", "green"),
        "stale_reason": record.get("stale_reason"),
        "currentness": currentness(record),
    }


def pending_review_sql(entity_type, alias=""):
    """Only internal callers provide the fixed entity type/SQL alias."""
    prefix = f"{alias}." if alias else ""
    hard = "stale" if entity_type == "claim" else "needs_reprocessing"
    return (
        f"{prefix}staleness_reviewed_at IS NULL AND "
        f"({prefix}staleness IN ('yellow', 'red') OR {prefix}{hard} = 1)"
    )
