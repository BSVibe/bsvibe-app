"""Leiden community detection — Lift E20 Phase C.

Each node in the code graph gets a ``community_id`` so the LLM
synthesis phase can produce ONE Pattern/Principle note per community
rather than per file. Leiden is the modern alternative to Louvain
(Traag 2019) — better partition quality, the same speed envelope, and
it's the default in GraphRAG / Mem0 / Cursor's code-RAG paper.

We run Leiden on the UNDIRECTED projection of the code graph. Community
detection cares about "which nodes belong together" — direction
("module imports module" vs "method calls method") is signal the
**LLM summarizer** uses later, but it would force Leiden into the
"directed" branch which is slower and less stable for our size class.

Determinism: igraph's Leiden uses a seeded RNG. We pass an explicit
seed so the same graph + same node order yields the same membership
across runs.
"""

from __future__ import annotations

from typing import Any

import networkx as nx
import structlog

logger = structlog.get_logger(__name__)


#: Leiden RNG seed — fixed so a re-bootstrap of the same repo gives the
#: same community ids. Bumping this re-shuffles every workspace's
#: community ids, which is fine on a fresh bootstrap.
_LEIDEN_SEED = 42


def detect_communities(graph: nx.DiGraph) -> dict[str, int]:
    """Run Leiden on the undirected projection of ``graph``.

    Returns ``{node_id: community_id}`` for every node. Empty graph →
    empty map; a singleton node → ``{node_id: 0}`` without running
    Leiden (igraph dislikes 1-node graphs).
    """
    if graph.number_of_nodes() == 0:
        return {}
    if graph.number_of_nodes() == 1:
        # igraph's Leiden treats this as a degenerate case.
        only_node = next(iter(graph.nodes))
        return {only_node: 0}

    # Undirected projection. We preserve node identity so the
    # membership keys come back as the original ids.
    undirected = graph.to_undirected(as_view=False)
    try:
        import igraph  # noqa: PLC0415 — heavy import, deferred to call site
    except ImportError:  # pragma: no cover — dep declared in pyproject.toml
        logger.warning("leiden_igraph_missing — every node lands in its own community")
        return {nid: idx for idx, nid in enumerate(graph.nodes)}

    # python-igraph's from_networkx walks node order so we read the
    # membership back in the same order.
    ig_graph: Any = igraph.Graph.from_networkx(undirected)
    try:
        result = ig_graph.community_leiden(objective_function="modularity", n_iterations=10)
    except Exception:  # noqa: BLE001 — Leiden's C bindings throw on edge cases
        logger.warning("leiden_detection_failed — falling back to weakly connected components")
        return _fallback_components(graph)

    membership = result.membership
    # python-igraph annotates nodes with ``_nx_name`` carrying the
    # original NetworkX node id.
    ordered_ids = [v["_nx_name"] for v in ig_graph.vs]
    return {nid: int(cid) for nid, cid in zip(ordered_ids, membership, strict=True)}


def _fallback_components(graph: nx.DiGraph) -> dict[str, int]:
    """Use weakly-connected components as a degraded community signal."""
    mapping: dict[str, int] = {}
    for idx, component in enumerate(nx.weakly_connected_components(graph)):
        for nid in component:
            mapping[nid] = idx
    return mapping


def annotate_communities(graph: nx.DiGraph) -> None:
    """Run :func:`detect_communities` and write the result onto the graph.

    After the call, every node has ``graph.nodes[id]["community_id"]``
    set. This is the input to the LLM synthesis loop: group nodes by
    community, summarize each group as a single LLM call.
    """
    memberships = detect_communities(graph)
    for nid, cid in memberships.items():
        graph.nodes[nid]["community_id"] = cid


__all__ = ["annotate_communities", "detect_communities"]
