# Issue #158: released-source upgrade and resource acceptance

## Scope and interpretation

- Issue: <https://github.com/rka-project/rka-core/issues/158>.
- Reported development source: `f8db01b`, already displaying version 3.0.0.
- Tested released source: `3425a2becd079fb9463b64e65cc179d2ddca7955`
  (tag `v3.0.0`). Core runtime code is unchanged by this acceptance follow-up.
- The remediation is already present in PR #159 (`3cf1210`) and especially
  PR #160 (`6dbb0e0`): durable worker-owned backfill, spawned native inference,
  2 KiB input / 4 KiB batch-padding ceilings, and backed-up offline reindexing.
- This follow-up adds a combined legacy-schema/real-corpus fixture, an API
  restart regression, fixture safety regressions and operator documentation.
  It does not add automatic chunking, silently truncate research text, change
  encoding identities, or redeploy a user's existing instance.

The issue's original data and exact 2.9.0 source revision are unavailable.
This is a representative synthetic acceptance, not a claim that the reporter's
personal database was reproduced. Oversized records remain explicit failures;
valid-vector progress must not be mistaken for a globally ready semantic index.

## Isolation and artifact identity

- Dedicated worktree/branch: `codex/issue-158-acceptance`, based on the release.
- Image built with the release Dockerfile and `uv.lock`, package installed
  non-editably; runtime imports resolve to `/app/.venv/lib/python3.13/site-packages/rka`.
  No Core source overlay is used by API, worker, boundary or admin commands.
- Local test image: `rka-issue158:3425a2b-MWqTSU`, Linux arm64;
  Docker image identity `sha256:86f172fea9cc21c0deca09a24fd07f4d199b207eade3cad2430755b2e5a4cdb5`.
- This is **not** published-GHCR-image acceptance: pulling the released digest
  was unauthorized. Package distribution permissions remain a separate gate.
- Docker Desktop 29.6.1, 8,321,515,520-byte VM, 10 CPUs. API cap 2 GiB / 1 CPU;
  worker cap 4 GiB / 2 CPUs; boundary container cap 2 GiB / 1 CPU.
- New synthetic volume `rka-issue158-20260911-MWqTSU`; resources carry
  `org.rka.audit=issue158-20260911-MWqTSU`. All runtime tests use `--network none`,
  no published ports, read-only container roots and bounded `/tmp` tmpfs.
- Only public model-cache files were copied from the previous labeled synthetic
  E-series audit volume, mounted read-only for that copy. No prior database,
  production volume, LM Studio, home model cache or demo package was used.

## Fixture and reproduction contract

`scripts/issue158_legacy_fixture.py seed` must run with `PYTHONPATH=/legacy`
and `--legacy-root /legacy`, where `/legacy/rka` is extracted using `git archive`
from `0dc1842ca771962e6e07676b6de9a6abecd74ac9` (representative final pre-split
2.9.0 source). Dependencies come from the current image, not the historical lock.
Seeding refuses an existing DB, config, target candidate, marker or dangling alias and checks the
imported source/root/version before opening SQLite.

The fixture contains 1,747 journals, 687 claims, 507 decisions, 527 literature
records and 145 missions: **3,613 rows**, approximately 4 MB. One journal contains
exactly 20,535 ASCII bytes. The other **3,612** records fit the native input policy.
It starts with 703 placeholder legacy vectors (687 claims and 16 journals), all
with intentionally stale hashes, and **no** `embedding_index_state` table.
Stale hashes force real recomputation; placeholder vectors are never counted as
successful model inference. This does not assert that all real legacy vectors
need discarding: verified legacy pairs are reusable in the released code.

Seed does not call the current reconciler or queue. The installed API's normal
`rka serve` startup must migrate, reconcile and create the durable job itself.
Run the existing `scripts/embedding_resource_acceptance.py worker --data-dir /data`
in a separate container, with only audit scripts mounted read-only at `/audit`.
That helper performs one actual worker attempt; exit 0 alone is not proof of a
complete index. Inspect durable job state and metadata as well as memory counters.

After each inference/maintenance phase, use the current installed package:

```sh
python /audit/issue158_legacy_fixture.py verify --data-dir /data --expected-metadata 3612 --expected-dimensions 768
```

Verification compares every historical canonical column of the seven embedding
source tables (excluding only the derived claims `embedding_pending` flag),
checks the intact poison row/no vector, metadata dimensions, physical pair/hash
coherence, SQLite integrity and foreign keys. It rejects a falsely ready global
index while the poison row is unembedded. Run it after the worker attempt exits,
not against concurrently advancing vector counts. The initial canonical SHA-256 for this run is
`1de40e7e956a9fe3b4f2abe23fb009fd8eb1d48da1aeb5ec8237520a1fd99fbe`.

For the 384-dimensional phase, stop **only the audit API/worker peers**, then:

```sh
rka admin embedding dry-run --data-dir /data --target-config /data/synthetic-bge-config.json --json
rka admin embedding rebuild --data-dir /data --target-config /data/synthetic-bge-config.json --peers-stopped --json
```

The candidate selects `BAAI/bge-small-en-v1.5`, dim 384, threads 2 and the same
isolated public cache. Restart the audit API/worker, then repeat verification
with `--expected-dimensions 384`. Retain the backup and recovery receipt.

## Observed results

The planned representative source/runtime acceptance is complete. Both real
models made full valid-row progress without OOM; the oversized row deliberately
prevents a globally ready index. This is not a production-deployment claim.

