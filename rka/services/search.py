"""Hybrid search service: FTS5 keyword + sqlite-vec vector + Reciprocal Rank Fusion."""

from __future__ import annotations

import logging
import re
import struct
from dataclasses import dataclass
from typing import Any

from rka.infra.database import Database
from rka.infra.embeddings import EmbeddingService
from rka.services.artifacts import build_artifact_text, build_figure_text
from rka.services.currentness import CURRENCY_COLUMNS, currentness

logger = logging.getLogger(__name__)


def _tokens_remain(query: str) -> bool:
    """True when a constraint-stripped query still carries content tokens."""
    return bool(re.findall(r"[^\W_]{3,}", query, re.UNICODE))


# entity_type -> (source table, currency columns to lift onto the hit)
# Only tables that actually carry a lifecycle signal appear here.
_CURRENCY_COLUMNS: dict[str, tuple[str, tuple[str, ...]]] = {
    kind: (table, CURRENCY_COLUMNS[kind]) for kind, table in {
        "decision": "decisions", "journal": "journal", "claim": "claims",
        "cluster": "evidence_clusters", "mission": "missions", "literature": "literature",
    }.items()
}


@dataclass
class SearchHit:
    """A single search result."""

    entity_type: str
    entity_id: str
    title: str
    snippet: str
    score: float = 0.0
    fts_rank: int | None = None
    vec_rank: int | None = None
    # Currency signals. Without these a superseded decision is indistinguishable
    # from a current one in a search result, which is how a session ends up
    # acting on knowledge that has already been overturned. Every other read
    # surface (ego_graph, multi_hop, operation="entity") already carries them.
    status: str | None = None
    superseded_by: str | None = None
    stale: bool | None = None
    staleness: str | None = None
    staleness_verdict: str | None = None
    currentness: dict | None = None


