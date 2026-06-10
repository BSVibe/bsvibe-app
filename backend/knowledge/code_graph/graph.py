"""NetworkX graph builder + JSON persistence — Lift E20 Phase C.

The graph's only job is to be a structured view the LLM (one synthesis
call per community) and the MCP query tools can read. NetworkX is the
right shape:

* ``DiGraph`` so direction is preserved (imports / calls / inherits all
  have direction; doc references too).
* Node attribute dict carries the full :class:`CodeNode.to_dict` shape
  plus optional ``community_id`` (set by :mod:`.community`) and
  ``pagerank`` (set by :func:`top_nodes_by_pagerank` when computed).

Persistence is JSON (``graph.json``) at
``<vault_root>/<region>/<workspace_id>/code_graph/graph.json``. The
shape is intentionally human-readable so the founder can ``cat`` it
when debugging.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import networkx as nx
import structlog

from backend.knowledge.code_graph.types import CodeEdge, CodeNode

logger = structlog.get_logger(__name__)


def build_graph(nodes: Iterable[CodeNode], edges: Iterable[CodeEdge]) -> nx.DiGraph:
    """Compose nodes + edges into a typed ``networkx.DiGraph``.

    Each node carries its :meth:`CodeNode.to_dict` as the attribute
    dictionary so downstream consumers can read all the parsed metadata
    via ``graph.nodes[id]``.

    Edges keep their ``kind`` attribute (``imports`` / ``calls`` /
    ``inherits`` / ``doc_references``) so a follow-up filter pass can
    say "show me only the import sub-graph".

    Dropped: edges whose source or destination is not in the node set
    (the parser already emits external-symbol destinations with a
    ``external:`` prefix; we keep those, but only if the source IS in
    the node set — orphan edges are noise).
    """
    g: nx.DiGraph = nx.DiGraph()
    node_ids: set[str] = set()
    for node in nodes:
        g.add_node(node.id, **node.to_dict())
        node_ids.add(node.id)
    for edge in edges:
        if edge.src_id not in node_ids:
            continue
        # Lazy-add ``external:`` / ``wiki::`` destinations so the edge
        # is present and a future cross-file pass can rewire it.
        if edge.dst_id not in node_ids:
            g.add_node(
                edge.dst_id,
                id=edge.dst_id,
                kind="external",
                name=edge.dst_id.split("::", 1)[-1],
                path="",
                start_line=0,
                end_line=0,
                language="",
            )
            node_ids.add(edge.dst_id)
        g.add_edge(edge.src_id, edge.dst_id, kind=edge.kind.value)
    return g


def top_nodes_by_pagerank(graph: nx.DiGraph, *, limit: int) -> list[tuple[str, float]]:
    """Return the top-``limit`` (node_id, pagerank) tuples, highest first.

    Edge direction matters — calls / inherits flow toward the
    "more-referenced" definition, so PageRank on the directed graph is
    the right Aider-style centrality. We also annotate the graph in
    place so callers that want every node's score can read it from
    ``graph.nodes[id]["pagerank"]``.

    An empty graph returns ``[]``; a graph with no edges falls back to
    the uniform PageRank (everyone is the same score).
    """
    if graph.number_of_nodes() == 0:
        return []
    try:
        scores = nx.pagerank(graph)
    except nx.NetworkXError:
        # Defensive — undirected NetworkX edge cases. Fall back to
        # uniform centrality so the loop still produces a sane order.
        scores = {n: 1.0 / graph.number_of_nodes() for n in graph.nodes}
    for nid, score in scores.items():
        graph.nodes[nid]["pagerank"] = score
    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    return ranked[:limit]


def save_graph(graph: nx.DiGraph, path: Path) -> None:
    """Write the graph as JSON (``nodes``+``edges`` lists) to ``path``.

    Atomic via temp-file + ``os.replace`` so a crashed process can't
    leave a half-written file. Parent directory is created on demand
    (the founder's vault may not have the ``code_graph/`` subfolder
    yet at first bootstrap).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "nodes": [{"id": nid, **_serialize_attrs(graph.nodes[nid])} for nid in graph.nodes],
        "edges": [{"src_id": u, "dst_id": v, **dict(d)} for u, v, d in graph.edges(data=True)],
    }
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=str(path.parent), delete=False
    ) as tmp:
        json.dump(payload, tmp, ensure_ascii=False)
        tmp_path = tmp.name
    os.replace(tmp_path, path)


def _serialize_attrs(raw: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in raw.items():
        if k == "id":
            continue
        out[k] = v
    return out


def load_graph(path: Path) -> nx.DiGraph:
    """Read a previously-saved ``graph.json`` back into a ``DiGraph``."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    g: nx.DiGraph = nx.DiGraph()
    for node in raw.get("nodes", []):
        nid = node["id"]
        attrs = {k: v for k, v in node.items() if k != "id"}
        g.add_node(nid, id=nid, **attrs)
    for edge in raw.get("edges", []):
        attrs = {k: v for k, v in edge.items() if k not in ("src_id", "dst_id")}
        g.add_edge(edge["src_id"], edge["dst_id"], **attrs)
    return g


__all__ = [
    "build_graph",
    "load_graph",
    "save_graph",
    "top_nodes_by_pagerank",
]
