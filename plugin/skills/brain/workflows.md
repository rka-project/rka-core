# Brain — Workflows

Procedural reference for the Brain skill. Each section is a self-contained workflow loaded on demand when the top-level `SKILL.md` points here.

> **v2.7.0 dispatch translation.** The legacy tool names used in this file (`rka_get_status`, `rka_add_decision`, `rka_create_mission`, `rka_extract_claims`, `rka_review_cluster`, etc.) are synonyms for `rka_query` / `rka_execute` operations under the v2.7.0+ typed-arg surface. Treat any `rka_X(arg=val, ...)` example as shorthand for the discriminated-union call, e.g.:
>
> | Legacy shorthand | v2.7.0 dispatch shape |
> |---|---|
> | `rka_get_status()` | `rka_query(args={"operation": "status", "project_id": <pinned>})` |
> | `rka_get_changelog(since="...")` | `rka_query(args={"operation": "changelog", "project_id": <pinned>, "filters": {"since": "..."}})` |
> | `rka_get_pending_maintenance()` | `rka_query(args={"operation": "pending_maintenance", "project_id": <pinned>})` |
> | `rka_get_research_map()` | `rka_query(args={"operation": "research_map", "project_id": <pinned>})` |
> | `rka_search(query="...")` | `rka_query(args={"operation": "search", "project_id": <pinned>, "query": "..."})` |
> | `rka_add_note(...)` | `rka_execute(args={"operation": "record_note", "project_id": <pinned>, ...})` |
> | `rka_add_decision(...)` | `rka_execute(args={"operation": "record_decision", "project_id": <pinned>, ...})` |
> | `rka_update_decision(id=..., ...)` | `rka_execute(args={"operation": "update_decision", "project_id": <pinned>, "id": "...", ...})` |
> | `rka_create_mission(...)` | `rka_execute(args={"operation": "create_mission", "project_id": <pinned>, ...})` |
> | `rka_extract_claims(...)` | `rka_execute(args={"operation": "extract_claims", "project_id": <pinned>, ...})` |
> | `rka_review_cluster(...)` | `rka_execute(args={"operation": "review_cluster", "project_id": <pinned>, ...})` |
> | `rka_check_freshness()` | `rka_query(args={"operation": "freshness", "project_id": <pinned>})` |
> | `rka_flag_stale(..., propagate=true)` | `rka_execute(args={"operation": "flag_stale", "project_id": <pinned>, "propagate": True, ...})` |
> | `rka_detect_contradictions(...)` | `rka_query(args={"operation": "contradictions", "project_id": <pinned>, "id": "clm_..."})` |
> | `rka_set_project(...)` | Deprecated no-op; `project_id` is now passed as a required field on every operation. |
>
> Full per-operation signature lookup: `rka_describe(operation="<name>")`. Index of operations: `rka_describe(operation="")`.

---

## Session Start — Full Walkthrough

The `SKILL.md` body lists the 6-step checklist. Here is the expanded worked example.

```
Brain: rka_set_project("prj_01KKQM9JFG67GT5FGWTAHD9YE4")
Brain: rka_get_status()
  → Phase: design, 31 decisions, 145 entries, 5 missions
Brain: rka_get_changelog(since="2026-04-10")
  → 12 new journal entries, 3 new decisions, 2 literature added
Brain: rka_get_pending_maintenance()
  → 12 items: 3 decisions without justified_by, 2 unassigned clusters, 7 entries without tags
Brain: [silently processes the highest-priority items that fit this session]
  - For each decision_without_justified_by: rka_update_decision(id, related_journal=[...])
  - For each unassigned_cluster: rka_review_cluster(id, confidence=..., synthesis=..., research_question_id=...)  # review_cluster requires confidence + synthesis
Brain: rka_get_research_map()
  → 5 RQs, 104 clusters, 549 claims
Brain: "Hi! I've caught up on the project. The research map has 5 active research questions…"
```

**Why the order matters**: changelog before maintenance so the Brain knows what's new before deciding what to fix. Maintenance before the research map so the map view is coherent. For a read-only query, audit, or evaluation, inspect maintenance but do not execute fix-up writes; preserve those findings for a separately authorized pass.

### Cold lifecycle question after catch-up

For a question such as “why did we change this design?” or “what do we now
conclude?”, the session-start snapshot is only orientation. Apply `SKILL.md`'s
cold lifecycle contract:

- recover the framing/basis, predecessor decision, trigger, terminal successor
  or status, execution record, latest conclusion, and latest caveat;
