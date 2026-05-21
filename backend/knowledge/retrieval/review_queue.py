"""ReviewQueue — generate .bsage/review_queue.md from AMBIGUOUS graph data.

Scans the graph backend for entities and relationships with AMBIGUOUS
confidence and writes a human-readable review queue to the vault.
"""

from __future__ import annotations

from datetime import UTC, datetime

from backend.knowledge.graph.graph_backend import GraphBackend
from backend.knowledge.graph.graph_models import ConfidenceLevel
from backend.knowledge.graph.storage import StorageBackend

_REVIEW_QUEUE_PATH = ".bsage/review_queue.md"


async def generate_review_queue(backend: GraphBackend, storage: StorageBackend) -> int:
    """Scan for AMBIGUOUS items and write review_queue.md.

    Returns the number of AMBIGUOUS items found.
    """
    lines: list[str] = [
        "# Review Queue (auto-generated)",
        "",
        f"> Last updated: {datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')}",
        "> Approve or reject each item. Approved items will be promoted to INFERRED.",
        "",
    ]

    graph = backend.to_networkx()
    items: list[tuple[str, str]] = []  # (section_key, line)

    # Scan edges for AMBIGUOUS confidence
    for u, v, _key, data in graph.edges(keys=True, data=True):
        if data.get("confidence") != ConfidenceLevel.AMBIGUOUS:
            continue

        source_name = graph.nodes[u].get("name", u) if graph.has_node(u) else u
        target_name = graph.nodes[v].get("name", v) if graph.has_node(v) else v
        rel_type = data.get("rel_type", "related_to")
        source_path = data.get("source_path", "")

        items.append(
            (
                source_path,
                f"- [[{source_name}]] --{rel_type}--> [[{target_name}]]? (source: `{source_path}`)",
            )
        )

    # Scan nodes for AMBIGUOUS confidence
    for node_id, data in graph.nodes(data=True):
        if data.get("confidence") != ConfidenceLevel.AMBIGUOUS:
            continue
        name = data.get("name", node_id)
        entity_type = data.get("entity_type", "unknown")
        source_path = data.get("source_path", "")

        items.append(
            (
                source_path,
                f"- [[{name}]] (type: {entity_type})? (source: `{source_path}`)",
            )
        )

    if not items:
        lines.append("No items pending review.")
    else:
        # Group by source path
        by_source: dict[str, list[str]] = {}
        for source_path, line in items:
            by_source.setdefault(source_path, []).append(line)

        for source_path in sorted(by_source):
            lines.append(f"### `{source_path}`")
            lines.extend(by_source[source_path])
            lines.append("")

    await storage.write(_REVIEW_QUEUE_PATH, "\n".join(lines) + "\n")
    return len(items)
