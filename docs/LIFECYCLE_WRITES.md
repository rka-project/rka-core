# Scoped lifecycle writes

Mission parent and dependency chains are same-project, acyclic relationships.
Review queue targets and calibration decisions must belong to the selected
project. Missing or foreign targets are rejected before any side effect.
Actor labels remain declared provenance, not authentication or authorization.

A checkpoint can be resolved once. The resolution, optional new decision,
events and audit are one transaction. An exact retry returns the original
resolution and linked decision. A conflicting second resolution is rejected;
it does not create an orphan decision. The decision service must share both the
checkpoint database and project.

A mission accepts its first report in pending, active, partial or complete
state (the historical first-submission workflow is preserved). Blocked and
cancelled missions reject completion reports. An exact report retry with the
same actor returns the existing report without duplicating findings. A different
report or an attempt to reopen a reported mission is rejected. This batch does
not introduce report revisioning: create a follow-up mission for new work.

Review resolution requires a reason. Pending items can be acknowledged; resolved
or dismissed items allow exact retries only. Freshness resolution closes only
the matching stale/re-distillation alerts, not unrelated contradiction alerts.
Reflagging opens a new freshness review without rewriting old queue receipts.

General decision update cannot resurrect a superseded record. Replacement goes
through the atomic supersede operation described in
[directive dependencies](DIRECTIVE_DEPENDENCIES.md). Historical records remain
readable; none of these operations authorizes production cleanup or migration.
