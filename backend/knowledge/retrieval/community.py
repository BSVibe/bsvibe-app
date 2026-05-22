"""Community detection and summarization for the BSage knowledge graph.

Uses NetworkX community detection algorithms (Louvain by default) to identify
clusters of densely connected entities. Optionally generates LLM-based
summaries and markdown community notes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import networkx as nx
import structlog

logger = structlog.get_logger(__name__)


@dataclass
class Community:
    """A detected community of entities."""

    id: int
    members: list[str] = field(default_factory=list)  # node IDs
    member_names: list[str] = field(default_factory=list)
    size: int = 0
    label: str = ""
    summary: str = ""
    cohesion: float = 0.0  # internal edge density


def detect_communities(
    graph: nx.MultiDiGraph,
    *,
    algorithm: str = "louvain",
    resolution: float = 1.0,
    min_size: int = 2,
) -> list[Community]:
    """Detect communities in the knowledge graph.

    Args:
        graph: The NetworkX graph to analyze.
        algorithm: "louvain" or "label_propagation".
        resolution: Louvain resolution parameter (higher = more communities).
        min_size: Minimum community size to include.

    Returns:
        List of Community objects sorted by size (descending).
    """
    if graph.number_of_nodes() == 0:
        return []

    # Convert to undirected simple graph for community detection
    undirected = nx.Graph(graph.to_undirected())

    partition: dict[str, int] = {}
    if algorithm == "label_propagation":
        raw_communities = nx.community.label_propagation_communities(undirected)
    else:
        raw_communities = nx.community.louvain_communities(
            undirected, resolution=resolution, seed=42
        )
    for idx, comm in enumerate(raw_communities):
        for node in comm:
            partition[node] = idx

    # Group nodes by community
    comm_members: dict[int, list[str]] = {}
    for node_id, comm_id in partition.items():
        comm_members.setdefault(comm_id, []).append(node_id)

    # Build Community objects
    communities: list[Community] = []
    for comm_id, members in comm_members.items():
        if len(members) < min_size:
            continue

        member_names = [graph.nodes[m].get("name", m) for m in members if graph.has_node(m)]

        cohesion = _compute_cohesion(undirected, members)

        communities.append(
            Community(
                id=comm_id,
                members=members,
                member_names=sorted(member_names),
                size=len(members),
                label=_auto_label(graph, members),
                cohesion=cohesion,
            )
        )

    communities.sort(key=lambda c: c.size, reverse=True)

    # Assign community IDs to graph nodes
    for comm in communities:
        for member in comm.members:
            if graph.has_node(member):
                graph.nodes[member]["community"] = comm.id

    logger.info(
        "communities_detected",
        algorithm=algorithm,
        total=len(communities),
        sizes=[c.size for c in communities[:10]],
    )
    return communities


def _compute_cohesion(graph: nx.Graph, members: list[str]) -> float:
    """Compute internal edge density of a community."""
    member_set = set(members)
    if len(member_set) < 2:
        return 1.0

    subgraph = graph.subgraph(member_set)
    actual = subgraph.number_of_edges()
    possible = len(member_set) * (len(member_set) - 1) / 2
    return actual / possible if possible > 0 else 0.0


def _auto_label(graph: nx.MultiDiGraph, members: list[str]) -> str:
    """Generate an automatic label from the most connected member."""
    if not members:
        return "Unknown"

    # Find highest-degree node in the community
    best_node = max(members, key=lambda m: graph.degree(m) if graph.has_node(m) else 0)
    name = graph.nodes[best_node].get("name", best_node) if graph.has_node(best_node) else best_node
    entity_type = graph.nodes[best_node].get("entity_type", "") if graph.has_node(best_node) else ""

    if entity_type:
        return f"{name} ({entity_type})"
    return name


def generate_community_notes(communities: list[Community]) -> list[dict[str, Any]]:
    """Generate markdown content for community notes.

    Returns a list of dicts with ``path`` and ``content`` keys, suitable
    for writing via StorageBackend.
    """
    notes: list[dict[str, Any]] = []

    for comm in communities:
        members_links = "\n".join(f'  - "[[{name}]]"' for name in comm.member_names)
        content = (
            f"---\n"
            f"type: community\n"
            f'title: "Community: {comm.label}"\n'
            f"community_id: {comm.id}\n"
            f"size: {comm.size}\n"
            f"cohesion: {comm.cohesion:.3f}\n"
            f"members:\n{members_links}\n"
            f"---\n\n"
            f"Auto-detected community of {comm.size} entities "
            f"centered around **{comm.label}**.\n"
        )
        if comm.summary:
            content += f"\n## Summary\n\n{comm.summary}\n"

        slug = comm.label.lower().replace(" ", "-").replace("(", "").replace(")", "")
        slug = "".join(c for c in slug if c.isalnum() or c == "-")[:50]
        path = f"garden/communities/community-{comm.id}-{slug}.md"

        notes.append({"path": path, "content": content})

    return notes


def communities_to_graph_data(communities: list[Community]) -> list[dict[str, Any]]:
    """Convert communities to a format suitable for the frontend graph API.

    Returns a list of dicts with ``id``, ``label``, ``size``, ``cohesion``,
    ``members`` (node IDs), and ``color`` fields.
    """
    # Distinct colors for communities
    colors = [
        "#4edea3",
        "#adc6ff",
        "#ffb95f",
        "#ff7eb3",
        "#7ec8e3",
        "#c4b5fd",
        "#fca5a5",
        "#86efac",
        "#fde68a",
        "#a5b4fc",
        "#f0abfc",
        "#67e8f9",
        "#fdba74",
        "#d9f99d",
        "#cbd5e1",
    ]

    result: list[dict[str, Any]] = []
    for i, comm in enumerate(communities):
        result.append(
            {
                "id": comm.id,
                "label": comm.label,
                "size": comm.size,
                "cohesion": round(comm.cohesion, 3),
                "members": comm.members,
                "color": colors[i % len(colors)],
            }
        )
    return result
