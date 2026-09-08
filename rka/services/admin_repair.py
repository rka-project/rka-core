"""Admin repair operations for v2.7.0.4 supersede-bug fallout.

v2.7.0.4 had a 422 on every `rka_supersede_decision` call (REST adapter
wrapped its body incorrectly; the receiving FastAPI route bound the
whole body to `DecisionCreate(extra='forbid')`). PI cockpit sessions
hit by the bug used an UNSAFE workaround: record a fresh decision +
update the old decision's `status='superseded'` directly. The
canonical atomic supersede flow does 5 things; the workaround only
does the status flip, leaving these gaps:

    1. `decisions.superseded_by` FK on old row — NOT set
    2. `entity_links` row {link_type='supersedes', source=new, target=old}
       — MISSING
    3. `decisions.scope_version` on new row — NOT bumped to old+1
    4. Staleness cascade SKIPPED: claims sourced from journals linked to
       the OLD decision are NOT marked stale=1; evidence_clusters are
       NOT marked needs_reprocessing=1
    5. `decision_superseded` event NOT emitted; review_queue NOT updated

This module surfaces two operations:

    `list_orphan_supersedes(db, project_id)` — discovery (read-only).
        Returns the decisions where `status='superseded' AND
        superseded_by IS NULL` for the given project. PI uses this
        listing to build the `--map old=new` arguments for repair.

    `repair_orphan_supersedes(svc, mapping, *, dry_run, actor)` —
        for each (old, new) pair, replay steps 2-5 above WITHOUT
        creating a new decision row (the new row already exists from
        the workaround). Per-pair atomic — uses `BEGIN`/`COMMIT` so a
        mid-pair failure rolls back that pair without partial state.

Idempotency: every step uses deterministic IDs (sha256-derived) or
`INSERT OR IGNORE`, so a re-run that finds the same orphan pair
already partially-repaired produces an `ALREADY` marker for each
already-satisfied step instead of a duplicate row.

CLI surface: see `rka admin list-orphan-supersedes` and
`rka admin repair-supersedes` in `rka/cli.py`. The CLI is the only
client; this is intentionally NOT exposed via MCP (admin operations
require shell-level intent per the v2.7.0.6 design ratification).
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from typing import Any

from rka.infra.database import Database
from rka.infra.ids import generate_id

logger = logging.getLogger(__name__)


@dataclass
class StepReport:
    """One step of a per-pair repair. `state` is one of:

    - "WOULD"   — dry-run preview; would execute the step
    - "DONE"    — applied successfully
    - "ALREADY" — step's invariant already satisfied (no-op)
    - "SKIPPED" — step skipped because a prerequisite failed
    - "FAILED"  — step raised an exception; pair was rolled back
    """

    name: str
    state: str
    detail: str = ""


@dataclass
class PairReport:
    """Result of repairing one (old, new) pair."""

    old_decision_id: str
    new_decision_id: str
    project_id: str
    steps: list[StepReport] = field(default_factory=list)
    failure_reason: str | None = None

    @property
    def applied(self) -> bool:
        """True if at least one step's state is DONE."""
        return any(s.state == "DONE" for s in self.steps)

    @property
    def rolled_back(self) -> bool:
        return self.failure_reason is not None

    def add(self, name: str, state: str, detail: str = "") -> None:
        self.steps.append(StepReport(name=name, state=state, detail=detail))


def _deterministic_link_id(new_id: str, old_id: str) -> str:
    """sha256-derived ID for the entity_links row so re-runs don't
    duplicate. The CHECK on entity_links is on the natural-key tuple
    (source_type, source_id, link_type, target_type, target_id); the
    id column is a TEXT PRIMARY KEY but `INSERT OR IGNORE` works on
    the tuple constraint, so even a randomly-generated id would be
    idempotent. We use a deterministic id anyway so that the row's
    id field is stable across re-runs — useful for downstream readers
    that pin to the id."""
    h = hashlib.sha256(f"supersedes:{new_id}->{old_id}".encode("utf-8")).hexdigest()
    return f"link_{h[:24]}"


def _deterministic_review_id(old_id: str, new_id: str) -> str:
    """Same idempotency rationale for the review_queue row."""
    h = hashlib.sha256(f"re_distill_review:{old_id}->{new_id}".encode("utf-8")).hexdigest()
    return f"review_{h[:24]}"


