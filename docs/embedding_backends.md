# Embedding backends (v2.4.0+)

RKA supports three embedding backends; the choice is configurable in the
web UI at **Settings → Embeddings**. Configuration persists at
`/data/embedding_config.json` (file-mode 0600 to protect the optional
`api_key`) and survives `docker compose up -d --build`.

Semantic search is **ON by default** in v2.4.0 with the FastEmbed
baseline. The first-run banner in the web UI points new users at the
Settings page if they want a different backend.

## Backend matrix

| Backend          | Where it runs                         | Default port | Auth       | Required config                |
|------------------|---------------------------------------|--------------|------------|--------------------------------|
| **FastEmbed**    | in-process (ONNX via `fastembed`)     | n/a          | n/a        | `model_name` (default nomic-768) |
| **OpenAI-compat**| any service exposing `/v1/embeddings` | varies       | optional   | `base_url`, `model`, `api_key` (optional), `dim`  |
| **Ollama**       | local Ollama daemon                   | 11434        | none       | `base_url`, `model`, `dim` (auto-detected) |

The OpenAI-compat backend handles many deployment targets: the OpenAI
API itself, LM Studio (`http://host.docker.internal:1234`), vLLM,
Together AI, Anthropic-via-shim. The differentiating field is
`base_url`; `api_key` is optional because LM Studio and several local
proxies don't require auth.

The Ollama backend is intentionally separate: it speaks Ollama's native
`/api/embeddings` (singular `prompt` + singular `{"embedding": [...]}`)
rather than the OpenAI list-wrapped `data: [{"embedding": [...]}]`
shape.

## When to use each

- **FastEmbed** — no external service is required, and it runs offline after
  the model has been cached. The first uncached use downloads roughly 520 MB
  of model data into the persistent FastEmbed cache, so the initial startup
  needs network access or a pre-seeded cache. Its 768-dim vectors are fast and
  accurate for general English text. Best for "I want it to work without
  managing a separate embedding service."
- **OpenAI-compat → a managed local sidecar or LM Studio** — when a local
  service exposes `/v1/embeddings`. Models that distinguish query and
  document inputs can use the optional templates described below.
- **Ollama** — when Ollama is your local model server of choice. Pull
  any embedding model with `ollama pull nomic-embed-text`, then point
  the UI at `http://host.docker.internal:11434`.
- **OpenAI-compat → cloud API** — when you want to use OpenAI's
  `text-embedding-3-small` (1536-dim) or Together's hosted embeddings.
  Set `api_key` to your provider key; the field is stored at file-mode
  0600 and never re-displayed after save.

## Config schema

`/data/embedding_config.json`:

```json
{
  "backend": "openai_compat",
  "config": {
    "base_url": "http://host.docker.internal:1234",
    "model": "text-embedding-qwen3-embedding-8b",
    "api_key": "sk-...",
    "dim": 4096,
    "query_template": "{text}",
    "document_template": "{text}"
  },
  "updated_at": "2026-05-15T16:00:00Z",
  "updated_by": "pi"
}
```

The `backend` field is one of `"fastembed" | "openai_compat" | "ollama"`.
The outer object rejects unknown fields. The backend-specific `config`
subobject is intentionally extensible; recognized fields are validated when
the backend is constructed, while consumers should not assume that every
unknown nested key is rejected.

For a pinned local runtime, `embedding_space_id` may carry the immutable
artifact/runtime identity used for embedding metadata. It must change when
model bytes, quantization, pooling, normalization, dimensions, or document
encoding change. `query_template` and `document_template` each contain exactly
one literal `{text}` placeholder. Query-only instruction changes do not require
document re-indexing; changes to the document template or embedding space do.
See [ADR 0017](adr/0017-portable-embedding-runtime-boundary.md).

<a id="resource-limits-unreleased-hardening"></a>

## Resource limits (Core 3.0.0)

All three built-in backends validate input before loading a model or sending a
request. Single embeddings, queries, batches and connection probes use the same
limits. **Oversized input is rejected, not silently truncated.** Canonical source
text is unchanged. This intentionally tightens compatibility for long documents.

| `config.resource_limits` key | HTTP default/maximum | FastEmbed effective ceiling |
|---|---:|---:|
| `max_input_bytes` | 8192 | 2048 |
| `max_batch_inputs` | 8 | 8 |
| `max_batch_bytes` | 16384 | 4096 |
| `max_padding_bytes` | 16384 | 4096 |
| `max_call_inputs` | 128 | 128 |
| `max_call_bytes` | 262144 | 262144 |
| `call_timeout_seconds` | 120 | 120 |

