# Directive lifecycle dependencies

An explicit `directive_dependencies` record binds a directive to a particular
decision revision for supersession propagation. A caller declares the same-project
directive/decision pair, actor and rationale. It is not inferred from text,
`references`, `justified_by`, `related_decisions`, or PI authorship.

When that decision is superseded, only explicitly dependent active directives
become `status=superseded`. Their original text, attribution and dependency
record are preserved. `journal.superseded_by` is not set to a decision ID (it is
a journal-to-journal field). Audit records explain the causal decision instead.
Independent PI principles and ordinary citations retain their lifecycle. Linked
directives without explicit dependencies appear as review candidates, not as
automatically revoked instructions. No semantic inference is authoritative.

Live supersession and admin repair share discovery and application helpers.
New decision creation, old-head transition, cascade, review and audit run in a
single transaction. Only a current, unsuperseded head (`active` or the existing
`revisit` state) can be replaced; the successor must also be in one of these
states. An exact
retry returns the recorded successor; a different retry conflicts. Historical
chains are retained, and a retry of A→B after B→C does not create D.

`POST /api/notes/{directive_id}/dependencies` (MCP
`record_directive_dependency`) declares a dependency. `GET` on the same endpoint
reads it. Declarations are append-only; changing intent requires a new directive,
not silently rewriting the historical dependency. Actors are asserted provenance,
not credentials. Existing directives acquire no dependency during migration.