- Normal upgrade startup: API HTTP 200 at about 123 MiB, metadata 0 after stale
  pair removal, one pending generation-1 job, attempts 0/max 5. No API inference.
- Worker fault injection: SIGKILL at **510** committed vectors; **510** remained
  afterward and API health stayed HTTP 200. Exit 137 was intentional;
  `OOMKilled=false`. Replacement worker reclaimed the same job as attempt 2
  and advanced beyond that count. No queue/lease timestamp was edited.
- Nomic replacement attempt: **3,612** valid rows embedded in total, peak sampled
  process-tree RSS **1,145,454,592 bytes** (about 1.07 GiB), cgroup peak
  **1,099,636,736 bytes**, all OOM counters 0, exit 0. The resumed attempt took
  **1,127.63 seconds** while host regressions also ran; this is not an isolated
  throughput benchmark. The 20,535-byte poison remained unembedded with an
  explicit `embedding_input_limit` error, not a false complete/ready result.
- Native boundary: old near-8-KiB query rejected before inference; 2,034-byte
  query and two near-limit documents produced 768-dimensional vectors. Elapsed
  **19.71 seconds**, cgroup peak **1,440,194,560 bytes**, all OOM counters 0,
  exit 0. This measurement is configuration-specific, not a universal RSS bound.
- During backfill: scoped lexical search returned HTTP 200 with 20 results;
  health probes returned 200. An initial probe omitted the required project
  header and returned 422; the correctly scoped request passed. Reading the
  poison journal through scoped REST also returned 200 and all 20,535 bytes.
- Target-config dry-run: explicit `offline_rebuild`, source 768/target 384,
  `provider_validation=not_run`, no work queued and no config transition.
- Nomic integrity/coverage: canonical SHA-256 unchanged; all **3,612** metadata
  hashes verified and physical pairs reusable/coherent, exactly one missing
  pair (the poison), SQLite integrity `ok`, no FK violations. Index remained
  `reindexing`, job pending at attempt 2/max 5 with the input-limit error.
  The one-attempt helper does not drain the remaining retry backoff; finite
  exhaustion is covered by the model-free restart regression, not this run.
- Real API restart after Nomic: still **3,612** metadata rows, same single job
  and generation, attempts still 2, HTTP 200. No reset/re-embedding was admitted.
  API cgroup peak before restart: **165,658,624 bytes**, all OOM counters 0.
- Offline CLI: after all audit peers closed, `rebuild --peers-stopped` returned
  `prepared`, generation **2**, `backfill=queued`, exit 0. Retained recovery
  directory: `/data/rka.db.runtime-locks/1b67765bd20648738d81e75621b09adb`.
- Retained database/original-config/target-config checksums all matched the
  manifest on read-back; the committed recovery receipt recorded generation 2.
- BGE-small 384: **3,612** valid rows embedded in **159.17 seconds**, peak sampled
  process-tree RSS **356,823,040 bytes** (about 340 MiB), cgroup peak
  **293,912,576 bytes**, all OOM counters 0, exit 0. Source SHA-256 remained
  unchanged, all 3,612 hashes/pairs verified, SQLite/FK checks passed. Only the
  poison was missing; generation 2 stayed `reindexing`, job pending at attempt
  1/max 5 with its input-limit error. Health and scoped lexical search returned
  200 (20 search hits); API cgroup peak including audit exec probes/backup
  hashing was **183,066,624 bytes**, OOM counters 0.

## Final isolation read-back

All five named audit containers are stopped. The first worker has intentional
exit 137; the other four exited 0, and every container reports `OOMKilled=false`.
The audit image, synthetic volume, public cache and retained recovery copy are
preserved for reproduction; no running audit processes remain.

The real `rka-server` and `rka-worker` retain their original container identities
(`f2324b35901e`, `eea189fdf005`), image `rka-core-local:i-series-99131dc`, start
times `2026-09-11T13:29:10.74119625Z` / `2026-09-11T13:29:10.748155792Z`,
and restart counts 0. The canonical checkout remains `6b7c281` on main with
only the pre-existing untracked demo directory. Neither it nor the protected
portable-embedding worktree was altered. Upstream main still resolves to the
tested release commit `3425a2b`.

## Model-free regression coverage

- `test_embedding_poison_restart.py` uses the real FastEmbed prefix/input
  admission path with synthetic native inference. A valid row embeds once, a
  20,535-byte row remains intact/unembedded, finite attempts exhaust, two API
  lifecycles retain the failed job and valid metadata/vector, and health stays
  available. Only this test's attempt cap/backoff is shortened.
- `test_issue158_fixture.py` rejects wrong/missing archived source, current
  source, existing DB/config/candidate/marker, dangling DB aliases and unrelated markers
  before any SQLite connection.
- Focused resource/durable/native/API tests: 114 passed. Offline inspection,
  recovery, dimension reshape, runtime admission and Phase-2 startup: 102 passed.
  Final fixture guards: 10 passed. Ruff for new Python files and diff checks pass.
- Complete Core profile on macOS/Python 3.13.11: **3,900 passed, 1 skipped,
  294 deselected, 5 subtests passed** in **965.78 seconds**. Six deprecation
  warnings; no failures. This run collected the original seven fixture guards;
  the three added guard cases were included in the separate final ten-case run.

## Remaining boundaries

These measurements do not certify all ONNX models, Linux/macOS/Windows host
variants, published-container distribution, long-document semantic coverage or
production deployment. The installed local research service and its database
remain outside the test scope. Issue closure must not imply those outcomes.