- follow `superseded_by` target by target with a visited-ID guard;
- read `exp_` / `run_` / `obs_` evidence through typed experiment queries, and
  read nested `epv_` / `rue_` / `elc_` / `evr_` records through their parent
  response;
- stop only when the terminal conclusion and caveat are verified or explicitly
  reported absent after a targeted search.

Use no more than 12 project reads in the common case. If the evidence remains
incomplete at that point, answer with the verified partial chain and the exact
missing slots instead of widening indefinitely.

## Research Protocol — Gate 0 (Project Start / Major Pivot)

Before opening a new research direction, the Brain and PI should co-author a Research Protocol as a `directive` journal entry tagged `research-protocol`. This is the contract against which all subsequent decisions, missions, and findings are evaluated.

**When to create a protocol:**
- Starting a new research project.
- Opening a new research question.
- Making a major methodological change.
- Pivoting research direction based on new evidence.

**Template:**

```python
rka_add_note(
    content="""
    # Research Protocol: [Title]

    ## Research Question
    [Precise, testable question]

    ## Scope
    - IN: [what this research covers]
    - OUT: [what is explicitly excluded]

    ## Key Assumptions (numbered)
    1. [Assumption — what we take as given]
    2. [Assumption]
    3. [Assumption]

    ## Success Criteria
    - [What "answered" looks like — specific, testable]
    - [Minimum evidence threshold]

    ## Methodology
    - [Approach: literature review, experiment, prototype, survey, etc.]
    - [Data sources]
    - [Validation method]

    ## Known Risks
    - [Risk 1 and mitigation]
    - [Risk 2 and mitigation]
    """,
    source="pi",
    verbatim_input="[PI's original direction that initiated this protocol]",
    type="directive",
    tags=["research-protocol", "gate-0"],
    provenance={"related_decisions": ["dec_..."]},
)
```

**Periodic protocol review**: when significant results arrive or the research direction feels uncertain, search for `tags:research-protocol` in the current project, re-read, and check whether current work still aligns. If assumptions have been invalidated by evidence, flag with a Confirmation Brief rather than silently adapting.

## Interpretation Staging and Claim Promotion

Journal entries are first distilled into reviewable interpretation candidates.
This staging boundary prevents noisy notes, plans, speculation, and model
inference from silently entering the canonical claim graph.

**What makes a good candidate:**
- ONE atomic statement (not a paragraph).
- An exact source locator (`text_offset`, page, line range, section, fragment, or full record).
- A clear epistemic kind: observation, reported fact, inference, hypothesis, plan, or author intent.
- Scope conditions, uncertainty, and a falsifier when applicable.
- An optional proposed canonical claim type; the proposal is not promotion.

Use `uncertainty=none|low|medium|high|unknown` for interpretation uncertainty.
The later `claim_confidence` records source-grounding fidelity only. Neither
field encodes replication or scientific evidence strength; that belongs in the
separate categorical `evidence_status` review.

**Example extraction:**

Entry: *"The stress test showed 12% packet loss above 400 connections. We used MQTT with QoS 1."*

Candidates:
1. reported fact: `"12% packet loss above 400 connections"`, proposed type `evidence`.
2. reported fact: `"Stress test used MQTT with QoS 1"`, proposed type `method`.

```python
rka_execute(args={
    "operation": "extract_claims",
    "project_id": "prj_01...",
    "entry_id": "jrn_01...",
    "claims": [
        {"claim_type": "evidence", "text": "12% packet loss above 400 connections",
         "epistemic_kind": "reported_fact", "uncertainty": "low"},
        {"claim_type": "method", "text": "Stress test used MQTT with QoS 1",
         "epistemic_kind": "reported_fact", "uncertainty": "none"},
    ],
})
```

The response contains `icd_` IDs, not `clm_` IDs. Inspect each candidate and
its source, then explicitly promote, merge, defer, reject, classify, or request
new evidence. Promotion requires an expected revision, reason, and explicit
grounding confirmation:

```python
rka_query(args={
    "operation": "interpretation_candidates",
    "project_id": "prj_01...",
    "id": "icd_01...",
})
rka_execute(args={
    "operation": "triage_interpretation_candidate",
    "project_id": "prj_01...",
    "id": "icd_01...",
    "action": "promote",
    "expected_revision": 1,
    "actor": "brain",
    "reason": "Checked the exact journal locator and preserved its scope.",
    "grounding_verified": True,
    "claim_confidence": 0.8,
})
```

