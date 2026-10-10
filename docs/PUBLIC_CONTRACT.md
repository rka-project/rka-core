# RKA Core public contract

RKA Core exposes versioned REST and MCP contracts for clients that do not
import Core models, services, or database code. The discovery endpoint is
`GET /api/capabilities`; clients should require `rka-core/v1` before starting
a workflow. The reviewable baselines are checked into
`contracts/rka-rest-v1.openapi.json` and `contracts/rka-mcp-v1.json`.

## Stable, preview, and compatibility surfaces

The initial `rka-rest/v1` baseline contains 133 stable Core operations. The
initial `rka-mcp/v1` baseline contains 81 stable typed operations behind the
five default transport tools (`rka_query`, `rka_execute`, `rka_describe`,
`rka_load_tools`, and the compatibility alias `rka_help`).

This snapshot describes the default stdio profile. Optional connector-only
skill adapter tools (`rka_list_skills`, `rka_read_skill`, and
`rka_start_session`) are disabled while the snapshot is generated and remain
outside `rka-mcp/v1`. The two compatibility transport entrypoints
`rka_load_tools` and `rka_help` are frozen here as transport schemas; they are
not additional typed research operations.

Product ownership and usage readiness are different dimensions. The current
runtime also exposes 28 REST and 22 MCP Core-owned preview operations. They are
listed in each snapshot so their boundary cannot drift silently, but they do
not receive the v1 stability promise. Frozen Writer operations, shelved
Agentic operations, and Core-legacy summary/session operations are likewise
inventoried but excluded from the stable Core contract.

An intentional additive change, such as a new optional response field or a
new operation, may update a v1 snapshot after review. Removing or renaming an
operation or field, adding a required input, narrowing an enum/type, changing
response status or shape, or changing project-scoping semantics requires a new
contract version unless an explicitly reviewed compatibility plan preserves
existing clients.

## Project scope

Every project-scoped REST request must explicitly send either the
`X-RKA-Project` header or the `project_id` query parameter. Every
project-scoped MCP operation must explicitly include `project_id`. There is no
active-project or default-project fallback. Unscoped discovery and lifecycle
operations, including capabilities, health, project listing, and project
creation, are the exceptions declared by their schemas.

The server validates that the selected project exists before a scoped
operation runs. A missing scope returns 422; an unknown project returns 404.
Clients must keep one project ID pinned through a logical workflow rather than
relying on session state.

## Revisions, hashes, and cursors

Revision and hash fields are not universal properties of every Core record.
Clients may depend on them only where the endpoint schema declares them. For
example, claim-scope writes use `expected_revision`; a stale revision returns
409, and the resulting scope version records `revision` and
`claim_content_hash`. Experiment plans and evidence/artifact records have
their own declared revision or content-hash fields.

`GET /api/changes?cursor=...` returns a project-scoped synchronization cursor.
That cursor orders the change feed; it is not an entity revision and must not
be used as an optimistic-lock value. Knowledge-pack manifests and future
export contracts may define additional checksums independently.

## Retry and idempotency expectations

Core does not currently implement a general `Idempotency-Key` header. Retry
behavior is therefore operation-specific:

- Reads are safe to retry.
- Claim-to-cluster `member_of` edge creation uses a natural key and returns the
  existing edge when repeated. Artifact registration is content-addressed
  within a project. These writes are safe to repeat with identical inputs.
- Revision-guarded writes must reuse the revision they actually read. A stale
  write fails with 409 and requires a new read and an explicit reconciliation;
  it must not be silently replayed against the newer revision.
- General create operations, including journal entries, decisions,
  literature, missions, projects, and most other POST requests, are not
  covered by a generic idempotency promise. After a timeout or lost response,
  query the relevant public read/change surface and reconcile before deciding
  whether to create again. Blind retry can create a second record.

These rules describe the current implementation. The architectural goal that
all retriable writes eventually accept an idempotency key is not yet a public
Core v1 guarantee.

## Read diagnostics and pagination

`GET /api/maintenance` and `pending_maintenance` accept `limit` (1–200,
default 50) and `offset` (non-negative, default 0). Pagination applies
independently to each category, with deterministic ordering for an unchanged
database. Re-read from offset 0 if records change while paging; this is not a
snapshot cursor and should not be used while automatically repairing rows.

- `total_items` counts **issue occurrences**, not unique entities, and uses
  the same count source as `/api/maintenance/summary` regardless of page size.
- Each category retains `count` as its total and adds `category_total`,
  `returned_count`, `limit`, `offset`, `has_more`, and `next_offset`.
  Lifecycle dependency occurrences are directive/decision pairs; `ids` is
  deduplicated within their page and `candidates` retains every pair.
- Existing advisory selection limits remain explicit: gate review selects at
  most 10 missions; lifecycle review selects at most 100 dependency pairs.
  These are policy-bounded advisory totals, not exhaustive audits. The ordinary
  SQL categories use full counts. `advisory_limits` documents the distinction.
- The manifest and summary label `scope=project` and the requested project ID.
  Estimated tool calls refer to the counted backlog, not the page. An item is
  a review suggestion, not proof that a claim is missing or authority to
  manufacture claims, edit attribution, or delete content.

For older backends, MCP retains the default legacy read and warns that totals
may be capped. A non-default page without response pagination metadata is
reported as unsupported rather than silently returning the wrong page.

Integrity findings carry `scope=project` with their project ID, or
`scope=database` for database-wide index/stranded-entity checks. The latter
retain their existing diagnostic sampling and mark `count_is_exact=false`
(except the count of unreadable index tables). These findings do not assert
that content belongs to the requested project, nor authorize recovery/purge.

MCP status/search/context distinguish embedding availability from index
coverage, consuming optional `index_status`/`warning` capability fields when
the backend supplies them. Discovery failure means **unknown**, not FTS-only.
Backends without index metadata do not confirm complete coverage; this change
does not add or migrate the partial-index storage implementation.

Journal/changelog typed input schemas and discovery expose the REST limit
maximum of 200; the existing default/null and lower-bound behavior is retained.
An over-limit request fails earlier, before HTTP, rather than reaching REST's
existing 422 response. Journal listings mark bodies longer than 500 characters
as truncated and identify the project-scoped `entity` read. A full changelog
page does not establish completeness; use `changes_since` for cursor paging.

## Reviewing and updating snapshots

Run the read-only check locally with:

```bash
python scripts/update_contract_snapshots.py
```

For an intentional contract change, regenerate both baselines with:

```bash
python scripts/update_contract_snapshots.py --write
```

The pull request must explain the semantic change and include the readable
JSON diff. CI runs the check independently and fails if runtime schemas and the
checked-in snapshots differ. Prose-only schema descriptions, examples, build
timestamps, and package patch versions are removed from the baseline so the
diff remains focused on wire behavior.

Knowledge Pack format v8 also transports immutable `src_` registered-source
envelopes, `sad_` explicit admissions, and the exact artifact bytes they hash.
Import fails closed when the source manifest, artifact hash, candidate revision,
canonical target, or provenance edge does not agree. Source registration alone
never grants canonical journal/claim/decision status.
