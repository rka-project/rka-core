"""Graph service — entity relationship queries for the research map."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from rka.infra.database import Database
from rka.infra.ids import generate_id
from rka.services.base import BaseService
from rka.services.currentness import CURRENCY_COLUMNS

if TYPE_CHECKING:
    from rka.services.search import SearchService


class GraphService:
    """Queries the entity_links table and related entities to produce
    graph structures for the research map UI and MCP tools."""

    _ENTITY_TABLES: dict[str, str] = {
        # Reuse the write path's canonical link-endpoint registry so graph
        # reads do not silently reject an entity type that add_link accepts.
        **BaseService._LINK_ENTITY_TABLES,
        "summary": "exploration_summaries",
        "event": "events",
        "review": "review_queue",
    }
    _CURRENTNESS_COLUMNS: dict[str, tuple[str, ...]] = {
        "decision": ("status", "superseded_by"),
        "mission": ("status",),
        "journal": ("status", "confidence", "superseded_by"),
        "literature": ("status",),
        "checkpoint": ("status",),
        "claim": ("stale", "valid_from", "valid_until", "staleness"),
        "cluster": (
            "confidence",
            "needs_reprocessing",
            "staleness",
            "synthesis_valid_until",
        ),
        "review": ("status",),
        **CURRENCY_COLUMNS,
    }

    def __init__(self, db: Database):
        self.db = db

    @staticmethod
    def _project_clause(alias: str = "") -> str:
        prefix = f"{alias}." if alias else ""
        return f"{prefix}project_id = ?"

    # ------------------------------------------------------------------
    # Full graph (all entity_links + nodes)
    # ------------------------------------------------------------------

    async def get_full_graph(
        self,
        *,
        project_id: str = "proj_default",
        include_types: list[str] | None = None,
        phase: str | None = None,
        limit: int = 500,
    ) -> dict[str, Any]:
        """Return the full knowledge graph as {nodes, edges}.

        Each node: {id, type, label, status?, phase?, created_at}
        Each edge: {source, target, link_type, created_at}
        """
        nodes: dict[str, dict] = {}
        edges: list[dict] = []

        # Fetch all entity links
        link_rows = await self.db.fetchall(
            "SELECT source_type, source_id, link_type, target_type, target_id, created_at "
            f"FROM entity_links WHERE {self._project_clause()} ORDER BY created_at DESC LIMIT ?",
            [project_id, limit],
        )

        # Collect unique entity IDs we need to look up
        entity_ids: dict[str, set[str]] = {}
        for row in link_rows:
            for prefix in ("source", "target"):
                etype = row[f"{prefix}_type"]
                eid = row[f"{prefix}_id"]
                entity_ids.setdefault(etype, set()).add(eid)
            edges.append(
                {
                    "source": row["source_id"],
                    "target": row["target_id"],
                    "link_type": row["link_type"],
                    "created_at": row["created_at"],
                }
            )

        # Also include entities that have no links yet (orphans)
        for etype, table, label_col, status_col, has_phase in [
            ("decision", "decisions", "question", "status", True),
            ("mission", "missions", "objective", "status", True),
            ("journal", "journal", "content", "confidence", True),
            ("literature", "literature", "title", "status", False),
            ("checkpoint", "checkpoints", "description", "status", False),
            ("claim", "claims", "content", "claim_type", False),
            (
                "interpretation_candidate",
                "interpretation_candidates",
                "statement",
                "review_status",
                False,
            ),
            ("cluster", "evidence_clusters", "label", "confidence", False),
        ]:
            if include_types and etype not in include_types:
                continue
            conditions = []
            params: list = [project_id]
            conditions.append(self._project_clause())
            if phase and has_phase:
                conditions.append("phase = ?")
                params.append(phase)
            where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
            phase_col = "COALESCE(phase, '') as phase" if has_phase else "'' as phase"
            rows = await self.db.fetchall(
                f"SELECT id, {label_col}, {status_col}, "
                f"{phase_col}, created_at "
                f"FROM {table} {where} ORDER BY created_at DESC LIMIT ?",
                params + [limit],
            )
            for r in rows:
                nid = r["id"]
                label_text = r[label_col] or ""
                nodes[nid] = {
                    "id": nid,
                    "type": etype,
                    "label": label_text[:120],
                    "status": r[status_col],
                    "phase": r.get("phase", ""),
                    "created_at": r["created_at"],
                }
                entity_ids.setdefault(etype, set()).add(nid)

        # Fill in any linked nodes that weren't fetched as orphans
        await self._fill_missing_nodes(nodes, entity_ids, project_id=project_id)

        # Filter by type if requested
        if include_types:
            nodes = {k: v for k, v in nodes.items() if v["type"] in include_types}

        # A malformed/imported edge may carry this project's project_id while
        # pointing at a foreign or missing record. Hydration is the ownership
        # boundary: never return an edge whose endpoint did not resolve here.
        valid_ids = set(nodes)
        edges = [
            edge
            for edge in edges
            if edge["source"] in valid_ids and edge["target"] in valid_ids
        ]

        return {"nodes": list(nodes.values()), "edges": edges}

    async def get_graph_view(
        self,
        *,
        project_id: str = "proj_default",
        view: str = "full",
        include_types: list[str] | None = None,
        phase: str | None = None,
        limit: int = 500,
    ) -> dict[str, Any]:
        """Return graph payload for different map modes.

        - full: existing entity graph
        - condensed/keynodes: precomputed keynode view (or auto-build if absent)
        """
        if view == "full":
            return await self.get_full_graph(
                project_id=project_id,
                include_types=include_types,
                phase=phase,
                limit=limit,
            )

        if view not in {"condensed", "keynodes"}:
            raise ValueError("Unsupported graph view. Use one of: full, condensed, keynodes")

        cached = await self.db.fetchone(
            "SELECT nodes, edges, id, created_at FROM graph_views "
            f"WHERE name = ? AND {self._project_clause()} ORDER BY created_at DESC LIMIT 1",
            ["condensed", project_id],
        )
        if not cached:
            await self.refresh_condensed_view(project_id=project_id)
            cached = await self.db.fetchone(
                "SELECT nodes, edges, id, created_at FROM graph_views "
                f"WHERE name = ? AND {self._project_clause()} ORDER BY created_at DESC LIMIT 1",
                ["condensed", project_id],
            )

        if not cached:
            return {"nodes": [], "edges": [], "view": view}

        return {
            "view": "condensed",
            "view_id": cached["id"],
            "created_at": cached["created_at"],
            "nodes": json.loads(cached["nodes"] or "[]"),
            "edges": json.loads(cached["edges"] or "[]"),
        }

    async def refresh_condensed_view(
        self,
        *,
        project_id: str = "proj_default",
        top_per_kind: int = 8,
        min_importance: float = 0.45,
    ) -> dict[str, Any]:
        """Build a condensed keynode-centric graph view and persist it.

        Heuristic ranking is used so this works even without LLM dependencies.
        """
        candidates = await self._collect_keynode_candidates(
            project_id=project_id,
            top_per_kind=top_per_kind,
        )
        selected = [c for c in candidates if c["importance"] >= min_importance]

        nodes: list[dict[str, Any]] = []
        keynode_rows: list[list[Any]] = []
        ref_to_keynode: dict[tuple[str, str], str] = {}

        for item in selected:
            keynode_id = generate_id("keynode")
            node_refs = [
                {
                    "entity_type": item["entity_type"],
                    "entity_id": item["entity_id"],
                }
            ]
            keynode_rows.append(
                [
                    keynode_id,
                    item["kind"],
                    item["title"],
                    item.get("summary"),
                    "system",
                    item["importance"],
                    json.dumps(node_refs),
                    project_id,
                ]
            )
            ref_to_keynode[(item["entity_type"], item["entity_id"])] = keynode_id
            nodes.append(
                {
                    "id": keynode_id,
                    "type": item["kind"],
                    "label": item["title"],
                    "importance": item["importance"],
                    "summary": item.get("summary"),
                    "source": {
                        "entity_type": item["entity_type"],
                        "entity_id": item["entity_id"],
                    },
                }
            )

        edges = await self._build_condensed_edges(
            project_id=project_id,
            ref_to_keynode=ref_to_keynode,
        )

        view_id = generate_id("graphview")
        params = {"top_per_kind": top_per_kind, "min_importance": min_importance}
        async with self.db.transaction():
            await self.db.execute(
                f"DELETE FROM keynodes WHERE blessed = 0 AND {self._project_clause()}",
                [project_id],
            )
            for row in keynode_rows:
                await self.db.execute(
                    """INSERT INTO keynodes
                       (id, kind, title, summary, produced_by, importance,
                        node_refs, blessed, project_id)
                       VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?)""",
                    row,
                )
            await self.db.execute(
                """INSERT INTO graph_views
                   (id, name, params, nodes, edges, project_id)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                [
                    view_id,
                    "condensed",
                    json.dumps(params),
                    json.dumps(nodes),
                    json.dumps(edges),
                    project_id,
                ],
            )
            await self.db.commit()

        return {
            "view": "condensed",
            "view_id": view_id,
            "params": params,
            "nodes": nodes,
            "edges": edges,
        }

    # ------------------------------------------------------------------
    # Ego graph (neighborhood of a single entity)
    # ------------------------------------------------------------------

    async def _attest_project_entities(
        self, entity_ids: list[str], *, project_id: str
    ) -> list[str]:
        """Return only explicit anchors that exist in the requested project.

        Edge queries were already project-scoped, but an unverified foreign or
        nonexistent seed survived as a placeholder node.  That fail-open
        identity signal is misleading even when no foreign content is exposed.
        """
        grouped: dict[str, list[str]] = {}
        for entity_id in dict.fromkeys(entity_ids):
            entity_type = self._guess_type_from_id(entity_id)
            if entity_type in self._ENTITY_TABLES:
                grouped.setdefault(entity_type, []).append(entity_id)

        found: set[str] = set()
        for entity_type, ids in grouped.items():
            table = self._ENTITY_TABLES[entity_type]
            placeholders = ",".join("?" for _ in ids)
            rows = await self.db.fetchall(
                f"SELECT id FROM {table} "
                f"WHERE project_id = ? AND id IN ({placeholders})",
                [project_id, *ids],
            )
            found.update(row["id"] for row in rows)
        return [entity_id for entity_id in entity_ids if entity_id in found]

    async def get_ego_graph(
        self, entity_id: str, depth: int = 1, project_id: str = "proj_default"
    ) -> dict[str, Any]:
        """Return the subgraph centered on entity_id up to `depth` hops."""
        if not await self._attest_project_entities([entity_id], project_id=project_id):
            return {"nodes": [], "edges": []}
        visited: set[str] = set()
        frontier: set[str] = {entity_id}
        all_edges: list[dict] = []

        for _ in range(depth):
            if not frontier:
                break
            placeholders = ",".join("?" for _ in frontier)
            frontier_list = list(frontier)
            rows = await self.db.fetchall(
                f"SELECT source_type, source_id, link_type, target_type, target_id, created_at "
                f"FROM entity_links "
                f"WHERE {self._project_clause()} AND (source_id IN ({placeholders}) OR target_id IN ({placeholders}))",
                [project_id] + frontier_list + frontier_list,
            )
            # Also expand via claim_edges (cluster membership + claim-to-claim relations).
            # Returns no rows when the frontier contains no clm_/ecl_ IDs, so this is safe
            # for non-claim entities.
            # Edge direction: member_of points claim -> cluster (source_claim_id -> cluster_id,
            # target_claim_id is NULL). Other relations (supports/contradicts/qualifies/supersedes)
            # point source_claim_id -> target_claim_id.
            ce_rows = await self.db.fetchall(
                f"SELECT source_claim_id, target_claim_id, cluster_id, relation, created_at "
                f"FROM claim_edges "
                f"WHERE {self._project_clause()} AND ("
                f"source_claim_id IN ({placeholders}) OR "
                f"target_claim_id IN ({placeholders}) OR "
                f"cluster_id IN ({placeholders}))",
                [project_id] + frontier_list + frontier_list + frontier_list,
            )
            visited |= frontier
            next_frontier: set[str] = set()
            for row in rows:
                all_edges.append(
                    {
                        "source": row["source_id"],
                        "target": row["target_id"],
                        "link_type": row["link_type"],
                        "created_at": row["created_at"],
                    }
                )
                for eid in (row["source_id"], row["target_id"]):
                    if eid not in visited:
                        next_frontier.add(eid)
            for row in ce_rows:
                src = row["source_claim_id"]
                # member_of edges point claim -> cluster (target_claim_id is NULL).
                # All other relations point claim -> claim.
                tgt = (
                    row["cluster_id"] if row["relation"] == "member_of" else row["target_claim_id"]
                )
                if not src or not tgt:
                    continue
                all_edges.append(
                    {
                        "source": src,
                        "target": tgt,
                        "link_type": row["relation"],
                        "created_at": row["created_at"],
                    }
                )
                for eid in (src, tgt):
                    if eid not in visited:
                        next_frontier.add(eid)
            frontier = next_frontier

        # Deduplicate edges
        seen = set()
        unique_edges = []
        for e in all_edges:
            key = (e["source"], e["target"], e["link_type"])
            if key not in seen:
                seen.add(key)
                unique_edges.append(e)

        # Collect all node IDs
        node_ids: set[str] = {entity_id}
        for e in unique_edges:
            node_ids.add(e["source"])
            node_ids.add(e["target"])

        entity_ids: dict[str, set[str]] = {}
        for nid in node_ids:
            etype = self._guess_type_from_id(nid)
            entity_ids.setdefault(etype, set()).add(nid)

        nodes: dict[str, dict] = {}
        await self._fill_missing_nodes(nodes, entity_ids, project_id=project_id)
        valid_ids = set(nodes)
        unique_edges = [
            edge
            for edge in unique_edges
            if edge["source"] in valid_ids and edge["target"] in valid_ids
        ]

        return {"nodes": list(nodes.values()), "edges": unique_edges}

    # ------------------------------------------------------------------
    # Multi-hop recursive retrieval — query-anchored ranked subgraph
    # ------------------------------------------------------------------

    # Default per-relation weights for multi-hop traversal. Per Brain decision
    # dec_01KQQRZ0CJHB68P2F6233AHEJ5 (Improvement 2, mis_01KQQS3DYQ2EVJV288PNHX0CMY).
    DEFAULT_EDGE_WEIGHTS: dict[str, float] = {
        # entity_links provenance edges
        "answers": 1.0,  # Cluster→parent-RQ; backfilled by migration 023 from evidence_clusters.research_question_id FK
        "justified_by": 1.0,
        "motivated": 1.0,
        "evidence_for": 1.0,
        "derived_from": 1.0,
        "cites": 0.7,
        "references": 0.7,
        "informed_by": 0.7,
        "produced": 0.5,
        "resolved_as": 0.5,
        "builds_on": 0.5,
        "supersedes": 0.3,
        # claim_edges relations
        "member_of": 1.0,
        "supports": 0.9,
        "qualifies": 0.9,
        "contradicts": 1.1,
    }

    async def multi_hop_retrieval(
        self,
        query: str,
        *,
        seeds: list[str] | None = None,
        max_depth: int = 3,
        max_nodes: int = 50,
        edge_weights: dict[str, float] | None = None,
        project_id: str = "proj_default",
        search_service: "SearchService | None" = None,
    ) -> dict:
        """Query-anchored ranked-subgraph traversal.

        Seeds the traversal with the top hits from `SearchService.search(query)`,
        then BFS-expands via `entity_links` and `claim_edges`. Each node accumulates
        a relevance score = max(parent_score * edge_weight) over incoming edges.
        Result is capped by `max_nodes` and ordered by relevance descending.

        Args:
            query: Natural-language query to seed the traversal.
            seeds: Optional explicit seed entity IDs. If provided, bypasses the
                search step (useful for tests + when caller has anchor entities).
            max_depth: Maximum BFS depth (default 3).
            max_nodes: Maximum nodes returned (default 50). Prevents traversal
                explosion on hub entities.
            edge_weights: Per-relation weights. Falls back to DEFAULT_EDGE_WEIGHTS.
            project_id: Project scope.
            search_service: SearchService to use for seeding (required when
                `seeds` is None).

        Returns: {nodes: [{id, type, label, score, depth}, ...], edges: [...],
                  query: str, seeds: [str]}
        """
        weights = {**self.DEFAULT_EDGE_WEIGHTS, **(edge_weights or {})}

        # Step 1: seed selection
        if seeds is None:
            if search_service is None:
                raise ValueError(
                    "multi_hop_retrieval requires either explicit seeds or a search_service"
                )
            hits = await search_service.with_project(project_id).search(query, limit=10)
            seeds = [h.entity_id for h in hits]
        seeds = await self._attest_project_entities(seeds, project_id=project_id)
        if not seeds:
            return {"nodes": [], "edges": [], "query": query, "seeds": []}

        # Step 2: BFS with relevance accumulation
        # Each node stores (score, depth). A node's score is the max over all
        # paths reaching it; depth is the shortest path that delivered the
        # current best score. Seeds start at score=1.0, depth=0.
        seeds_set: set[str] = set(seeds)
        scores: dict[str, float] = {sid: 1.0 for sid in seeds_set}
        depths: dict[str, int] = {sid: 0 for sid in seeds_set}
        all_edges: list[dict] = []
        edges_seen: set[tuple] = set()

        frontier: set[str] = set(seeds_set)
        for hop in range(max_depth):
            if not frontier or len(scores) >= max_nodes * 2:
                # Cap exploration before truncation; *2 lets us truncate ranked
                # rather than truncating the BFS frontier blindly.
                break
            placeholders = ",".join("?" for _ in frontier)
            frontier_list = list(frontier)

            el_rows = await self.db.fetchall(
                f"""SELECT source_type, source_id, link_type, target_type, target_id, created_at
                    FROM entity_links
                    WHERE {self._project_clause()}
                      AND (source_id IN ({placeholders}) OR target_id IN ({placeholders}))""",
                [project_id] + frontier_list + frontier_list,
            )
            ce_rows = await self.db.fetchall(
                f"""SELECT source_claim_id, target_claim_id, cluster_id, relation, created_at
                    FROM claim_edges
                    WHERE {self._project_clause()} AND (
                        source_claim_id IN ({placeholders})
                        OR target_claim_id IN ({placeholders})
                        OR cluster_id IN ({placeholders}))""",
                [project_id] + frontier_list + frontier_list + frontier_list,
            )

            next_frontier: set[str] = set()

            for row in el_rows:
                src, tgt, link = row["source_id"], row["target_id"], row["link_type"]
                w = weights.get(link, 0.5)
                edge_key = (src, tgt, link)
                if edge_key not in edges_seen:
                    edges_seen.add(edge_key)
                    all_edges.append(
                        {
                            "source": src,
                            "target": tgt,
                            "link_type": link,
                            "weight": w,
                            "created_at": row["created_at"],
                        }
                    )
                # Propagate score across the edge (both directions count for
                # relevance; an edge from a high-score seed to a neighbor
                # makes that neighbor relevant).
                for parent, child in ((src, tgt), (tgt, src)):
                    parent_score = scores.get(parent)
                    if parent_score is None:
                        continue
                    new_score = parent_score * w
                    if child not in scores or new_score > scores[child]:
                        scores[child] = new_score
                        depths[child] = hop + 1
                        if child not in frontier and child not in seeds_set:
                            next_frontier.add(child)

            for row in ce_rows:
                src = row["source_claim_id"]
                relation = row["relation"]
                tgt = row["cluster_id"] if relation == "member_of" else row["target_claim_id"]
                if not src or not tgt:
                    continue
                w = weights.get(relation, 0.5)
                edge_key = (src, tgt, relation)
                if edge_key not in edges_seen:
                    edges_seen.add(edge_key)
                    all_edges.append(
                        {
                            "source": src,
                            "target": tgt,
                            "link_type": relation,
                            "weight": w,
                            "created_at": row["created_at"],
                        }
                    )
                for parent, child in ((src, tgt), (tgt, src)):
                    parent_score = scores.get(parent)
                    if parent_score is None:
                        continue
                    new_score = parent_score * w
                    if child not in scores or new_score > scores[child]:
                        scores[child] = new_score
                        depths[child] = hop + 1
                        next_frontier.add(child)

            frontier = next_frontier

        # Step 3: rank and cap — with seed protection. Expansion neighbors
        # can out-score seeds (e.g. contradicts edges at weight 1.1, or a
        # node reached from several seeds), so a plain top-N cut can evict
        # the directly-relevant seeds the traversal started from; eval-v3
        # measured paragraph-seeded multi_hop scoring BELOW flat search for
        # exactly this reason. Seeds are exempt from the cap (same
        # anchor_aware UNION pattern as the context engine and
        # collect_report_context); expansion nodes fill the remaining
        # budget. Final order remains score-descending.
        expansion_ranked = sorted(
            (nid for nid in scores if nid not in seeds_set),
            key=lambda nid: scores[nid],
            reverse=True,
        )
        budget = max(max_nodes - len(seeds_set), 0)
        ranked_ids = sorted(
            list(seeds_set) + expansion_ranked[:budget],
            key=lambda nid: scores[nid],
            reverse=True,
        )
        # Hydrate node metadata
        node_ids: dict[str, set[str]] = {}
        for nid in ranked_ids:
            etype = self._guess_type_from_id(nid)
            node_ids.setdefault(etype, set()).add(nid)
        nodes: dict[str, dict] = {}
        await self._fill_missing_nodes(nodes, node_ids, project_id=project_id)
        ranked_ids = [node_id for node_id in ranked_ids if node_id in nodes]
        ranked_set = set(ranked_ids)

        result_nodes = []
        for nid in ranked_ids:
            n = nodes[nid]
            n["score"] = scores[nid]
            n["depth"] = depths[nid]
            result_nodes.append(n)

        # Filter edges to those connecting two ranked nodes (drops noise)
        result_edges = [
            e for e in all_edges if e["source"] in ranked_set and e["target"] in ranked_set
        ]

        return {
            "nodes": result_nodes,
            "edges": result_edges,
            "query": query,
            "seeds": list(seeds),
        }

    # Stopwords stripped from a report description when no angle_queries are
    # provided. Deliberately small: only request-framing vocabulary, not a
    # general English list — domain terms must survive.
    _REPORT_STOPWORDS: frozenset = frozenset(
        "i want to write a report on about the and of in for with how its any "
        "that were what came out it needed was along way which where when who "
        "this these those they them their there is are be been being should "
        "could would will can may might must have has had do does did".split()
    )

    async def collect_report_context(
        self,
        description: str,
        *,
        angle_queries: list[str] | None = None,
        max_depth: int = 2,
        max_nodes: int = 60,
        seed_limit: int = 8,
        edge_weights: dict[str, float] | None = None,
        project_id: str = "proj_default",
        search_service: "SearchService | None" = None,
    ) -> dict:
        """Assemble the node set relevant to a report described in prose.

        Composite retrieval per the eval-v3 report-context findings: one-shot
        paragraph search reached 0.32 mean cohort recall while an agent loop
        (angle queries + graph expansion + verification) reached 0.80. This
        operation runs that loop's mechanical core server-side:

        1. Seed from EVERY angle query (caller-provided short queries; the
           normalized description is always added as one more angle). Seed
           score reflects best search rank across angles.
        2. BFS-expand seeds through ``entity_links`` + ``claim_edges`` with
           provenance-weighted edges (same weights as multi_hop_retrieval).
        3. Seed protection: seeds are never displaced by expansion neighbors
           (the anchor_aware UNION pattern from the context engine — without
           it, paragraph-seeded multi_hop scored BELOW flat search).
        4. Every returned node carries ``included_via`` — either the angle
           query + rank that surfaced it, or the parent node + link type that
           reached it — so the consumer (Writer, Brain) can audit the bundle.

        Args:
            description: The PI's prose description of the report scope.
            angle_queries: Short (1–4 word) seed queries decomposing the
                description into search angles. STRONGLY recommended — the
                caller (an LLM) decomposes far better than stopword
                stripping. When omitted, the description is keyword-
                normalized and used as the only angle.
            max_depth: BFS expansion depth (default 2; report scopes are
                link-dense, depth 3 mostly adds noise).
            max_nodes: Result cap (default 60). Seeds are exempt.
            seed_limit: Search hits taken per angle query (default 8).
            edge_weights: Per-relation overrides; falls back to
                DEFAULT_EDGE_WEIGHTS.
            project_id: Project scope.
            search_service: Required — seeds always come from search.

        Returns: {nodes: [{id, type, label, score, depth, included_via,
                  tags}, ...], queries: [...], seed_count, expanded_count,
                  truncated}
        """
        if search_service is None:
            raise ValueError("collect_report_context requires a search_service")
        weights = {**self.DEFAULT_EDGE_WEIGHTS, **(edge_weights or {})}

        # Step 1: build the angle-query list. The normalized description is
        # always appended as a recall backstop behind the caller's angles.
        import re as _re

        desc_terms = [
            t
            for t in _re.findall(r"[a-zA-Z0-9][a-zA-Z0-9_-]*", description.lower())
            if t not in self._REPORT_STOPWORDS and (len(t) > 2 or t.isdigit())
        ]
        queries: list[str] = []
        for q in angle_queries or []:
            q = q.strip()
            if q and q.lower() not in {x.lower() for x in queries}:
                queries.append(q)
        if desc_terms:
            norm = " ".join(desc_terms)
            if norm.lower() not in {x.lower() for x in queries}:
                queries.append(norm)
        if not queries:
            return {
                "nodes": [],
                "queries": [],
                "seed_count": 0,
                "expanded_count": 0,
                "truncated": False,
            }

        # Step 2: seed from every angle. Seed score = best (highest) rank
        # score across angles; rank 0 → 1.0, decaying to 0.5 at seed_limit.
        scoped_search = search_service.with_project(project_id)
        scores: dict[str, float] = {}
        depths: dict[str, int] = {}
        included_via: dict[str, dict] = {}
        for q in queries:
            hits = await scoped_search.search(q, limit=seed_limit)
            for rank, h in enumerate(hits):
                s = 1.0 - (rank / (2 * max(seed_limit, 1)))
                if h.entity_id not in scores or s > scores[h.entity_id]:
                    scores[h.entity_id] = s
                    depths[h.entity_id] = 0
                    included_via[h.entity_id] = {
                        "via": "search",
                        "query": q,
                        "rank": rank,
                    }
        seeds_set = set(scores.keys())
        if not seeds_set:
            return {
                "nodes": [],
                "queries": queries,
                "seed_count": 0,
                "expanded_count": 0,
                "truncated": False,
            }

        # Step 3: BFS expansion (same edge sources as multi_hop_retrieval,
        # plus inclusion-provenance tracking on every score improvement).
        frontier: set[str] = set(seeds_set)
        for hop in range(max_depth):
            if not frontier or len(scores) >= max_nodes * 3:
                break
            placeholders = ",".join("?" for _ in frontier)
            frontier_list = list(frontier)

            el_rows = await self.db.fetchall(
                f"""SELECT source_id, link_type, target_id
                    FROM entity_links
                    WHERE {self._project_clause()}
                      AND (source_id IN ({placeholders}) OR target_id IN ({placeholders}))""",
                [project_id] + frontier_list + frontier_list,
            )
            ce_rows = await self.db.fetchall(
                f"""SELECT source_claim_id, target_claim_id, cluster_id, relation
                    FROM claim_edges
                    WHERE {self._project_clause()} AND (
                        source_claim_id IN ({placeholders})
                        OR target_claim_id IN ({placeholders})
                        OR cluster_id IN ({placeholders}))""",
                [project_id] + frontier_list + frontier_list + frontier_list,
            )

            next_frontier: set[str] = set()

            def _propagate(src: str, tgt: str, link: str) -> None:
                w = weights.get(link, 0.5)
                for parent, child in ((src, tgt), (tgt, src)):
                    parent_score = scores.get(parent)
                    if parent_score is None:
                        continue
                    new_score = parent_score * w
                    if child not in scores or new_score > scores[child]:
                        # Seeds keep their search provenance + depth 0.
                        if child in seeds_set:
                            continue
                        scores[child] = new_score
                        depths[child] = hop + 1
                        included_via[child] = {
                            "via": "link",
                            "from": parent,
                            "link_type": link,
                        }
                        next_frontier.add(child)

            for row in el_rows:
                _propagate(row["source_id"], row["target_id"], row["link_type"])
            for row in ce_rows:
                src = row["source_claim_id"]
                relation = row["relation"]
                tgt = row["cluster_id"] if relation == "member_of" else row["target_claim_id"]
                if src and tgt:
                    _propagate(src, tgt, relation)

            frontier = next_frontier

        # Step 4: rank with seed protection — all seeds survive, expansion
        # nodes fill the remaining budget by score.
        expansion_ranked = sorted(
            (nid for nid in scores if nid not in seeds_set),
            key=lambda nid: scores[nid],
            reverse=True,
        )
        budget = max(max_nodes - len(seeds_set), 0)
        truncated = len(expansion_ranked) > budget
        kept = (
            sorted(seeds_set, key=lambda nid: scores[nid], reverse=True) + expansion_ranked[:budget]
        )

        # Step 5: hydrate metadata + tags.
        node_ids: dict[str, set[str]] = {}
        for nid in kept:
            node_ids.setdefault(self._guess_type_from_id(nid), set()).add(nid)
        nodes: dict[str, dict] = {}
        await self._fill_missing_nodes(nodes, node_ids, project_id=project_id)
        kept = [node_id for node_id in kept if node_id in nodes]
        seeds_set.intersection_update(kept)

        tags_by_id: dict[str, list[str]] = {}
        if kept:
            placeholders = ",".join("?" for _ in kept)
            project_clause = "project_id = ?"
            for row in await self.db.fetchall(
                f"SELECT entity_id, tag FROM tags "
                f"WHERE {project_clause} "
                f"AND entity_id IN ({placeholders})",
                [project_id, *kept],
            ):
                tags_by_id.setdefault(row["entity_id"], []).append(row["tag"])

        result_nodes = []
        for nid in kept:
            n = nodes[nid]
            n["score"] = round(scores[nid], 4)
            n["depth"] = depths[nid]
            n["included_via"] = included_via[nid]
            n["tags"] = tags_by_id.get(nid, [])
            result_nodes.append(n)

        return {
            "nodes": result_nodes,
            "queries": queries,
            "seed_count": len(seeds_set),
            "expanded_count": len(kept) - len(seeds_set),
            "truncated": truncated,
        }

    # ------------------------------------------------------------------
    # Decision tree (hierarchical)
    # ------------------------------------------------------------------

    async def get_decision_tree(
        self, root_id: str | None = None, project_id: str = "proj_default"
    ) -> list[dict]:
        """Return decisions as a tree structure.

        If root_id is given, return only that subtree.
        Each node: {id, question, chosen, status, phase, children: [...], linked_entities: [...]}
        """
        rows = await self.db.fetchall(
            "SELECT id, parent_id, question, chosen, status, phase, rationale, "
            "decided_by, related_missions, related_literature, created_at "
            f"FROM decisions WHERE {self._project_clause()} ORDER BY created_at",
            [project_id],
        )

        # Build lookup
        by_id: dict[str, dict] = {}
        for r in rows:
            by_id[r["id"]] = {
                "id": r["id"],
                "parent_id": r["parent_id"],
                "question": r["question"],
                "chosen": r["chosen"],
                "status": r["status"],
                "phase": r["phase"],
                "rationale": r["rationale"],
                "decided_by": r.get("decided_by"),
                "created_at": r["created_at"],
                "children": [],
                "linked_entities": [],
            }

        # Fetch entity_links for decisions
        dec_ids = list(by_id.keys())
        if dec_ids:
            placeholders = ",".join("?" for _ in dec_ids)
            links = await self.db.fetchall(
                f"SELECT source_type, source_id, link_type, target_type, target_id "
                f"FROM entity_links "
                f"WHERE {self._project_clause()} AND (source_id IN ({placeholders}) OR target_id IN ({placeholders}))",
                [project_id] + dec_ids + dec_ids,
            )
            for link in links:
                for dec_id in dec_ids:
                    if link["source_id"] == dec_id or link["target_id"] == dec_id:
                        other_id = (
                            link["target_id"] if link["source_id"] == dec_id else link["source_id"]
                        )
                        other_type = (
                            link["target_type"]
                            if link["source_id"] == dec_id
                            else link["source_type"]
                        )
                        if dec_id in by_id:
                            by_id[dec_id]["linked_entities"].append(
                                {
                                    "id": other_id,
                                    "type": other_type,
                                    "link_type": link["link_type"],
                                }
                            )

        # Build tree
        roots: list[dict] = []
        for node in by_id.values():
            pid = node["parent_id"]
            if pid and pid in by_id:
                by_id[pid]["children"].append(node)
            else:
                roots.append(node)

        if root_id and root_id in by_id:
            return [by_id[root_id]]
        return roots

    # ------------------------------------------------------------------
    # Timeline (events + entity_links combined)
    # ------------------------------------------------------------------

    async def get_timeline(
        self,
        project_id: str = "proj_default",
        phase: str | None = None,
        since: str | None = None,
        limit: int = 100,
    ) -> list[dict]:
        """Return a chronological timeline of events with linked context."""
        conditions = [self._project_clause()]
        params: list = [project_id]
        if phase:
            conditions.append("phase = ?")
            params.append(phase)
        if since:
            conditions.append("timestamp >= ?")
            params.append(since)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        params.append(limit)

        rows = await self.db.fetchall(
            f"SELECT id, timestamp, event_type, entity_type, entity_id, actor, "
            f"summary, caused_by_event, phase "
            f"FROM events {where} ORDER BY timestamp DESC LIMIT ?",
            params,
        )
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    async def get_stats(self, project_id: str = "proj_default") -> dict[str, Any]:
        """Return graph statistics: node/edge counts by type."""
        node_counts = {}
        for etype, table in [
            ("decision", "decisions"),
            ("mission", "missions"),
            ("journal", "journal"),
            ("literature", "literature"),
            ("checkpoint", "checkpoints"),
            ("claim", "claims"),
            ("interpretation_candidate", "interpretation_candidates"),
            ("cluster", "evidence_clusters"),
        ]:
            row = await self.db.fetchone(
                f"SELECT COUNT(*) as cnt FROM {table} WHERE {self._project_clause()}",
                [project_id],
            )
            node_counts[etype] = row["cnt"] if row else 0

        edge_row = await self.db.fetchone(
            f"SELECT COUNT(*) as cnt FROM entity_links WHERE {self._project_clause()}",
            [project_id],
        )
        total_edges = edge_row["cnt"] if edge_row else 0

        edge_type_rows = await self.db.fetchall(
            f"SELECT link_type, COUNT(*) as cnt FROM entity_links WHERE {self._project_clause()} GROUP BY link_type",
            [project_id],
        )
        edge_counts = {r["link_type"]: r["cnt"] for r in edge_type_rows}

        # Canonical claim scope is metadata on claim nodes, not a competing
        # graph node type. Count the derived readiness projection separately.
        from rka.services.claims import ClaimService

        scope_readiness_counts: dict[str, int] = {}
        for claim in await ClaimService(
            self.db,
            project_id=project_id,
        ).list(limit=1_000_000):
            scope_readiness_counts[claim.scope_readiness] = (
                scope_readiness_counts.get(claim.scope_readiness, 0) + 1
            )

        return {
            "node_counts": node_counts,
            "total_nodes": sum(node_counts.values()),
            "total_edges": total_edges,
            "edge_counts_by_type": edge_counts,
            "claim_scope_readiness_counts": scope_readiness_counts,
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _fill_missing_nodes(
        self,
        nodes: dict[str, dict],
        entity_ids: dict[str, set[str]],
        project_id: str = "proj_default",
    ) -> None:
        """Look up entity metadata for IDs not already in the nodes dict."""
        # Affordance B (Mission B): for clusters, additionally fetch
        # needs_reprocessing so the multi-hop label gets the STALE prefix.
        from rka.services.rendering import with_staleness_prefix

        table_map: dict[str, tuple[str, str, str, bool]] = {
            "decision": ("decisions", "question", "status", True),
            "mission": ("missions", "objective", "status", True),
            "journal": ("journal", "content", "confidence", True),
            "literature": ("literature", "title", "status", False),
            "checkpoint": ("checkpoints", "description", "status", False),
            "claim": ("claims", "content", "claim_type", False),
            "experiment_observation": (
                "experiment_observations",
                "summary",
                "direction",
                False,
            ),
            "interpretation_candidate": (
                "interpretation_candidates",
                "statement",
                "review_status",
                False,
            ),
            "cluster": ("evidence_clusters", "label", "confidence", False),
        }
        for etype, ids in entity_ids.items():
            missing = [eid for eid in ids if eid not in nodes]
            if not missing:
                continue
            info = table_map.get(etype)
            if not info:
                attested = set(
                    await self._attest_project_entities(missing, project_id=project_id)
                )
                for eid in missing:
                    if eid not in attested:
                        continue
                    nodes[eid] = {
                        "id": eid,
                        "type": etype,
                        "label": eid,
                        "status": None,
                        "phase": "",
                        "created_at": "",
                    }
                continue
            table, label_col, status_col, has_phase = info
            placeholders = ",".join("?" for _ in missing)
            phase_col = "COALESCE(phase, '') as phase" if has_phase else "'' as phase"
            extra_cols = ", needs_reprocessing" if etype == "cluster" else ""
            rows = await self.db.fetchall(
                f"SELECT id, {label_col}, {status_col}, "
                f"{phase_col}, created_at{extra_cols} "
                f"FROM {table} WHERE {self._project_clause()} AND id IN ({placeholders})",
                [project_id] + missing,
            )
            for r in rows:
                label_text = r[label_col] or ""
                if etype == "cluster":
                    # Apply STALE prefix BEFORE the 120-char truncation so
                    # the prefix isn't truncated off long cluster labels.
                    label_text = (
                        with_staleness_prefix(label_text, r.get("needs_reprocessing")) or ""
                    )
                nodes[r["id"]] = {
                    "id": r["id"],
                    "type": etype,
                    "label": label_text[:120],
                    "status": r[status_col],
                    "phase": r.get("phase", ""),
                    "created_at": r["created_at"],
                }
        await self._augment_claim_scope_nodes(nodes, project_id)
        await self._augment_node_currentness(nodes, project_id)

    async def _augment_node_currentness(
        self,
        nodes: dict[str, dict],
        project_id: str,
    ) -> None:
        """Attach the resolver's canonical lifecycle projection to graph nodes.

        Graph labels historically overloaded ``status`` with confidence or
        claim type.  Preserve that display field for compatibility and expose
        lifecycle state separately so a linked-neighborhood consumer can
        distinguish current, stale, superseded, and unresolved records.
        """
        if not nodes:
            return
        # Reuse the resolver's pure lifecycle rule without invoking the full
        # entity-resolution packet (tags, normalized records, hashes, and
        # contradiction closure). Graph reads need only a few lifecycle
        # columns and should not hold a long read transaction over large text.
        from rka.services.entity_resolver import _currentness

        by_type: dict[str, list[str]] = {}
        for entity_id, node in nodes.items():
            entity_type = node.get("type") or self._guess_type_from_id(entity_id)
            if entity_type in self._ENTITY_TABLES:
                by_type.setdefault(entity_type, []).append(entity_id)

        as_of = datetime.now(timezone.utc)
        for entity_type, ids in by_type.items():
            table = self._ENTITY_TABLES[entity_type]
            columns = self._CURRENTNESS_COLUMNS.get(entity_type, ())
            placeholders = ",".join("?" for _ in ids)
            select = ", ".join(("id", *columns))
            rows = await self.db.fetchall(
                f"SELECT {select} FROM {table} "
                f"WHERE project_id = ? AND id IN ({placeholders})",
                [project_id, *ids],
            )
            for row in rows:
                record = dict(row)
                node = nodes[row["id"]]
                node["currentness"] = _currentness(record, as_of=as_of)
                node["lifecycle_status"] = record.get("status")
                node["superseded_by"] = record.get("superseded_by")
                node["stale"] = (
                    bool(record.get("stale")) if "stale" in record else None
                )

    async def _augment_claim_scope_nodes(
        self,
        nodes: dict[str, dict],
        project_id: str,
    ) -> None:
        """Attach the current canonical scope projection to claim nodes."""
        claim_ids = sorted(
            node_id for node_id, node in nodes.items() if node.get("type") == "claim"
        )
        if not claim_ids:
            return

        from rka.services.claims import ClaimService

        placeholders = ",".join("?" for _ in claim_ids)
        rows = await self.db.fetchall(
            f"""SELECT c.*, {ClaimService._CONTRADICTED_PROJECTION},
                       {ClaimService._SCOPE_PROJECTION}
                FROM claims AS c
                {ClaimService._SCOPE_JOIN}
                WHERE c.project_id = ? AND c.id IN ({placeholders})""",
            [project_id, *claim_ids],
        )
        for row in rows:
            claim = ClaimService._row_to_model(row)
            node = nodes.get(claim.id)
            if node is None:
                continue
            node["scope_revision"] = claim.scope_revision
            node["scope_readiness"] = claim.scope_readiness
            node["scope_findings"] = [
                finding.model_dump(mode="json") for finding in claim.scope_findings
            ]
            node["scope_contract"] = (
                claim.scope_contract.model_dump(mode="json") if claim.scope_contract else None
            )

    async def _collect_keynode_candidates(
        self,
        *,
        project_id: str = "proj_default",
        top_per_kind: int,
    ) -> list[dict[str, Any]]:
        """Collect and score likely key nodes for condensed graph mode."""
        candidates: list[dict[str, Any]] = []

        journal_rows = await self.db.fetchall(
            """SELECT id, type, content, summary, confidence, created_at
               FROM journal
               WHERE project_id = ?
                 AND type IN ('finding', 'insight', 'hypothesis', 'methodology', 'summary')
               ORDER BY created_at DESC
               LIMIT ?""",
            [project_id, top_per_kind * 6],
        )
        confidence_bonus = {"verified": 0.28, "tested": 0.18, "hypothesis": 0.10}
        for row in journal_rows:
            imp = 0.48 + confidence_bonus.get((row.get("confidence") or "").lower(), 0.05)
            candidates.append(
                {
                    "kind": "finding",
                    "entity_type": "journal",
                    "entity_id": row["id"],
                    "title": (row.get("summary") or row.get("content") or "Finding")[:90],
                    "summary": (row.get("summary") or row.get("content") or "")[:240],
                    "importance": min(1.0, imp),
                }
            )

        lit_rows = await self.db.fetchall(
            """SELECT id, title, abstract, status, relevance_score, created_at
               FROM literature
               WHERE project_id = ?
               ORDER BY created_at DESC
               LIMIT ?""",
            [project_id, top_per_kind * 8],
        )
        status_bonus = {"cited": 0.30, "read": 0.22, "reading": 0.12, "to_read": 0.05}
        for row in lit_rows:
            rel = float(row.get("relevance_score") or 0.0)
            base = 0.40 + status_bonus.get((row.get("status") or "").lower(), 0.04)
            imp = min(1.0, base + max(0.0, min(rel, 1.0)) * 0.25)
            candidates.append(
                {
                    "kind": "literature",
                    "entity_type": "literature",
                    "entity_id": row["id"],
                    "title": (row.get("title") or "Literature")[:90],
                    "summary": (row.get("abstract") or "")[:240],
                    "importance": imp,
                }
            )

        dec_rows = await self.db.fetchall(
            """SELECT id, question, rationale, status, created_at
               FROM decisions
               WHERE project_id = ?
               ORDER BY created_at DESC
               LIMIT ?""",
            [project_id, top_per_kind * 5],
        )
        dec_bonus = {
            "active": 0.30,
            "merged": 0.20,
            "revisit": 0.16,
            "superseded": 0.10,
            "abandoned": 0.08,
        }
        for row in dec_rows:
            imp = 0.52 + dec_bonus.get((row.get("status") or "").lower(), 0.1)
            candidates.append(
                {
                    "kind": "decision",
                    "entity_type": "decision",
                    "entity_id": row["id"],
                    "title": (row.get("question") or "Decision")[:90],
                    "summary": (row.get("rationale") or "")[:240],
                    "importance": min(1.0, imp),
                }
            )

        mission_rows = await self.db.fetchall(
            """SELECT id, objective, report, status, completed_at, created_at
               FROM missions
               WHERE project_id = ?
                 AND status IN ('complete', 'partial', 'blocked', 'active')
               ORDER BY COALESCE(completed_at, created_at) DESC
               LIMIT ?""",
            [project_id, top_per_kind * 6],
        )
        milestone_bonus = {"complete": 0.35, "partial": 0.26, "blocked": 0.22, "active": 0.16}
        for row in mission_rows:
            imp = 0.42 + milestone_bonus.get((row.get("status") or "").lower(), 0.1)
            candidates.append(
                {
                    "kind": "milestone",
                    "entity_type": "mission",
                    "entity_id": row["id"],
                    "title": (row.get("objective") or "Milestone")[:90],
                    "summary": (row.get("report") or "")[:240],
                    "importance": min(1.0, imp),
                }
            )

        per_kind: dict[str, list[dict[str, Any]]] = {
            "finding": [],
            "literature": [],
            "decision": [],
            "milestone": [],
        }
        for candidate in candidates:
            per_kind[candidate["kind"]].append(candidate)

        selected: list[dict[str, Any]] = []
        for kind, items in per_kind.items():
            ranked = sorted(items, key=lambda candidate: candidate["importance"], reverse=True)
            selected.extend(ranked[:top_per_kind])
        return selected

    async def _build_condensed_edges(
        self,
        project_id: str,
        ref_to_keynode: dict[tuple[str, str], str],
    ) -> list[dict[str, Any]]:
        """Aggregate entity links into condensed keynode edges."""
        if not ref_to_keynode:
            return []

        rows = await self.db.fetchall(
            """SELECT source_type, source_id, target_type, target_id, link_type,
                      COALESCE(link_weight, 0.0) as link_weight,
                      link_reason
               FROM entity_links
               WHERE project_id = ?""",
            [project_id],
        )

        aggregated: dict[tuple[str, str, str], dict[str, Any]] = {}
        for row in rows:
            source_key = (row["source_type"], row["source_id"])
            target_key = (row["target_type"], row["target_id"])
            src_keynode = ref_to_keynode.get(source_key)
            tgt_keynode = ref_to_keynode.get(target_key)
            if not src_keynode or not tgt_keynode or src_keynode == tgt_keynode:
                continue

            agg_key = (src_keynode, tgt_keynode, row["link_type"])
            current = aggregated.get(agg_key)
            weight = float(row.get("link_weight") or 0.0)
            if current is None:
                aggregated[agg_key] = {
                    "source": src_keynode,
                    "target": tgt_keynode,
                    "link_type": row["link_type"],
                    "weight": max(0.2, weight),
                    "reason": row.get("link_reason")
                    or "Aggregated from linked supporting entities.",
                }
            else:
                current["weight"] = max(current["weight"], weight)

        return list(aggregated.values())

    @staticmethod
    def _guess_type_from_id(entity_id: str) -> str:
        """Guess entity type from ID prefix."""
        prefix_map = {
            "dec": "decision",
            "lit": "literature",
            "jrn": "journal",
            "mis": "mission",
            "chk": "checkpoint",
            "evt": "event",
            "art": "artifact",
            "fig": "figure",
            "sum": "summary",
            "qas": "qa_session",
            "qal": "qa_log",
            "lnk": "link",
            "clm": "claim",
            "ecl": "cluster",
            "icd": "interpretation_candidate",
            "obs": "experiment_observation",
            "ced": "claim_edge",
            "rev": "review",
        }
        prefix = entity_id.split("_")[0] if "_" in entity_id else ""
        return prefix_map.get(prefix, "unknown")

    # ------------------------------------------------------------------
    # Staleness blast-radius (eval-v3 theme B, 2026-06-12)
    # ------------------------------------------------------------------

    # Per-link-type dependency semantics: which endpoint EPISTEMICALLY
    # DEPENDS on the other. When the depended-on side goes stale
    # (superseded / retracted / abandoned), the dependent side is impacted.
    # Direction values name the dependent endpoint of the stored edge.
    #
    # Deliberately excluded:
    #   produced     -- raw observations are immutable; a mission's findings
    #                   stand even if its motivating decision is overturned
    #                   (paper section 5.2: raw layer immutable).
    #   supersedes   -- the supersession relation itself, not a dependency.
    #   contradicts  -- disagreement, not dependency.
    _IMPACT_DEPENDENT: dict[str, str] = {
        # dependent = source (source depends on target)
        "derived_from": "source",  # claim depends on its source journal
        "justified_by": "source",  # decision depends on its evidence
        "cites": "source",  # journal depends on cited literature
        "references": "source",  # weak contextual dependency
        "answers": "source",  # cluster depends on its parent RQ
        "builds_on": "source",
        # dependent = target (target depends on source)
        "informed_by": "target",  # decision depends on informing literature
        "motivated": "target",  # mission depends on motivating decision
        "supports": "target",
        "evidence_for": "target",
        "qualifies": "target",
        # claim_edges relations
        "member_of": "target",  # cluster depends on member claims
    }

    _STALE_STATUSES = ("superseded", "retracted", "abandoned")

    async def _entity_status(self, eid: str, project_id: str) -> str | None:
        """Lifecycle status for any entity id; None when the entity is absent.

        journal carries lifecycle in `confidence`; decisions/missions in
        `status`; claims/clusters in `staleness` (yellow/red treated as
        stale-adjacent but NOT blast-radius roots).
        """
        etype = self._guess_type_from_id(eid)
        spec = {
            "journal": ("journal", "confidence"),
            "decision": ("decisions", "status"),
            "literature": ("literature", "status"),
            "mission": ("missions", "status"),
            "claim": ("claims", "staleness"),
            "cluster": ("evidence_clusters", "staleness"),
        }.get(etype)
        if spec is None:
            return None
        table, col = spec
        row = await self.db.fetchone(
            f"SELECT {col} AS s FROM {table} WHERE id = ? AND {self._project_clause()}",
            [eid, project_id],
        )
        return (row["s"] or "") if row else None

    async def staleness_impact(
        self,
        entity_id: str,
        *,
        max_depth: int = 3,
        project_id: str = "proj_default",
    ) -> dict:
        """Downstream blast-radius of a stale (or about-to-be-stale) entity.

        BFS over entity_links + claim_edges following DEPENDENT direction
        only (see _IMPACT_DEPENDENT): returns every entity whose reasoning
        rests, directly or transitively, on `entity_id`. Built because the
        freshness pass only covers the 1-hop claims-with-superseded-source
        case; overturning a decision also impacts the missions it motivated,
        the clusters answering it, and any manuscript manifest citing it.

        Returns {root, root_status, impacted: [{id, type, label, status,
        depth, via: {from, link_type}}], counts}.
        """
        root_status = await self._entity_status(entity_id, project_id)
        impacted: dict[str, dict] = {}
        frontier: set[str] = {entity_id}
        seen: set[str] = {entity_id}

        for depth in range(1, max_depth + 1):
            if not frontier:
                break
            placeholders = ",".join("?" for _ in frontier)
            frontier_list = list(frontier)
            el_rows = await self.db.fetchall(
                f"""SELECT source_id, link_type, target_id FROM entity_links
                    WHERE {self._project_clause()}
                      AND (source_id IN ({placeholders}) OR target_id IN ({placeholders}))""",
                [project_id] + frontier_list + frontier_list,
            )
            ce_rows = await self.db.fetchall(
                f"""SELECT source_claim_id, target_claim_id, cluster_id, relation
                    FROM claim_edges
                    WHERE {self._project_clause()} AND (
                        source_claim_id IN ({placeholders})
                        OR target_claim_id IN ({placeholders})
                        OR cluster_id IN ({placeholders}))""",
                [project_id] + frontier_list + frontier_list + frontier_list,
            )

            next_frontier: set[str] = set()

            def _maybe_add(parent: str, child: str, link: str) -> None:
                if parent not in seen or child in seen or not child:
                    return
                seen.add(child)
                impacted[child] = {
                    "id": child,
                    "type": self._guess_type_from_id(child),
                    "depth": depth,
                    "via": {"from": parent, "link_type": link},
                }
                next_frontier.add(child)

            for row in el_rows:
                dep = self._IMPACT_DEPENDENT.get(row["link_type"])
                if dep is None:
                    continue
                src, tgt = row["source_id"], row["target_id"]
                if dep == "source":
                    _maybe_add(tgt, src, row["link_type"])
                else:
                    _maybe_add(src, tgt, row["link_type"])

            for row in ce_rows:
                relation = row["relation"]
                dep = self._IMPACT_DEPENDENT.get(relation)
                if dep is None:
                    continue
                src = row["source_claim_id"]
                tgt = row["cluster_id"] if relation == "member_of" else row["target_claim_id"]
                if not src or not tgt:
                    continue
                if dep == "source":
                    _maybe_add(tgt, src, relation)
                else:
                    _maybe_add(src, tgt, relation)

            frontier = next_frontier

        # Hydrate labels + statuses for the impacted set.
        node_ids: dict[str, set[str]] = {}
        for nid in impacted:
            node_ids.setdefault(self._guess_type_from_id(nid), set()).add(nid)
        nodes: dict[str, dict] = {}
        await self._fill_missing_nodes(nodes, node_ids, project_id=project_id)
        result = []
        for nid, info in impacted.items():
            meta = nodes.get(nid, {})
            info["label"] = (meta.get("label") or "")[:120]
            info["status"] = meta.get("status")
            result.append(info)
        result.sort(key=lambda x: (x["depth"], x["type"], x["id"]))

        counts: dict[str, int] = {}
        for info in result:
            counts[info["type"]] = counts.get(info["type"], 0) + 1
        return {
            "root": entity_id,
            "root_status": root_status,
            "root_is_stale": (root_status or "").lower() in self._STALE_STATUSES,
            "impacted": result,
            "counts": counts,
            "max_depth": max_depth,
        }
