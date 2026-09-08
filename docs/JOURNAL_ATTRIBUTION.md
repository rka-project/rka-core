# Journal attribution and capture (unreleased I3)

`source` is the asserted author/origin. `actor` is the caller-declared executor
of a correction, not an authenticated user or proof of authorship. Neither an
HTTP header nor a `source=pi` label confers authorization. The local operator
still controls API access. This history is not a cryptographically signed or
tamper-proof ledger against a database owner.

## Compatibility boundary

Ordinary `PUT /api/notes/{id}`, typed `update_note`, deferred `rka_update_note`
and journal items in `bulk_update` reject non-null `source`/`verbatim_input`.
Use the correction endpoint below instead. This intentional tightening prevents
these fields from bypassing reasoned, revision-guarded history. Omitted/null
fields on ordinary updates retain their previous no-change meaning.
Bulk preflight rejects the entire request before writes when it contains an
invalid attribution edit; unrelated valid batches remain best-effort.

Body, summary, tags, confidence, status, pinning and links still use ordinary
updates. They do not advance `attribution_revision`: that counter protects only
the asserted source, exact original and capture mode, not the entire note or its content.
Ordinary updates also reject `capture_mode`; use a correction to change it.

## Creation and capture modes

`capture_mode` describes how original text was retained, not authenticated
authorship, factual correctness, or the current body's wording. `content` is a
mutable working representation; `verbatim_input` is the separately retained
original. Never reconstruct an original from an agent's paraphrase.

| Mode | Contract |
| --- | --- |
| `unknown` | Default for omitted mode on direct REST/MCP/service/batch creation. No inference from source, quote presence, or content equality. Missing originals stay missing. |
| `raw_capture` | Explicit preservation of supplied text. On single-note creation only, an absent/null original snapshots the supplied content exactly. An explicit original is retained without trimming or ID rewriting, even if content is a different display representation. Blank originals are rejected. |
| `agent_restatement` | Declares an agent-rendered body. PI source requires a non-blank, separately supplied exact original. Other sources may omit an original. No automatic copy from content. |

These explicit-mode rules are enforced by the shared creation model and
revalidated in the service, including REST, typed/deferred MCP and batch import.
Legacy MCP `record_note` additionally keeps its existing PI-quotation guard
when mode is `unknown`; REST/deferred `add_note` retain legacy unknown-mode
acceptance. Selecting `unknown` does not verify an author or a quotation.

Examples for `record_note` (also valid fields on POST `/api/notes`):

```json
{"content": "  PI's exact wording.\r\n", "source": "pi", "capture_mode": "raw_capture"}
```

```json
{"content": "Agent analysis of the request.", "source": "pi", "capture_mode": "agent_restatement", "verbatim_input": "  PI's exact wording.\r\n"}
```

Document ingestion is an explicit raw-input workflow: every created non-empty
section keeps its exact source slice, including original headings/line endings,
while content retains the existing formatted display. MCP PI document ingestion
no longer incorrectly requires a second copy of the supplied document.
Workspace full-text ingestion preserves decoded text and line endings. The
bounded reader marks truncated text and DOCX extraction as `unknown`; generated
code previews/file metadata also remain `unknown`, without fabricated originals.
This is a text contract, not a claim of file-byte identity or source authenticity.
Other internal creators (mission output, manuscript registration, researcher
summaries and source admissions) remain unclassified unless they explicitly
supply a mode; an actor or source label does not select one automatically.

## Correction workflow

1. Read `GET /api/notes/{id}` in the selected project and take its
   `attribution_revision` (initially `0`). In MCP use
   `rka_query(args={"operation": "entity", "project_id": "...", "id": "jrn_..."})`;
   `journal` is a truncated listing, not the full record.
2. Submit a full attribution replacement, including a non-blank reason, explicit
   actor and a fresh request ID. `source` and `verbatim_input` are both required.
   Optional `capture_mode` changes the mode; omission/null preserves the previous
   mode. Explicit `unknown` removes a capture claim with a recorded reason.
3. Retain the returned immutable correction. A conflict requires re-reading and
   reviewing the current record, not automatically retrying with a newer revision.

REST uses the normal `X-RKA-Project` scope header:

```json
{
  "expected_revision": 0,
  "request_id": "correct-original-1",
  "actor": "executor",
  "reason": "Checked the supplied original and corrected its author",
  "source": "pi",
  "verbatim_input": "Preserve my exact wording."
}
```

Send to `POST /api/notes/{id}/attribution-corrections`. MCP uses
`rka_execute(args={"operation": "correct_note_attribution", "project_id": "...",
"id": "...", ...})` with the same fields. The deferred equivalent is
`rka_correct_note_attribution`.

- Success and exact retry both return HTTP 200 with the **correction event**,
  not the current journal. The event includes revision, before/after values,
  actor, `actor_basis=caller_asserted`, reason and request ID.
- `expected_revision` is a non-negative integer, not a timestamp. Concurrent
  corrections from the same revision accept only one distinct request.
- The same request ID and identical payload return the original event even
  after later corrections. Reusing an ID with different data returns 409.
  IDs are scoped to a project and journal; reasons/quotes compare exactly.
  Mode is part of the correction intent. On retry an omitted mode means that
  event's previous mode, not the journal's potentially newer mode.
- Stale revisions return 409; invalid input or a new no-op correction returns
  422; an entry outside the selected project returns 404. MCP retains its
  existing `API error <status>: ...` error rendering.
- Missing historical originals remain `null`. Non-PI replacements may explicitly
  set `verbatim_input=null`. A PI correction requires a non-blank quotation;
  do not fabricate missing words to satisfy it. A correction into, or remaining
  in, `raw_capture` also requires an explicitly supplied non-blank original;
  it never copies the potentially edited current body.

## History, durability and portability

Read `GET /api/notes/{id}/attribution-history?after_revision=0&limit=50` or
`rka_query(args={"operation": "note_attribution_history", "project_id": "...",
"id": "...", "after_revision": 0, "limit": 50})`. Results are ordered by
revision; use the final returned revision as the next cursor. Limit is 1–200.
An empty history means no corrections have been recorded, not that authorship
has been verified. This is not full journal edit history.

Migration 056 adds a zero-valued attribution counter and an empty immutable
correction table without changing old source/original values or inventing old
events. The first correction preserves the previously stored state. The current
row, correction and audit commit together; failure or cancellation rolls all
three back. SQL guards reject attribution writes without a matching correction.
Migration 057 adds capture mode to journal rows and both sides of each correction.
All existing rows/events become `unknown`, without changing their source, quotes,
timestamps or revision numbers, and without creating historical events.

`journal_attribution_revisions` is mandatory canonical pack data, exported even
without optional audit logs. IDs and parent references are re-keyed on import;
quoted originals and correction reasons are preserved literally, including any
IDs they mention. They are evidence text, not rewritten graph references.
Imports with a missing chain, mismatched predecessor or inconsistent head fail
atomically, including mismatched capture-mode chains. Old packs without revision
fields import at revision zero; absent capture fields import as `unknown`. Use an
upgraded Core when importing packs containing the new table/column. Explicit
project deletion removes the history with the project; ordinary deletion or
rewriting of individual correction records is forbidden.

## Scope and release status

I3a/b/c are local, unreleased development. Creation classification and correction
history do not authenticate historical records. Currentness/resolve_stale (I4),
directive invalidation (I5), full content revision history, release and deployment
remain separate work. No production data is migrated by this development batch.
