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

import os.path
import random
from collections import Counter
from typing import Any

import networkx as nx
import structlog

logger = structlog.get_logger(__name__)


#: Leiden RNG seed — fixed so a re-bootstrap of the same repo gives the
#: same community ids. Bumping this re-shuffles every workspace's
#: community ids, which is fine on a fresh bootstrap.
_LEIDEN_SEED = 42

#: F3 — max nodes in a community before we recursively subdivide it. Leiden's
#: modularity objective has a resolution limit (Traag 2011): on a LARGE graph
#: it cannot resolve communities below ~sqrt(2·|E|) and merges distinct areas
#: into one oversized blob that can only be labelled with a shallow prefix
#: ("backend"). We re-run Leiden on each oversized community's induced subgraph
#: — where the limit is far lower — to split it. The cap is a human-navigation
#: bound (a community you'd drill into should be scannable), NOT reverse-derived
#: from one repo's numbers; 50/60/80 give near-identical results across repos.
#: Subdivision is SPLIT-ONLY (it re-runs detection on a subset), so it can never
#: increase a community's size and is a no-op on small repos that have no blob.
_MAX_COMMUNITY = 60

#: A subdivision is accepted only if it yields at least two parts each this
#: large — otherwise the oversized community is genuinely cohesive (a clique)
#: and we keep it whole rather than shattering it into singleton noise.
_SUBDIVIDE_MIN_PART = 5

#: Minimum fraction of a community's files that must share a directory
#: prefix for it to be used as the label. Strict full consensus (every
#: file shares the prefix) collapsed real communities to "misc" the
#: moment a single outlier file appeared — e.g. a 223-file community
#: that was 91% ``backend/`` got labelled "misc" because of two
#: ``plugin/``/``bsvibe_sdk/`` strays. A 60% majority keeps the label
#: honest ("backend") without letting a thin plurality mislabel a truly
#: scattered community.
_DOMINANT_FRACTION = 0.6

#: A sub-area (one directory level deeper than the label) is worth naming
#: in the description only if it covers at least this fraction of the
#: community's files. Below it the area is noise; above it the founder
#: wants to know "backend — but which parts?".
_SUBAREA_FRACTION = 0.2

#: How many sub-areas to surface, at most.
_SUBAREA_TOP_N = 3


def _leiden_membership(graph: nx.DiGraph) -> dict[str, int] | None:
    """One Leiden pass on the undirected projection of ``graph``.

    Returns ``{node_id: community_id}`` or ``None`` if igraph is missing or
    Leiden's C bindings throw (the caller decides how to degrade).
    """
    undirected = graph.to_undirected(as_view=False)
    try:
        import igraph  # noqa: PLC0415 — heavy import, deferred to call site
    except ImportError:  # pragma: no cover — dep declared in pyproject.toml
        logger.warning("leiden_igraph_missing — every node lands in its own community")
        return None

    # Determinism: Leiden's refinement uses a random node order. Seed igraph's
    # RNG before every call (top-level AND each subgraph) so the same graph +
    # node order yields the same membership across runs — a re-bootstrap of the
    # same repo must give stable community ids, and the split-only invariant
    # below is only meaningful against a deterministic base. (The docstring long
    # claimed this; the seed was never actually wired until F3.)
    igraph.set_random_number_generator(random.Random(_LEIDEN_SEED))  # noqa: S311 — reproducibility seed, not crypto

    # python-igraph's from_networkx walks node order so we read the
    # membership back in the same order.
    ig_graph: Any = igraph.Graph.from_networkx(undirected)
    try:
        result = ig_graph.community_leiden(objective_function="modularity", n_iterations=10)
    except Exception:  # noqa: BLE001 — Leiden's C bindings throw on edge cases
        logger.warning("leiden_detection_failed — falling back to weakly connected components")
        return None

    ordered_ids = [v["_nx_name"] for v in ig_graph.vs]
    return {nid: int(cid) for nid, cid in zip(ordered_ids, result.membership, strict=True)}


