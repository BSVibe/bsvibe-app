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

    def test_cross_cutting_community_surfaces_subareas(self) -> None:
        """A large community whose majority label is shallow (e.g. just
        'backend') but which actually spans two co-dominant sub-areas must
        surface those sub-areas — in a ``subareas`` field and in the
        description — instead of leaving the founder with a bare 'backend'.
        The label itself stays honest (not falsely narrowed)."""
        g = nx.DiGraph()
        # 5 files under backend/api + 4 under backend/mcp → label "backend",
        # but the community clearly spans api + mcp.
        paths = [f"backend/api/a{i}.py" for i in range(5)] + [
            f"backend/mcp/m{i}.py" for i in range(4)
        ]
        for idx, p in enumerate(paths):
            g.add_node(
                f"n{idx}",
                id=f"n{idx}",
                kind="module",
                path=p,
                name=f"sym{idx}",
                language="python",
                community_id=8,
                pagerank=0.1,
            )
        labels = derive_community_labels(g, min_size=3)

        assert 8 in labels
        lab = labels[8]
        # Label stays the honest majority prefix.
        assert lab["label"] == "backend"
        # Sub-areas surface both co-dominant areas.
        assert set(lab["subareas"]) == {"backend/api", "backend/mcp"}
        assert "backend/api" in lab["description"]
        assert "backend/mcp" in lab["description"]

    def test_uniform_deep_community_has_no_subareas(self) -> None:
        """A community that lives entirely under one deep directory has no
        meaningful sub-areas — the ``subareas`` field stays empty rather
        than echoing the label."""
        g = _seed_community_graph()  # community 0 all under backend/api/v1/oauth
        labels = derive_community_labels(g, min_size=3)
        assert labels[0]["subareas"] == []

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

    def test_top_symbols_exclude_external_stubs(self) -> None:
        """External import stubs (BaseModel, typing, …) are the most-central
        nodes by PageRank because everything imports them. They must NOT
        dominate a community's ``top_symbols`` — the label should surface the
        community's own local symbols, mirroring the path filter that already
        skips ``kind == 'external'``."""
        g = nx.DiGraph()
        # A high-PageRank external stub that is a member of the community.
        g.add_node(
            "external:BaseModel",
            id="external:BaseModel",
            kind="external",
            name="symbol/BaseModel",
            path="",
            community_id=3,
            pagerank=0.05,  # far higher than the local nodes
        )
        for idx, (name, pr) in enumerate([("ConnectorResolver", 0.002), ("dispatch", 0.001)]):
            g.add_node(
                f"local{idx}",
                id=f"local{idx}",
                kind="class" if idx == 0 else "module",
                name=name,
                path=f"backend/dispatch/m{idx}.py",
                language="python",
                community_id=3,
                pagerank=pr,
            )
        labels = derive_community_labels(g, min_size=3)

        assert 3 in labels
        top = labels[3]["top_symbols"]
        assert "symbol/BaseModel" not in top
        # The local symbols survive, ranked by their own centrality.
        assert top[0] == "ConnectorResolver"

    def test_dominant_prefix_survives_a_few_outliers(self) -> None:
        """A community where the vast majority of files share a top-level
        directory must label by that dominant prefix — NOT collapse to
        'misc' just because a couple of outlier files break a strict
        full-consensus prefix. Mirrors the real prod community-9 shape
        (91% backend, a few plugin/sdk outliers) that surfaced as 'misc'."""
        g = nx.DiGraph()
        # 8 files under backend/** (no single shared subdir) ...
        backend_paths = [
            "backend/data/migrations/m1.py",
            "backend/data/migrations/m2.py",
            "backend/knowledge/graph/store.py",
            "backend/knowledge/retrieval/r.py",
            "backend/workflow/application/run.py",
            "backend/router/dispatch.py",
            "backend/connectors/auth/oauth.py",
            "backend/executors/worker/loop.py",
        ]
        # ... plus 2 outliers under entirely different top dirs.
        outlier_paths = ["plugin/obsidian/sync.py", "bsvibe_sdk/client.py"]
        for idx, p in enumerate(backend_paths + outlier_paths):
            g.add_node(
                f"n{idx}",
                id=f"n{idx}",
                kind="module",
                path=p,
                name=f"sym{idx}",
                language="python",
                community_id=7,
                pagerank=0.1,
            )
        labels = derive_community_labels(g, min_size=3)

        assert 7 in labels
        # 8/10 = 80% share 'backend' => dominant prefix label, not 'misc'.
        assert labels[7]["label"] == "backend"


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
