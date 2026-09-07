# ADR 0016: Safe source registration and explicit admission

Status: Accepted for E2.5

## Context

Researchers need to bring files, pasted notes, repository snapshots, URLs, and
Zotero records into RKA without turning unreviewed material into journal entries,
claims, or decisions. The existing artifact table can preserve bytes and hashes,
and Interpretation Staging can hold source-located candidate statements, but Core
previously had no typed provenance envelope connecting those two facilities.

## Decision

Core adds two immutable, project-scoped records:

- `registered_sources` (`src_`) records source kind, exact content hash, stable
  locator when applicable, ownership, provenance, actor, and a deterministic
  manifest hash. Every source owns one managed `art_` payload. For locator-only
  sources, that payload is a canonical JSON locator manifest.
- `source_admissions` (`sad_`) records the explicit, grounded, revision-guarded
  review that connects one artifact-backed `icd_` interpretation to an
  **already-existing** journal entry, claim, or decision.

Registration is idempotent on `(project_id, manifest_hash)`. Supplied bytes are
copied into private Core-managed storage and verified before database insertion.
File registration rejects symbolic links, non-regular files, oversized inputs,
unsafe file names, and an optional expected-hash mismatch. The MCP connector
reads host-local files and transfers bounded bytes to Core, so Docker does not
need the host path mounted. URL, repository, and Zotero locators are never
fetched by Core.

The admission transition requires all of the following in the same project:

1. the candidate is pending or in review at the exact expected revision;
2. its source is the registered source's artifact;
3. `grounding_verified=true` and a non-empty review reason;
4. Core re-hashes the managed artifact before any candidate mutation;
5. the canonical target already exists;
6. one immutable admission, review event, and `derived_from` edge are written
   atomically while the candidate becomes resolved.

An admitted candidate cannot be reopened. A future revocation design, if needed,
must be explicit and additive rather than mutating this history.

Knowledge Pack format v8 includes source envelopes, admissions, and exact artifact
bytes. Export and import reject mismatched source/artifact hashes, provenance
manifest hashes, candidate revisions, targets, or provenance edges.

## Non-goals

E2.5 does not add a Writer or Workbench interface, remote fetching, repository
checkout, secret scanning, content execution, LLM extraction, automatic
interpretation, or automatic canonical writes. Creating a candidate remains a
separate explicit operation; creating/selecting the canonical target remains a
separate explicit operation.

## Public operations

- REST: `POST /api/sources`, `GET /api/sources`, `GET /api/sources/{id}`,
  `POST /api/sources/{id}/admissions`
- typed MCP query: `sources`
- typed MCP execute: `register_source`, `admit_source_interpretation`

The registration result returns `artifact_id`; callers use that ID with the
existing `create_interpretation_candidate(source_type="artifact", ...)` operation.

## Consequences

Unreviewed source material is portable and auditable without gaining canonical
status. Admission is intentionally a short, human-in-the-loop chain rather than
an ingestion pipeline: register, stage an interpretation, review it, create or
select a canonical target, then admit it.

## 2026-09-05 hardening addendum: path authority

The host-read/bounded-bytes design is retained. A caller-supplied path does not
itself authorize the read: MCP host paths require operator-owned
`RKA_HOST_FILE_ROOTS`; direct REST paths require the separate
`RKA_SERVER_FILE_ROOTS`. Both default to empty, and neither a project directory,
request field nor a server-returned manifest can expand them. Managed artifact
bytes and explicit uploads retain their existing ownership boundary.

See [File access](../FILE_ACCESS.md) for the snapshot-based read design,
compatibility action, cross-platform restrictions and remaining threat limits.
This addendum does not change source admission or historical source records.