def detect_communities(
    graph: nx.DiGraph, *, max_community_size: int = _MAX_COMMUNITY
) -> dict[str, int]:
    """Run Leiden on the undirected projection of ``graph``, then recursively
    subdivide any oversized community (F3).

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

    membership = _leiden_membership(graph)
    if membership is None:
        return _fallback_components(graph)
    return _subdivide_oversized(
        graph, membership, max_size=max_community_size, min_part=_SUBDIVIDE_MIN_PART
    )


def _subdivide_oversized(
    graph: nx.DiGraph,
    membership: dict[str, int],
    *,
    max_size: int,
    min_part: int,
) -> dict[str, int]:
    """Recursively split communities larger than ``max_size`` (F3).

    For each community over the cap we re-run Leiden on its induced subgraph —
    where the resolution limit is far lower — and accept the split only when it
    yields ≥2 parts each ≥ ``min_part`` (otherwise the community is genuinely
    cohesive and we keep it whole). SPLIT-ONLY: a community's nodes are only ever
    partitioned, never merged, so the maximum community size can never increase
    and small graphs with no oversized community pass through unchanged.

    Re-numbers every community with a fresh contiguous id so split parts get
    distinct ids; the actual integer values are opaque.
    """
    groups: dict[int, list[str]] = {}
    for nid, cid in membership.items():
        groups.setdefault(cid, []).append(nid)

    out: dict[str, int] = {}
    next_id = 0
    # Sort by id for deterministic id assignment across runs.
    for _cid, members in sorted(groups.items()):
        if len(members) <= max_size:
            for nid in members:
                out[nid] = next_id
            next_id += 1
            continue

        sub_membership = _leiden_membership(graph.subgraph(members))
        parts: dict[int, list[str]] = {}
        if sub_membership is not None:
            for nid, sub_cid in sub_membership.items():
                parts.setdefault(sub_cid, []).append(nid)

        substantial = [p for p in parts.values() if len(p) >= min_part]
        if len(substantial) < 2:
            # Unsplittable (cohesive clique-like) — keep the community whole.
            for nid in members:
                out[nid] = next_id
            next_id += 1
            continue

        # Accept the split; recurse so a part still over the cap splits again.
        refined = _subdivide_oversized(
            graph.subgraph(members),
            {nid: sub_cid for nid, sub_cid in sub_membership.items()},  # type: ignore[union-attr]
            max_size=max_size,
            min_part=min_part,
        )
        local_to_global: dict[int, int] = {}
        for nid in members:
            local = refined[nid]
            if local not in local_to_global:
                local_to_global[local] = next_id
                next_id += 1
            out[nid] = local_to_global[local]

    return out


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


def derive_community_labels(
    graph: nx.DiGraph,
    *,
    min_size: int = 3,
    top_symbols_k: int = 5,
) -> dict[int, dict[str, Any]]:
    """Lift E25 — produce a structured label for every non-trivial community.

    Without any LLM call: the path prefix of the community's files, the top
    symbols (by PageRank if annotated, else by node order), language tally,
    and a 1-line human-readable description. This answers the founder's
    question "why are these nodes grouped?" with concrete signals every
    time, even on a degraded LLM path.

    Communities below ``min_size`` are dropped — Leiden produces a long
    tail of singletons + 2-node fragments that would just add noise.

    Returns ``{community_id: {label, description, size, file_count,
    languages, top_symbols, top_paths}}``.
    """
    if graph.number_of_nodes() == 0:
        return {}

    grouped: dict[int, list[str]] = {}
    for nid in graph.nodes:
        cid_raw = graph.nodes[nid].get("community_id")
        if cid_raw is None:
            continue
        grouped.setdefault(int(cid_raw), []).append(nid)

    out: dict[int, dict[str, Any]] = {}
    for cid, members in grouped.items():
        if len(members) < min_size:
            continue
        out[cid] = _label_for_community(graph, cid, members, top_symbols_k=top_symbols_k)
    return out


def _label_for_community(
    graph: nx.DiGraph,
    cid: int,
    members: list[str],
    *,
    top_symbols_k: int,
) -> dict[str, Any]:
    """Build one community's label dict from member node attrs."""
    paths: list[str] = []
    names_by_rank: list[tuple[str, float]] = []
    lang_counter: Counter[str] = Counter()
    for nid in members:
        attrs = graph.nodes[nid]
        is_external = attrs.get("kind") == "external"
        path = attrs.get("path") or ""
        if path and not is_external:
            paths.append(path)
        name = attrs.get("name") or ""
        # Skip external import stubs: they carry the highest PageRank
        # (everything imports BaseModel / typing / …) and would otherwise
        # dominate every label's symbols, telling the founder nothing about
        # the community's own code. Same filter the path label already uses.
        if name and not is_external:
            # Higher PageRank = more central = preferred label symbol. Fall
            # back to a tiny positive constant when PR is missing so the
            # node still participates in the symbol shortlist.
            pr = float(attrs.get("pagerank") or 0.0001)
            names_by_rank.append((name, pr))
        lang = attrs.get("language") or ""
        if lang:
            lang_counter[lang] += 1

    label = _common_path_label(paths)
    top_symbols = [n for n, _ in sorted(names_by_rank, key=lambda kv: -kv[1])[:top_symbols_k]]
    file_count = len({p for p in paths})
    subareas = _subareas_for(paths, label)

    desc_parts: list[str] = []
    if file_count:
        plural = "files" if file_count != 1 else "file"
        prefix = label or "various paths"
        desc_parts.append(f"{file_count} {plural} in {prefix}")
    if subareas:
        # The community spans several sub-areas under a shallow label —
        # name them so "backend" becomes "backend, spanning api + mcp".
        desc_parts.append(f"spanning {', '.join(subareas)}")
    if top_symbols:
        desc_parts.append(f"top symbols: {', '.join(top_symbols[:3])}")
    desc_parts.append(f"{len(members)} nodes")
    description = " · ".join(desc_parts)

    return {
        "community_id": cid,
        "label": label or "misc",
        "description": description,
        "size": len(members),
        "file_count": file_count,
        "languages": dict(lang_counter),
        "top_symbols": top_symbols,
        "subareas": subareas,
        "top_paths": sorted({p for p in paths})[:5],
    }


