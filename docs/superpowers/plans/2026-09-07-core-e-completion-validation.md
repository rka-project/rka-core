# Audit E-series completion: implementation and acceptance

- Branch: `codex/core-e2-index-recovery`, based on merged PR #159 (`3cf1210`).
- Prior E2a/E2b1 commits: `e7a3495`, `3574a80`.
- Scope: E1 native-process/resource closure and E2 lifetime admission/offline
  recovery. I3-I7, R1-R3, release publication and production deployment are separate.
- User authorized completion, PR and merge. Merge remains gated on the final PR
  revision's native Linux/macOS/Windows checks; check the PR for current CI results.

## Delivered

- Lifetime DB admission before SQLite opens, retained until connection close;
  API failed-startup/exception cleanup, workers, direct DB clients, read-only
  inspectors, backup/export readers and standalone config writes participate.
- Gate plus 128 fixed kernel-locked slots; no PID-based authority or lock-file
  deletion. Hard links/SQLite URIs/unsafe lock files fail; old/uncooperative peers
  require explicit operator stop on shared lock-capable storage.
- CLI `rebuild`, `resume`, unfinished-operation `rollback`: global preflight,
  verified private SQLite/config snapshots, durable blocking intent, atomic
  schema/generation/job receipt, config publication, and retained recovery copies.
  Prepared/queued is not a ready-index assertion. Core workers perform inference.
- Same-space hash repair/adoption preserves verified pairs; dimension/document
  space transitions invalidate only the derived index, across all projects.
- Spawn-only reusable FastEmbed child, bounded provider admission, independent
  pipe deadline watchdog, termination/reaping, model-switch replacement and parent
  death cleanup. JSON pipe payloads are bounded; no DB handle is inherited.
- Native effective ceilings: 2 KiB input, 4 KiB batch/padding; HTTP ceilings stay
  unchanged. Larger saved generic settings remain parseable but cannot raise
  native ceilings. Accepted text/hash/space identities do not change.
- Read-only inspection recognizes the fixed Core image sqlite-vec artifact, not
  just the Python wrapper. Docker CI now exercises actual installed-image offline
  recovery, with no model/network, on disposable storage.

## Regression evidence

Focused/base-profile gates include real sqlite-vec with no FastEmbed/bibtexparser
installation. Coverage includes native spawned owner death, peer exclusion,
failed/cancelled open/close, API failed startup, eight real process-death recovery
cases, transaction/config crash windows, restartable rollback, tampered backups,
no rollback after admission reopens, all seven entities, multi-project source
preservation, empty first configuration, legacy reuse and document/query policy.
Native inference tests include child death, cancellation, timeout, model reuse,
model switch and a full request pipe whose child never reads.

- Complete Core after native-ceiling tightening: **3664 passed**, 1 skipped,
  294 deselected, 5 subtests passed (303.62 s); an additional final pass and the
  final PR's full Core/native matrix cover subsequent pipe/image-boundary tests.
- Base focused gate including the new pipe and image-loader tests: **200 passed**,
  32.52 s. No FastEmbed or bibtexparser installed in that base environment.
- Public REST/MCP snapshots unchanged. Isolated startup smoke passes installed
  entrypoint, migrations, Phase-2 locking, REST, MCP, worker, sqlite-vec and web.
- Changed-file Ruff and `git diff --check` pass. Final native matrix is required
  on Linux/macOS/Windows × Python 3.11/3.13, with real sqlite-vec.

JUnit evidence is in `/private/tmp/rka-core-hardening-5OnbMY/`, including
`e-final-core-results.xml`, `e-closeout-core-results.xml`, `e-final-base-results.xml`.
These are local run records, not a substitute for final-revision CI.

## Real model, container and fault evidence

Only new labeled containers/volumes (`org.rka.audit=e-completion-20260907`) were
used. No host ports, production volume, LM Studio, personal model cache or real
research records were used. The initial public model download used a separate
test cache; cached tests ran network-disabled. Core source was mounted read-only
over an existing dependency image, so these are source/runtime acceptance tests,
not published-artifact or all-platform real-model claims.

`scripts/embedding_resource_acceptance.py` seeds #158-scale synthetic distribution:
1747 journal, 687 claims, 507 decisions, 527 literature, 145 missions = **3613**
rows, about 4 MB of text. One journal is intentionally 20,535 bytes and must remain
unembedded/preserved; the other **3612** rows are within the final native ceiling.

1. Nomic 768, API limit 2 GiB / worker 4 GiB, CPUs 1/2. At **1038** stored vectors,
   worker cgroup memory peak was **1,087,803,392 bytes**, OOM counters zero. Injected
   SIGKILL into only that worker; API still returned HTTP 200. Replacement worker
   claimed attempt 2 and advanced beyond the preserved progress to **3612** rows.
   Replacement peak sampled process-tree RSS: **1,158,225,920 bytes**, 508.16 s.
   The full-corpus run started before final tighter caps; its non-poison inputs
   and effective two-row batches fit the final caps unchanged.
