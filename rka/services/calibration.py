"""CalibrationService — records decision outcomes and computes Brier + ECE.

Scope per Mission 1B-iii (mis_01KPF6B0CTZ7D6QT8K5CXDDEB3):
- ``record`` / ``list_for_decision`` / ``list_all`` — CRUD over the
  ``calibration_outcomes`` table (migration 018).
- ``compute_metrics`` — on-demand Brier + ECE over decisions that have both
  a forecast (recommended_option_id → confidence_numeric) AND at least one
  recorded outcome.

Brier + ECE are implemented as pure-function helpers at module top so unit
tests can verify them without the DB in the loop.
"""

from __future__ import annotations

from rka.infra.ids import generate_id
from rka.models.calibration import (
    CalibrationBin,
    CalibrationMetrics,
    CalibrationOutcome,
    CalibrationOutcomeCreate,
)
from rka.services.base import BaseService


# Outcome → numeric mapping for Brier / ECE computation. "unresolved" is
# excluded entirely (no ground truth yet). "mixed" is treated as 0.5 — a
# half-success — per the mission spec. If the Brain later rules that mixed
# should be excluded entirely, flip the OUTCOME_SCORES mapping.
OUTCOME_SCORES: dict[str, float] = {
    "succeeded": 1.0,
    "failed": 0.0,
    "mixed": 0.5,
}

MIN_SAMPLES_FOR_METRICS = 5
ECE_NUM_BINS = 10


# ============================================================
# Pure computation helpers (no DB; unit-testable in isolation)
# ============================================================


def brier_score(forecasts: list[float], outcomes: list[float]) -> float:
    """Brier score = mean((f - o)²).

    Both lists must be same length. Raises ValueError on mismatch or empty
    input.
    """
    if len(forecasts) != len(outcomes):
        raise ValueError(
            f"brier_score: forecasts ({len(forecasts)}) and outcomes ({len(outcomes)}) length mismatch"
        )
    if not forecasts:
        raise ValueError("brier_score: empty input")
    squared_errors = [(f - o) ** 2 for f, o in zip(forecasts, outcomes)]
    return sum(squared_errors) / len(squared_errors)


def ece(
    forecasts: list[float],
    outcomes: list[float],
    num_bins: int = ECE_NUM_BINS,
) -> tuple[float, list[CalibrationBin]]:
    """Expected Calibration Error with ``num_bins`` equal-width bins on [0, 1].

    Returns ``(ece_value, bin_breakdown)``. The breakdown lists only
    populated bins (empty bins excluded from both the ECE sum and the
    returned list).

    Interpretation: ``accuracy(bin)`` = mean outcome value in the bin. When
    outcomes are strictly {0.0, 1.0}, this is the standard success rate.
    With ``mixed=0.5`` present, accuracy becomes a weighted success rate —
    i.e. a mixed outcome counts as "half successful" for calibration. See
    Mission 1B-iii Backbrief for the interpretation rationale.
    """
    if len(forecasts) != len(outcomes):
        raise ValueError(
            f"ece: forecasts ({len(forecasts)}) and outcomes ({len(outcomes)}) length mismatch"
        )
    if not forecasts:
        raise ValueError("ece: empty input")

    n = len(forecasts)
    # Bin edges: [0, 0.1), [0.1, 0.2), …, [0.9, 1.0] (last bin is inclusive on the upper edge).
    bin_breakdown: list[CalibrationBin] = []
    ece_sum = 0.0
    for b in range(num_bins):
        lo = b / num_bins
        hi = (b + 1) / num_bins
        # Inclusive upper edge for the last bin so forecast=1.0 lands there.
        if b == num_bins - 1:
            in_bin = [i for i, f in enumerate(forecasts) if lo <= f <= hi]
        else:
            in_bin = [i for i, f in enumerate(forecasts) if lo <= f < hi]
        if not in_bin:
            continue
        bin_n = len(in_bin)
        bin_accuracy = sum(outcomes[i] for i in in_bin) / bin_n
        bin_confidence = sum(forecasts[i] for i in in_bin) / bin_n
        gap = abs(bin_accuracy - bin_confidence)
        ece_sum += (bin_n / n) * gap
        bin_breakdown.append(CalibrationBin(
            bin_range=(lo, hi),
            n=bin_n,
            accuracy=bin_accuracy,
            mean_confidence=bin_confidence,
        ))
    return ece_sum, bin_breakdown


# ============================================================
# CalibrationService
# ============================================================


