"""Tests for backend.knowledge.code_graph.community — Lift E20 Phase C.

Leiden community detection on the undirected projection.
"""

from __future__ import annotations

import networkx as nx
import pytest

from backend.knowledge.code_graph.community import (
    annotate_communities,
    derive_community_labels,
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


# ── Lift E25 — community labels ──────────────────────────────────────────────


def _seed_community_graph() -> nx.DiGraph:
    """Build a graph with two semantically distinct communities by file path
    so derive_community_labels can ground each one in a recognizable label.

    Community 0 — auth/oauth files (3 nodes, all under backend/api/v1/oauth/).
    Community 1 — dispatch files (3 nodes, under backend/dispatch/).
    """
    g = nx.DiGraph()

    def _add(nid: str, path: str, name: str, cid: int, kind: str = "module") -> None:
        g.add_node(
            nid,
            id=nid,
            kind=kind,
            name=name,
            path=path,
            language="python",
            community_id=cid,
            pagerank=0.1,
        )

    _add("a1", "backend/api/v1/oauth/server.py", "server", 0)
    _add("a2", "backend/api/v1/oauth/callback.py", "callback", 0)
    _add("a3", "backend/api/v1/oauth/dcr.py", "dcr", 0)
    _add("d1", "backend/dispatch/adapter.py", "adapter", 1)
    _add("d2", "backend/dispatch/resolver.py", "resolver", 1)
    _add("d3", "backend/dispatch/__init__.py", "dispatch", 1)
    return g


class TestDeriveCommunityLabels:
    """E25 — each community gets a structured label so the founder can see
    WHY nodes were grouped (common path / top symbols / size) instead of
    bare integer community ids."""

    def test_labels_capture_common_path_prefix(self) -> None:
        g = _seed_community_graph()
        labels = derive_community_labels(g, min_size=3)

        assert 0 in labels
        assert 1 in labels
        assert labels[0]["label"] == "backend/api/v1/oauth"
        assert labels[1]["label"] == "backend/dispatch"

    def test_labels_include_size_and_top_symbols(self) -> None:
        g = _seed_community_graph()
        labels = derive_community_labels(g, min_size=3)

        l0 = labels[0]
        assert l0["size"] == 3
        # Top symbols are the three module names.
        assert set(l0["top_symbols"]) == {"server", "callback", "dcr"}
        # File count + language summary populated.
        assert l0["file_count"] == 3
        assert l0["languages"] == {"python": 3}

    def test_labels_include_human_readable_description(self) -> None:
        g = _seed_community_graph()
        labels = derive_community_labels(g, min_size=3)

        l1 = labels[1]
        desc = l1["description"]
        assert isinstance(desc, str)
        assert "backend/dispatch" in desc
        assert "3 files" in desc

    def test_small_communities_below_min_size_are_dropped(self) -> None:
        g = _seed_community_graph()
        # Drop one node from community 0 so it has only 2 nodes.
        g.remove_node("a3")
        labels = derive_community_labels(g, min_size=3)
        assert 0 not in labels, "community below min_size must not be labelled"
        assert 1 in labels

    def test_empty_graph_returns_empty_labels(self) -> None:
        labels = derive_community_labels(nx.DiGraph(), min_size=3)
        assert labels == {}

    def test_mixed_paths_falls_back_to_root_prefix(self) -> None:
        """Members with no shared parent directory get the shallowest
        meaningful prefix (or a generic 'misc') so the label never
        explodes into a long irrelevant path."""
        g = nx.DiGraph()
        g.add_node("x", id="x", path="src/a/foo.py", name="foo", language="python", community_id=2)
        g.add_node("y", id="y", path="lib/b/bar.py", name="bar", language="python", community_id=2)
        g.add_node(
            "z", id="z", path="other/c/baz.py", name="baz", language="python", community_id=2
        )
        labels = derive_community_labels(g, min_size=3)

        assert 2 in labels
        # No common prefix => empty string fallback (handled as "misc")
        assert labels[2]["label"] in {"misc", ""}


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
