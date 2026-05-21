"""Graph analytics — centrality, stats, and structural insights.

Provides NetworkX-powered analysis on top of any GraphBackend:
- Centrality measures (degree, betweenness, PageRank)
- Graph-level statistics (density, connected components)
- "God nodes" — highest-degree hubs
- "Knowledge gaps" — isolated nodes, thin communities
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import networkx as nx
import structlog

logger = structlog.get_logger(__name__)


@dataclass
class NodeStats:
    """Per-node centrality and connectivity metrics."""

    id: str
    name: str
    entity_type: str
    degree: int
    betweenness: float = 0.0
    pagerank: float = 0.0


@dataclass
class GraphStats:
    """Graph-level summary statistics."""

    num_nodes: int
    num_edges: int
    num_components: int
    density: float
    avg_degree: float
    isolated_nodes: list[str]  # node IDs with degree == 0


def _pagerank_pure(
    graph: nx.DiGraph, *, alpha: float = 0.85, max_iter: int = 100, tol: float = 1e-6
) -> dict[str, float]:
    """Pure-Python PageRank — avoids scipy/numpy dependency."""
    nodes = list(graph.nodes())
    n = len(nodes)
    if n == 0:
        return {}

    pr = dict.fromkeys(nodes, 1.0 / n)
    out_degree = {node: graph.out_degree(node) for node in nodes}

    for _ in range(max_iter):
        new_pr = dict.fromkeys(nodes, (1.0 - alpha) / n)
        for node in nodes:
            for _pred in graph.predecessors(node):
                od = out_degree.get(_pred, 0)
                if od > 0:
                    new_pr[node] += alpha * pr[_pred] / od
        # Convergence
        delta = sum(abs(new_pr[n] - pr[n]) for n in nodes)
        pr = new_pr
        if delta < tol * n:
            break
    return pr


def compute_centrality(
    graph: nx.MultiDiGraph,
    *,
    top_k: int = 20,
    include_betweenness: bool = False,
) -> list[NodeStats]:
    """Compute centrality metrics and return top-k nodes by PageRank.

    Betweenness is O(n*m) so gated behind a flag for large graphs.
    """
    if graph.number_of_nodes() == 0:
        return []

    # Convert to simple directed for NetworkX algorithms
    simple = nx.DiGraph(graph)

    # Degree: use NetworkX degree (counts multi-edges as separate)
    degrees = dict(graph.degree())

    # PageRank — prefer numpy-free pure-Python implementation when numpy missing
    try:
        pr = _pagerank_pure(simple, alpha=0.85, max_iter=100)
    except nx.NetworkXError:
        pr = dict.fromkeys(graph.nodes, 1.0 / max(graph.number_of_nodes(), 1))

    betweenness: dict[str, float] = {}
    if include_betweenness:
        try:
            betweenness = nx.betweenness_centrality(simple)
        except nx.NetworkXError:
            betweenness = {}

    results: list[NodeStats] = []
    for node_id, attrs in graph.nodes(data=True):
        results.append(
            NodeStats(
                id=node_id,
                name=attrs.get("name", node_id),
                entity_type=attrs.get("entity_type", ""),
                degree=degrees.get(node_id, 0),
                betweenness=betweenness.get(node_id, 0.0),
                pagerank=pr.get(node_id, 0.0),
            )
        )

    results.sort(key=lambda n: -n.pagerank)
    return results[:top_k]


def compute_graph_stats(graph: nx.MultiDiGraph) -> GraphStats:
    """Compute graph-level summary statistics."""
    n_nodes = graph.number_of_nodes()
    n_edges = graph.number_of_edges()

    if n_nodes == 0:
        return GraphStats(
            num_nodes=0,
            num_edges=0,
            num_components=0,
            density=0.0,
            avg_degree=0.0,
            isolated_nodes=[],
        )

    undirected = graph.to_undirected()
    components = list(nx.connected_components(undirected))
    density = nx.density(graph)
    degrees = dict(graph.degree())
    avg_degree = sum(degrees.values()) / n_nodes if n_nodes else 0.0
    isolated = [nid for nid, d in degrees.items() if d == 0]

    return GraphStats(
        num_nodes=n_nodes,
        num_edges=n_edges,
        num_components=len(components),
        density=round(density, 4),
        avg_degree=round(avg_degree, 2),
        isolated_nodes=isolated,
    )


def find_god_nodes(
    graph: nx.MultiDiGraph,
    *,
    top_k: int = 10,
    min_degree: int = 5,
) -> list[NodeStats]:
    """Return highest-degree hub entities (the graph's "god nodes").

    Filters out low-degree noise nodes via ``min_degree``.
    """
    degrees = dict(graph.degree())
    candidates = [(nid, d) for nid, d in degrees.items() if d >= min_degree]
    candidates.sort(key=lambda x: -x[1])

    results: list[NodeStats] = []
    for node_id, deg in candidates[:top_k]:
        attrs = graph.nodes[node_id]
        results.append(
            NodeStats(
                id=node_id,
                name=attrs.get("name", node_id),
                entity_type=attrs.get("entity_type", ""),
                degree=deg,
            )
        )
    return results


def find_knowledge_gaps(graph: nx.MultiDiGraph) -> dict[str, Any]:
    """Identify structural weaknesses in the knowledge graph.

    Returns a dict with:
    - ``isolated`` — fully disconnected nodes (degree 0)
    - ``thin`` — low-connectivity nodes (degree 1) suggesting stubs
    - ``small_components`` — connected subgraphs smaller than 3 nodes
    """
    if graph.number_of_nodes() == 0:
        return {"isolated": [], "thin": [], "small_components": []}

    degrees = dict(graph.degree())
    isolated = [
        {"id": nid, "name": graph.nodes[nid].get("name", nid)}
        for nid, d in degrees.items()
        if d == 0
    ]
    thin = [
        {"id": nid, "name": graph.nodes[nid].get("name", nid)}
        for nid, d in degrees.items()
        if d == 1
    ]

    undirected = graph.to_undirected()
    small_components = []
    for comp in nx.connected_components(undirected):
        if 2 <= len(comp) < 3:
            small_components.append(
                [{"id": nid, "name": graph.nodes[nid].get("name", nid)} for nid in comp]
            )

    return {
        "isolated": isolated,
        "thin": thin,
        "small_components": small_components,
    }
