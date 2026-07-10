"""Deterministic concept knowledge-graph builder (no LLM, no network).

The PWA Knowledge surface (`/knowledge`) shows the settled *concepts* the trust
ratchet has promoted ("What I know"), and a force-directed "Knowledge graph"
panel beside them. The graph endpoint (:func:`backend.api.v1.inside.get_graph`)
used to source its picture from a :class:`~backend.knowledge.graph.vault_backend.VaultBackend`
``.bsage/graph_cache.json`` snapshot — but that cache is produced by a
GraphSubscriber/extractor path that is NOT wired in this deployment, so the
graph was *always* empty even when concepts existed ("No connections yet").

This module rebuilds the graph **deterministically from the settled
canonicalization vault** instead — pure, idempotent, no model call and no
network. Two concepts that appeared together in the same piece of verified work
are "related"; that co-occurrence is the structural signal the force-directed
view renders.

The builder reads only the caller's per-workspace ``StorageBackend`` (rooted at
``<vault_root>/<region>/<workspace_id>/``), so it can never see another
workspace's vault. It produces a :class:`networkx.MultiDiGraph` whose node/edge
attribute names match exactly what
:func:`backend.api.v1.inside.get_graph` reads (``name`` / ``entity_type`` on
nodes, ``rel_type`` / ``weight`` on edges).

Edge sources (both deterministic):

* **alias-of** — when an active concept carries an alias that is itself another
  concept node, or a merged tombstone (``concepts/merged/<old>.md``) redirects
  to a canonical concept, an ``alias-of`` edge (weight ``1.0``) connects the
  alias/merged id to the canonical concept. Aliases that are not themselves
  concept nodes are skipped (the point is to connect *concepts*).
* **co-occurs** — for every garden observation note, the surviving content tags
  (structural + filler dropped, exactly as
  :meth:`backend.knowledge.canonicalization.promotion.GardenObservationPromoter._collect_candidate_tags`
  does) are each resolved to a concept id via :class:`TagResolver`. For every
  unordered pair of distinct co-present concept ids, a single ``co-occurs`` edge
  is emitted with ``weight`` = the number of observations the pair co-occurred
  in (a pair seen in two notes → weight ``2.0``). No self-loops; one edge per
  undirected relationship (not both directions).
"""

from __future__ import annotations

import networkx as nx

from backend.knowledge.canonicalization.index import InMemoryCanonicalizationIndex
from backend.knowledge.canonicalization.resolver import TagResolver
from backend.knowledge.canonicalization.store import NoteStore
from backend.knowledge.graph.storage import StorageBackend

# Structural tags the settle/garden write path stamps to describe the *kind* of
# note, not what it is *about* — they must never become nodes. Mirrors
# ``GardenObservationPromoter._DEFAULT_STRUCTURAL_TAGS``.
_STRUCTURAL_TAGS: frozenset[str] = frozenset({"settle", "verified-run"})

_ALIAS_REL = "alias-of"
_COOCCUR_REL = "co-occurs"


async def build_concept_graph(
    storage: StorageBackend, *, language: str | None = None
) -> nx.MultiDiGraph:
    """Build the workspace concept graph deterministically from the vault.

    Pure + idempotent: building twice over the same vault yields the same
    graph; a fresh/empty workspace yields an empty graph. Reads only ``storage``
    (the caller's per-workspace root) — never another workspace's vault.

    ``language`` (founder decision 2026-07) — when set to a workspace language
    tag (e.g. ``"ko"``), a concept that carries a display label for that tag
    renders its node ``name`` in that language. The node ``id`` is always the
    stable English identifier, so identity / edges / dedup are unchanged; only
    the human-facing label localizes. ``None`` / ``"en"`` / an unlabelled tag
    keep the English display.
    """
    index = InMemoryCanonicalizationIndex()
    await index.initialize(storage)
    resolver = TagResolver(index=index)
    store = NoteStore(storage)

    graph: nx.MultiDiGraph = nx.MultiDiGraph()

    # 1. Nodes — the workspace's active concepts, minus any that carry a
    #    ``retracted_at`` tombstone. Filtering here (not in the shared
    #    ``read_concept``) keeps retraction invisible to the graph without
    #    changing canonicalization's merge/resolve invariants. Mirrors the RAG
    #    retriever's skip predicate — falsy = live (a half-written note fails open).
    concepts = [c for c in await index.list_active_concepts() if not c.retracted_at]
    concept_ids: set[str] = set()
    for concept in concepts:
        concept_ids.add(concept.concept_id)
        # Lift E28 — let the seedling note kind drive the graph node's
        # ``entity_type`` so the PWA Knowledge view's TYPE legend buckets
        # concepts by Pattern / Principle / TechInsight / DomainModel
        # rather than collapsing every concept under one ``concept`` label.
        # Pre-E26 / untyped concepts fall back to the generic ``concept``
        # so the legend never breaks on a NULL.
        entity_type = concept.note_type or "concept"
        # Localized node label when the workspace language has one; else the
        # English display. The id stays English so edges / dedup are unchanged.
        label = concept.display or concept.concept_id
        if language and language != "en":
            label = concept.display_labels.get(language) or label
        graph.add_node(
            concept.concept_id,
            name=label,
            entity_type=entity_type,
        )

    if not concept_ids:
        return graph

    # 2. Alias / merge edges — connect a variant id to its canonical concept,
    #    but only when BOTH endpoints are concept nodes.
    for concept in concepts:
        for alias in concept.aliases:
            alias_id = resolver.normalize(alias)
            if not alias_id or alias_id == concept.concept_id:
                continue
            if alias_id in concept_ids:
                _add_alias_edge(graph, alias_id, concept.concept_id)
    for old_id in sorted(await _list_tombstone_ids(store, storage)):
        tombstone = await index.get_tombstone(old_id)
        if tombstone is None:
            continue
        if old_id in concept_ids and tombstone.merged_into in concept_ids:
            _add_alias_edge(graph, old_id, tombstone.merged_into)

    # 3. Co-occurrence edges — pairs of concepts present in the same garden
    #    observation, weighted by the count of co-occurring observations.
    pair_counts: dict[tuple[str, str], int] = {}
    for path in await store.list_garden_paths():
        present = await concept_ids_in_observation(path, store, resolver)
        # Only live concept nodes may form edges — a retracted concept whose id
        # still appears in a live observation's tags must not be re-introduced as
        # a node via ``add_edge``'s implicit node creation.
        present &= concept_ids
        ordered = sorted(present)
        for i, left in enumerate(ordered):
            for right in ordered[i + 1 :]:
                key = (left, right)
                pair_counts[key] = pair_counts.get(key, 0) + 1

    for (left, right), count in sorted(pair_counts.items()):
        graph.add_edge(left, right, rel_type=_COOCCUR_REL, weight=float(count))

    # 4. Community detection — a deterministic ``community`` attribute per node
    #    so the Knowledge view's COMMUNITY legend can colour by emergent cluster
    #    (vs the TYPE/``entity_type`` mode). Pure + stable: same vault → same ids.
    _assign_communities(graph)

    return graph