2. Old near-8-KiB single-input Nomic boundary **failed** in a 2 GiB container:
   child OOM and EOF, demonstrating byte caps alone were insufficient. Tightened
   native admission to 2/4 KiB. The corrected boundary test rejects the old input
   before inference and successfully embeds a 2034-byte query and two near-limit
   documents: 768 dimensions, **13.56 s**, exit 0, `OOMKilled=false`.
3. Stopped the isolated API and ran the actual CLI on the populated synthetic
   store: **768→384**, generation **1→2**, verified retained backup
   `aacd7c06d8474cfc8b7dbb164d320958`, durable job queued. Restarted API/worker with
   BGE-small and final native limits. All **3612** valid rows embedded in **129.80 s**;
   peak sampled process-tree RSS **338,481,152 bytes**, cgroup peak **324,800,512
   bytes**, all OOM counters zero; API HTTP 200. RSS sum can exceed cgroup charges
   because shared pages are counted per process.
4. Seven-type canonical document projections compared against the pre-transition
   backup have the identical SHA256:
   `2c922f97b2f6620d8d2bc32364104db206fd321cbd6e6d5476a60ebd8414f59c`.
   The poison row remains intact and explicitly pending/failed; the global index
   is not falsely labeled ready. Finite retry/exhaustion is covered separately.
5. Repeated the entire Nomic corpus from an immutable archive of `465cd8c` with
   the final native ceilings and pipe watchdog, network disabled, fresh synthetic
   DB and cached public model: **3612** valid rows, **758.18 s**, sampled process-tree
   RSS peak **1,169,592,320 bytes**, cgroup peak **1,114,886,144 bytes**, all OOM
   counters zero, API peak **116,310,016 bytes** and HTTP 200. The later connection
   lifecycle follow-ups do not change provider/budget/document code. The single
   poison row is explicitly rejected at 2048 bytes. This supersedes relying only
   on the earlier full-corpus run's looser admission configuration.

## Failed runs and corrections

- Initial full regression caught the admission directory creating a DB parent
  with 0755 instead of 0700. Corrected creation order; retained the original test.
- Final adversarial close-order review reproduced close racing with an in-flight
  connect, which could release admission early. A per-instance lifecycle lock
  now serializes connect/close; regressions also reject concurrent duplicate open.
- Maintenance-owned connections retain a counted reference too: simulated close
  failure now prevents even the maintenance gate's context exit from unlocking
  a still-open schema handle. This extends the same fail-closed rule to inspection
  connections used inside maintenance, not only ordinary runtime slots.
- Windows job-log review caught a false-green CI result: pytest's generated ID
  for a 64 KiB malformed config exceeded the Windows environment-variable limit,
  while PowerShell let later successful commands hide that earlier error. Short
  explicit parameter IDs keep the full oversized fixture, and the native matrix
  now uses explicit fail-fast Bash on all OSes, with a workflow regression guard.
  The affected green jobs are not accepted as a passing native gate; the corrected
  revision must pass every command in the matrix again.
- First disposable API used the dependency image's `/app` working directory and
  accidentally imported old source. Stopped only those test containers, retained
  their volume, and reran on a new volume with `/work` and verified module path.
- The 8 KiB native OOM drove the tighter supported ceilings; it is not hidden by
  the successful average-corpus result.
- Offline preflight correctly refused an incomplete vector inspection: the
  image's Python sqlite-vec wrapper failed although its compiled v0.1.6 artifact
  loaded. Added fixed-image-artifact precedence and a real container CI gate.
- The first host startup smoke was sandbox-blocked on loopback bind; it passed
  with the scoped permission required for an ephemeral test port, never 9712.

## Security and operational boundary

The security-review skill drove checks at path/extension, ownership, config,
backup and subprocess boundaries. All data-to-DDL choices are fixed supported
tables with validated dimensions; there is no new remote maintenance endpoint.
Inspection loads only installed code or the fixed Core image extension, never a
path from stored data. Backup/config secrets are private and absent from errors.
Export/report destinations protect the admission/recovery namespace as well.
This is focused evidence, not a complete penetration/dependency audit.

Advisory locks cannot exclude unrelated software or prove an operator stopped
old binaries. The first upgrade requires coordinated stop; unstable aliases,
hard links, non-locking filesystems and unshared container namespaces are not
supported. Native process isolation is not a universal RSS sandbox. The measured
results do not establish memory safety for arbitrary models, tokenizers or
threads. Chunking/truncation and old-release upgrade artifact matrices remain
separate decisions/gates. No production deployment occurs as part of this PR.
