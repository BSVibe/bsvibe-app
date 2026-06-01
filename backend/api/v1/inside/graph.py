"""``GET /api/v1/inside/graph`` — the force-directed knowledge graph view.

Nodes + edges, built deterministically from the per-workspace canonicalization
vault (FS-as-SoT) by :func:`build_concept_graph`. Strictly read-only. When the
graph is large it is capped to the ``_MAX_GRAPH_NODES`` most-connected nodes
so the founder sees the structural hubs; edges are filtered to those between
surviving nodes so the response stays internally consistent.
"""

from __future__ import annotations

from typing import Annotated

import networkx as nx
from fastapi import APIRouter, Depends

from ._dependencies import build_inside_graph
from ._helpers import _MAX_GRAPH_NODES
from ._schemas import GraphEdge, GraphNode, GraphResponse

router = APIRouter()


@router.get("/graph")
async def get_graph(
    graph: Annotated[nx.MultiDiGraph, Depends(build_inside_graph)],
) -> GraphResponse:
    """The workspace knowledge graph as nodes + edges for a force-directed view.

    Entities → nodes (id + display name + entity_type + degree), relationships →
    edges (source/target/type/weight), built deterministically from the
    per-workspace canonicalization vault (FS-as-SoT) by
    :func:`build_inside_graph`. Strictly read-only.

    When the graph is large it is capped to the ``_MAX_GRAPH_NODES`` most-
    connected nodes (top-N by degree — the hubs the founder cares about);
    edges are filtered to those between surviving nodes so the response stays
    internally consistent. A fresh/sparse workspace yields
    ``{nodes: [], edges: []}`` — 200, never an error.
    """
    if graph.number_of_nodes() == 0:
        return GraphResponse(nodes=[], edges=[])

    degrees = dict(graph.degree())

    # Cap to the most-connected nodes (highest degree — the structural hubs)
    # when the graph exceeds the view budget. Degree, not PageRank: a hub with
    # many out-edges should survive, but PageRank flows rank *to* its leaves.
    if graph.number_of_nodes() > _MAX_GRAPH_NODES:
        ranked = sorted(degrees.items(), key=lambda item: item[1], reverse=True)
        keep_ids = {node_id for node_id, _deg in ranked[:_MAX_GRAPH_NODES]}
    else:
        keep_ids = set(graph.nodes())

    nodes = [
        GraphNode(
            id=str(node_id),
            label=str(attrs.get("name") or node_id),
            kind=(str(attrs["entity_type"]) if attrs.get("entity_type") else None),
            community=(str(attrs["community"]) if attrs.get("community") else None),
            weight=int(degrees.get(node_id, 0)),
        )
        for node_id, attrs in graph.nodes(data=True)
        if node_id in keep_ids
    ]

    # Dedupe parallel multi-edges into a single edge per (source, target, type)
    # — a calm picture, not every recorded fact — and keep only edges between
    # surviving (kept) nodes.
    seen: set[tuple[str, str, str | None]] = set()
    edges: list[GraphEdge] = []
    for source, target, attrs in graph.edges(data=True):
        if source not in keep_ids or target not in keep_ids:
            continue
        rel_type = str(attrs["rel_type"]) if attrs.get("rel_type") else None
        key = (str(source), str(target), rel_type)
        if key in seen:
            continue
        seen.add(key)
        edges.append(
            GraphEdge(
                source=str(source),
                target=str(target),
                type=rel_type,
                weight=float(attrs.get("weight", 0.5)),
            )
        )

    return GraphResponse(nodes=nodes, edges=edges)


__all__ = ["router"]