M1 promotion is journal-backed; literature and artifact interpretations remain
staged until generalized multi-source claim grounding is implemented. A
promotion creates a claim with `evidence_status=unassessed`. Make an explicit
scientific evidence assessment only after inspecting current supporting,
qualifying, and contradictory records:

```python
rka_execute(args={
    "operation": "review_claims",
    "project_id": "prj_01...",
    "claim_ids": ["clm_01..."],
    "action": "adjust",
    "evidence_status": "partially_supported",
})
```

Use `partially_supported` only with the limiting conditions preserved in the
claim or linked qualifiers. Use `inconclusive` when the available record cannot
resolve the proposition, and `contradicted` when current counterevidence wins.
Never bulk-promote unrelated claims merely because they share a source entry.

**Cluster assignment heuristic:**
- Claim fits an existing cluster's theme → assign to it.
- Claim introduces a genuinely new sub-topic → create a new cluster with `rka_create_cluster`.
- Unsure → leave it unassigned for review; never force noisy evidence into the
  closest cluster merely to make the map look complete.
- Use `rka_list_clusters()` to see what exists before deciding.

## Parsing PI Instructions Into Missions

The PI often gives compound instructions that contain multiple independent tasks. Decompose them into separate missions.

**Rule**: one mission = one independent objective. If two tasks could be done in parallel by different Executors, they should be separate missions.

| PI says… | Parse as… | Why |
|---|---|---|
| "Fix the search bug and update the README" | 2 missions | Independent objectives, different scopes. |
| "Fix the search bug — find root cause, write fix, test it" | 1 mission with 3 tasks | Sequential steps toward one objective. |
| "Improve the research map: add details, fix counts, make it interactive" | 1 mission with 3 tasks | All contribute to one objective. |
| "Review the paper draft, also check why imports fail, and update the skills docs" | 3 missions | Three unrelated objectives. |

**Example decomposition.** PI says: *"I need you to fix the knowledge pack import bug, also create a user manual, and while you're at it check if the search indexes claims properly."*

Three missions:
```python
rka_create_mission(
    objective="Fix knowledge pack import FK constraint failure",
    motivated_by_decision="dec_...",  # provenance enforcement
    ...,
)
rka_create_mission(
    objective="Create comprehensive user manual",
    motivated_by_decision="dec_...",  # documentation
    ...,
)
rka_create_mission(
    objective="Verify FTS5 indexes cover claims and clusters",
    motivated_by_decision="dec_...",  # search reliability
    ...,
)
```

Each has its own acceptance criteria, scope, and provenance chain.

