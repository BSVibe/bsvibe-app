"""Hybrid search — RRF-combined ranking across BM25, graph, and vector.

Graphiti-inspired: runs multiple search methods in parallel and fuses results
using Reciprocal Rank Fusion (RRF). BM25 handles keyword precision, graph
traversal handles relational relevance, and optional embedding similarity
handles semantic proximity.
"""

from __future__ import annotations

import asyncio
import math
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import structlog

from backend.knowledge.graph.graph_backend import GraphBackend
from backend.knowledge.graph.graph_models import GraphEntity
from backend.knowledge.retrieval.vector_store import _cosine_similarity

logger = structlog.get_logger(__name__)

EmbedFn = Callable[[str], Awaitable[list[float]]]


@dataclass
class SearchResult:
    """Hybrid search result with per-method contributions."""

    entity: GraphEntity
    score: float
    bm25_rank: int | None = None
    graph_rank: int | None = None
    vector_rank: int | None = None
    matched_via: list[str] = field(default_factory=list)


_TOKENIZE_RE = re.compile(r"[^\w]+", re.UNICODE)


def _tokenize(text: str) -> list[str]:
    return [t for t in _TOKENIZE_RE.split(text.lower()) if t]


def _bm25_score(
    query_tokens: list[str],
    doc_tokens: list[str],
    avg_doc_len: float,
    doc_freqs: dict[str, int],
    total_docs: int,
    *,
    k1: float = 1.5,
    b: float = 0.75,
) -> float:
    """Compute BM25 score for a document given a query."""
    if not doc_tokens:
        return 0.0

    doc_len = len(doc_tokens)
    token_counts: dict[str, int] = {}
    for t in doc_tokens:
        token_counts[t] = token_counts.get(t, 0) + 1

    score = 0.0
    for term in query_tokens:
        tf = token_counts.get(term, 0)
        if tf == 0:
            continue
        df = doc_freqs.get(term, 0)
        if df == 0:
            continue
        idf = math.log((total_docs - df + 0.5) / (df + 0.5) + 1)
        numerator = tf * (k1 + 1)
        denominator = tf + k1 * (1 - b + b * doc_len / max(avg_doc_len, 1))
        score += idf * numerator / denominator
    return score


async def _bm25_search(
    backend: GraphBackend,
    query: str,
    *,
    limit: int,
) -> list[GraphEntity]:
    """BM25 search over all entity names."""
    query_tokens = _tokenize(query)
    if not query_tokens:
        return []

    # Gather all entities via to_networkx (one pass)
    graph = backend.to_networkx()
    entities: list[GraphEntity] = []
    doc_tokens_list: list[list[str]] = []
    for node_id, attrs in graph.nodes(data=True):
        name = attrs.get("name", "")
        if not name:
            continue
        tokens = _tokenize(name)
        doc_tokens_list.append(tokens)
        entities.append(
            GraphEntity(
                id=node_id,
                name=name,
                entity_type=attrs.get("entity_type", ""),
                source_path=attrs.get("source_path", ""),
                properties=attrs.get("properties", {}),
                confidence=attrs.get("confidence", ""),
                knowledge_layer=attrs.get("knowledge_layer"),
            )
        )

    if not entities:
        return []

    total = len(doc_tokens_list)
    avg_len = sum(len(d) for d in doc_tokens_list) / total
    doc_freqs: dict[str, int] = {}
    for doc in doc_tokens_list:
        for term in set(doc):
            doc_freqs[term] = doc_freqs.get(term, 0) + 1

    scored = [
        (i, _bm25_score(query_tokens, doc_tokens_list[i], avg_len, doc_freqs, total))
        for i in range(total)
    ]
    scored = [(i, s) for i, s in scored if s > 0]
    scored.sort(key=lambda x: -x[1])

    return [entities[i] for i, _ in scored[:limit]]


