"""Deterministic concept knowledge-graph builder (no LLM, no network).

`build_concept_graph` rebuilds the workspace knowledge graph from the settled
canonicalization vault — active concepts become nodes, and concepts that
co-occur in the same garden observation become ``co-occurs`` edges (weighted by
how many observations they share). Aliases / merged tombstones that point at a
concept node become ``alias-of`` edges.

These tests prove the build is deterministic and pure: they seed a real
per-workspace ``FileSystemStorage`` with active concepts (via a permissive
``CanonicalizationService``, exactly like the promotion e2e helpers) and garden
observation notes whose tags reference those concepts, then assert the graph's
node/edge structure and attribute names (the ones ``get_graph`` reads). No model
or network is involved — the proposer/resolver are purely lexical.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from backend.knowledge.canonicalization.concept_graph import build_concept_graph
from backend.knowledge.canonicalization.index import InMemoryCanonicalizationIndex
from backend.knowledge.canonicalization.lock import AsyncIOMutationLock
from backend.knowledge.canonicalization.resolver import TagResolver
from backend.knowledge.canonicalization.service import CanonicalizationService
from backend.knowledge.canonicalization.store import NoteStore
from backend.knowledge.graph.storage import FileSystemStorage

pytestmark = pytest.mark.asyncio

_FIXED_NOW = datetime(2026, 5, 24, 12, 0, 0)
_REGION = "us-1"
_WORKSPACE_ID = "22222222-2222-2222-2222-222222222222"


@pytest.fixture
def workspace_storage(tmp_path: Path) -> FileSystemStorage:
    vault_root = tmp_path / _REGION / _WORKSPACE_ID
    vault_root.mkdir(parents=True, exist_ok=True)
    return FileSystemStorage(vault_root)


async def _make_permissive_service(storage: FileSystemStorage) -> CanonicalizationService:
    index = InMemoryCanonicalizationIndex()
    await index.initialize(storage)
    return CanonicalizationService(
        store=NoteStore(storage),
        lock=AsyncIOMutationLock(),
        index=index,
        resolver=TagResolver(index=index),
        clock=lambda: _FIXED_NOW,
        safe_mode=lambda: False,
    )


async def _seed_concepts(storage: FileSystemStorage, ids: list[str]) -> None:
    """Create active concepts via the permissive service (real apply path)."""
    service = await _make_permissive_service(storage)
    for cid in ids:
        draft = await service.create_action_draft(
            kind="create-concept", params={"concept": cid, "title": cid}
        )
        await service.apply_action(draft, actor="test")


def _garden_note(*tags: str) -> str:
    lines = ["---", "tags:"]
    lines += [f"  - {t}" for t in tags]
    lines += ["---", "# obs", ""]
    return "\n".join(lines)


def _cooccurs_pairs(graph) -> set[frozenset[str]]:
    return {
        frozenset((s, t)) for s, t, a in graph.edges(data=True) if a.get("rel_type") == "co-occurs"
    }


def _edge_weight(graph, a: str, b: str, rel: str) -> float:
    for s, t, attrs in graph.edges(data=True):
        if {s, t} == {a, b} and attrs.get("rel_type") == rel:
            return float(attrs["weight"])
    raise AssertionError(f"no {rel} edge between {a} and {b}")


# ---------------------------------------------------------------------------
# nodes
# ---------------------------------------------------------------------------
async def test_active_concepts_become_nodes_with_attrs(
    workspace_storage: FileSystemStorage,
) -> None:
    await _seed_concepts(workspace_storage, ["python", "calculator"])

    graph = await build_concept_graph(workspace_storage)

    assert set(graph.nodes()) == {"python", "calculator"}
    for node_id, attrs in graph.nodes(data=True):
        # get_graph reads `name` (display) and `entity_type`.
        assert attrs["entity_type"] == "concept"
        assert attrs["name"] == node_id


async def test_empty_workspace_yields_empty_graph(
    workspace_storage: FileSystemStorage,
) -> None:
    graph = await build_concept_graph(workspace_storage)
    assert graph.number_of_nodes() == 0
    assert graph.number_of_edges() == 0


# ---------------------------------------------------------------------------
# co-occurrence edges
# ---------------------------------------------------------------------------
async def test_cooccurring_concepts_get_a_single_edge(
    workspace_storage: FileSystemStorage,
) -> None:
    await _seed_concepts(workspace_storage, ["python", "calculator", "vaultwarden"])
    # python + calculator co-occur in ONE observation; vaultwarden is alone.
    await workspace_storage.write(
        "garden/seedling/obs-a.md",
        _garden_note("settle", "verified-run", "python", "calculator"),
    )
    await workspace_storage.write(
        "garden/seedling/obs-b.md",
        _garden_note("settle", "vaultwarden"),
    )

    graph = await build_concept_graph(workspace_storage)

    pairs = _cooccurs_pairs(graph)
    assert frozenset({"python", "calculator"}) in pairs
    # vaultwarden never co-occurs with anything → no co-occurs edge to it.
    assert not any("vaultwarden" in p for p in pairs)
    # A single observation → weight 1.0, and exactly one edge (not both dirs).
    assert _edge_weight(graph, "python", "calculator", "co-occurs") == 1.0
    cooccur_edges = [
        (s, t) for s, t, a in graph.edges(data=True) if a.get("rel_type") == "co-occurs"
    ]
    assert len(cooccur_edges) == 1


async def test_cooccurrence_weight_counts_observations(
    workspace_storage: FileSystemStorage,
) -> None:
    await _seed_concepts(workspace_storage, ["python", "calculator"])
    for i in range(2):
        await workspace_storage.write(
            f"garden/seedling/obs-{i}.md",
            _garden_note("settle", "python", "calculator"),
        )

    graph = await build_concept_graph(workspace_storage)

    # Seen together in 2 observations → weight 2.0.
    assert _edge_weight(graph, "python", "calculator", "co-occurs") == 2.0


async def test_no_edge_between_concepts_that_never_cooccur(
    workspace_storage: FileSystemStorage,
) -> None:
    await _seed_concepts(workspace_storage, ["python", "rust"])
    await workspace_storage.write("garden/seedling/obs-py.md", _garden_note("settle", "python"))
    await workspace_storage.write("garden/seedling/obs-rs.md", _garden_note("settle", "rust"))

    graph = await build_concept_graph(workspace_storage)

    assert _cooccurs_pairs(graph) == set()


async def test_no_self_loops(workspace_storage: FileSystemStorage) -> None:
    await _seed_concepts(workspace_storage, ["python"])
    # python tagged twice in the same note must not create a self-loop.
    await workspace_storage.write(
        "garden/seedling/obs.md", _garden_note("settle", "python", "Python")
    )

    graph = await build_concept_graph(workspace_storage)

    assert not any(s == t for s, t in graph.edges())


async def test_unsettled_and_structural_tags_never_become_nodes_or_edges(
    workspace_storage: FileSystemStorage,
) -> None:
    await _seed_concepts(workspace_storage, ["python", "calculator"])
    # Mix in structural markers + tags that were never promoted to a concept
    # (no separate filler deny-list needed — only active concepts become nodes).
    await workspace_storage.write(
        "garden/seedling/obs.md",
        _garden_note("settle", "verified-run", "else", "created", "line", "python", "calculator"),
    )

    graph = await build_concept_graph(workspace_storage)

    # Only the two seeded (active) concepts are nodes — unsettled/structural
    # tags resolve to no active concept, so they never appear.
    assert set(graph.nodes()) == {"python", "calculator"}
    # The real pair still co-occurs (the unsettled tags did not contaminate it).
    assert frozenset({"python", "calculator"}) in _cooccurs_pairs(graph)


async def test_tag_without_active_concept_contributes_no_node_or_edge(
    workspace_storage: FileSystemStorage,
) -> None:
    """A garden tag with no settled concept resolves to ``new_candidate`` and
    must not appear as a node (it is not an active concept)."""
    await _seed_concepts(workspace_storage, ["python"])
    await workspace_storage.write(
        "garden/seedling/obs.md",
        _garden_note("settle", "python", "neverpromoted"),
    )

    graph = await build_concept_graph(workspace_storage)

    assert set(graph.nodes()) == {"python"}
    assert graph.number_of_edges() == 0


# ---------------------------------------------------------------------------
# alias / merge edges
# ---------------------------------------------------------------------------
async def test_alias_edge_connects_two_concept_nodes(
    workspace_storage: FileSystemStorage,
) -> None:
    """When an active concept's id is itself listed as another active concept's
    alias, an ``alias-of`` edge (weight 1.0) connects the two concept nodes.

    (The point of alias edges is to *connect concepts*; an alias spelling that
    is not itself a concept node is skipped — see the merge case below.)"""
    # Two concepts where ``self-host`` is recorded as an alias of ``self-hosting``.
    await workspace_storage.write(
        "concepts/active/self-hosting.md",
        "---\n"
        "created_at: 2026-05-24T12:00:00\n"
        "updated_at: 2026-05-24T12:00:00\n"
        "aliases:\n"
        "  - self-host\n"
        "---\n"
        "# self-hosting\n",
    )
    await workspace_storage.write(
        "concepts/active/self-host.md",
        "---\ncreated_at: 2026-05-24T12:00:00\nupdated_at: 2026-05-24T12:00:00\n---\n# self-host\n",
    )

    graph = await build_concept_graph(workspace_storage)

    assert {"self-hosting", "self-host"} <= set(graph.nodes())
    alias_edges = [(s, t) for s, t, a in graph.edges(data=True) if a.get("rel_type") == "alias-of"]
    assert ("self-host", "self-hosting") in alias_edges
    assert _edge_weight(graph, "self-host", "self-hosting", "alias-of") == 1.0


async def test_merge_alias_to_non_node_is_skipped(
    workspace_storage: FileSystemStorage,
) -> None:
    """After a REAL merge the merged id is a tombstone, not an active concept —
    so it is not a node, and the spec says to skip alias edges whose endpoint is
    not a concept node. The survivor stays a node with no dangling alias edge."""
    service = await _make_permissive_service(workspace_storage)
    for cid in ("self-hosting", "self-host"):
        draft = await service.create_action_draft(
            kind="create-concept", params={"concept": cid, "title": cid}
        )
        await service.apply_action(draft, actor="test")
    merge = await service.create_action_draft(
        kind="merge-concepts",
        params={"canonical": "self-hosting", "merge": ["self-host"]},
    )
    await service.apply_action(merge, actor="test")

    graph = await build_concept_graph(workspace_storage)

    # Only the survivor is a node; the merged id (now a tombstone) is not.
    assert set(graph.nodes()) == {"self-hosting"}
    # No alias-of edge dangles to the non-node merged id.
    assert not any(a.get("rel_type") == "alias-of" for _s, _t, a in graph.edges(data=True))


# ---------------------------------------------------------------------------
# determinism / purity
# ---------------------------------------------------------------------------
async def test_build_is_idempotent(workspace_storage: FileSystemStorage) -> None:
    await _seed_concepts(workspace_storage, ["python", "calculator", "rust"])
    await workspace_storage.write(
        "garden/seedling/obs.md",
        _garden_note("settle", "python", "calculator"),
    )

    first = await build_concept_graph(workspace_storage)
    second = await build_concept_graph(workspace_storage)

    assert set(first.nodes()) == set(second.nodes())
    assert _cooccurs_pairs(first) == _cooccurs_pairs(second)
    assert first.number_of_edges() == second.number_of_edges()


# ---------------------------------------------------------------------------
# community detection
# ---------------------------------------------------------------------------
async def test_every_node_gets_a_community_id(workspace_storage: FileSystemStorage) -> None:
    """Each node carries a stable, non-empty ``community`` attribute (the
    legend's COMMUNITY mode colours by it). get_graph reads ``community``."""
    await _seed_concepts(workspace_storage, ["python", "calculator", "rust"])
    await workspace_storage.write(
        "garden/seedling/obs.md",
        _garden_note("settle", "python", "calculator"),
    )

    graph = await build_concept_graph(workspace_storage)

    for _node_id, attrs in graph.nodes(data=True):
        assert isinstance(attrs.get("community"), str)
        assert attrs["community"]


async def test_connected_concepts_share_a_community(
    workspace_storage: FileSystemStorage,
) -> None:
    """Two clusters that never cross-link land in different communities; the
    members of one cluster share a community id (greedy modularity is stable
    on this clearly-partitioned input)."""
    await _seed_concepts(workspace_storage, ["a1", "a2", "b1", "b2"])
    # Cluster A: a1+a2 co-occur. Cluster B: b1+b2 co-occur. No A-B link.
    await workspace_storage.write("garden/seedling/a.md", _garden_note("settle", "a1", "a2"))
    await workspace_storage.write("garden/seedling/b.md", _garden_note("settle", "b1", "b2"))

    graph = await build_concept_graph(workspace_storage)
    comm = {n: a["community"] for n, a in graph.nodes(data=True)}

    assert comm["a1"] == comm["a2"]
    assert comm["b1"] == comm["b2"]
    assert comm["a1"] != comm["b1"]


async def test_isolated_node_gets_own_community(
    workspace_storage: FileSystemStorage,
) -> None:
    """A lone concept with no edges still gets a valid community id (trivial
    case — never crashes, never empty)."""
    await _seed_concepts(workspace_storage, ["lonely"])

    graph = await build_concept_graph(workspace_storage)

    assert isinstance(graph.nodes["lonely"]["community"], str)
    assert graph.nodes["lonely"]["community"]


async def test_community_assignment_is_deterministic(
    workspace_storage: FileSystemStorage,
) -> None:
    """Building twice over the same vault yields the same community ids (no
    randomness — the legend must not flicker between reads)."""
    await _seed_concepts(workspace_storage, ["a1", "a2", "b1", "b2"])
    await workspace_storage.write("garden/seedling/a.md", _garden_note("settle", "a1", "a2"))
    await workspace_storage.write("garden/seedling/b.md", _garden_note("settle", "b1", "b2"))

    first = await build_concept_graph(workspace_storage)
    second = await build_concept_graph(workspace_storage)

    assert {n: a["community"] for n, a in first.nodes(data=True)} == {
        n: a["community"] for n, a in second.nodes(data=True)
    }