async def list_orphan_supersedes(db: Database, project_id: str) -> list[dict[str, Any]]:
    """Return decisions where status='superseded' but superseded_by is
    NULL — the signature of a v2.7.0.4-era cockpit workaround.

    The result rows include `id`, `question`, `phase`, `decided_by`,
    `created_at`, `updated_at`, `scope_version`. PI uses the listing
    to build the `--map old_id=new_id` pairs for `repair-supersedes`.
    The MAPPING is PI-supplied because the orphan row gives no
    automatic pointer to its replacement (the cockpit's workaround
    created the new row in a separate call).
    """
    rows = await db.fetchall(
        """SELECT id, question, phase, decided_by, created_at, updated_at,
                  scope_version, chosen
           FROM decisions
           WHERE project_id = ?
             AND status = 'superseded'
             AND superseded_by IS NULL
           ORDER BY updated_at DESC""",
        [project_id],
    )
    return [dict(r) for r in rows]


async def _validate_pair(
    db: Database,
    project_id: str,
    old_id: str,
    new_id: str,
) -> tuple[bool, str | None, dict | None, dict | None]:
    """Pre-flight checks before mutating. Returns
    `(ok, error_message, old_row, new_row)`.

    Refuses to repair when:
        - old and new are the same decision (a decision cannot supersede
          itself; see below)
        - old or new decision does not exist in the given project
        - old.status != 'superseded' (not actually an orphan)
        - old.superseded_by is already set (the supersede link is
          present; nothing to repair)
        - old.superseded_by points at a DIFFERENT new decision (real
          supersede; refuse to overwrite)
    """
    # A self-link is the one bad pair that every other check would wave
    # through: both rows exist, the status is right, and superseded_by is
    # empty. Applying it sets superseded_by to the row's own id and writes a
    # `supersedes` self-loop, which makes the decision permanently its own
    # replacement — it reads as stale forever, and that is precisely the
    # currency signal this repair exists to restore.
    if old_id == new_id:
        return (
            False,
            f"old and new decision are the same ({old_id}) — "
            f"a decision cannot supersede itself",
            None,
            None,
        )

    old_row = await db.fetchone(
        "SELECT id, status, superseded_by, scope_version, phase, project_id "
        "FROM decisions WHERE id = ? AND project_id = ?",
        [old_id, project_id],
    )
    if old_row is None:
        return False, f"old decision {old_id} not found in project {project_id}", None, None
    if old_row["status"] != "superseded":
        return (
            False,
            f"old decision {old_id} has status={old_row['status']!r}, "
            f"not 'superseded' — not an orphan",
            dict(old_row),
            None,
        )
    if old_row["superseded_by"] is not None and old_row["superseded_by"] != new_id:
        return (
            False,
            f"old decision {old_id} already has superseded_by="
            f"{old_row['superseded_by']!r} pointing at a different decision — "
            f"will not overwrite",
            dict(old_row),
            None,
        )

    new_row = await db.fetchone(
        "SELECT id, status, project_id FROM decisions WHERE id = ? AND project_id = ?",
        [new_id, project_id],
    )
    if new_row is None:
        return (
            False,
            f"new decision {new_id} not found in project {project_id}",
            dict(old_row),
            None,
        )
    return True, None, dict(old_row), dict(new_row)


async def _find_affected_entries(
    db: Database,
    project_id: str,
    old_id: str,
) -> set[str]:
    """Use the same typed discovery as live supersession."""
    from rka.services.lifecycle import find_affected_entries
    return await find_affected_entries(db, project_id, old_id)


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


async def _repair_one_pair(
    db: Database, project_id: str, old_id: str, new_id: str, actor: str, dry_run: bool,
) -> PairReport:
    # Validate and apply in the same snapshot; a parallel live transition cannot
    # change the pair between discovery and mutation.
    async with db.transaction():
        return await _repair_one_pair_locked(db, project_id, old_id, new_id, actor, dry_run)


