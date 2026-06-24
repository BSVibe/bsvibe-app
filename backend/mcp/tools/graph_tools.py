"""Code-graph MCP query surface — Lift E20 Phase D.

The five tools below serve the persisted ``code_graph/graph.json`` that
the bootstrap orchestrator (:func:`run_repo_bootstrap`) wrote during
ingest. The graph is workspace-scoped exactly like the vault — same
``(region, workspace_id)`` boundary the existing vault tools enforce.

Tools registered:

* ``bsvibe_graph_get_node`` — fetch a node + its 1-hop neighbors.
* ``bsvibe_graph_neighbors`` — list neighbors filtered by kind / dir.
* ``bsvibe_graph_shortest_path`` — connect two nodes via N hops.
* ``bsvibe_graph_community`` — list communities or one community's
  member nodes.
* ``bsvibe_graph_search`` — text search across node names, signatures,
  docstrings.

All five are ``mcp:read`` — the graph is built from the workspace's
own clone, but a future cross-workspace query (if it ever lands) would
need an explicit elevation.

Caching: the per-call vault root is resolved off the principal +
workspace's region (same as :mod:`._helpers`); we load the graph JSON
on every call. For E20 the graph is small enough (≤10k nodes for the
founder's largest repo) that the latency is irrelevant; if it becomes
a bottleneck a per-process LRU keyed on
``(workspace_id, mtime)`` is the trivial fix.
"""

from __future__ import annotations

from typing import Any, Literal

import networkx as nx
import structlog
from pydantic import BaseModel, ConfigDict, Field

from backend.knowledge.code_graph.graph import load_graph
from backend.knowledge.code_graph.pipeline import (
    code_graph_vault_path,
    community_labels_vault_path,
)
from backend.mcp.api import Tool, ToolContext, ToolError, ToolRegistry
from backend.mcp.tools._helpers import vault_root_for, workspace_region

logger = structlog.get_logger(__name__)


async def _community_labels_for_call(ctx: ToolContext) -> dict[int, dict[str, Any]]:
    """Async sibling of :func:`_graph_for_call` for the labels sidecar."""
    import json as _json  # noqa: PLC0415

    region = await workspace_region(ctx.session, ctx.principal.workspace_id)
    vault_root = vault_root_for(region=region, workspace_id=ctx.principal.workspace_id)
    path = community_labels_vault_path(vault_root=vault_root)
    if not path.is_file():
        return {}
    try:
        raw = _json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, KeyError) as exc:
        logger.warning("mcp_community_labels_load_failed", path=str(path), error=str(exc))
        return {}
    out: dict[int, dict[str, Any]] = {}
    for entry in raw.get("communities") or []:
        cid = entry.get("community_id")
        if isinstance(cid, int):
            out[cid] = entry
    return out


# ---------------------------------------------------------------------------
# Per-call graph loader. Returns ``None`` when no graph has been
# bootstrapped yet — handlers turn that into a precise ToolError.
# ---------------------------------------------------------------------------
async def _graph_for_call(ctx: ToolContext) -> nx.DiGraph | None:
    region = await workspace_region(ctx.session, ctx.principal.workspace_id)
    vault_root = vault_root_for(region=region, workspace_id=ctx.principal.workspace_id)
    path = code_graph_vault_path(vault_root=vault_root)
    if not path.is_file():
        return None
    try:
        return load_graph(path)
    except (OSError, ValueError, KeyError) as exc:
        logger.warning("mcp_graph_load_failed", path=str(path), error=str(exc))
        raise ToolError(f"code_graph load failed: {path}") from exc


def _require_graph(graph: nx.DiGraph | None) -> nx.DiGraph:
    if graph is None:
        raise ToolError(
            "no code graph yet — run product bootstrap on a repo first "
            "(graph.json is written under <vault>/code_graph/)."
        )
    return graph


def _node_to_dict(graph: nx.DiGraph, node_id: str) -> dict[str, Any]:
    raw = dict(graph.nodes[node_id])
    raw.setdefault("id", node_id)
    return raw


# ---------------------------------------------------------------------------
# bsvibe_graph_get_node
# ---------------------------------------------------------------------------
class GetNodeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    node_id: str = Field(..., min_length=1, max_length=512)