async def _graph_search(
    backend: GraphBackend,
    query: str,
    *,
    limit: int,
) -> list[GraphEntity]:
    """Graph-based search: substring match + neighbor expansion."""
    if not query.strip():
        return []
    seeds = await backend.search_entities(query, limit=3)
    seen_ids: set[str] = set()
    results: list[GraphEntity] = []

    for seed in seeds:
        if seed.id not in seen_ids:
            seen_ids.add(seed.id)
            results.append(seed)

    hop_results = await asyncio.gather(
        *(backend.multi_hop_query(seed.id, max_hops=1) for seed in seeds)
    )
    for hops in hop_results:
        for _depth, ent in hops:
            if ent.id not in seen_ids:
                seen_ids.add(ent.id)
                results.append(ent)
                if len(results) >= limit:
                    return results

    return results[:limit]


async def _vector_search(
    backend: GraphBackend,
    query: str,
    embed_fn: EmbedFn,
    *,
    limit: int,
) -> list[GraphEntity]:
    """Vector similarity search over entity embeddings."""
    q_vec = await embed_fn(query)
    graph = backend.to_networkx()

    scored: list[tuple[float, GraphEntity]] = []
    for node_id, attrs in graph.nodes(data=True):
        emb = attrs.get("properties", {}).get("embedding")
        if not emb or not isinstance(emb, list) or len(emb) != len(q_vec):
            continue
        sim = _cosine_similarity(q_vec, emb)
        ent = GraphEntity(
            id=node_id,
            name=attrs.get("name", ""),
            entity_type=attrs.get("entity_type", ""),
            source_path=attrs.get("source_path", ""),
            properties=attrs.get("properties", {}),
            confidence=attrs.get("confidence", ""),
            knowledge_layer=attrs.get("knowledge_layer"),
        )
        scored.append((sim, ent))

    scored.sort(key=lambda x: -x[0])
    return [e for _, e in scored[:limit]]


async def hybrid_search(
    backend: GraphBackend,
    query: str,
    *,
    limit: int = 10,
    embed_fn: EmbedFn | None = None,
    rrf_k: int = 60,
) -> list[SearchResult]:
    """Run BM25 + graph + (optional) vector search in parallel, fuse with RRF.

    RRF score for each candidate is ``sum(1 / (rrf_k + rank))`` across methods.
    Standard value k=60 is from the original RRF paper.
    """
    tasks = [
        _bm25_search(backend, query, limit=limit * 2),
        _graph_search(backend, query, limit=limit * 2),
    ]
    if embed_fn is not None:
        tasks.append(_vector_search(backend, query, embed_fn, limit=limit * 2))

    raw_lists = await asyncio.gather(*tasks, return_exceptions=True)

    def _ok(idx: int) -> list[GraphEntity]:
        if idx >= len(raw_lists):
            return []
        value = raw_lists[idx]
        if isinstance(value, BaseException):
            return []
        return value

    bm25_list = _ok(0)
    graph_list = _ok(1)
    vector_list = _ok(2)

    # RRF fusion
    scores: dict[str, float] = {}
    entity_lookup: dict[str, GraphEntity] = {}
    ranks: dict[str, dict[str, int]] = {}

    def _contribute(method: str, results: list[GraphEntity]) -> None:
        for rank, ent in enumerate(results, start=1):
            scores[ent.id] = scores.get(ent.id, 0.0) + 1.0 / (rrf_k + rank)
            entity_lookup[ent.id] = ent
            ranks.setdefault(ent.id, {})[method] = rank

    _contribute("bm25", bm25_list)
    _contribute("graph", graph_list)
    _contribute("vector", vector_list)

    # Sort by combined RRF score
    ordered = sorted(scores.items(), key=lambda x: -x[1])[:limit]

    results: list[SearchResult] = []
    for ent_id, score in ordered:
        r = ranks.get(ent_id, {})
        results.append(
            SearchResult(
                entity=entity_lookup[ent_id],
                score=score,
                bm25_rank=r.get("bm25"),
                graph_rank=r.get("graph"),
                vector_rank=r.get("vector"),
                matched_via=sorted(r.keys()),
            )
        )

    logger.info(
        "hybrid_search",
        query=query,
        bm25=len(bm25_list),
        graph=len(graph_list),
        vector=len(vector_list),
        fused=len(results),
    )
    return results