async def _repair_one_pair_locked(
    db: Database,
    project_id: str,
    old_id: str,
    new_id: str,
    actor: str,
    dry_run: bool,
) -> PairReport:
    """Apply (or preview) the 5-step backfill for one (old, new) pair.

    Step order matches the LIVE `DecisionService.supersede_decision`
    sequence so a re-run produces semantically identical end state:
        1. scope_version bump on new row
        2. superseded_by FK on old row
        3. entity_links row (supersedes: new -> old)
        4. staleness cascade on affected claims + clusters
        5. emit decision_superseded event + review_queue row

    Per-pair atomicity: an apply wraps steps 1-5 in `BEGIN`/`COMMIT`.
    A mid-step exception triggers `ROLLBACK`; the pair's row state is
    fully reverted (no partial scope_bump-without-FK shape). The
    dry-run path executes nothing — it just inspects the current row
    state and reports WOULD/ALREADY markers.
    """
    report = PairReport(
        old_decision_id=old_id, new_decision_id=new_id, project_id=project_id,
    )

    ok, msg, old_row, new_row = await _validate_pair(db, project_id, old_id, new_id)
    if not ok:
        report.failure_reason = msg
        report.add("validate", "FAILED", msg or "")
        return report
    assert old_row is not None and new_row is not None  # type narrowing

    old_scope = old_row.get("scope_version") or 1
    expected_new_scope = old_scope + 1

    cur_new_scope = await db.fetchone(
        "SELECT scope_version FROM decisions WHERE id = ?", [new_id]
    )
    cur_new_scope_v = (cur_new_scope or {}).get("scope_version") or 1
    needs_scope_bump = cur_new_scope_v != expected_new_scope

    needs_fk = old_row.get("superseded_by") != new_id

    existing_link = await db.fetchone(
        """SELECT id FROM entity_links
           WHERE source_type = 'decision' AND source_id = ?
             AND link_type = 'supersedes'
             AND target_type = 'decision' AND target_id = ?
             AND project_id = ?""",
        [new_id, old_id, project_id],
    )
    needs_link = existing_link is None

    affected = await _find_affected_entries(db, project_id, old_id)

    review_id = _deterministic_review_id(old_id, new_id)
    existing_review = await db.fetchone(
        "SELECT id FROM review_queue WHERE id = ?", [review_id]
    )
    needs_review = (existing_review is None) and bool(affected)

    needs_event = needs_fk or needs_scope_bump or needs_link or needs_review

    if dry_run:
        report.add(
            "scope_version_bump",
            "WOULD" if needs_scope_bump else "ALREADY",
            f"new.scope_version: {cur_new_scope_v} -> {expected_new_scope}",
        )
        report.add(
            "superseded_by_fk",
            "WOULD" if needs_fk else "ALREADY",
            f"old.superseded_by: {old_row.get('superseded_by')!r} -> {new_id!r}",
        )
        report.add(
            "supersedes_entity_link",
            "WOULD" if needs_link else "ALREADY",
            f"link_type='supersedes' {new_id} -> {old_id}",
        )
        report.add(
            "staleness_cascade",
            "WOULD" if affected else "ALREADY",
            f"{len(affected)} affected journal entries",
        )
        report.add(
            "review_queue_row",
            "WOULD" if needs_review else "ALREADY",
            f"review_id={review_id}",
        )
        report.add(
            "decision_superseded_event",
            "WOULD" if needs_event else "ALREADY",
            "(idempotency: event only emitted when at least one mutation happens)",
        )
        return report

    # Apply path — per-pair transaction. On exception, ROLLBACK leaves
    # the row state unchanged and the failure is reported via
    # PairReport.failure_reason.
    transaction = db.transaction()
    await transaction.__aenter__()
    try:
        if needs_scope_bump:
            await db.execute(
                "UPDATE decisions SET scope_version = ?, updated_at = ? "
                "WHERE id = ? AND project_id = ?",
                [expected_new_scope, _now_iso(), new_id, project_id],
            )
            report.add(
                "scope_version_bump",
                "DONE",
                f"new.scope_version: {cur_new_scope_v} -> {expected_new_scope}",
            )
        else:
            report.add("scope_version_bump", "ALREADY")

        if needs_fk:
            await db.execute(
                "UPDATE decisions SET superseded_by = ?, updated_at = ? "
                "WHERE id = ? AND project_id = ?",
                [new_id, _now_iso(), old_id, project_id],
            )
            report.add("superseded_by_fk", "DONE", f"old.superseded_by -> {new_id}")
        else:
            report.add("superseded_by_fk", "ALREADY")

        if needs_link:
            await db.execute(
                """INSERT OR IGNORE INTO entity_links
                   (id, source_type, source_id, link_type, target_type, target_id,
                    created_by, project_id)
                   VALUES (?, 'decision', ?, 'supersedes', 'decision', ?, ?, ?)""",
                [_deterministic_link_id(new_id, old_id), new_id, old_id, actor, project_id],
            )
            report.add("supersedes_entity_link", "DONE")
        else:
            report.add("supersedes_entity_link", "ALREADY")

        if affected:
            cascade_now = _now_iso()
            from rka.services.lifecycle import apply_decision_impact
            await apply_decision_impact(db, project_id, old_id, new_id, actor, cascade_now, reflag=False)
            report.add(
                "staleness_cascade", "DONE",
                f"cascaded across {len(affected)} entries",
            )
        else:
            report.add("staleness_cascade", "ALREADY", "no affected entries")

        if needs_review:
            await db.execute(
                """INSERT OR IGNORE INTO review_queue
                   (id, item_type, item_id, flag, context, priority, project_id)
                   VALUES (?, 'decision', ?, 're_distill_review', ?, 60, ?)""",
                [
                    review_id, new_id,
                    json.dumps({
                        "old_decision_id": old_id,
                        "affected_entries": sorted(affected),
                        "repair_source": "rka admin repair-supersedes",
                    }),
                    project_id,
                ],
            )
            report.add("review_queue_row", "DONE", f"id={review_id}")
        else:
            report.add("review_queue_row", "ALREADY")

        # Event emission — only if we actually mutated something this run.
        if needs_event:
            event_id = generate_id("event")
            await db.execute(
                """INSERT INTO events
                   (id, event_type, entity_type, entity_id, actor, summary,
                    details, project_id)
                   VALUES (?, 'decision_superseded', 'decision', ?, ?, ?, ?, ?)""",
                [
                    event_id, old_id, actor,
                    f"Backfilled supersede link {old_id} -> {new_id} "
                    f"(admin repair v2.7.0.6)",
                    json.dumps({
                        "new_decision_id": new_id,
                        "affected_entries": len(affected),
                        "repair_source": "rka admin repair-supersedes",
                    }),
                    project_id,
                ],
            )
            report.add("decision_superseded_event", "DONE", f"event_id={event_id}")
        else:
            report.add(
                "decision_superseded_event", "ALREADY",
                "no mutation this run — event already covered by prior pass",
            )

    except BaseException as exc:  # rollback per-pair on failure or cancellation
        logger.exception("repair-supersedes: pair %s -> %s failed; rolling back", old_id, new_id)
        try:
            await transaction.__aexit__(type(exc), exc, exc.__traceback__)
        except Exception:
            pass
        if not isinstance(exc, Exception):
            raise
        report.failure_reason = f"{type(exc).__name__}: {exc}"
        report.add("transaction", "FAILED", report.failure_reason)
    else:
        await transaction.__aexit__(None, None, None)
    return report


