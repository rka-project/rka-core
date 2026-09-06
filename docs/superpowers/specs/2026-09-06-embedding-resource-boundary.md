# E1a: bounded embedding calls before durable backfill migration

- Date: 2026-09-06
- Baseline: `f659be0`, isolated `codex/core-audit-hardening` worktree.
- Parent: [approved hardening design](2026-09-05-core-hardening-design.md).
- Scope: resource admission, batch planning, cancellation ownership, oversized-row
  isolation and paged legacy hash inspection. E1b owns durable backfill scheduling.

## Frozen acceptance for this slice

All three built-in backends enforce one shared policy on single/query/document,
batch and connection-test paths. Planning validates the entire logical call
before inference, including template/prefix expansion, preserves order, and never
modifies canonical research text. A rejected input never produces a vector/hash.

| Budget | Default / maximum in this slice |
|---|---:|
| UTF-8 bytes per prepared input | 8192 |
| Inputs in a provider batch | 8 |
| Total UTF-8 bytes per provider batch | 16384 |
| Longest prepared input bytes × batch count | 16384 |
| Inputs in one logical call | 128 |
| Total prepared bytes in one logical call | 262144 |
| Whole logical-call deadline, including retries | 120 seconds |
| Active provider calls per process | 1, no in-memory waiting queue |

Optional `config.resource_limits` can tighten, not disable or raise, these bounds.
Unknown keys, booleans, nonfinite/nonpositive values and inconsistent bounds are
rejected. Existing HTTP `timeout_seconds` remains a per-request timeout; the
logical-call deadline also applies and is no longer multiplied by retries.

These are **byte budgets**, with a byte-based padding proxy. They are not exact
token counts, universal tokenizer bounds, RSS limits or a proof of model safety.
No tokenizer/model download is needed to enforce them. Accepted strings, prefixes,
templates and model identities remain unchanged; admission limits do not change
the document encoding and therefore do not themselves invalidate existing vectors.
Any later chunking/truncation policy needs an encoding revision and identity gate.

Resource rejection and busy admission have stable error codes without source text
or credentials. They do not falsely mark a reachable provider as unavailable.
Queries retain the existing lexical fallback; an unembeddable document remains
pending with an explicit failed backfill result, never falsely complete.

## Cancellation ownership

HTTP calls use one process-local admission slot. Cancellation requests cancellation
of the HTTP task; only actual task completion releases the slot. A bounded deadline
is a caller deadline, not an assertion that a remote server stopped computing.

Native inference uses a dedicated single-thread executor. Admission occurs before
submission. The concurrent future owns slot release until native work truly ends,
even if its asyncio caller times out or is cancelled. There is no request backlog
in the executor. A hung native call keeps the slot busy rather than allowing another
model load/inference to overlap. This cannot kill ONNX or bound its RSS: the separate
Core inference process boundary and supervised recovery remain E1b requirements.

## Backfill and inspection

Backfill preflights each record using the same prepared-input policy. Oversized or
invalid inputs are left untouched, counted in a bounded error summary, and skipped
while valid rows in the same type continue. Existing provider-wide failures still
stop that type; this slice does not retry every row during a provider outage.
Compose failures are failures, not silent successful omissions. Empty eligible text
keeps its existing behavior. Per-row error samples are bounded, not accumulated for
the whole corpus. Database fetch sizes are validated and legacy hash inspection
uses keyset pages instead of loading every stored record into one list.
Backfill's effective fetch cap also respects worst-case prepared-input size
under the configured batch/call budgets (two rows by default). Paging bounds row
count, not the size of an individual pre-existing database record.

## Verification / explicit non-claims

- Model-free transports and a synthetic native model capture actual provider input.
- Reproduce the reported 8 × 20.5 KB input before the patch; after the patch, oversized
  rows never reach inference and later valid rows still persist.
- Verify byte boundaries, multibyte text, prefixes/templates, later-invalid inputs,
  order, batch count/total/padding constraints and factory validation.
- Exercise timeout, cancellation and a second request while native work is still
  running, without live endpoints or model downloads.
- Verify raw records, metadata/vector coupling, healthy-index no-op and paged hashes.
- Run focused tests, full Core profile, base-install tests and startup smoke.

Do not close #158 or all E1 on these tests. API backfill is still in-process;
persistent generation ownership, heartbeat, cancellation/retry state, process loss,
old-database upgrade fixtures, real-model RSS and OS-specific gates remain pending.

References: [Python asyncio cancellation](https://docs.python.org/3/library/asyncio-task.html),
[FastEmbed ONNX adapter](https://github.com/qdrant/fastembed/blob/main/fastembed/text/onnx_embedding.py).
Implementation is checked against the locally locked dependency, not an unpinned
upstream signature alone.