class SearchService:
    """Hybrid FTS5 + vector search across all entity types.

    Behaviour adapts to available backends:
      - FTS5 is always available (built into SQLite).
      - Vector search requires sqlite-vec extension + embeddings.
      - When only FTS5 is available, falls back to keyword-only.
      - When only LIKE is available (no FTS5 data), falls back to Phase 1 search.
    """

    # Maps entity types to their FTS5 table and columns
    FTS_MAP = {
        "journal": {
            "table": "fts_journal",
            "source": "journal",
            "title_expr": "'[' || j.type || ']'",
            "snippet_col": "content",
            "join_alias": "j",
        },
        "decision": {
            "table": "fts_decisions",
            "source": "decisions",
            "title_expr": "d.question",
            "snippet_col": "question",
            "join_alias": "d",
        },
        "literature": {
            "table": "fts_literature",
            "source": "literature",
            "title_expr": "l.title",
            "snippet_col": "title",
            "join_alias": "l",
        },
        "mission": {
            "table": "fts_missions",
            "source": "missions",
            "title_expr": "m.objective",
            "snippet_col": "objective",
            "join_alias": "m",
        },
        "claim": {
            "table": "fts_claims",
            "source": "claims",
            "title_expr": "'[' || c.claim_type || ']'",
            "snippet_col": "content",
            "join_alias": "c",
        },
        "cluster": {
            "table": "fts_clusters",
            "source": "evidence_clusters",
            "title_expr": "ec.label",
            "snippet_col": "label",
            "join_alias": "ec",
        },
    }

    VEC_MAP = {
        "journal": "vec_journal",
        "decision": "vec_decisions",
        "literature": "vec_literature",
        "mission": "vec_missions",
        "artifact": "vec_artifacts",
        "figure": "vec_artifacts",
        "claim": "vec_claims",
    }

    SOURCE_MAP = {
        "journal": "journal",
        "decision": "decisions",
        "literature": "literature",
        "mission": "missions",
        "artifact": "artifacts",
        "figure": "figures",
        "claim": "claims",
        "cluster": "evidence_clusters",
    }

    def __init__(
        self,
        db: Database,
        embeddings: EmbeddingService | None = None,
        project_id: str = "proj_default",
    ):
        self.db = db
        self.embeddings = embeddings
        self.project_id = project_id

    def with_project(self, project_id: str) -> "SearchService":
        return SearchService(db=self.db, embeddings=self.embeddings, project_id=project_id)

    async def _embedding_index_ready(self) -> bool:
        if self.embeddings is None:
            return False
        check = getattr(self.embeddings, "index_search_ready", None)
        if check is None:
            return True
        return bool(await check())

    # Query-understanding patterns (eval-v3 theme E follow-up; Eval-v1
    # Finding: temporal language and actor anchors fell back to literal FTS
    # tokens and missed — "today" matched nothing, "PI directives" ignored
    # the source column). Interpreted phrases are stripped from the FTS
    # query and applied as metadata constraints on the candidates.
    _TEMPORAL_PATTERNS = (
        (re.compile(r"\btoday\b", re.I), "-1 day"),
        (re.compile(r"\byesterday\b", re.I), "-2 days"),
        (re.compile(r"\bthis week\b", re.I), "-7 days"),
        (re.compile(r"\blast week\b", re.I), "-14 days"),
        (re.compile(r"\bthis month\b", re.I), "-31 days"),
        (re.compile(r"\brecent(?:ly)?\b", re.I), "-14 days"),
    )
    _ACTOR_PATTERNS = (
        # (pattern, source filter, optional journal-type filter)
        (re.compile(r"\bpi directives?\b", re.I), "pi", "directive"),
        (re.compile(r"\b(?:from|by) the pi\b", re.I), "pi", None),
        (re.compile(r"\bpi instructions?\b", re.I), "pi", "directive"),
        (re.compile(r"\bexecutor (?:logs?|notes?|reports?)\b", re.I), "executor", None),
        (re.compile(r"\bbrain (?:notes?|synthes\w+)\b", re.I), "brain", None),
    )

    @classmethod
    def parse_query_constraints(cls, query: str) -> tuple[str, dict]:
        """Interpret temporal/actor phrases as constraints; strip them from
        the lexical query. Returns (stripped_query, constraints)."""
        constraints: dict = {}
        stripped = query
        for pat, offset in cls._TEMPORAL_PATTERNS:
            if pat.search(stripped):
                # keep the WIDEST window if several phrases appear
                cur = constraints.get("created_within")
                if cur is None or int(offset.split()[0]) < int(cur.split()[0]):
                    constraints["created_within"] = offset
                stripped = pat.sub(" ", stripped)
        for pat, source, jtype in cls._ACTOR_PATTERNS:
            if pat.search(stripped):
                constraints["source"] = source
                if jtype:
                    constraints["journal_type"] = jtype
                stripped = pat.sub(" ", stripped)
                break
        return stripped.strip(), constraints

    async def _apply_constraints(
        self, hits: list[SearchHit], constraints: dict
    ) -> list[SearchHit]:
        """Filter candidate hits by interpreted metadata constraints.

        Constraint columns live on journal (source, type, created_at) and on
        the other entity tables (created_at only); hits are batch-checked via
        their source tables.
        """
        if not constraints or not hits:
            return hits
        keep_ids: set[str] = set()
        by_type: dict[str, list[str]] = {}
        for h in hits:
            by_type.setdefault(h.entity_type, []).append(h.entity_id)
        for etype, ids in by_type.items():
            table = self.SOURCE_MAP.get(etype)
            if not table:
                continue
            conds = ["id IN (%s)" % ",".join("?" for _ in ids), "project_id = ?"]
            params: list = ids + [self.project_id]
            if constraints.get("created_within"):
                conds.append("created_at >= datetime('now', ?)")
                params.append(constraints["created_within"])
            if constraints.get("source"):
                if etype == "journal":
                    conds.append("source = ?")
                    params.append(constraints["source"])
                else:
                    # actor anchors are journal-specific; drop other types
                    continue
            if constraints.get("journal_type") and etype == "journal":
                conds.append("type = ?")
                params.append(constraints["journal_type"])
            rows = await self.db.fetchall(
                f"SELECT id FROM {table} WHERE {' AND '.join(conds)}", params
            )
            keep_ids.update(r["id"] for r in rows)
        return [h for h in hits if h.entity_id in keep_ids]

    async def search(
        self,
        query: str,
        entity_types: list[str] | None = None,
        limit: int = 20,
        keyword_weight: float = 0.3,
        semantic_weight: float = 0.7,
    ) -> list[SearchHit]:
        """Hybrid search combining FTS5 keyword and vector semantic search.

        Falls back gracefully:
          - No embeddings → keyword only
          - No FTS5 data → LIKE fallback
          - No sqlite-vec → keyword only

        Temporal phrases ("today", "this week") and actor anchors ("PI
        directives") are interpreted as metadata constraints rather than
        searched as literal tokens (see parse_query_constraints).
        """
        types = entity_types or ["decision", "literature", "journal", "mission", "artifact", "figure", "claim", "cluster"]

        stripped_query, constraints = self.parse_query_constraints(query)
        if constraints:
            # Actor-anchored queries are journal queries by construction.
            if constraints.get("source") and entity_types is None:
                types = ["journal"]
            inner = stripped_query if _tokens_remain(stripped_query) else query
            return await self._search_current_first_window(
                inner,
                types,
                limit,
                keyword_weight,
                semantic_weight,
                constraints=constraints,
            )
        return await self._search_current_first_window(
            query,
            types,
            limit,
            keyword_weight,
            semantic_weight,
        )

    async def _search_current_first_window(
        self,
        query: str,
        types: list[str],
        limit: int,
        keyword_weight: float,
        semantic_weight: float,
        *,
        constraints: dict | None = None,
    ) -> list[SearchHit]:
        """Expand candidates until current-first ranking is conclusive.

        A fixed overfetch factor fails when an arbitrarily long run of retired
        exact matches precedes a live replacement. Expansion stops as soon as
        the requested number of live hits is present, or when the retrieval
        sources return fewer rows than requested and are therefore exhausted.
        """
        candidate_limit = max(limit * 2, limit + 10)
        while True:
            plain = await self._search_unconstrained(
                query,
                types,
                candidate_limit,
                keyword_weight,
                semantic_weight,
            )
            candidates = plain
            if constraints:
                if not candidates and constraints.get("source"):
                    candidates = await self._metadata_only_journal(
                        constraints, candidate_limit
                    )
                candidates = await self._apply_constraints(candidates, constraints)
            annotated = await self._attach_currency(candidates)
            ordered = self._current_first(annotated)
            live_count = sum(not self._is_retired(hit) for hit in ordered)
            if live_count >= max(limit, 0) or len(plain) < candidate_limit:
                return ordered[:limit]
            candidate_limit *= 2

    @staticmethod
    def _is_retired(hit: SearchHit) -> bool:
        projection = hit.currentness or currentness(vars(hit))
        return not projection["is_current"]

    @staticmethod
    def _current_first(hits: list[SearchHit]) -> list[SearchHit]:
        """Rank a retired entity below a live one, never above.

        The currency signals above were attached for reading, not ranking, so
        a superseded decision could outrank the decision that replaced it —
        which is the failure their own comment describes, arrived at by a
        different route. `hide_superseded` already takes this position for
        journal listings; this is the same position for search.

        A stable partition: relative order inside each group is whatever the
        fusion produced, so this decides only the current-vs-retired tie and
        leaves every other ranking alone.
        """
        return sorted(hits, key=SearchService._is_retired)

    async def _attach_currency(self, hits: list[SearchHit]) -> list[SearchHit]:
        """Fill status / superseded_by / stale on every hit that has one.

        Applied at the single public exit so it covers all four retrieval
        paths (FTS, vector, tag, LIKE fallback) rather than each of their
        twelve construction sites. One batched lookup per entity type
        present in the result set.
        """
        by_type: dict[str, list[str]] = {}
        for hit in hits:
            if hit.entity_type in _CURRENCY_COLUMNS:
                by_type.setdefault(hit.entity_type, []).append(hit.entity_id)
        if not by_type:
            return hits

        lifted: dict[tuple[str, str], dict] = {}
        for etype, ids in by_type.items():
            table, columns = _CURRENCY_COLUMNS[etype]
            placeholders = ",".join("?" for _ in ids)
            try:
                rows = await self.db.fetchall(
                    f"SELECT id, {', '.join(columns)} FROM {table}"
                    f" WHERE id IN ({placeholders}) AND project_id = ?",
                    list(ids) + [self.project_id],
                )
            except Exception:  # pragma: no cover — pre-migration snapshot
                logger.debug("currency lookup skipped for %s", etype, exc_info=True)
                continue
            for row in rows:
                lifted[(etype, row["id"])] = {c: row[c] for c in columns}

        for hit in hits:
            values = lifted.get((hit.entity_type, hit.entity_id))
            if not values:
                continue
            if values.get("status") is not None:
                hit.status = values["status"]
            if values.get("superseded_by"):
                hit.superseded_by = values["superseded_by"]
            if values.get("stale") is not None:
                hit.stale = bool(values["stale"])
            hit.staleness = values.get("staleness")
            hit.staleness_verdict = values.get("staleness_verdict")
            hit.currentness = currentness(values)
        return hits

    async def _metadata_only_journal(self, constraints: dict, limit: int) -> list[SearchHit]:
        conds = ["project_id = ?"]
        params: list = [self.project_id]
        if constraints.get("source"):
            conds.append("source = ?")
            params.append(constraints["source"])
        if constraints.get("journal_type"):
            conds.append("type = ?")
            params.append(constraints["journal_type"])
        if constraints.get("created_within"):
            conds.append("created_at >= datetime('now', ?)")
            params.append(constraints["created_within"])
        rows = await self.db.fetchall(
            f"SELECT * FROM journal WHERE {' AND '.join(conds)} "
            f"ORDER BY created_at DESC LIMIT ?", params + [limit],
        )
        out: list[SearchHit] = []
        for r in rows:
            title, snippet = self._extract_title_snippet("journal", dict(r))
            out.append(SearchHit(entity_type="journal", entity_id=r["id"],
                                 title=title, snippet=snippet, score=0.0))
        return out

    async def _search_unconstrained(
        self,
        query: str,
        types: list[str],
        limit: int,
        keyword_weight: float,
        semantic_weight: float,
    ) -> list[SearchHit]:

        # 1. FTS5 keyword search
        fts_results = await self._fts_search(query, types, limit * 2)

        # 2. Vector search (if available)
        vec_results: list[SearchHit] = []
        if self.embeddings and self.db.vec_available:
            try:
                if await self._embedding_index_ready():
                    query_vec = await self.embeddings.embed(query)
                    # Hold one read snapshot from the final generation check
                    # through KNN. A concurrent transition either waits or is
                    # observed here, so query and document vectors always come
                    # from the same generation.
                    async with self.db.transaction(write=False):
                        if await self._embedding_index_ready():
                            vec_results = await self._vector_search(
                                query_vec,
                                types,
                                limit * 2,
                            )
            except Exception as exc:
                logger.warning("Vector search failed, using keyword only: %s", exc)

        # 3. Tag search — tags are capture-time relevance labels (assigned by
        #    Brain/Executor/PI when the entity is written) and FTS cannot see
        #    them; eval-v3 found most residual report-context misses were
        #    reachable through one tag hop. Merged as a third RRF source.
        tag_results = await self._tag_search(query, types, limit * 2)

        # 4. Supplemental LIKE search for entity types without dedicated FTS tables.
        supplemental = await self._like_fallback(
            query,
            [etype for etype in types if etype not in self.FTS_MAP],
            limit,
        )

        # 5. If all ranked sources are empty, fall back to LIKE search
        if not fts_results and not vec_results and not tag_results:
            if supplemental:
                return supplemental[:limit]
            return await self._like_fallback(query, types, limit)

        # 6. Reciprocal Rank Fusion across the non-empty ranked sources
        fused = self._rrf_merge(
            fts_results, vec_results, keyword_weight, semantic_weight,
            tag_results=tag_results,
        )
        ranked = self._merge_ranked_hits(fused, supplemental)
        return self._truncate_with_lexical_floor(
            ranked,
            fts_results,
            limit,
            keyword_weight=keyword_weight,
        )

    # Request-framing vocabulary stripped from natural-language queries
    # before FTS matching. Deliberately small — only words that carry no
    # retrieval signal in any research corpus; domain terms must survive.
    _QUERY_STOPWORDS: frozenset = frozenset(
        "a an the and or of to in on at for with about from into over is are "
        "was were be been being do does did have has had can could should "
        "would will may might must this that these those it its they them "
        "their there i we you he she what which who whom whose why how when "
        "where want need please show me find get tell us our your my his her "
        "report write".split()
    )

    @classmethod
    def _sanitize_fts_query(cls, query: str) -> str:
        """Convert a natural-language query to a safe FTS5 query.

        Strategy: split into words, drop request-framing stopwords, quote
        each remaining word individually, then join per the
        `RKA_FTS_QUERY_MODE` env var:

          - `or` (default): explicit `OR` separator — bm25() ranks by
            match count, so multi-word natural-language queries degrade
            gracefully instead of failing closed. (FTS5's implicit
            operator for space-separated terms is AND, not OR; the
            pre-fix space-join made production 'or' mode behave as AND.)
          - `and`: explicit `AND` separator — tightens precision at the
            cost of recall (see mis_01KRKJ9G20EM5XMA147JTKQCFF).

        Stopword stripping only applies when content words remain — an
        all-stopword query falls through unstripped rather than matching
        nothing. Quoting each word avoids issues with hyphens, special
        characters, and FTS5 operators inside terms. Unknown env values
        fall back silently to `or` (safe default).
        """
        import os
        import re
        words = re.findall(r"[^\W_]+", query, re.UNICODE)
        if not words:
            return query
        content_words = [w for w in words if w.lower() not in cls._QUERY_STOPWORDS]
        if content_words:
            words = content_words
        mode = os.environ.get("RKA_FTS_QUERY_MODE", "or").strip().lower()
        separator = " AND " if mode == "and" else " OR "
        return separator.join(f'"{w}"' for w in words)

    async def _fts_search(
        self, query: str, entity_types: list[str], limit: int,
    ) -> list[SearchHit]:
        """Full-text search across FTS5 virtual tables."""
        results: list[SearchHit] = []
        fts_query = self._sanitize_fts_query(query)

        for etype in entity_types:
            info = self.FTS_MAP.get(etype)
            if not info:
                continue

            try:
                # The JOIN is what makes `limit` mean this project's top N.
                # Ranking globally and filtering afterwards spent the whole
                # budget on whichever projects happen to hold most of the
                # corpus: two of them hold 68% of the journal rows here, and
                # a small project searching its own material could get zero
                # candidates back — indistinguishable from having nothing on
                # the topic.
                #
                # It also drops rows whose source entity is gone, so the
                # orphan FTS rows left behind by project deletion stop
                # competing for slots without a separate reaper.
                rows = await self.db.fetchall(
                    f"""SELECT f.id, f.rank
                        FROM {info['table']} f
                        JOIN {info['source']} s ON s.id = f.id
                        WHERE {info['table']} MATCH ?
                          AND s.project_id = ?
                        ORDER BY f.rank
                        LIMIT ?""",
                    [fts_query, self.project_id, limit],
                )
            except Exception:
                # FTS5 table might be empty or query invalid
                continue

            if not rows:
                continue

            # Fetch full data for matched IDs
            ids = [row["id"] for row in rows]
            rank_map = {row["id"]: i for i, row in enumerate(rows)}
            placeholders = ",".join("?" for _ in ids)

            data_rows = await self.db.fetchall(
                f"SELECT * FROM {info['source']} WHERE id IN ({placeholders}) AND project_id = ?",
                ids + [self.project_id],
            )

            for row in data_rows:
                title, snippet = self._extract_title_snippet(etype, row)
                results.append(SearchHit(
                    entity_type=etype,
                    entity_id=row["id"],
                    title=title,
                    snippet=snippet,
                    fts_rank=rank_map.get(row["id"], 999),
                ))

        # Sort by FTS rank (lower is better)
        results.sort(key=lambda h: 999 if h.fts_rank is None else h.fts_rank)
        return results

    async def _vector_search(
        self, query_vec: list[float], entity_types: list[str], limit: int,
    ) -> list[SearchHit]:
        """KNN search across sqlite-vec virtual tables."""
        results: list[SearchHit] = []
        vec_blob = struct.pack(f"{len(query_vec)}f", *query_vec)

        for etype in entity_types:
            table = self.VEC_MAP.get(etype)
            if not table:
                continue

            source = self.SOURCE_MAP.get(etype)
            if not source:
                continue

            try:
                filter_sql = "project_id = ?"
                params: list[Any] = [vec_blob, self.project_id]
                if table == "vec_artifacts":
                    filter_sql += " AND entity_type = ?"
                    params.append(etype)
                params.append(limit)
                rows = await self.db.fetchall(
                    f"""SELECT id, distance
                        FROM {table}
                        WHERE embedding MATCH ?
                          AND {filter_sql}
                          AND k = ?
                        ORDER BY distance
                        """,
                    params,
                )
            except Exception:
                continue

            if not rows:
                continue

            ids = [row["id"] for row in rows]
            dist_map = {row["id"]: row["distance"] for row in rows}
            rank_map = {row["id"]: i for i, row in enumerate(rows)}
            placeholders = ",".join("?" for _ in ids)

            data_rows = await self.db.fetchall(
                f"SELECT * FROM {source} WHERE id IN ({placeholders}) AND project_id = ?",
                ids + [self.project_id],
            )

            for row in data_rows:
                title, snippet = self._extract_title_snippet(etype, row)
                dist = dist_map.get(row["id"], 1.0)
                results.append(SearchHit(
                    entity_type=etype,
                    entity_id=row["id"],
                    title=title,
                    snippet=snippet,
                    score=max(0.0, 1.0 - dist),  # cosine similarity
                    vec_rank=rank_map.get(row["id"], 999),
                ))

        # Sort by distance (lower distance = more similar)
        results.sort(key=lambda h: 999 if h.vec_rank is None else h.vec_rank)
        return results

    async def _tag_search(
        self,
        query: str,
        types: list[str],
        limit: int,
    ) -> list[SearchHit]:
        """Match query tokens against capture-time tags.

        Tags (e.g. ``eval-harness``, ``pluggable-embeddings``) are assigned
        when entities are written and are not FTS-indexed, so they carry
        relevance signal FTS cannot see. A query token matches a tag when it
        equals the tag or one of its hyphen-delimited segments. Entities are
        ranked by the number of distinct query tokens their tags match;
        hydration through SOURCE_MAP applies project scoping.
        """
        import re

        tokens = {
            t.lower()
            for t in re.findall(r"[^\W_]+", query, re.UNICODE)
            if len(t) > 2 and t.lower() not in self._QUERY_STOPWORDS
        }
        if not tokens:
            return []

        matched: dict[tuple[str, str], set[str]] = {}
        matched_tags: dict[tuple[str, str], set[str]] = {}
        project_clause = (
            "(project_id = ? OR project_id IS NULL)"
            if self.project_id == "proj_default"
            else "project_id = ?"
        )
        for tok in sorted(tokens)[:12]:
            rows = await self.db.fetchall(
                f"""SELECT tag, entity_type, entity_id FROM tags
                   WHERE {project_clause} AND (
                          tag = ?
                       OR tag LIKE ? || '-%'
                       OR tag LIKE '%-' || ?
                       OR tag LIKE '%-' || ? || '-%')""",
                [self.project_id, tok, tok, tok, tok],
            )
            for r in rows:
                if r["entity_type"] not in types:
                    continue
                key = (r["entity_type"], r["entity_id"])
                matched.setdefault(key, set()).add(tok)
                matched_tags.setdefault(key, set()).add(r["tag"])
        if not matched:
            return []

        ranked = sorted(matched.items(), key=lambda kv: len(kv[1]), reverse=True)

        # Hydrate (and project-filter) through the entities' source tables.
        by_type: dict[str, list[str]] = {}
        for (etype, eid), _ in ranked:
            by_type.setdefault(etype, []).append(eid)
        rows_by_id: dict[str, dict] = {}
        for etype, ids in by_type.items():
            table = self.SOURCE_MAP.get(etype)
            if not table:
                continue
            placeholders = ",".join("?" for _ in ids)
            for row in await self.db.fetchall(
                f"SELECT * FROM {table} WHERE id IN ({placeholders}) AND project_id = ?",
                ids + [self.project_id],
            ):
                rows_by_id[row["id"]] = dict(row)

        hits: list[SearchHit] = []
        for (etype, eid), toks in ranked:
            row = rows_by_id.get(eid)
            if row is None:
                continue  # other project or stale tag
            title, snippet = self._extract_title_snippet(etype, row)
            tag_note = ", ".join(sorted(matched_tags[(etype, eid)])[:4])
            hits.append(
                SearchHit(
                    entity_type=etype,
                    entity_id=eid,
                    title=title,
                    snippet=f"[tags: {tag_note}] {snippet}"[:300],
                    score=float(len(toks)),
                )
            )
            if len(hits) >= limit:
                break
        return hits

    def _rrf_merge(
        self,
        fts_results: list[SearchHit],
        vec_results: list[SearchHit],
        keyword_weight: float,
        semantic_weight: float,
        k: int = 60,
        tag_results: list[SearchHit] | None = None,
        tag_weight: float = 0.25,
    ) -> list[SearchHit]:
        """Reciprocal Rank Fusion — merge ranked lists from different sources.

        RRF score = w_kw / (k + rank_fts) + w_sem / (k + rank_vec)
                    [+ w_tag / (k + rank_tag) when tag hits are supplied]
        """
        # Build score maps
        scores: dict[str, float] = {}
        hits: dict[str, SearchHit] = {}

        for rank, hit in enumerate(fts_results):
            key = f"{hit.entity_type}:{hit.entity_id}"
            scores[key] = scores.get(key, 0.0) + keyword_weight / (k + rank + 1)
            hits[key] = hit

        for rank, hit in enumerate(vec_results):
            key = f"{hit.entity_type}:{hit.entity_id}"
            scores[key] = scores.get(key, 0.0) + semantic_weight / (k + rank + 1)
            if key not in hits:
                hits[key] = hit

        for rank, hit in enumerate(tag_results or []):
            key = f"{hit.entity_type}:{hit.entity_id}"
            scores[key] = scores.get(key, 0.0) + tag_weight / (k + rank + 1)
            if key not in hits:
                hits[key] = hit

        # Sort by fused score descending
        sorted_keys = sorted(scores.keys(), key=lambda k: scores[k], reverse=True)
        result = []
        for key in sorted_keys:
            hit = hits[key]
            hit.score = scores[key]
            result.append(hit)

        return result

    async def _like_fallback(
        self, query: str, entity_types: list[str], limit: int,
    ) -> list[SearchHit]:
        """Phase 1 LIKE-based fallback search."""
        results: list[SearchHit] = []
        q = f"%{query}%"

        if "decision" in entity_types:
            rows = await self.db.fetchall(
                "SELECT * FROM decisions WHERE project_id = ? AND (question LIKE ? OR rationale LIKE ?) LIMIT ?",
                [self.project_id, q, q, limit],
            )
            for row in rows:
                results.append(SearchHit(
                    entity_type="decision", entity_id=row["id"],
                    title=row["question"][:100], snippet=row["question"][:200],
                ))

        if "literature" in entity_types:
            rows = await self.db.fetchall(
                "SELECT * FROM literature WHERE project_id = ? AND (title LIKE ? OR abstract LIKE ?) LIMIT ?",
                [self.project_id, q, q, limit],
            )
            for row in rows:
                results.append(SearchHit(
                    entity_type="literature", entity_id=row["id"],
                    title=row["title"][:100], snippet=(row.get("abstract") or "")[:200],
                ))

        if "journal" in entity_types:
            rows = await self.db.fetchall(
                "SELECT * FROM journal WHERE project_id = ? AND content LIKE ? LIMIT ?",
                [self.project_id, q, limit],
            )
            for row in rows:
                results.append(SearchHit(
                    entity_type="journal", entity_id=row["id"],
                    title=f"[{row['type']}]", snippet=row["content"][:200],
                ))

        if "mission" in entity_types:
            rows = await self.db.fetchall(
                "SELECT * FROM missions WHERE project_id = ? AND objective LIKE ? LIMIT ?",
                [self.project_id, q, limit],
            )
            for row in rows:
                results.append(SearchHit(
                    entity_type="mission", entity_id=row["id"],
                    title=row["objective"][:100], snippet=row["objective"][:200],
                ))

        if "artifact" in entity_types:
            rows = await self.db.fetchall(
                """SELECT * FROM artifacts
                   WHERE project_id = ?
                     AND (filename LIKE ? OR filepath LIKE ? OR mime LIKE ? OR metadata LIKE ?)
                   LIMIT ?""",
                [self.project_id, q, q, q, q, limit],
            )
            for row in rows:
                results.append(SearchHit(
                    entity_type="artifact",
                    entity_id=row["id"],
                    title=(row.get("filename") or "")[:100],
                    snippet=build_artifact_text(
                        filename=row.get("filename") or "",
                        filetype=row.get("filetype"),
                        mime=row.get("mime"),
                        metadata=row.get("metadata"),
                    )[:200],
                ))

        if "figure" in entity_types:
            rows = await self.db.fetchall(
                """SELECT * FROM figures
                   WHERE project_id = ?
                     AND (caption LIKE ? OR summary LIKE ? OR claims LIKE ?)
                   LIMIT ?""",
                [self.project_id, q, q, q, limit],
            )
            for row in rows:
                results.append(SearchHit(
                    entity_type="figure",
                    entity_id=row["id"],
                    title=(row.get("caption") or f"Figure {row['id']}")[:100],
                    snippet=build_figure_text(
                        caption=row.get("caption"),
                        summary=row.get("summary"),
                        claims=row.get("claims"),
                    )[:200],
                ))

        if "claim" in entity_types:
            rows = await self.db.fetchall(
                "SELECT * FROM claims WHERE project_id = ? AND content LIKE ? LIMIT ?",
                [self.project_id, q, limit],
            )
            for row in rows:
                results.append(SearchHit(
                    entity_type="claim", entity_id=row["id"],
                    title=f"[{row.get('claim_type', 'claim')}]",
                    snippet=(row.get("content") or "")[:200],
                ))

        if "cluster" in entity_types:
            rows = await self.db.fetchall(
                "SELECT * FROM evidence_clusters WHERE project_id = ? AND (label LIKE ? OR synthesis LIKE ?) LIMIT ?",
                [self.project_id, q, q, limit],
            )
            for row in rows:
                results.append(SearchHit(
                    entity_type="cluster", entity_id=row["id"],
                    title=(row.get("label") or "")[:100],
                    snippet=(row.get("synthesis") or "")[:200],
                ))

        return results[:limit]

    @staticmethod
    def _merge_ranked_hits(primary: list[SearchHit], secondary: list[SearchHit]) -> list[SearchHit]:
        """Append secondary hits after primary hits while removing duplicates."""
        merged: list[SearchHit] = []
        seen: set[str] = set()
        for hit in [*primary, *secondary]:
            key = f"{hit.entity_type}:{hit.entity_id}"
            if key in seen:
                continue
            seen.add(key)
            merged.append(hit)
        return merged

    @staticmethod
    def _truncate_with_lexical_floor(
        ranked: list[SearchHit],
        fts_results: list[SearchHit],
        limit: int,
        *,
        keyword_weight: float,
    ) -> list[SearchHit]:
        """Keep the best lexical hit from being evicted by hybrid fusion.

        A semantic-heavy RRF window can exclude even the first FTS match when
        enough vector-only candidates exist. Reserve only the final slot for
        that one lexical anchor; otherwise preserve the fused order exactly.
        Explicit keyword-free searches retain pure semantic behavior.
        """
        window = ranked[: max(limit, 0)]
        if limit <= 0 or keyword_weight <= 0 or not fts_results:
            return window

        best_fts = fts_results[0]
        best_key = (best_fts.entity_type, best_fts.entity_id)
        if any((hit.entity_type, hit.entity_id) == best_key for hit in window):
            return window
        return [*window[:-1], best_fts]

    @staticmethod
    def _extract_title_snippet(etype: str, row: dict) -> tuple[str, str]:
        """Extract display title and snippet from a raw DB row."""
        if etype == "journal":
            return f"[{row.get('type', 'note')}]", (row.get("content") or "")[:200]
        elif etype == "decision":
            return (row.get("question") or "")[:100], (row.get("rationale") or row.get("question") or "")[:200]
        elif etype == "literature":
            return (row.get("title") or "")[:100], (row.get("abstract") or "")[:200]
        elif etype == "mission":
            return (row.get("objective") or "")[:100], (row.get("context") or row.get("objective") or "")[:200]
        elif etype == "artifact":
            text = build_artifact_text(
                filename=row.get("filename") or "",
                filetype=row.get("filetype"),
                mime=row.get("mime"),
                metadata=row.get("metadata"),
            )
            return (row.get("filename") or "")[:100], text[:200]
        elif etype == "figure":
            text = build_figure_text(
                caption=row.get("caption"),
                summary=row.get("summary"),
                claims=row.get("claims"),
            )
            return (row.get("caption") or f"Figure {row.get('id', '')}")[:100], text[:200]
        elif etype == "claim":
            return f"[{row.get('claim_type', 'claim')}]", (row.get("content") or "")[:200]
        elif etype == "cluster":
            return (row.get("label") or "")[:100], (row.get("synthesis") or "")[:200]
        return "", ""
