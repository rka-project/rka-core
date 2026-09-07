# E1c: pack and legacy CLI backfill entry points

Baseline `a25fb93`, isolated `codex/core-audit-hardening`. Continues the approved
Core hardening design and E1b lifecycle; not a release or deployment claim.

- Import graph, strict lexical indexes, completed import receipt and optional
  generation-bound embedding intent commit together. The receipt is a system
  `jobs` record (`pack_import`), not runnable research data. Existing jobs are
  excluded from packs; no schema migration. No provider inference during import.
- `defer_indexing` remains an accepted compatibility parameter, but lexical
  indexing is always synchronous/transactional and vectors always use the worker.
  `index_project` becomes a bounded, strict lexical repair helper, never a second
  vector loop. Cluster FTS remains supported; cluster vectors remain parked.
- Import polling reads the receipt and linked embedding job from SQLite. It
  distinguishes lexical completion from semantic pending/running/complete/failed
  or disabled, and exposes generation-wide readiness separately. A disabled
  backend is not reported as semantic ready. Old volatile IDs are not recoverable.
- The existing generation dedupe key remains the single active backfill intent.
  Project-scoped requests never coalesce into broader scopes; differing project,
  entity subset or force requirements report busy before mutation. Import's busy
  failure rolls back its rows/files/FTS/receipt. Retry upload after the owner ends.
- Scoped/force/hash-check/nondefault-batch intents use `embedding_backfill_v2`
  with payload version 2. E1b workers reject the unknown type instead of ignoring
  the scope and running global work. New workers handle both versions under the
  same generation owner lookup and reject unknown payload versions before
  inference. Coordinated API/worker upgrades are still required: old workers
  may consume failed attempts, and mixed-version schedulers are unsupported.
- Legacy `backfill-embeddings` becomes an enqueue-only adapter. It loads saved
  config, requires a compatible initialized generation, preserves project/type
  selection and returns a durable job ID, not invented completed counts. No env
  default fallback, online dimension maintenance or unguarded inference loop.
- Preserve the legacy default content-hash check, not only missing metadata:
  the worker keyset-scans the selected scope, counts changed inputs without model
  calls and skips unchanged compatible rows. This narrow CLI compatibility mode
  is not E2's shared recipe redesign; force mode deliberately revisits all rows.
- `--force` revisits selected rows in the current space using the same bounded
  worker/source/lease guards. It replaces each vector atomically, never clears
  tables. Missing-only retries retain good vectors; force retries can revisit
  good rows after process loss, within the existing finite attempt budget.
- Scoped completion checks selected coverage and coherence. Only the existing
  global coverage gate can declare the entire generation ready; a completed
  scoped job may leave the global index reindexing pending work elsewhere.
- Tests use temporary synthetic SQLite, fake HTTP/providers and isolated CLI
  environments. No production data/cache/port/LM Studio, push, merge or deploy.

E2 input/hash unification, offline dimension change/backup/exclusive maintenance,
real-model RSS and native cross-platform execution remain separate gates.
