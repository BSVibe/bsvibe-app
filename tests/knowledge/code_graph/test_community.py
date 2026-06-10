"""Tests for backend.knowledge.code_graph.community — Lift E20 Phase C.

Leiden community detection on the undirected projection.
"""

from __future__ import annotations

import networkx as nx
import pytest

from backend.knowledge.code_graph.community import (
    annotate_communities,
    detect_communities,
)


def _two_cluster_graph() -> nx.DiGraph:
    g = nx.DiGraph()
    # Cluster A — densely connected triangle.
    g.add_edge("a", "b")
    g.add_edge("b", "c")
    g.add_edge("c", "a")
    # Cluster B — separate triangle.
    g.add_edge("x", "y")
    g.add_edge("y", "z")
    g.add_edge("z", "x")
    # No edge between clusters.
    return g


class TestDetectCommunities:
    def test_two_disconnected_clusters_get_distinct_ids(self) -> None:
        g = _two_cluster_graph()
        memberships = detect_communities(g)
        # Each cluster gets one id; the two cluster ids differ.
        a_id = memberships["a"]
        b_id = memberships["x"]
        assert memberships["b"] == a_id
        assert memberships["c"] == a_id
        assert memberships["y"] == b_id
        assert memberships["z"] == b_id
        assert a_id != b_id

    def test_empty_graph_returns_empty_map(self) -> None:
        g = nx.DiGraph()
        assert detect_communities(g) == {}

    def test_singleton_graph_one_community(self) -> None:
        g = nx.DiGraph()
        g.add_node("solo")
        m = detect_communities(g)
        assert m == {"solo": 0}


class TestAnnotateCommunities:
    def test_annotate_writes_community_id_attribute(self) -> None:
        g = _two_cluster_graph()
        annotate_communities(g)
        for node in g.nodes:
            assert "community_id" in g.nodes[node]


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
