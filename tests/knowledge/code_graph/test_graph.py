"""Tests for backend.knowledge.code_graph.graph — Lift E20 Phase C.

The graph module assembles per-file ParseResult records into a
``networkx.DiGraph``, computes PageRank centrality, and serializes the
result to ``graph.json`` on the vault. ``load_graph`` round-trips it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.knowledge.code_graph.graph import (
    annotate_pagerank,
    build_graph,
    load_graph,
    save_graph,
    top_nodes_by_pagerank,
)
from backend.knowledge.code_graph.parser import parse_source
from backend.knowledge.code_graph.types import CodeEdge, CodeNode


def _py(path: str, source: bytes) -> tuple[list[CodeNode], list[CodeEdge]]:
    result = parse_source(path=path, source=source, language="python")
    return result.nodes, result.edges


class TestBuildGraph:
    def test_empty_inputs_returns_empty_graph(self) -> None:
        g = build_graph([], [])
        assert g.number_of_nodes() == 0
        assert g.number_of_edges() == 0

    def test_nodes_become_graph_nodes(self) -> None:
        nodes, edges = _py(
            "a.py",
            b"def f():\n    pass\nclass C:\n    def m(self):\n        f()\n",
        )
        g = build_graph(nodes, edges)
        assert g.number_of_nodes() == len(nodes)
        # Module + f + C + C.m → at minimum 4 nodes.
        assert g.number_of_nodes() >= 4

    def test_edges_become_graph_edges(self) -> None:
        nodes, edges = _py(
            "a.py",
            b"def f():\n    pass\n\ndef g():\n    f()\n",
        )
        graph = build_graph(nodes, edges)
        # CALLS edge from g to f survives.
        callers = list(graph.predecessors("python:a.py::f"))
        assert any(c.endswith("::g") for c in callers)


class TestPageRank:
    def test_pagerank_returns_descending_score(self) -> None:
        # Two functions; one calls the other.
        nodes, edges = _py(
            "a.py",
            b"def helper():\n    pass\n\ndef caller():\n    helper()\n",
        )
        g = build_graph(nodes, edges)
        ranked = top_nodes_by_pagerank(g, limit=4)
        # helper has incoming edge → ranks above caller.
        ids = [r[0] for r in ranked]
        helper_idx = ids.index("python:a.py::helper")
        caller_idx = ids.index("python:a.py::caller")
        assert helper_idx < caller_idx


class TestAnnotatePageRank:
    """The MCP graph_search ranking and the E25 community top_symbols both
    read ``node["pagerank"]``. If the pipeline never annotates it, every
    node scores 0.0 and ranking degrades to node-iteration order — which
    is exactly what surfaced on the live graph (all pr=0.0)."""

    def test_every_node_gets_a_pagerank_attr(self) -> None:
        nodes, edges = _py(
            "a.py",
            b"def helper():\n    pass\n\ndef caller():\n    helper()\n",
        )
        g = build_graph(nodes, edges)
        # Pre-condition: build_graph alone does NOT annotate pagerank.
        assert all("pagerank" not in g.nodes[n] for n in g.nodes)

        scores = annotate_pagerank(g)

        for nid in g.nodes:
            assert "pagerank" in g.nodes[nid]
            assert g.nodes[nid]["pagerank"] > 0.0
        # Returned dict mirrors the annotation.
        assert scores["python:a.py::helper"] == g.nodes["python:a.py::helper"]["pagerank"]

    def test_empty_graph_is_a_noop(self) -> None:
        g = build_graph([], [])
        assert annotate_pagerank(g) == {}


class TestSaveLoadGraph:
    def test_round_trip(self, tmp_path: Path) -> None:
        nodes, edges = _py(
            "a.py",
            b"def f():\n    pass\nclass C(Base):\n    def m(self):\n        f()\n",
        )
        g = build_graph(nodes, edges)
        # Assign a sample community map so the persisted form carries it.
        community_map = {n: i % 2 for i, n in enumerate(g.nodes)}
        for node_id, cid in community_map.items():
            g.nodes[node_id]["community_id"] = cid
        out = tmp_path / "graph.json"
        save_graph(g, out)
        assert out.exists()
        # Persisted shape is valid JSON.
        raw = json.loads(out.read_text())
        assert "nodes" in raw and "edges" in raw
        # Round-trip.
        g2 = load_graph(out)
        assert g2.number_of_nodes() == g.number_of_nodes()
        assert g2.number_of_edges() == g.number_of_edges()
        # community_id preserved.
        for nid in g.nodes:
            assert g2.nodes[nid]["community_id"] == g.nodes[nid]["community_id"]


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
