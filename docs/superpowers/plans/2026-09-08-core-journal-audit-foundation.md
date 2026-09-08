# I-series kickoff and I3a journal update audit foundation

- Baseline: merged E-series PR #160, `6dbb0e0`.
- Branch: `codex/core-i3-journal-audit` in a separate temporary worktree.
- Status: I3a implemented locally; validation below. I3 as a whole remains open.
- No production DB, model runtime, credentials, service restart or deployment.

This is the historical I3a validation record. The same branch now also contains
[I3b guarded corrections and portable history](2026-09-08-core-journal-attribution-corrections.md);
the implementation/exclusion lists below describe I3a at its own acceptance point.

## I-series scope

I1/I2's confirmed repairs were merged in PR #159. Remaining packages are I3
(journal attribution and audit), I4 (shared currentness and stale resolution),
I5 (explicit directive dependencies and supersession), I6 (pack references and
hash integrity), and I7 (project ownership and lifecycle holes). I5 depends on
I3/I4. Each package needs its own bounded implementation and acceptance evidence;
this change does not claim to complete any of I4-I7.

## Baseline reproduction

A disposable synthetic DB, initialized through the ordinary schema/Phase-2
initializers, reproduced all of the following on `6dbb0e0`:

- An executor note can be changed to `source=pi` without an original quotation.
- `verbatim_input` can be written and then overwritten.
- All three update audits record `actor=system` and only a field-name list; no
  before/after values survive in those audit records.
- A tags-only update does not append an audit record.

The probe loaded `NoteService` from the new I3 worktree, with embeddings/LLMs
disabled, an unreachable API URL and fresh temporary data. The historical probe
is `/private/tmp/rka-core-i3.emTbB6/probe_journal_attribution.py`; it describes the
baseline defects and is not a passing assertion against the repaired source.
Existing focused journal behavior was green before edits: 39 tests passed.

## Implemented I3a boundary

- `NoteService.update(..., actor=...)` separates the executor of the update from
  the asserted author (`source`). An explicitly supplied actor is validated and
  also labels new relationship edges.
- `actor_basis=caller_asserted` explicitly means a declaration, not authentication.
  Existing callers that omit the actor retain `system` for compatibility but are
  marked `actor_basis=legacy_default`. No author or HTTP header is promoted into
  a verified identity. REST/MCP transport actor plumbing is not added in I3a.
- Update audit details retain `fields` and add before/after values for requested
  stored fields. JSON-backed relationship columns retain their stored JSON
  representation; tags use normalized stored values. Generated `updated_at`
  remains in the legacy field list rather than the user-field snapshots.
- Tags-only writes now update the timestamp and append their before/after audit.
  Empty updates remain no-ops; omission/null behavior is unchanged.
- Reading the before state, mutating the row/tags/links/FTS, inserting the audit
  and obtaining the response all happen under the same managed transaction.
  Audit failure rolls everything back. Concurrent callers receive their own
  mutation's read-back, not the following caller's values.
- Existing creation, raw workspace ingestion, status/confidence and bulk's
  documented best-effort semantics are unchanged. No public schema or DB
  migration is introduced in this foundation batch.

## Required next I3 batches, not implemented here

1. Define explicit raw-capture versus agent-restatement contracts. The create
   model deliberately permits raw workspace content without a duplicated
   `verbatim_input`; a blanket PI-verbatim validator would break that workflow.
   Missing historical originals stay unknown, never reconstructed by guessing.
2. Add a durable per-journal revision/correction ledger, reason and conflict
   preconditions, with exact-retry behavior and project-scoped ordered history.
   This must be canonical exportable provenance, not merely optional bulk audit
   logs. Existing pack defaults do not establish that guarantee for `audit_log`.
3. Route source/verbatim changes through that explicit correction contract on
   service, REST, typed/legacy MCP and bulk surfaces. Ordinary updates must no
   longer provide a bypass. Wire declared execution actors separately and label
   their trust basis; do not use spoofable headers as authority.
4. Test fresh/upgraded DBs, incomplete legacy provenance, exact retries,
   concurrent conflicts, failed writes, project boundaries and pack round trips.
   Preserve #152's status/confidence/bulk fixes and E-series retrieval semantics.

I3a adds evidence but intentionally does not yet reject ordinary attribution
requests for missing reasons/revisions. A reasoned, revision-guarded correction
surface and an explicit capture contract are still required before closing I3.

## Validation record

- New tests cover actor/author separation, before/after source and quotation
  values, tags-only audit, invalid actor, rollback after an inserted audit,
  concurrent history/read-back, wrong project, empty update and graph actor.
- REST, typed MCP, legacy MCP and bulk updates are read back through `/api/audit`.
- One new graph test initially omitted required `DecisionCreate.decided_by`;
  this was a fixture error, corrected explicitly. The early full run was
  interrupted and is not counted as acceptance evidence.
- Focused journal/audit/transaction regression: **83 passed**, 10.99 s, including
  all four public write paths.
- Full Core regression (`not writer and not agentic`): **3685 passed, 1 skipped,
  294 deselected, 5 subtests passed**, 300.06 s. Five existing SWIG deprecation
  warnings were reported. Acceptance evidence is
  `/private/tmp/rka-core-i3.emTbB6/i3a-full-core-v2-results.xml`.
- Public REST/MCP contract snapshots are unchanged; changed-file Ruff and diff
  checks pass. No commit, remote PR, push, merge or deployment is part of this
  batch.

The security-review workflow influenced actor trust labeling and transaction,
cross-project and read-back tests. This is not an authentication implementation
or a claim of complete provenance protection.