class CalibrationService(BaseService):
    """Records calibration outcomes and computes project-level Brier/ECE."""

    def _row_to_model(self, row: dict) -> CalibrationOutcome:
        return CalibrationOutcome(
            id=row["id"],
            decision_id=row["decision_id"],
            project_id=row["project_id"],
            outcome=row["outcome"],
            outcome_details=row["outcome_details"],
            recorded_at=row["recorded_at"],
            recorded_by=row["recorded_by"],
        )

    async def record(
        self, decision_id: str, data: CalibrationOutcomeCreate,
    ) -> CalibrationOutcome:
        async with self.db.transaction():
            await self._require_link_entity("decision", decision_id, project_id=self.project_id)
            return await self._record_scoped(decision_id, data)

    async def _record_scoped(
        self,
        decision_id: str,
        data: CalibrationOutcomeCreate,
    ) -> CalibrationOutcome:
        """Insert a calibration_outcomes row for ``decision_id`` and return it.

        The caller is responsible for verifying the decision has a recorded
        PI selection (selected_option_id or override_rationale) — see
        ``rka_record_outcome`` in the MCP layer for the refusal rule.

        Fires ``post_record_outcome`` hook after commit (Mission 2). Payload
        carries the new outcome plus a flattened snapshot of CalibrationMetrics
        so brain_notify hooks can interpolate ``{override_rate}``,
        ``{brier_score}``, etc. directly without dotted-path traversal.
        """
        outcome_id = generate_id("calibration_outcome")
        await self.db.execute(
            """INSERT INTO calibration_outcomes
               (id, decision_id, project_id, outcome, outcome_details, recorded_by)
               VALUES (?, ?, ?, ?, ?, ?)""",
            [
                outcome_id,
                decision_id,
                self.project_id,
                data.outcome,
                data.outcome_details,
                data.recorded_by,
            ],
        )
        await self.db.commit()
        row = await self.db.fetchone(
            "SELECT * FROM calibration_outcomes WHERE id = ?",
            [outcome_id],
        )
        result = self._row_to_model(row)

        # Fire post_record_outcome hook (Mission 2). Flatten current
        # CalibrationMetrics into the payload so brain_notify templates can
        # reference {override_rate}, {brier_score}, etc. without dotted paths.
        # All hook firing failures are silent.
        try:
            metrics = await self.compute_metrics()
            from rka.services.hook_dispatcher import HookDispatcher
            await HookDispatcher(self.db).fire(
                event="post_record_outcome",
                payload={
                    "decision_id": decision_id,
                    "outcome": data.outcome,
                    # Flattened metrics_after — both metric families.
                    "brier_score": metrics.brier_score,
                    "ece": metrics.ece,
                    "n_outcomes": metrics.n,
                    "metrics_available": metrics.metrics_available,
                    "override_rate": metrics.override_rate,
                    "escape_hatch_rate": metrics.escape_hatch_rate,
                    "near_miss_rate": metrics.near_miss_rate,
                    "qualifying_decisions": metrics.qualifying_decisions,
                    "override_metrics_available": metrics.override_metrics_available,
                },
                project_id=self.project_id,
            )
        except Exception:
            # logger import is inside the try because BaseService doesn't expose one;
            # we never want hook firing to break outcome recording.
            import logging
            logging.getLogger(__name__).warning(
                "post_record_outcome hook fire failed", exc_info=True,
            )

        return result

    async def list_for_decision(self, decision_id: str) -> list[CalibrationOutcome]:
        """All outcomes for a single decision, most recent first."""
        rows = await self.db.fetchall(
            """SELECT * FROM calibration_outcomes
               WHERE decision_id = ? AND project_id = ?
               ORDER BY recorded_at DESC""",
            [decision_id, self.project_id],
        )
        return [self._row_to_model(r) for r in rows]

    async def list_all(
        self,
        since: str | None = None,
        outcome_filter: str | None = None,
    ) -> list[CalibrationOutcome]:
        """All outcomes across the service's project, filterable by time/outcome."""
        clauses = ["project_id = ?"]
        params: list = [self.project_id]
        if since:
            clauses.append("recorded_at >= ?")
            params.append(since)
        if outcome_filter:
            clauses.append("outcome = ?")
            params.append(outcome_filter)
        where = " AND ".join(clauses)
        rows = await self.db.fetchall(
            f"SELECT * FROM calibration_outcomes WHERE {where} ORDER BY recorded_at DESC",
            params,
        )
        return [self._row_to_model(r) for r in rows]

    async def _compute_override_metrics(self) -> dict[str, float | int | None]:
        """Compute the three selection-pattern rates in a single SQL pass.

        Per Mission 2 spec (jrn_01KPJXXYB7T8FJTXHB17J3BRC3):

        - ``override_rate`` = fraction of qualifying decisions where the PI
          did NOT select exactly the Brain's recommended option (includes
          escape-hatch cases where ``pi_selected_option_id`` IS NULL).
        - ``escape_hatch_rate`` = fraction where ``pi_override_rationale``
          is populated.
        - ``near_miss_rate`` = fraction where the PI selected a survivor
          option (dominated_by IS NULL) that is not the recommended one —
          a signal of engaged disagreement rather than rubber-stamping.

        Qualifying denominator = decisions with ``recommended_option_id IS
        NOT NULL`` (v2.2 multi-choice UX used) AND any PI resolution
        recorded (``pi_selected_option_id`` or ``pi_override_rationale``).
        Outcome status is NOT a qualifying filter.

        Uses ``IS DISTINCT FROM`` for NULL-safe comparison so escape-hatch
        cases (where ``pi_selected_option_id`` is NULL) correctly count as
        "selected != recommended". SQLite ≥3.39 supports IS DISTINCT FROM;
        runtime container ships 3.46.1.

        Returns a dict with the four scalar values. Rates are None when
        qualifying_decisions is zero.
        """
        row = await self.db.fetchone(
            """
            WITH q AS (
                SELECT
                    d.id,
                    d.recommended_option_id,
                    d.pi_selected_option_id,
                    d.pi_override_rationale
                FROM decisions d
                WHERE d.recommended_option_id IS NOT NULL
                  AND (d.pi_selected_option_id IS NOT NULL
                       OR d.pi_override_rationale IS NOT NULL)
                  AND d.project_id = ?
            )
            SELECT
                COUNT(*) AS qualifying_decisions,
                AVG(CASE WHEN pi_selected_option_id IS DISTINCT FROM recommended_option_id
                         THEN 1.0 ELSE 0.0 END) AS override_rate,
                AVG(CASE WHEN pi_override_rationale IS NOT NULL
                         THEN 1.0 ELSE 0.0 END) AS escape_hatch_rate,
                AVG(CASE WHEN pi_selected_option_id IS NOT NULL
                         AND pi_selected_option_id IS DISTINCT FROM recommended_option_id
                         AND EXISTS (
                             SELECT 1 FROM decision_options o
                             WHERE o.id = q.pi_selected_option_id
                               AND o.dominated_by IS NULL
                         )
                         THEN 1.0 ELSE 0.0 END) AS near_miss_rate
            FROM q
            """,
            [self.project_id],
        )
        qualifying = int(row["qualifying_decisions"] or 0) if row else 0
        if qualifying == 0:
            return {
                "qualifying_decisions": 0,
                "override_rate": None,
                "escape_hatch_rate": None,
                "near_miss_rate": None,
            }
        return {
            "qualifying_decisions": qualifying,
            "override_rate": float(row["override_rate"]),
            "escape_hatch_rate": float(row["escape_hatch_rate"]),
            "near_miss_rate": float(row["near_miss_rate"]),
        }

    async def compute_metrics(self) -> CalibrationMetrics:
        """Compute Brier + ECE across the project's eligible decisions.

        Eligibility: decision has (a) a ``recommended_option_id`` with a
        ``confidence_numeric`` AND (b) at least one non-``unresolved``
        outcome. The most recent outcome per decision is used.

        Returns a CalibrationMetrics row. When N<5, metrics_available=False
        and a warning explains.
        """
        # Join decisions → decision_options (recommended) → latest outcome.
        rows = await self.db.fetchall(
            """SELECT d.id AS decision_id,
                      dop.confidence_numeric AS confidence,
                      (SELECT outcome FROM calibration_outcomes co
                       WHERE co.decision_id = d.id AND co.project_id = d.project_id
                       ORDER BY co.recorded_at DESC LIMIT 1) AS latest_outcome
               FROM decisions d
               JOIN decision_options dop ON dop.id = d.recommended_option_id
               WHERE d.project_id = ?
                 AND d.recommended_option_id IS NOT NULL""",
            [self.project_id],
        )
        forecasts: list[float] = []
        outcomes: list[float] = []
        covered: list[str] = []
        for row in rows:
            oc = row["latest_outcome"]
            if oc is None or oc == "unresolved":
                continue
            score = OUTCOME_SCORES.get(oc)
            if score is None:
                # Guard against schema drift — shouldn't happen with CHECK in place.
                continue
            forecasts.append(float(row["confidence"]))
            outcomes.append(score)
            covered.append(row["decision_id"])

        n = len(forecasts)

        # Selection-pattern metrics run on a different denominator — decisions
        # with any PI resolution, regardless of outcome. Always computed.
        override = await self._compute_override_metrics()
        override_available = override["qualifying_decisions"] >= MIN_SAMPLES_FOR_METRICS

        if n < MIN_SAMPLES_FOR_METRICS:
            return CalibrationMetrics(
                n=n,
                metrics_available=False,
                warning=(
                    f"Need \u2265{MIN_SAMPLES_FOR_METRICS} outcomes for meaningful "
                    f"calibration; current N={n}"
                ),
                decisions_covered=covered,
                qualifying_decisions=override["qualifying_decisions"],
                override_metrics_available=override_available,
                override_rate=override["override_rate"],
                escape_hatch_rate=override["escape_hatch_rate"],
                near_miss_rate=override["near_miss_rate"],
            )

        brier = brier_score(forecasts, outcomes)
        ece_value, breakdown = ece(forecasts, outcomes)
        return CalibrationMetrics(
            n=n,
            metrics_available=True,
            brier_score=brier,
            ece=ece_value,
            bin_breakdown=breakdown,
            decisions_covered=covered,
            qualifying_decisions=override["qualifying_decisions"],
            override_metrics_available=override_available,
            override_rate=override["override_rate"],
            escape_hatch_rate=override["escape_hatch_rate"],
            near_miss_rate=override["near_miss_rate"],
        )