FastEmbed applies the smaller of the saved limit and its native ceiling. Existing
valid saved generic limits remain loadable but cannot raise these native caps.
A default-model real-inference test showed that the prior 8 KiB native input
ceiling could still OOM a 2 GiB container. The tighter native ceilings reject that
input before inference; no source text is truncated or re-encoded. These are
measured defaults for the supported model, not a memory guarantee for every ONNX
model, tokenizer, thread setting or host.

These are UTF-8 **byte budgets, not token counts or memory limits**. Prefixes and
templates count toward the input limit. Padding cost is conservatively represented
by longest prepared input bytes multiplied by batch size. The complete logical
call is validated first; accepted inputs are then split into ordered batches.
Backfill also caps its database fetch size conservatively, allowing at most two
maximum-sized inputs per fetch with the default budgets. Legacy hash inspection
uses keyset pages of eight rows; it no longer loads the entire corpus at once.

The optional nested object can only tighten these limits. For example, add
`"resource_limits": {"max_input_bytes": 1024, "call_timeout_seconds": 60}` inside
`config`. Limits must be positive, within the maxima above and consistent
(input ≤ batch bytes ≤ call bytes, input ≤ padding bytes, batch count ≤ call count).
Unknown resource-limit keys and booleans are rejected. These advanced settings
are accepted in backend configuration; the web form has no dedicated controls.
Changing admission limits alone does not change the encoding identity or discard
compatible vectors. Same-backend web edits preserve existing advanced config;
switching backend kinds starts a new config, so reapply any tighter limits then.

Only one provider call is admitted **per process**, across backend instances.
There is no in-memory waiting queue. HTTP logical-call deadlines include all
requests and retries, and use the smaller of `timeout_seconds` and
`call_timeout_seconds`. FastEmbed uses one reusable **spawned inference child**
per API/worker process. Model changes replace/reap the child; timeout/cancellation
terminates it before admission becomes reusable. Database descriptors are not
inherited, and the child exits when its parent dies.

- `embedding_input_limit`: a row is too large or a call exceeds its budget.
  Backfill leaves that row pending, reports failure and continues with valid rows.
  Existing durable entity jobs retain their normal finite retry/failed-state
  behavior. Long content remains stored and available to lexical retrieval;
  unchanged oversized input will not become embeddable merely by retrying it.
- `embedding_resource_busy`: another inference is active; retry later. Busy or
  input rejection does not itself mark a reachable provider unavailable.
- `embedding_call_timeout`: the whole-call deadline expired. Core terminates its
  native child; it cannot guarantee a remote HTTP provider stopped computing.
- `embedding_native_failed`: a native child failed to load/infer or exited.
  Durable jobs retain finite retry/exhaustion behavior; the next call can start
  a fresh child. Do not treat this as an instruction to repeatedly restart API.