class _NeighborEntry(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: str
    kind: str
    name: str
    direction: Literal["in", "out"]
    edge_kind: str


class GetNodeOutput(BaseModel):
    model_config = ConfigDict(extra="allow")
    node: dict[str, Any]
    neighbors: list[_NeighborEntry] = Field(default_factory=list)


async def _h_get_node(args: GetNodeInput, ctx: ToolContext) -> Any:
    graph = _require_graph(await _graph_for_call(ctx))
    if args.node_id not in graph.nodes:
        raise ToolError(f"node not found: {args.node_id}")
    node = _node_to_dict(graph, args.node_id)
    neighbors: list[_NeighborEntry] = []
    for nbr in graph.successors(args.node_id):
        attrs = graph.nodes[nbr]
        edge = graph.get_edge_data(args.node_id, nbr) or {}
        neighbors.append(
            _NeighborEntry(
                id=nbr,
                kind=str(attrs.get("kind", "")),
                name=str(attrs.get("name", nbr)),
                direction="out",
                edge_kind=str(edge.get("kind", "")),
            )
        )
    for nbr in graph.predecessors(args.node_id):
        attrs = graph.nodes[nbr]
        edge = graph.get_edge_data(nbr, args.node_id) or {}
        neighbors.append(
            _NeighborEntry(
                id=nbr,
                kind=str(attrs.get("kind", "")),
                name=str(attrs.get("name", nbr)),
                direction="in",
                edge_kind=str(edge.get("kind", "")),
            )
        )
    return GetNodeOutput(node=node, neighbors=neighbors)


# ---------------------------------------------------------------------------
# bsvibe_graph_neighbors
# ---------------------------------------------------------------------------
class NeighborsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    node_id: str = Field(..., min_length=1, max_length=512)
    kind: str | None = Field(
        default=None,
        description="Optional edge kind filter — 'imports' / 'calls' / 'inherits' / 'doc_references'.",
    )
    direction: Literal["in", "out", "both"] = Field(
        default="both",
        description="Walk incoming, outgoing, or both directions from the node.",
    )
    limit: int = Field(20, ge=1, le=200)


class NeighborsOutput(BaseModel):
    model_config = ConfigDict(extra="allow")
    node_id: str
    total: int
    neighbors: list[_NeighborEntry]


async def _h_neighbors(args: NeighborsInput, ctx: ToolContext) -> Any:
    graph = _require_graph(await _graph_for_call(ctx))
    if args.node_id not in graph.nodes:
        raise ToolError(f"node not found: {args.node_id}")
    out: list[_NeighborEntry] = []
    if args.direction in {"out", "both"}:
        for nbr in graph.successors(args.node_id):
            edge = graph.get_edge_data(args.node_id, nbr) or {}
            edge_kind = str(edge.get("kind", ""))
            if args.kind is not None and edge_kind != args.kind:
                continue
            attrs = graph.nodes[nbr]
            out.append(
                _NeighborEntry(
                    id=nbr,
                    kind=str(attrs.get("kind", "")),
                    name=str(attrs.get("name", nbr)),
                    direction="out",
                    edge_kind=edge_kind,
                )
            )
    if args.direction in {"in", "both"}:
        for nbr in graph.predecessors(args.node_id):
            edge = graph.get_edge_data(nbr, args.node_id) or {}
            edge_kind = str(edge.get("kind", ""))
            if args.kind is not None and edge_kind != args.kind:
                continue
            attrs = graph.nodes[nbr]
            out.append(
                _NeighborEntry(
                    id=nbr,
                    kind=str(attrs.get("kind", "")),
                    name=str(attrs.get("name", nbr)),
                    direction="in",
                    edge_kind=edge_kind,
                )
            )
    capped = out[: args.limit]
    return NeighborsOutput(node_id=args.node_id, total=len(capped), neighbors=capped)


# ---------------------------------------------------------------------------
# bsvibe_graph_shortest_path
# ---------------------------------------------------------------------------
class ShortestPathInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    src_id: str = Field(..., min_length=1, max_length=512)
    dst_id: str = Field(..., min_length=1, max_length=512)
    max_hops: int = Field(5, ge=1, le=20)


class ShortestPathOutput(BaseModel):
    model_config = ConfigDict(extra="allow")
    src_id: str
    dst_id: str
    hops: int
    path: list[dict[str, Any]]


async def _h_shortest_path(args: ShortestPathInput, ctx: ToolContext) -> Any:
    graph = _require_graph(await _graph_for_call(ctx))
    if args.src_id not in graph.nodes:
        raise ToolError(f"source node not found: {args.src_id}")
    if args.dst_id not in graph.nodes:
        raise ToolError(f"destination node not found: {args.dst_id}")
    try:
        # NetworkX shortest_path doesn't take a hop cap directly; we
        # compute it then bail out if it overshoots.
        path = nx.shortest_path(graph, args.src_id, args.dst_id)
    except nx.NetworkXNoPath:
        return ShortestPathOutput(src_id=args.src_id, dst_id=args.dst_id, hops=-1, path=[])
    except nx.NodeNotFound as exc:
        raise ToolError(f"node disappeared mid-query: {exc}") from exc
    hops = len(path) - 1
    if hops > args.max_hops:
        # Same shape as "no path" — caller treats negative hops as no path.
        return ShortestPathOutput(src_id=args.src_id, dst_id=args.dst_id, hops=-1, path=[])
    return ShortestPathOutput(
        src_id=args.src_id,
        dst_id=args.dst_id,
        hops=hops,
        path=[_node_to_dict(graph, nid) for nid in path],
    )


# ---------------------------------------------------------------------------
# bsvibe_graph_community
# ---------------------------------------------------------------------------
class CommunityInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    community_id: int | None = Field(
        default=None,
        description="Pass to list members of one community. Omit for an overview of all communities.",
    )
    limit: int = Field(50, ge=1, le=500)


class _CommunitySummary(BaseModel):
    """Lift E25 — overview-row shape now carries the founder-facing label,
    description, and centrality signals so the MCP response answers "why
    are these grouped" without a follow-up call."""

    model_config = ConfigDict(extra="allow")
    community_id: int
    size: int
    label: str | None = None
    description: str | None = None
    top_symbols: list[str] = Field(default_factory=list)
    # Sub-areas a community spans below a shallow label (e.g. "backend"
    # spanning backend/api + backend/mcp). Empty for uniform communities.
    subareas: list[str] = Field(default_factory=list)
    top_paths: list[str] = Field(default_factory=list)


class CommunityOutput(BaseModel):
    model_config = ConfigDict(extra="allow")
    total: int
    communities: list[_CommunitySummary] = Field(default_factory=list)
    members: list[dict[str, Any]] = Field(default_factory=list)
    # Lift E25 — when ``community_id`` is set, surface the requested
    # community's label alongside its members so a single call returns
    # both the rendering header and the rows.
    community: _CommunitySummary | None = None


async def _h_community(args: CommunityInput, ctx: ToolContext) -> Any:
    graph = _require_graph(await _graph_for_call(ctx))
    labels = await _community_labels_for_call(ctx)
    if args.community_id is None:
        sizes: dict[int, int] = {}
        for nid in graph.nodes:
            cid_raw = graph.nodes[nid].get("community_id")
            if cid_raw is None:
                continue
            cid = int(cid_raw)
            sizes[cid] = sizes.get(cid, 0) + 1
        # Only the LABELED communities are navigable. Leiden + recursive
        # subdivision leave a long tail of singleton / 2-node fragments that
        # derive_community_labels drops (min_size); listing all ~2500 raw ids
        # (most with label=None) buried the ~370 meaningful ones. Surface the
        # labeled communities, biggest first.
        summaries = [
            _CommunitySummary(
                community_id=cid,
                size=sizes.get(cid, int(entry.get("size") or 0)),
                label=entry.get("label"),
                description=entry.get("description"),
                top_symbols=list(entry.get("top_symbols") or []),
                subareas=list(entry.get("subareas") or []),
                top_paths=list(entry.get("top_paths") or []),
            )
            for cid, entry in labels.items()
        ]
        summaries.sort(key=lambda s: (-s.size, s.community_id))
        return CommunityOutput(total=len(summaries), communities=summaries)
    target = args.community_id
    members: list[dict[str, Any]] = []
    size = 0
    for nid in graph.nodes:
        cid_raw = graph.nodes[nid].get("community_id")
        if cid_raw is None:
            continue
        if int(cid_raw) != target:
            continue
        size += 1
        if len(members) < args.limit:
            members.append(_node_to_dict(graph, nid))
    label_entry = labels.get(target, {})
    summary = _CommunitySummary(
        community_id=target,
        size=size,
        label=label_entry.get("label"),
        description=label_entry.get("description"),
        top_symbols=list(label_entry.get("top_symbols") or []),
        subareas=list(label_entry.get("subareas") or []),
        top_paths=list(label_entry.get("top_paths") or []),
    )
    return CommunityOutput(total=len(members), members=members, community=summary)


# ---------------------------------------------------------------------------
# bsvibe_graph_search
# ---------------------------------------------------------------------------
class GraphSearchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(..., min_length=1, max_length=200)
    kind: str | None = Field(
        default=None,
        description="Optional NodeKind filter (e.g. 'function', 'class', 'doc_section').",
    )
    limit: int = Field(10, ge=1, le=200)


class GraphSearchOutput(BaseModel):
    model_config = ConfigDict(extra="allow")
    query: str
    total: int
    results: list[dict[str, Any]]


async def _h_graph_search(args: GraphSearchInput, ctx: ToolContext) -> Any:
    graph = _require_graph(await _graph_for_call(ctx))
    needle = args.query.lower()
    hits: list[tuple[float, dict[str, Any]]] = []
    for nid in graph.nodes:
        attrs = graph.nodes[nid]
        if args.kind is not None and str(attrs.get("kind", "")) != args.kind:
            continue
        # F8 — external import stubs sink the most PageRank but are framework
        # imports, not the codebase's own code. Skip them on an unfiltered
        # search; an explicit kind="external" still surfaces them above.
        if args.kind is None and str(attrs.get("kind", "")) == "external":
            continue
        # Search across name + signature + docstring + path.
        haystack_parts = [
            str(attrs.get("name", "")),
            str(attrs.get("signature") or ""),
            str(attrs.get("docstring") or ""),
            str(attrs.get("path", "")),
        ]
        haystack = " ".join(haystack_parts).lower()
        if needle not in haystack:
            continue
        # Rank by PageRank (when present) — most-referenced first.
        score = float(attrs.get("pagerank", 0.0))
        hits.append((score, _node_to_dict(graph, nid)))
    hits.sort(key=lambda h: -h[0])
    capped = [h[1] for h in hits[: args.limit]]
    return GraphSearchOutput(query=args.query, total=len(capped), results=capped)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------
def register_graph_tools(registry: ToolRegistry) -> None:
    registry.register(
        Tool(
            name="bsvibe_graph_get_node",
            description=(
                "Fetch one code-graph node by id, plus its 1-hop neighbors "
                "in both directions. Node ids look like "
                "'<lang>:<path>::<qualname>' (e.g. 'python:backend/api.py::login'). "
                "Returns ToolError when no graph has been bootstrapped yet."
            ),
            input_schema=GetNodeInput,
            output_schema=GetNodeOutput,
            handler=_h_get_node,
            required_scopes=("mcp:read",),
        )
    )
    registry.register(
        Tool(
            name="bsvibe_graph_neighbors",
            description=(
                "List a node's neighbors filtered by edge kind "
                "(imports / calls / inherits / doc_references) and direction "
                "(in / out / both)."
            ),
            input_schema=NeighborsInput,
            output_schema=NeighborsOutput,
            handler=_h_neighbors,
            required_scopes=("mcp:read",),
        )
    )
    registry.register(
        Tool(
            name="bsvibe_graph_shortest_path",
            description=(
                "Return the shortest path between two nodes (up to `max_hops`). "
                "`hops=-1` means no path found within the cap."
            ),
            input_schema=ShortestPathInput,
            output_schema=ShortestPathOutput,
            handler=_h_shortest_path,
            required_scopes=("mcp:read",),
        )
    )
    registry.register(
        Tool(
            name="bsvibe_graph_community",
            description=(
                "Without `community_id`: list every detected community + its "
                "size. With `community_id`: list that community's member "
                "nodes (capped by `limit`)."
            ),
            input_schema=CommunityInput,
            output_schema=CommunityOutput,
            handler=_h_community,
            required_scopes=("mcp:read",),
        )
    )
    registry.register(
        Tool(
            name="bsvibe_graph_search",
            description=(
                "Substring search over node names, signatures, docstrings, "
                "paths. Optional `kind` filters to one NodeKind (function, "
                "class, method, doc_section, module). Ranked by PageRank."
            ),
            input_schema=GraphSearchInput,
            output_schema=GraphSearchOutput,
            handler=_h_graph_search,
            required_scopes=("mcp:read",),
        )
    )


__all__ = ["register_graph_tools"]