async def repair_orphan_supersedes(
    db: Database,
    project_id: str,
    mapping: dict[str, str],
    *,
    dry_run: bool = True,
    actor: str = "pi",
) -> list[PairReport]:
    """Repair (or preview) the supersede chain for each (old, new) pair
    in `mapping`. Returns one PairReport per pair, in input order."""
    if not mapping:
        return []
    reports: list[PairReport] = []
    for old_id, new_id in mapping.items():
        report = await _repair_one_pair(
            db=db, project_id=project_id, old_id=old_id, new_id=new_id,
            actor=actor, dry_run=dry_run,
        )
        reports.append(report)
    return reports


def render_pair_reports(
    reports: list[PairReport], *, dry_run: bool, json_output: bool,
) -> str:
    """Format PairReports for CLI output. JSON mode emits a parseable
    list-of-dicts; text mode emits a human-readable table-ish layout."""
    if json_output:
        return json.dumps(
            [
                {
                    "old_decision_id": r.old_decision_id,
                    "new_decision_id": r.new_decision_id,
                    "project_id": r.project_id,
                    "applied": r.applied,
                    "rolled_back": r.rolled_back,
                    "failure_reason": r.failure_reason,
                    "steps": [
                        {"name": s.name, "state": s.state, "detail": s.detail}
                        for s in r.steps
                    ],
                }
                for r in reports
            ],
            indent=2,
        )
    lines: list[str] = []
    banner = "DRY RUN (no mutations)" if dry_run else "APPLIED"
    lines.append(f"=== rka admin repair-supersedes — {banner} ===")
    for r in reports:
        lines.append(f"\n[{r.old_decision_id}] -> [{r.new_decision_id}]")
        if r.failure_reason:
            lines.append(f"  !! ROLLED BACK: {r.failure_reason}")
        for s in r.steps:
            marker = {
                "WOULD": "  [.] ",
                "DONE":  "  [+] ",
                "ALREADY": "  [=] ",
                "SKIPPED": "  [/] ",
                "FAILED":  "  [!] ",
            }.get(s.state, "  [?] ")
            line = f"{marker}{s.name}: {s.state}"
            if s.detail:
                line += f"  ({s.detail})"
            lines.append(line)
    if not reports:
        lines.append("(no pairs to repair)")
    return "\n".join(lines)
