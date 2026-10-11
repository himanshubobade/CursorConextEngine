"""Hybrid table discovery.

Four signals, fused in this order:

  1. vector similarity over table docs and column docs
  2. lexical BM25 (SQLite FTS5) over the same docs
  3. lineage proximity to the tables the first two agree on
  4. an LLM rerank over compact candidate cards

Vectors alone miss exact identifiers a business analyst copies from a
spreadsheet; lexical alone misses paraphrases. Lineage is what resolves the
case this platform exists for - a dozen tables have a `revenue` column, and
the right one is the one sitting in the same part of the graph as the other
tables the request mentions.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from app.config import get_settings
from app.context.embeddings import VectorIndex, get_embedding_client
from app.context.lineage_graph import neighborhood
from app.context.store import Store, get_store

log = logging.getLogger(__name__)

_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "get", "give", "has", "have",
    "how", "in", "is", "it", "last", "me", "of", "on", "or", "please", "show", "that", "the",
    "their", "them", "there", "this", "to", "want", "was", "we", "what", "when", "where", "which",
    "will", "with", "would", "you", "your", "data", "table", "tables", "need", "all",
}


@dataclass
class Candidate:
    table_id: str
    text_score: float = 0.0
    graph_score: float = 0.0
    matched_columns: list[str] = field(default_factory=list)
    signals: dict[str, Any] = field(default_factory=dict)
    rerank_score: float | None = None
    reason: str | None = None

    @property
    def score(self) -> float:
        return self.text_score + self.graph_score


def tokenize(text: str) -> list[str]:
    raw = re.split(r"[^A-Za-z0-9_]+", text.lower())
    out: list[str] = []
    for token in raw:
        if not token or token in _STOPWORDS or len(token) < 3:
            continue
        out.append(token)
        # Identifiers pasted straight from a spreadsheet ("net_revenue_amt")
        # should also match their individual parts.
        if "_" in token:
            out.extend(p for p in token.split("_") if len(p) >= 3 and p not in _STOPWORDS)
    seen, unique = set(), []
    for token in out:
        if token not in seen:
            seen.add(token)
            unique.append(token)
    return unique[:24]


# ----------------------------------------------------------------------
# individual signals
# ----------------------------------------------------------------------


def lexical_search(store: Store, query: str, limit: int) -> list[tuple[str, float]]:
    """FTS5 BM25 ranking, with a LIKE-based fallback for SQLite builds that
    were compiled without FTS5."""
    tokens = tokenize(query)
    if not tokens:
        return []

    if store.fts_enabled:
        match = " OR ".join(f'"{t}"*' for t in tokens)
        try:
            rows = store.query(
                "SELECT doc_id, bm25(docs_fts) AS rank FROM docs_fts WHERE docs_fts MATCH ? "
                "ORDER BY rank LIMIT ?",
                (match, limit),
            )
            # bm25() is negative-better; flip it so higher is better.
            return [(r["doc_id"], -float(r["rank"])) for r in rows]
        except Exception as exc:  # noqa: BLE001 - malformed MATCH shouldn't kill a search
            log.info("FTS query failed (%s); falling back to LIKE", exc)

    clauses = " + ".join(["(CASE WHEN LOWER(body) LIKE ? THEN 1 ELSE 0 END)"] * len(tokens))
    rows = store.query(
        f"SELECT doc_id, hits FROM (SELECT doc_id, ({clauses}) AS hits FROM docs) "
        "WHERE hits > 0 ORDER BY hits DESC LIMIT ?",
        [f"%{t}%" for t in tokens] + [limit],
    )
    return [(r["doc_id"], float(r["hits"])) for r in rows]


def vector_search(query_vector, limit: int) -> dict[str, list[tuple[str, float]]]:
    index = VectorIndex.get()
    return {
        "table": index.search(query_vector, limit, doc_type="table"),
        "column": index.search(query_vector, limit, doc_type="column"),
    }


def _rrf(rank: int, k: int) -> float:
    return 1.0 / (k + rank + 1)


def _table_of(doc_id: str) -> tuple[str, str | None]:
    """('table::cat.sch.tbl' | 'column::cat.sch.tbl.col') -> (table_id, column)."""
    kind, _, rest = doc_id.partition("::")
    if kind == "table":
        return rest, None
    parts = rest.rsplit(".", 1)
    return (parts[0], parts[1]) if len(parts) == 2 else (rest, None)


# ----------------------------------------------------------------------
# fusion
# ----------------------------------------------------------------------


def fuse(
    store: Store,
    vector_hits: dict[str, list[tuple[str, float]]],
    lexical_hits: list[tuple[str, float]],
    rrf_k: int,
) -> dict[str, Candidate]:
    candidates: dict[str, Candidate] = {}

    def add(doc_id: str, rank: int, signal: str, weight: float = 1.0) -> None:
        tid, column = _table_of(doc_id)
        cand = candidates.setdefault(tid, Candidate(table_id=tid))
        cand.text_score += weight * _rrf(rank, rrf_k)
        cand.signals.setdefault(signal, rank + 1)
        if column and column not in cand.matched_columns:
            cand.matched_columns.append(column)

    for rank, (doc_id, _score) in enumerate(vector_hits.get("table", [])):
        add(doc_id, rank, "vector_table")
    for rank, (doc_id, _score) in enumerate(vector_hits.get("column", [])):
        # A single strong column match is weaker evidence than a strong table
        # match, but several columns from the same table compound.
        add(doc_id, rank, "vector_column", weight=0.7)
    for rank, (doc_id, _score) in enumerate(lexical_hits):
        kind = doc_id.split("::", 1)[0]
        add(doc_id, rank, f"lexical_{kind}", weight=1.0 if kind == "table" else 0.7)

    # Drop anything that no longer exists in the snapshot.
    return {tid: c for tid, c in candidates.items() if store.get_table(tid)}


def apply_lineage_prior(
    store: Store,
    candidates: dict[str, Candidate],
    seed_count: int,
    hops: int,
    lineage_boost: float,
    cooccurrence_boost: float,
) -> None:
    """Boost tables by their graph position relative to the strongest text
    matches, and pull in graph neighbours that text search missed entirely."""
    if not candidates:
        return
    ranked = sorted(candidates.values(), key=lambda c: c.text_score, reverse=True)
    seeds = [c.table_id for c in ranked[:seed_count]]
    if not seeds:
        return

    reach = neighborhood(store, seeds, hops=hops)
    best_text = ranked[0].text_score or 1.0

    for tid, info in reach.items():
        if info.distance == 0:
            continue
        if not store.get_table(tid):
            continue
        cand = candidates.get(tid)
        if cand is None:
            # A neighbour with no text match at all still deserves to be
            # considered, but starts from zero textual evidence.
            cand = Candidate(table_id=tid)
            candidates[tid] = cand
            cand.signals["graph_only"] = True

        proximity = lineage_boost / info.distance
        shared = len(info.seeds)
        if shared > 1:
            proximity += cooccurrence_boost * (shared - 1) / max(1, len(seeds) - 1)
        cand.graph_score += proximity * best_text
        cand.signals["graph_distance"] = info.distance
        cand.signals["graph_direction"] = info.direction
        cand.signals["reached_from"] = sorted(info.seeds)


# ----------------------------------------------------------------------
# candidate cards + rerank
# ----------------------------------------------------------------------


def candidate_card(store: Store, cand: Candidate, max_columns: int = 14) -> dict:
    meta = store.get_table(cand.table_id) or {}
    columns = store.get_columns(cand.table_id)
    matched = [c for c in columns if c["column_name"] in cand.matched_columns]
    others = [c for c in columns if c["column_name"] not in cand.matched_columns]
    shown = (matched + others)[:max_columns]
    up, down = store.neighbors(cand.table_id)
    return {
        "table_id": cand.table_id,
        "table_type": meta.get("table_type"),
        "owner": meta.get("owner"),
        "comment": meta.get("comment"),
        "column_count": len(columns),
        "columns": [
            {"name": c["column_name"], "type": c["data_type"], "comment": c["comment"]}
            for c in shown
        ],
        "matched_columns": cand.matched_columns,
        "upstream": up[:6],
        "downstream": down[:6],
        "score": round(cand.score, 5),
        "signals": cand.signals,
    }


def rerank_cards(query: str, cards: list[dict], model_id: str | None, session_id: str | None) -> dict[str, dict]:
    """One cheap call that sees compact cards instead of full schemas."""
    from app.llm.client import complete_json
    from app.agent.prompts import render_prompt

    payload = [
        {
            "table_id": c["table_id"],
            "comment": c["comment"],
            "columns": [f"{col['name']} {col['type'] or ''}".strip() + (f" - {col['comment']}" if col["comment"] else "")
                        for col in c["columns"]],
            "matched_columns": c["matched_columns"],
            "upstream": c["upstream"],
            "downstream": c["downstream"],
        }
        for c in cards
    ]
    schema = {
        "type": "object",
        "properties": {
            "rankings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "table_id": {"type": "string"},
                        "relevance": {"type": "number", "description": "0 to 1"},
                        "reason": {"type": "string", "description": "one sentence, cites specific columns"},
                    },
                    "required": ["table_id", "relevance", "reason"],
                },
            }
        },
        "required": ["rankings"],
    }
    result = complete_json(
        stage="rerank",
        system=render_prompt("rerank"),
        user={"request": query, "candidates": payload},
        schema=schema,
        model_id=model_id,
        session_id=session_id,
    )
    return {r["table_id"]: r for r in result.get("rankings", []) if r.get("table_id")}


# ----------------------------------------------------------------------
# entry point
# ----------------------------------------------------------------------


def search_tables(
    query: str,
    top_k: int | None = None,
    use_rerank: bool = True,
    model_id: str | None = None,
    session_id: str | None = None,
    store: Store | None = None,
) -> dict:
    store = store or get_store()
    settings = get_settings()
    cfg = settings.section("retrieval")
    top_k = top_k or int(cfg.get("final_top_k", 8))

    if VectorIndex.get().size == 0:
        return {
            "query": query,
            "results": [],
            "warning": "No embeddings indexed yet. Run a catalog sync first.",
        }

    embedder = get_embedding_client()
    query_vector = embedder.embed_one(query)

    vector_hits = vector_search(query_vector, int(cfg.get("vector_top_k", 40)))
    lexical_hits = lexical_search(store, query, int(cfg.get("lexical_top_k", 40)))

    candidates = fuse(store, vector_hits, lexical_hits, int(cfg.get("rrf_k", 60)))
    apply_lineage_prior(
        store,
        candidates,
        seed_count=max(3, top_k // 2),
        hops=int(cfg.get("lineage_hops", 2)),
        lineage_boost=float(cfg.get("lineage_boost", 0.35)),
        cooccurrence_boost=float(cfg.get("cooccurrence_boost", 0.25)),
    )

    ordered = sorted(candidates.values(), key=lambda c: c.score, reverse=True)
    shortlist = ordered[: int(cfg.get("rerank_candidates", 30))]
    cards = [candidate_card(store, c) for c in shortlist]

    rerank_error = None
    if use_rerank and cards:
        try:
            rankings = rerank_cards(query, cards, model_id, session_id)
            for cand, card in zip(shortlist, cards):
                entry = rankings.get(cand.table_id)
                if entry:
                    cand.rerank_score = float(entry.get("relevance", 0))
                    cand.reason = entry.get("reason")
                    card["relevance"] = cand.rerank_score
                    card["reason"] = cand.reason
            shortlist.sort(key=lambda c: (c.rerank_score if c.rerank_score is not None else -1, c.score), reverse=True)
            cards.sort(key=lambda c: (c.get("relevance", -1), c["score"]), reverse=True)
        except Exception as exc:  # noqa: BLE001 - fall back to the fused order
            log.warning("rerank failed: %s", exc)
            rerank_error = str(exc)

    results = cards[:top_k]
    return {
        "query": query,
        "results": results,
        "considered": len(candidates),
        "embedding_cost_usd": round(embedder.cost_usd, 8),
        "reranked": use_rerank and rerank_error is None,
        "rerank_error": rerank_error,
    }


def multi_search(
    queries: list[str],
    intent_summary: str,
    top_k: int | None = None,
    model_id: str | None = None,
    session_id: str | None = None,
    store: Store | None = None,
) -> dict:
    """Run several phrasings of the same request and rerank the union once.

    Multiple queries buy recall - the analyst's words, the physical column
    vocabulary, the business entities - while the single shared rerank keeps
    it to one LLM call regardless of how many phrasings were tried.
    """
    store = store or get_store()
    settings = get_settings()
    cfg = settings.section("retrieval")
    top_k = top_k or int(cfg.get("final_top_k", 8))

    if VectorIndex.get().size == 0:
        return {"queries": queries, "results": [], "warning": "No embeddings indexed yet. Run a catalog sync first."}

    merged: dict[str, dict] = {}
    embedding_cost = 0.0
    for query in queries:
        partial = search_tables(
            query,
            top_k=int(cfg.get("rerank_candidates", 30)),
            use_rerank=False,
            session_id=session_id,
            store=store,
        )
        embedding_cost += partial.get("embedding_cost_usd", 0.0)
        for card in partial.get("results", []):
            existing = merged.get(card["table_id"])
            if existing is None:
                card = dict(card)
                card["matched_queries"] = [query]
                merged[card["table_id"]] = card
            else:
                existing["score"] = max(existing["score"], card["score"])
                existing["matched_queries"].append(query)
                for column in card.get("matched_columns", []):
                    if column not in existing["matched_columns"]:
                        existing["matched_columns"].append(column)

    cards = sorted(merged.values(), key=lambda c: (len(c["matched_queries"]), c["score"]), reverse=True)
    cards = cards[: int(cfg.get("rerank_candidates", 30))]

    rerank_error = None
    if cards:
        try:
            rankings = rerank_cards(intent_summary, cards, model_id, session_id)
            for card in cards:
                entry = rankings.get(card["table_id"])
                if entry:
                    card["relevance"] = float(entry.get("relevance", 0))
                    card["reason"] = entry.get("reason")
            cards.sort(key=lambda c: (c.get("relevance", -1), c["score"]), reverse=True)
        except Exception as exc:  # noqa: BLE001 - fused order is still useful
            log.warning("rerank failed: %s", exc)
            rerank_error = str(exc)

    return {
        "queries": queries,
        "results": cards[:top_k],
        "all_candidates": cards,
        "considered": len(merged),
        "embedding_cost_usd": round(embedding_cost, 8),
        "reranked": rerank_error is None,
        "rerank_error": rerank_error,
    }