**When NOT to split:**
- Tasks are sequential dependencies (step 2 needs step 1's output).
- Tasks share the same files and would create merge conflicts.
- The PI explicitly said "one mission" or "bundle these together."

## Working With the Executor

### Structured Mission Handoff Format

Every mission's `context` field should follow this structure:

- **INTENT** — why this work exists, not just what to do. Reference `motivated_by_decision`.
- **BACKGROUND** — key findings, prior attempts, relevant context. Include journal/decision/literature IDs the Executor should read with `rka_get(id)`.
- **CONSTRAINTS** — what the Executor must NOT do. Be explicit about scope boundaries.
- **ASSUMPTIONS** — what the Executor should take as given without verifying. Number them so the Executor's Backbrief can reference by number.
- **VERIFICATION** — how to verify the work is correct. Specific test commands, expected outputs, acceptance checks.

### Effective Mission Tips

- Always include `motivated_by_decision` so the Executor knows WHY.
- List specific files to investigate in the `context` field.
- Include related journal/decision IDs the Executor should read first.
- Write acceptance criteria as testable assertions, not vague goals.
- Set `scope_boundaries` to prevent scope creep.

### Reviewing the Executor's Backbrief

Before approving the Executor to proceed with significant work, the Executor will present a Backbrief — their plan for how they intend to accomplish the mission. Review against these checks:

1. Does the Executor's plan address ALL tasks in the mission?
2. Does their interpretation of acceptance criteria match your intent?
3. Are their stated assumptions consistent with the mission's numbered assumptions?
4. Do the risks they identify warrant scope changes or additional guidance?

If misalignment exists, correct it NOW — before implementation. A two-minute correction here saves hours of wasted work. If the misalignment is significant, recycle the mission with updated context.

### Reviewing Executor Reports

After `rka_submit_report`:
1. Read the report with `rka_get_report(mission_id)`.
2. Verify each acceptance criterion against live data.
3. Re-read the mission. Confirm every task is terminal (`complete` or an
   explicitly justified `skipped`) and treat any `consistency_warnings` as an
   open closeout defect.
4. Check for anomalies the Executor flagged.
5. Answer any questions the Executor raised.
6. Before endorsing a research conclusion, apply the lifecycle completion gate:
   verify the terminal decision status (and whether it yields an active
   in-force decision), post-report conclusion, and limiting caveat.
7. Accept the closeout or create follow-up missions.

## Research Map Navigation

The map is the three-level hierarchy: RQs → Clusters → Claims. See `architecture.md` for the full structure.

**Reading the map:**

```
rka_get_research_map()
  ● [dec_01...] RQ: How should X work? (7 clusters, 45 claims)
    ├─ [strong] Pipeline Architecture (8 claims) — ecl_01...
    ├─ [moderate] CRSI Methodology (6 claims) — ecl_01...
    └─ [emerging] New Topic (1 claim) — ecl_01...
```

**Advancing research:**
- `emerging` clusters need more evidence → extract claims from relevant entries.
- `moderate` clusters may need verification → review synthesis, check for gaps.
- `contested` clusters have contradictions → resolve with `rka_resolve_contradiction`.
- `strong` clusters are well-established → may be ready to inform decisions.

**Navigation commands:**
- `rka_list_clusters(research_question_id="dec_01...")` — all clusters under an RQ.
- `rka_get(id="ecl_01...")` — cluster detail with synthesis + inline claim summaries.
- `rka_review_cluster(cluster_id="ecl_01...", synthesis="...", confidence="moderate")` — write authoritative synthesis.
- `rka_review_claims(claim_ids=["clm_01..."], action="approve")` — approve
  source grounding only; use `action="adjust", evidence_status="..."` for the
  independent scientific assessment.
- `rka_trace_provenance(entity_id="ecl_01...", direction="upstream")` — see where evidence came from.

### Changelog — Efficient Session Catch-Up

`rka_get_changelog(since="2026-04-10")` returns created and modified entities across journal, decisions, literature, claims, clusters, and missions with counts and short labels. Use it with the Research Map as a cheap structural baseline. It is not a complete lifecycle account: a topic question still requires scoped retrieval, supersession traversal, and terminal conclusion/caveat checks.

## Evidence Assembly — Exporting Research Context

When a downstream client needs scoped research context, use
`rka_assemble_evidence` to produce a provenance-linked structured bundle. Core
does not turn that bundle into publication prose; authoring belongs to a
separately invoked writing client.

```python
rka_assemble_evidence(research_question_id="dec_01...", format="lit_review")
rka_assemble_evidence(research_question_id="dec_01...", format="progress_report")
rka_assemble_evidence(research_question_id="dec_01...", format="proposal_section")
```

Output is a markdown string composed from cluster syntheses, key claims,
decisions, and cited literature. No LLM is involved.

## Cluster Reorganization — Split and Merge

When a cluster grows beyond ~15 claims covering multiple sub-topics, split it:

```python
rka_split_cluster(
    source_id="ecl_01...",
    new_clusters=[
        {"label": "Sub-topic A", "claim_ids": ["clm_01...", "clm_02..."]},
        {"label": "Sub-topic B", "claim_ids": ["clm_03..."]},
    ],
)
```

Claims not listed stay in the source. Provenance links are preserved.

When multiple clusters have 1–2 claims on the same topic, merge them:

```python
rka_merge_clusters(
    source_ids=["ecl_01...", "ecl_02..."],
    target_label="Combined topic",
    target_synthesis="Merged synthesis…",
)
```

Use `rka_get(ecl_...)` to inspect inline claims before deciding how to split.

## Literature Reading Workflow

When processing a paper, use `rka_process_paper` instead of manually creating notes + extracting claims. One call captures all annotations as structured claims:

```python
rka_process_paper(
    lit_id="lit_01...",
    summary="This paper introduces a layered oracle architecture…",
    annotations=[
        {"text": "Table 3 shows 94% detection rate",
         "passage": "Table 3 shows 94% detection rate",
         "note": "Strong evidence for layered approach",
         "claim_type": "evidence", "confidence": 0.85, "cluster_id": "ecl_01..."},
        {"text": "The authors use Docker-in-Docker for isolation",
         "passage": "The authors use Docker-in-Docker for isolation",
         "note": "Same approach we considered",
         "claim_type": "method", "confidence": 0.9},
    ],
)
```

This creates a journal entry with reading notes, extracts one claim per annotation, assigns to clusters if specified, and auto-advances the literature status from `to_read` to `reading`.

## Research Question Advancement

Periodically review the Research Map. When clusters reach `strong`, assess whether the RQ can be advanced:

- **`open`** — default state, actively being investigated.
- **`partially_answered`** — some clusters strong, others still emerging.
- **`answered`** — sufficient evidence; write a conclusion.
- **`reframed`** — the question itself changed based on evidence.
- **`closed`** — no longer relevant.

```python
rka_advance_rq(
    rq_id="dec_01...",
    status="answered",
    conclusion="The evidence supports approach X because…",
    evidence_cluster_ids=["ecl_01...", "ecl_02..."],
)
```

An `answered` RQ with a formal conclusion is a completed research contribution.

## Knowledge Freshness

Knowledge decays. New evidence arrives; existing claims and syntheses become outdated but still appear as current context. Use the freshness tools to detect and manage staleness proactively.

### At Session Start

Run `rka_check_freshness()` alongside `rka_get_pending_maintenance()` to surface stale claims, superseded sources, and aging evidence that needs review.

### After Extracting Claims

Review contradiction candidates in the extraction response. When `rka_extract_claims` creates new claims, check if any conflict with existing knowledge.

### Flagging Stale Items

When new evidence contradicts old claims:

```python
rka_flag_stale(
    entity_id="clm_01...",
    reason="Contradicted by newer experiment in jrn_01...",
    staleness="red",
    propagate=true,
)
```

With `propagate=true`, staleness cascades: stale claim → parent cluster (if >50% claims stale) → decisions citing that cluster.

### Detecting Contradictions

Use `rka_detect_contradictions(entity_id="clm_01...")` to find similar claims that may conflict. The tool surfaces candidates; you decide if they're real contradictions.

### Assumption Tracking

When creating decisions, record assumptions explicitly:

```python
rka_execute(args={
    "operation": "record_decision",
    "project_id": "prj_01...",
    "question": "Should we use MQTT for sensor data?",
    "assumptions": ["Network latency <50ms", "Sensor count stays under 500"],
    # ... other required fields (chosen, rationale, decided_by, kind, related_journal, phase)
})
```

Periodically review assumption health: are recorded assumptions still valid given new evidence?

### Bi-temporal Validity (v2.2)

Migration 018 added `claims.valid_until` and `evidence_clusters.synthesis_valid_until`. Both are NULL by default (= currently valid). When a claim becomes no longer valid (not merely editorially stale), set `valid_until` to the timestamp it was invalidated. This is orthogonal to `staleness` — tri-state staleness is the Brain's editorial overlay; `valid_until` is ground-truth temporal end-of-validity.

## Validation Gates — Catching Errors Early

Gates are formal go/no-go checkpoints at critical transitions. They prevent compounding errors by forcing evaluation before proceeding.

### When to Create Gates

| Gate | When | Who Creates | Who Evaluates |
|---|---|---|---|
| Gate 0: Problem Framing | Before research starts | Brain | Brain + PI |
| Gate 1: Plan Validation | After mission created, before Executor starts | Brain | Brain |
| Gate 2: Evidence Review | After experiments / evidence gathering | Executor | Brain + PI |
| Gate 3: Synthesis Validation | Before committing conclusions | Brain | Brain + PI |

### Creating a Gate

```python
rka_create_gate(
    mission_id="mis_01...",
    gate_type="problem_framing",
    deliverables=["Research Protocol journal entry with tag research-protocol"],
    pass_criteria=[
        "Research question is precise and testable",
        "At least 3 assumptions are identified and numbered",
        "Success criteria are specific enough to evaluate",
    ],
    assumptions_to_verify=["The dataset is available", "The method scales to 1000 entries"],
)
```

### Evaluating a Gate

```python
rka_evaluate_gate(
    gate_id="chk_01...",
    verdict="go",
    notes="Plan is aligned. Assumption #2 verified by checking schema.",
    assumption_status={
        "The dataset is available": "validated",
        "The method scales to 1000 entries": "unvalidated",
    },
)
```

**Verdicts**: `go` (proceed), `kill` (abandon), `hold` (wait), `recycle` (revise). If any assumption is marked `invalidated`, the gate auto-flags the related decision as stale.

## Hook Registration (v2.3 — Mission 2)

The hook system (`dec_01KPJXN5QJ029FC93EK2WRNDFJ`) lets the Brain register notifications that fire on lifecycle events without consuming Brain attention between sessions. It supports five events (`session_start`, `post_journal_create`, `post_claim_extract`, `post_record_outcome`, `periodic`) and one executable handler type, `brain_notify`. Hooks are project-scoped; handler failures are logged to `hook_executions` without aborting the originating operation; the dispatcher caps cascades at depth 3.

### The brain_notify pattern (most useful in v1)

`brain_notify` writes a row to `brain_notifications`. The Brain reads the queue at session start (via `rka_get_brain_notifications`) and acts on the contents itself. This is the structural answer to "Brain forgets to run maintenance" — findings accumulate asynchronously while the Brain isn't present, then surface when it is.

### Unsupported legacy handlers

`sql` and `mcp_tool` cannot be registered or re-enabled (HTTP 422). Existing records remain readable and can be disabled, but dispatch records an error without executing their configuration. The former `mcp_tool` placeholder did not invoke tools; its historical `scheduled=true` logs are not evidence of execution. Use `brain_notify` to surface work, then invoke any downstream operation explicitly through normal MCP access. Hooks do not grant additional permissions.

### Three recommended default hooks

1. **Session-start maintenance nudge** — surfaces a reminder on every fresh project session.
   ```
   rka_add_hook(
     project_id="prj_...",
     event="session_start",
     handler_type="brain_notify",
     handler_config={"severity": "info", "content_template": {
       "reminder": "Run rka_get_pending_maintenance and rka_check_integrity",
       "project": "{project_id}"
     }},
     name="session-start-maintenance",
   )
   ```

2. **Drift watch** — fires after every `rka_record_outcome` with the flattened metric snapshot. The Brain decides whether the surfaced rates warrant follow-up.
   ```
   rka_add_hook(
     project_id="prj_...",
     event="post_record_outcome",
     handler_type="brain_notify",
     handler_config={"severity": "warning", "content_template": {
       "decision": "{decision_id}",
       "outcome": "{outcome}",
       "override_rate": "{override_rate}",
       "brier": "{brier_score}",
       "note": "Consider whether override_rate / brier indicate calibration drift"
     }},
     name="drift-watch",
   )
   ```
   Payload fields available for `{key}` interpolation: `decision_id`, `outcome`, `brier_score`, `ece`, `n_outcomes`, `metrics_available`, `override_rate`, `escape_hatch_rate`, `near_miss_rate`, `qualifying_decisions`, `override_metrics_available`.

3. **Note-creation review reminder** — surface newly created entries for later review. This is a notification, not a replacement for the record service's audit trail.
   ```
   rka_add_hook(
     project_id="prj_...",
     event="post_journal_create",
     handler_type="brain_notify",
     handler_config={"severity": "info", "content_template": {
       "entry": "{entry_id}",
       "type": "{type}",
       "reminder": "Review this entry when resuming the project"
     }},
     name="note-review",
   )
   ```

### Session-start UX

After hooks fire on the first tool call per project per session, brain_notifications accumulate. Best practice: at session start, after `rka_get_pending_maintenance`, also call `rka_get_brain_notifications` and surface a digest to the PI. After acting on findings, call `rka_clear_brain_notifications(ids=[...])` to mark them as processed.

(Legacy note: pre-v2.6, calling `rka_set_project` cleared the session-start fired marker for the new project. v2.6 removed the per-process active-project concept; the fired-marker is now keyed off the `project_id` field passed on each operation.)

### Periodic hooks

Trigger the `periodic` event from the host system on whatever cadence the PI chooses (cron, systemd timer, etc.):
```
rka periodic-hooks                         # all projects
rka periodic-hooks --project-id prj_X      # single project
```

The CLI command opens the DB, fires `periodic` once per project, then exits. Scheduling remains external.

### Auditing

`rka_get_hook_executions(hook_id?, since?, status?)` queries the audit log. Useful filters: `status="error"` for broken hooks, `status="aborted_depth_limit"` for cascade violations, `since="<iso>"` for recent activity.

---

### Not Every Task Needs All 4 Gates

- Quick bug fixes: Gate 1 only (plan validation).
- New research direction: all 4 gates.
- Literature review: Gate 0 (protocol) + Gate 3 (synthesis).
- Experiment: Gate 1 (plan) + Gate 2 (evidence review).

## Related

- Top-level rules, session protocol, anti-patterns: `SKILL.md`.
- Three-actor model, provenance/claim-edge vocabularies, evidence-promotion
  funnel, and research-map structure: `architecture.md`.
- Multi-choice decision UX + Confirmation Brief template: `decision_ux.md`.
- Worked examples for PI attribution and common mistakes: `examples.md`.
