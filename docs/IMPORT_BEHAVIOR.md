# Import behavior and compatibility

Status: Unreleased hardening. No schema migration, automatic historical repair,
remote fetching, or model execution is introduced by these fixes.

## BibTeX

With the academic extra, Core uses BibtexParser v2's
[`parse_string`](https://bibtexparser.readthedocs.io/en/main/quickstart.html).
The reviewed lock contains `bibtexparser==2.0.0b9`. Failed parser blocks are
reported before creating records, including malformed input and duplicate
citation keys/fields. A malformed library is rejected as a whole; a successfully
parsed library still reports per-entry validation/storage failures and retains
earlier successful entries. The `imported`, `skipped`, `errors`, `total_parsed`
response shape is unchanged. Inspect `errors`, not merely HTTP 200.

Without the optional dependency, Core supports a deliberately limited, iterative
parser: braced or parenthesized entries, nested braced and quoted field values,
numeric literals, percent comments and braced `@comment` blocks. A leading UTF-8
BOM is accepted without losing the first entry. Nesting is
limited to 64 levels. String macros, `@string`, `@preamble`, concatenation, and
parenthesized comments require `rka-core[academic]`; the base parser returns an
actionable error rather than guessing or importing truncated metadata.
This is not a full BibTeX/LaTeX interpreter. No imported content is executed.

Original entry text remains in `literature.bibtex`. Existing normalized metadata
behavior (brace/whitespace cleanup and author parsing) is retained. S2's 2 MiB
academic input limit and operator-owned file roots still apply. Uploading a .bib
file supplies bytes and does not authorize arbitrary filesystem reads.

Duplicate DOI/title preflight checks remain project-scoped. Setting
`skip_duplicates=false` disables the preflight skip, not the database's unique
DOI constraint: a collision is a per-entry error, with no half-written row.
Typed MCP `default_status` now reaches the import service. MCP shows import
error details as well as counts.

## Attribution and execution actor

`literature.added_by="import"` describes origin. It is not a new valid actor.
Academic import maps that origin to the existing execution actor `system`;
direct literature creation derives `system` only if no actor was supplied.
Explicit `actor="import"` at ordinary service boundaries remains invalid.
Mixed import's historical `actor="import"` alias is normalized to `system` at
the import adapter, consistently with typed MCP.

Journal creation now stores the requested `data.source` and `verbatim_input`
separately from the executing `actor`. Events, graph-link creators and audit
rows identify that actor; hook payloads identify the stored source. Callers that
previously relied on `NoteService.create(..., actor=...)` to overwrite source
must now set `JournalEntryCreate.source` explicitly. `actor="system"` no longer
attempts to put the invalid value `system` into the journal source column.

These fields are caller assertions, not identity authentication or authorization.
No existing source or verbatim text is rewritten. Audited corrections of
historical source/verbatim fields (I3) remain a separate work package.

## Verification profiles

`test_bibtex_parser.py`, `test_bibtex_import.py`, and
`test_mcp/test_import_roundtrip.py` cover parsing, authorized file import,
REST/upload/typed/legacy adapters, reading status, origin, actor, duplicate DOI,
rollback after an injected event failure, and mixed batches. Run these in both
the locked Core profile and a separate base+dev environment without the academic
extra; the latter must verify `bibtexparser` is actually absent.
