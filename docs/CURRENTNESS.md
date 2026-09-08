# Currentness and reviewed staleness

Core derives currentness from stored signals with one policy. Attribution is
declared provenance, not authentication. A review is not scientific validation.

| Signal | Currentness | Review queue |
| --- | --- | --- |
| No invalidation, green | Current | Closed |
| Yellow warning only | Current with warning | Open |
| Red warning | Not current | Open |
| Structural `stale` / `needs_reprocessing` | Not current | Open unless reviewed |
| Superseded/retracted/inactive lifecycle or expired validity | Not current | Independent of review |
| Reviewed current/dismissed | Current only if no other invalidation | Closed until new signal or aging interval |
| Reviewed historical/retired/superseded/retracted | Not current | Closed until a new signal |

`resolve_stale` records a nonblank rationale, declared actor and optional
same-project journal, closes the freshness flag and returns an audit receipt.
It never clears structural invalidation or changes source lifecycle. Exact
retries reuse the receipt; a different resolution requires reflagging. New
direct or propagated flags clear active review fields but preserve audit history.
Raw `stale=false` updates are rejected; re-distillation is separate from review.

REST: `POST /api/freshness/resolve-stale`. MCP: typed `resolve_stale` and the
deferred adapter. Resolution fields are portable research data; hashes do not
authenticate the actor. No production records are modified by this change.
