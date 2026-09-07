# E1b: durable Core backfill ownership

- Baseline: `ba7f5ea`; isolated `codex/core-audit-hardening` worktree.
- Parent: [approved hardening design](2026-09-05-core-hardening-design.md).
- This is the concrete implementation contract, not a release claim.

## Storage and authority

Reuse migration 008/037 `jobs`, not a second scheduler or new research table.
`job_type=embedding_backfill`, system scope `proj_default`, dedupe key includes
generation. Payload v1 holds only generation, space signature, model identity,
dimension and the validated seven-entity subset. No provider credentials or
research text are copied into the intent payload. `result` stores bounded attempt-local
progress; existing attempts, run_after, lease_token, worker_id and lease_until
own execution. Jobs are already excluded from knowledge packs as system data.

Startup, PUT and explicit backfill request durable work, never run corpus
inference. Matching active requests coalesce. A request for a wider/different
subset while a narrower job is active returns 409 rather than silently dropping
part of the requested scope. Generation changes fence and supersede old work.
Startup does not reset an exhausted or explicitly cancelled generation's retry
budget; explicit POST is the operator's retry action. Healthy indexes do not
enqueue startup work. PUT persists generation, queued intent and config in the
existing transaction/compensation boundary.

## Execution and recovery

The existing separate `rka worker` executes backfills using its persisted config,
never a model/config supplied in an arbitrary job payload. Each attempt checks
the generation identity before inference. Heartbeat renews a live lease; loss
of heartbeat cancels the attempt. Lease proof is checked inside vector-write
transactions, progress updates and the atomic generation-completion/job-completion
transaction. Ordinary entity jobs use the same lease write guard. Source changes
during inference must not allow an old result to replace a newer record's vector.

After process loss, the queue reclaims expired attempts with new lease tokens.
Resume scans the existing missing-row anti-join, retaining committed good vectors.
Progress is persisted per attempt and may reset when an attempt restarts; it is
not an invented cumulative count. Retry/backoff is finite. Final failed/expired
attempts leave the generation explicitly degraded. Coverage/consistency checks
still decide readiness; ending a cursor is insufficient.

Cancellation terminalizes the job as `failed` with `embedding_backfill_cancelled`
(preserving existing queue/status enums) and invalidates its lease atomically.
It cannot guarantee remote/native compute has stopped. Status remains available
after API/worker restart. Explicit retry creates a new job without deleting the
previous terminal job record. Config/status routes remain installation-wide operator surfaces,
not new remote visitor permissions.

## Compatibility and non-goals

Existing response keys/state values remain; durable `job_` IDs replace volatile
`bf_` IDs. New status fields expose attempt/lease/backoff/generation and error code.
Pack-import progress and vector construction continue using the existing API
background loop and separate endpoint; they are not silently converted into a
generation job. This is a remaining entry-point/release gate, not covered by the
startup/PUT/manual ownership claim. No schema migration is
needed. API-only installations must run `rka worker` for queued backfills.

This moves full backfill inference out of the API, using the already separate
Core worker and deployment supervisor. It does not eliminate every single-input
query/probe from the API, implement an ONNX kill sandbox, prove real-model RSS,
or complete E2 offline dimension maintenance. The legacy force/project CLI remains
an E2 compatibility gate and must not be presented as the new recovery command.

## Acceptance

Model-free, synthetic fixtures cover enqueue/no API inference; dedupe across
connections; heartbeat and expired-lease fencing; interrupted-run resume without
re-embedding committed rows; generation supersession; source-edit races; bounded
retries/exhaustion/cancel; persistent status; final coverage; healthy startup;
config transaction failure; and pack status separation. Full Core, base-plus-vec,
startup smoke, public contract snapshots and native CI configuration are gates.
Production data, ports, models and peer worktrees remain untouched.