This is not a hard RSS sandbox or a cross-process inference lock. Container/OS
supervision still supplies memory limits. Startup/config/manual backfills use
the durable worker lifecycle below. Chunking/truncation remains a separate
encoding decision; [offline recovery](embedding-inspection.md#offline-rebuild-interrupted-recovery-and-rollback)
now provides backed-up schema/config transitions. See the
[E1a design](superpowers/specs/2026-09-06-embedding-resource-boundary.md).

<a id="durable-backfill-lifecycle-unreleased-hardening"></a>

## Durable backfill lifecycle (Core 3.0.0)

Startup, **Save configuration**, `POST /api/config/embedding/backfill`, pack imports
and the legacy backfill CLI queue vector work. A separate **`rka worker`** loads the saved backend configuration and
performs these backfills. Docker Compose already starts that worker; an API-only
or foreground installation must start it separately using the same data/config
directory. No worker means the job remains visibly pending, not complete.

Jobs, generation identity, attempt count, backoff, progress and leases live in
SQLite. Settings reads the latest persisted job after a page refresh. A worker
renews its lease during inference; an expired, cancelled or superseded attempt
cannot write vectors or declare completion. A replacement worker resumes via
the missing-row scan and does not recalculate already committed compatible rows.
Progress counts are **per attempt** and may reset on retry. Startup does not
create unlimited new attempts after exhaustion or explicit cancellation.

The existing status URL accepts durable `job_` IDs (older volatile `bf_` IDs are
not recoverable after upgrade). Status keeps the existing `pending`, `running`,
`complete`, `failed` states and adds generation/attempt/lease/backoff fields.
`POST /api/config/embedding/backfill/{job_id}/cancel` invalidates an active lease
and stores `failed` with `error_code=embedding_backfill_cancelled`; it does not
promise to stop an already-running native or remote inference. A new explicit
backfill POST retries without deleting the previous job record or good vectors.

Identical or narrower entity selections in the same project scope and force mode
reuse an active job unless they request a tighter batch cap. A wider/different scope
returns `409 embedding_backfill_busy`; wait for or cancel the active job before
requesting the new scope. Invalid entity types return 422 before enqueueing.
These remain installation-wide operator endpoints, not public demo permissions.

Without a stored dimension, startup stays lexical and does **not** probe a model;
use Settings **Test connection**, then save to detect and persist the dimension.
Deploy API and worker from the same release. No new schema is introduced; the
existing queue is already excluded from research knowledge packs.

Scoped/force/hash-check jobs use a distinct version-2 task type. An E1b worker
rejects it instead of ignoring the project scope; an upgraded worker can resume
the same job while attempts remain. This is fail-closed protection, not support
for mixed-version operation: upgrade API and worker together before queueing work.

Pack imports commit graph rows, strict lexical indexes, a durable import receipt
and the embedding intent atomically. A scope conflict returns 409 and rolls back
the import, including its own staged/published files; wait for or cancel the active
backfill before retrying the upload. There is no in-process import vector loop.
`GET /api/projects/import/status` accepts the receipt's `job_` ID and separates
`lexical_state`, `semantic_state`, `embedding_job_id` and `semantic_ready`.
No backend means `semantic_state=disabled`, never semantic ready. A receipt remains
a historical reference to its linked job; later global repairs do not rewrite it.
Old volatile `imp_` IDs are not recovered. `defer_indexing` remains accepted by the
Python service, but both values now build lexical indexes transactionally and queue
vectors. `index_project` is lexical repair only; its visited count is not a vector count.

`rka backfill-embeddings --project PROJECT_ID` is now enqueue-only. It requires a
saved configuration and compatible initialized generation, does not initialize or
reshape vector tables, and prints a job ID instead of completed counts. Existing
artifact/figure/claim flags and project scope are preserved. The default retains
the legacy content-hash check: the worker repairs changed as well as missing rows,
without re-embedding unchanged compatible inputs. `--batch-size` accepts
1–128 as a cap, further reduced by worker resource limits. `--force` re-embeds the
selected rows in the **current space**, replacing each vector atomically, without
clearing tables. A failed force attempt retains prior good vectors; force retries
may revisit successful rows within the finite attempt budget. The Python
`backfill_embeddings` compatibility helper likewise returns a queued job, not counts.

A scoped job can complete while other projects still lack vectors. It does not
declare the global generation ready; run the normal all-types backfill to fill
remaining gaps. Scoped import status exposes this distinction through
`semantic_ready=false` and `index_state=reindexing`.

This compatibility entrypoint is not the E2 offline space-change command.
For cross-dimension changes use the [offline operator workflow](embedding-inspection.md).
All seven document/hash recipes are shared, and native inference is process-isolated.
Real-model measurements apply only to the measured configuration/corpus, not all
models and memory caps. See the
[E1b design](superpowers/specs/2026-09-06-durable-embedding-backfill.md) and
[entry-point design](superpowers/specs/2026-09-07-backfill-entrypoint-unification.md).

## Switching backends

1. Open the web UI → **Settings** in the sidebar → **Embeddings** card.
2. Pick the new backend from the dropdown. Form fields update to the
   relevant set.
3. Fill in `base_url`, `model`, etc. for the new backend.
4. Click **Test connection**. The result panel shows `ok`, detected
   `dim`, and `latency_ms`. A failed test is non-destructive; the
   previous config stays active.
5. Click **Save & re-embed**. A confirmation modal explains whether the
   change requires missing-row repair, a same-dimension rebuild, or supervised
   offline maintenance.
6. Confirm → backfill starts in the background; a progress bar in the
   Settings page polls every ~1.5 s until the job reaches `complete`
   or `failed`. During a generation rebuild, vector retrieval is withheld and
   the whole search path remains lexical until coverage and consistency checks
   mark the new generation ready.

## Cost of switching

Re-embedding triggers automatically when the stored embedding-space identity,
document template, dimensions, backend, or endpoint model changes. A changed
embedding space forces a clean rebuild even at the same dimension. Repointing
an endpoint backfills missing rows without discarding compatible vectors.
Changing credentials does not invalidate compatible vectors, but it may
schedule a missing-only repair and return 202 so records created during an
authentication outage are not stranded. Existing compatible rows are kept.
This repair does not rescan same-model metadata for changed content; failed
edit jobs remain visible in the durable job queue and must be retried.

A populated index cannot change vector dimension online. Core returns
`409 embedding_offline_reindex_required`; stop API and worker peers, follow
[`rka admin embedding rebuild`](embedding-inspection.md#offline-rebuild-interrupted-recovery-and-rollback),
then restart them for durable worker backfill. Keep the verified recovery copy.

## Troubleshooting

### Upgrade backfill OOM / issue #158

The issue report used development commit `f8db01b`, before the final Core 3.0.0
release (`3425a2b`). Those revisions share a version string but not the same
backfill implementation. Check the installed source/image revision, not only
`rka --version`. Update API, worker and admin CLI together; restarting an old
container does not install the fix. Keep your existing data volume and a verified
backup. Do not delete embedding tables, repeatedly increase memory caps, or
change dimensions by editing the persisted config in place.

The released implementation queues startup work for the worker, isolates native
inference in a child, and enforces the byte/padding budgets above. The API should
remain available while the worker progresses or reports a bounded failure.
Check **Settings → Embeddings** or
`GET /api/config/embedding/backfill/status` for durable attempt/error information.
`rka admin embedding inspect --data-dir /path/to/rka-data --json` checks stored
coverage without running inference; inspection does not certify model readiness.

An `embedding_input_limit` error is not an OOM and is not a completed index:
valid rows can have committed vectors while an oversized row remains unembedded.
The original long text stays intact and searchable lexically. A failed global
generation remains lexical until full coverage/consistency passes; repeated API
restarts do not reset exhausted jobs. Reducing batch size alone cannot admit a
single row larger than the input ceiling. Automatic chunking and truncation are
not part of this fix.

For a 768 → 384 model change, the supported procedure is now
[`rka admin embedding rebuild`](embedding-inspection.md#offline-rebuild-interrupted-recovery-and-rollback),
with all DB/config peers stopped and an explicit target config. The command
retains a verified backup, prepares the new generation and queues worker work.
It does **not** mean all rows were embedded; the same long-input policy still
applies after switching models.

### "Embedding config could not be loaded"

The Settings page surfaces this banner when `/data/embedding_config.json`
is corrupt. Hint: restore from `/data/embedding_config.backup.json` (a
pre-flight backup is written before every save) or remove the live file
and restart — the startup hook writes a fresh DEFAULT_CONFIG when
neither exists.

### "Connection refused" on Test connection (LM Studio / Ollama)

The Docker container reaches the host via `host.docker.internal`. Make
sure your `docker-compose.yml` has `extra_hosts: ["host.docker.internal:
host-gateway"]` (it does by default in v2.4.0). Then verify LM Studio /
Ollama is bound to all interfaces, not just `127.0.0.1`.

### "Dim mismatch: configured=N, server=M"

You typed a dim that doesn't match what the backend returns. Hit **Test
connection** to auto-detect the dim, then save again. This error is the
T2.5 calibration guard — `embed()` raises rather than silently mutating
`self._dim`, which would otherwise produce a confusing
sqlite-vec-side error far from the actual misconfiguration.

### Bind-mount + file-mode 0600 (R15 note)

`/data/embedding_config.json` is written at `0o600` so the optional
`api_key` is owner-readable only. This works as expected on Linux hosts
where `/data` is a true Docker volume (`docker volume create rka-data`).

If you bind-mount your host filesystem at `/data` (Docker Desktop on
macOS or Windows), the host's POSIX permissions model takes precedence
and `chmod 0600` from inside the container may not produce the intended
effect on the host file. Recommended:

```bash
# After first save, on your host (macOS/Linux):
chmod 600 /path/to/host/data/embedding_config.json
```

For containers that run as root with a bind-mounted host folder, the
file lands as root-owned on the host. Either use a Docker volume (the
default) or run with `--user $(id -u):$(id -g)` to keep host ownership
sane.

## LLM-driven features

Q&A and summary generation that used to live in the web UI were removed
in v2.4.0 per the LLM-capability-removal directive
(`jrn_01KRNZBS50K250HHHHEC58E4GC`). Server-side legacy LLM code remains
outside the embedding and Core retrieval contract. Any future generation
product requires its own explicit boundary decision.

`/api/capabilities` no longer returns the `llm` field (BREAKING change
at v2.4.0). Consumers of the `embedding` half of the response are
unaffected.