def _common_path_label(paths: list[str]) -> str:
    """Pick the deepest directory prefix shared by a *majority* of ``paths``.

    Uses POSIX-style separators so worktrees on macOS + Linux + CI agree.
    We label by directory components only — never a filename — and walk
    one level deeper as long as a single prefix still covers at least
    :data:`_DOMINANT_FRACTION` of the files. The previous strict
    full-consensus rule collapsed to an empty label (rendered "misc") the
    moment one outlier file diverged; a majority prefix keeps the label
    grounded ("backend") while still returning "" for a genuinely
    scattered community where no prefix reaches the threshold.
    """
    cleaned = [p.replace("\\", "/").strip("/") for p in paths if p]
    if not cleaned:
        return ""
    # Directory components only; a file with no parent contributes none.
    dir_parts: list[list[str]] = []
    for p in cleaned:
        parent = os.path.dirname(p)
        dir_parts.append(parent.split("/") if parent else [])

    total = len(dir_parts)
    best = ""
    depth = 1
    while True:
        prefixes = ["/".join(parts[:depth]) for parts in dir_parts if len(parts) >= depth]
        if not prefixes:
            break
        top, count = Counter(prefixes).most_common(1)[0]
        if count / total >= _DOMINANT_FRACTION:
            best = top
            depth += 1
        else:
            break
    return best


def _subareas_for(paths: list[str], label: str) -> list[str]:
    """Name the directory areas a community spans one level below ``label``.

    The majority label is honest but can be shallow: a 374-node community
    split ~50/50 between ``backend/api`` and ``backend/mcp`` only labels
    ``backend``. This surfaces those sub-areas so the founder sees the
    spread without the label lying about a single owner.

    F3 Lift 2 — a SHALLOW label (one path component, e.g. ``backend``, or the
    scattered ``misc``/empty case) that spreads thinly across MANY child dirs
    used to fall below the strict 20% bar and ship with EMPTY subareas — the
    exact un-navigable "bare backend" the redesign targets. For shallow labels
    we relax the bar and name the top child dirs regardless, so such a
    community always tells the founder *which parts* of backend it spans. DEEP
    labels keep the conservative 20%/>1 gate so an already-specific community
    isn't cluttered with noise. Returns at most :data:`_SUBAREA_TOP_N` prefixes,
    and only when **more than one** child area exists (a single dominant
    sub-area would have deepened the label, so echoing it adds nothing).
    """
    cleaned = [p.replace("\\", "/").strip("/") for p in paths if p]
    if not cleaned:
        return []
    label_depth = len(label.split("/")) if label else 0
    sub_depth = label_depth + 1

    dir_parts: list[list[str]] = []
    for p in cleaned:
        parent = os.path.dirname(p)
        dir_parts.append(parent.split("/") if parent else [])

    total = len(dir_parts)
    # Only consider files that reach the sub-area depth and, when a label
    # exists, actually sit under it.
    prefixes = [
        "/".join(parts[:sub_depth])
        for parts in dir_parts
        if len(parts) >= sub_depth and (not label or "/".join(parts[:label_depth]) == label)
    ]
    counts = Counter(prefixes)
    # Shallow labels (``backend``, ``misc``, …) are the navigability problem —
    # relax the fraction bar so a thinly-spread blob still names its areas.
    shallow = label_depth <= 1
    floor = 0.0 if shallow else _SUBAREA_FRACTION
    qualifying = [
        area
        for area, n in counts.most_common(_SUBAREA_TOP_N)
        if area != label and n / total >= floor
    ]
    return qualifying if len(qualifying) > 1 else []


__all__ = [
    "annotate_communities",
    "derive_community_labels",
    "detect_communities",
]
