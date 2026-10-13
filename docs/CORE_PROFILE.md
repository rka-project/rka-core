# RKA Core install and test profile

RKA Core owns the durable research record, provenance, integrity, retrieval,
migrations, and public REST/MCP contracts. It does not require the separately
developed Writer product or the shelved Agentic runtime.

## Dependency boundary

The full local retrieval profile installs:

```bash
python -m pip install -e ".[embeddings,academic,workspace]"
```

The base `rka-core` distribution provides SQLite/FTS retrieval and defaults
embeddings off. `embeddings` adds the local vector-retrieval dependencies `fastembed` and
`sqlite-vec`. The `llm` extra contains only frozen legacy provider-client
compatibility (`litellm` and `instructor`) and is not installed by the Docker
image or Core CI. Docker explicitly installs and enables the embeddings profile.

The production Docker image follows the supported Core profile automatically:

```bash
docker compose build
docker compose up -d
```

Server-side LLM behavior is disabled by default. Connected clients may reason
over retrieved records; Core itself does not need an LLM provider credential.

## Core test gate

Create a clean development environment and run the independently selectable
Core profile:

```bash
python -m venv .venv
.venv/bin/python -m pip install -e ".[embeddings,academic,workspace,dev]"
.venv/bin/python -m pytest -q --tb=short --strict-markers \
  -m "not writer and not agentic"
```

File-level test ownership is maintained by exact path in `tests/ownership.py`
and applied from the repository-level `conftest.py`. A downstream-specific test
inside an otherwise Core-owned module may carry an explicit local marker.
Unmarked tests are Core tests. Frozen Writer/Workbench tests receive the
`writer` marker; frozen Agentic/provider tests receive the `agentic` marker.
This avoids unreliable keyword-based classification and keeps downstream
compatibility tests in history without making them part of the Core release
gate.

At the establishment of this profile, pytest collected 3,269 tests: 2,975 in
Core, 253 in the frozen Writer/Workbench set, and 41 in the frozen Agentic set.
These counts are evidence, not assertions; they may change as tests evolve.

To run every retained compatibility test locally, install both profiles and do
not apply a marker expression:

```bash
.venv/bin/python -m pip install -e ".[embeddings,llm,academic,workspace,dev]"
.venv/bin/python -m pytest -q --tb=short --strict-markers
```

## Startup smoke gate

The deterministic smoke script uses a temporary database and ephemeral port;
it does not touch a live RKA volume or process. It verifies migrations,
sqlite-vec, REST health, the seven-tool MCP stdio surface (including direct
packaged skill reads), one worker pass, and,
when requested, the built dashboard:

```bash
npm --prefix web ci
npm --prefix web run build
.venv/bin/python scripts/core_startup_smoke.py --require-web --require-vec
```

The `rka migrate` command initializes both the base and Phase-2 schemas, so FTS
and vector tables are established consistently with normal REST and worker
startup.

## Retrieval baseline gate

The deterministic, provider-free retrieval baseline uses the fixed synthetic
research corpus and an in-process REST transport. It records direct recall and
latency for journals, claims, decisions, and literature; linked-neighborhood
node/edge/currentness coverage; project isolation; and stale/superseded
behavior:

```bash
.venv/bin/python eval-harness/v3/core_retrieval/runner.py \
  --check --repeats 7 --warmups 1 \
  --output /tmp/rka-core-retrieval.json
```

Its integration test is unmarked and therefore already runs inside the Core
pytest profile. See [CORE_RETRIEVAL_BASELINE.md](CORE_RETRIEVAL_BASELINE.md)
for the thresholds, environment boundary, and interpretation limits.

## Recovery smoke gate

Use [`scripts/core_recovery_smoke.py`](../scripts/core_recovery_smoke.py) only
against a real source through its read-only online-backup boundary. The script
upgrades temporary copies, checks table/ID/link/revision digests, validates
selected knowledge packs under intentional ID re-keying, and restores an exact
rollback copy. The separate pinned-previous-image step remains manual and is
documented in [CORE_RECOVERY.md](CORE_RECOVERY.md); no live migration or
container replacement is implied by this gate.

## CI contract

The `pytest` workflow installs no LLM-provider SDK. Its Core job restores the
frontend bundle produced by the web build, runs the Core marker expression,
and executes the full-profile startup smoke gate. The separate `wheel-smoke`
job builds `rka-core`, installs only that artifact into a fresh environment,
and runs it from outside the checkout with the base feature profile. A Docker
job builds the production image and verifies the dependency boundary inside
it. The existing `pytest` job name is retained for branch-protection
compatibility.
