# Portable knowledge integrity

Knowledge Pack import treats its manifest and archive as untrusted input. The
pipeline is: validate source schema and hashes, validate source references,
re-key declared references, recompute derived target hashes, validate the target
graph, then publish database rows and managed artifact files atomically. A
failure before commit rolls back rows and removes only the newly staged or
published import directory. It never repairs the original project in place.

SQLite `PRAGMA foreign_key_list` is the source of truth for physical foreign
keys. An additional explicit catalog covers logical references, typed graph
endpoints, review targets, tags, topic assignments and reference lists. This
includes checkpoint missions, recommended/selected options, hook references and
directive dependencies. Options must belong to their declaring decision;
dependency targets must actually be directives. All references remain in the
source project before remapping and in the target project after import.

Missing required relations produce structured issues with `category`, `table`,
`column`, source row `ids`, and target. Import refuses them rather than silently
replacing them with NULL. A detached legacy checkpoint is refused even though
the historical SQLite column was nullable. The caller must restore its mission
relationship before exporting a corrected pack. Duplicate source IDs and rows
claiming another source project are also refused.

The one explicitly excluded physical FK is
`reference_validation_attestations.validation_job_id`: worker jobs are local
runtime state. Import clears that FK, retains the source job/project identity in
the attestation payload, and reports `excluded_runtime_reference` as a warning.
An import creates its own completed lexical-index receipt; that is not a
restored source job or evidence of completed embedding inference.

Only declared JSON reference positions are re-keyed. Arbitrary labels, comments
and other string values are not rewritten just because their text resembles an
entity ID. Existing explicitly reference-bearing prose columns and the frozen
Writer outline figure/table/citation intention fields retain their established
re-keying behavior. Historical attribution originals remain untouched.

Original context-manifest hashes and expanded checkpoint dependency hashes are
verified **before** rewriting. Recomputed target digests do not excuse a corrupt
source digest. Legacy hash-only snapshots remain opaque: they cannot be verified
against absent components and are not silently certified as current. Hashes
detect inconsistent content; an unsigned pack does not authenticate its author.

The envelope remains format 8. Older packs without the additive review or
dependency fields remain readable with conservative defaults. New dependency
data is a registered core table; an older importer that lacks this table must
reject it rather than silently drop it. This change does not promise backward
import into older RKA installations.