def _assign_communities(graph: nx.MultiDiGraph) -> None:
    """Stamp a deterministic, stable ``community`` id on every node.

    The structural signal is connectedness, so communities are detected on an
    *undirected simple* projection of the concept graph (direction + parallel
    multi-edges are irrelevant to "which concepts cluster together"). We use
    NetworkX's :func:`greedy_modularity_communities` — deterministic given a
    fixed input (no random seed, unlike Louvain) — and isolated nodes each fall
    into their own singleton community.

    The community *id* is the lexicographically-smallest member node id, so it
    is stable across rebuilds regardless of the order communities are returned
    in (the legend must not flicker between reads). A trivial/empty graph is a
    no-op.
    """
    if graph.number_of_nodes() == 0:
        return

    # Undirected simple projection — community structure ignores edge direction
    # and multiplicity. Isolated nodes are preserved so they get a singleton id.
    undirected = nx.Graph()
    undirected.add_nodes_from(graph.nodes())
    for source, target in graph.edges():
        if source != target:
            undirected.add_edge(source, target)

    communities = nx.community.greedy_modularity_communities(undirected)

    for members in communities:
        community_id = min(str(m) for m in members)
        for member in members:
            graph.nodes[member]["community"] = community_id


def _add_alias_edge(graph: nx.MultiDiGraph, alias_id: str, canonical_id: str) -> None:
    """Add a single ``alias-of`` edge (idempotent for the same endpoints)."""
    if graph.has_edge(alias_id, canonical_id):
        for _key, attrs in graph.get_edge_data(alias_id, canonical_id).items():
            if attrs.get("rel_type") == _ALIAS_REL:
                return
    graph.add_edge(alias_id, canonical_id, rel_type=_ALIAS_REL, weight=1.0)


async def _list_tombstone_ids(store: NoteStore, storage: StorageBackend) -> set[str]:
    """Stems of ``concepts/merged/<old>.md`` tombstone notes."""
    from pathlib import PurePosixPath

    ids: set[str] = set()
    for path in await storage.list_files("concepts/merged"):
        name = PurePosixPath(path).name
        ids.add(name[:-3] if name.endswith(".md") else name)
    return ids


async def concept_ids_in_observation(
    path: str,
    store: NoteStore,
    resolver: TagResolver,
) -> set[str]:
    """Distinct active-concept ids referenced by one garden observation.

    Drops structural tags + normalizes, then resolves each surviving tag; only
    tags that resolve to an active concept contribute a node id. Noise is
    excluded *structurally* — a tag only resolves to an active concept if it
    cleared the promoter's recurrence gate, so no separate filler deny-list is
    needed here (the graph can only show concepts the promoter already settled).

    Exported so the concept *inspector*
    (:func:`backend.api.v1.inside.get_concept_detail`) can derive a concept's
    source observations with the exact tag→concept resolution the graph builder
    uses, rather than re-deriving (and drifting from) it.
    """
    try:
        fm = await store.read_garden_frontmatter(path)
    except FileNotFoundError:  # pragma: no cover — listing/read race
        return set()

    # A retracted observation contributes nothing — same tombstone predicate as
    # the RAG retriever (falsy value = live, so a half-written note fails open).
    if fm.get("retracted_at"):
        return set()
    tags = list(fm.get("tags") or [])

    present: set[str] = set()
    for raw in tags:
        if not isinstance(raw, str) or raw in _STRUCTURAL_TAGS:
            continue
        normalized = resolver.normalize(raw)
        if not normalized or normalized in _STRUCTURAL_TAGS:
            continue
        resolved = await resolver.resolve(raw)
        if resolved.status == "resolved" and resolved.concept_id is not None:
            present.add(resolved.concept_id)
    return present


__all__ = ["build_concept_graph", "concept_ids_in_observation"]
