"""Lift E20 Phase D — MCP code-graph query surface tests.

Five tools: bsvibe_graph_get_node, _neighbors, _shortest_path,
_community, _search. The fixture seeds a small ``graph.json`` under the
workspace's vault and exercises each tool's contract.
"""

from __future__ import annotations

import base64
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import networkx as nx
import pytest
import pytest_asyncio

import backend.identity.workspaces_db  # noqa: F401
from backend.config import get_settings
from backend.identity.workspaces_db import WorkspaceRow
from backend.knowledge.code_graph.graph import save_graph
from backend.mcp.api import McpPrincipal, ToolContext, ToolError, ToolRegistry
from backend.mcp.tools import register_all_tools

from .._support import db_engine

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def db(tmp_path: Path, monkeypatch) -> AsyncIterator:  # type: ignore[no-untyped-def]
    monkeypatch.setenv(
        "BSVIBE_GATEWAY_KMS_KEY_B64",
        base64.urlsafe_b64encode(b"0" * 32).decode(),
    )
    monkeypatch.setenv("BSVIBE_KNOWLEDGE_VAULT_ROOT", str(tmp_path / "vault"))
    get_settings.cache_clear()
    async with db_engine() as (engine, _is_pg):
        from sqlalchemy.ext.asyncio import async_sessionmaker

        yield async_sessionmaker(engine, expire_on_commit=False)
    get_settings.cache_clear()


@pytest.fixture
def workspace_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def user_id() -> uuid.UUID:
    return uuid.uuid4()


def _principal(*, workspace_id: uuid.UUID, user_id: uuid.UUID, scopes: tuple[str, ...]):
    return McpPrincipal(
        user_id=user_id,
        workspace_id=workspace_id,
        client_id="dcr-test",
        scopes=frozenset(scopes),
        jti=uuid.uuid4(),
    )


@pytest_asyncio.fixture
async def registry() -> ToolRegistry:
    reg = ToolRegistry()
    register_all_tools(reg)
    return reg


@pytest_asyncio.fixture
async def seeded_graph(db, workspace_id) -> AsyncIterator[Path]:
    """Plant a small graph.json under the workspace's vault."""
    async with db() as s:
        ws = WorkspaceRow(id=workspace_id, name="ws", region="us-1")
        s.add(ws)
        await s.commit()
    settings = get_settings()
    vault = Path(settings.knowledge_vault_root) / "us-1" / str(workspace_id)
    vault.mkdir(parents=True, exist_ok=True)

    graph: nx.DiGraph = nx.DiGraph()
    # Two communities, three nodes each.
    for nid in ["py:a.py::util", "py:a.py::caller", "py:a.py::module"]:
        graph.add_node(
            nid,
            id=nid,
            kind="function" if "::util" in nid or "::caller" in nid else "module",
            name=nid.split("::")[-1],
            path="a.py",
            start_line=1,
            end_line=10,
            language="python",
            community_id=0,
        )
    for nid in ["py:b.py::Box", "py:b.py::Box.open", "py:b.py::module"]:
        graph.add_node(
            nid,
            id=nid,
            kind="class"
            if "::Box" == nid.split("::")[-1]
            else ("method" if "open" in nid else "module"),
            name=nid.split("::")[-1],
            path="b.py",
            start_line=1,
            end_line=10,
            language="python",
            community_id=1,
            docstring=("A box you can open." if "Box.open" in nid else None),
        )
    graph.add_edge("py:a.py::caller", "py:a.py::util", kind="calls")
    graph.add_edge("py:a.py::module", "py:a.py::util", kind="imports")
    graph.add_edge("py:b.py::Box", "py:a.py::util", kind="calls")

    out = vault / "code_graph" / "graph.json"
    save_graph(graph, out)
    # Mirror prod: the labels sidecar is written alongside the graph. The
    # community overview surfaces only LABELED communities, so the fixture must
    # persist labels for its two (≥min_size) communities.
    from backend.knowledge.code_graph.pipeline import persist_community_labels

    persist_community_labels(graph, vault / "code_graph" / "communities.json")
    yield vault


async def test_get_node_returns_node_and_neighbors(
    db, workspace_id, user_id, registry, seeded_graph
) -> None:
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",)),
            session=s,
        )
        out = await registry.call_tool(
            "bsvibe_graph_get_node",
            {"node_id": "py:a.py::util"},
            ctx,
        )
    assert out["node"]["id"] == "py:a.py::util"
    # Should have at least the 'caller' as an incoming neighbor.
    inbound = [n for n in out["neighbors"] if n["direction"] == "in"]
    assert any(n["id"] == "py:a.py::caller" for n in inbound)


async def test_get_node_missing_raises(db, workspace_id, user_id, registry, seeded_graph) -> None:
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",)),
            session=s,
        )
        with pytest.raises(ToolError, match="not found"):
            await registry.call_tool(
                "bsvibe_graph_get_node",
                {"node_id": "py:nope.py::ghost"},
                ctx,
            )


async def test_neighbors_filters_by_edge_kind(
    db, workspace_id, user_id, registry, seeded_graph
) -> None:
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",)),
            session=s,
        )
        out = await registry.call_tool(
            "bsvibe_graph_neighbors",
            {"node_id": "py:a.py::util", "kind": "calls"},
            ctx,
        )
    # Only the call-edge neighbors survive.
    assert all(n["edge_kind"] == "calls" for n in out["neighbors"])
    # 'caller' (a.py) and 'Box' (b.py) both have CALLS edges to util.
    ids = {n["id"] for n in out["neighbors"]}
    assert "py:a.py::caller" in ids
    assert "py:b.py::Box" in ids


async def test_shortest_path_returns_chain(
    db, workspace_id, user_id, registry, seeded_graph
) -> None:
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",)),
            session=s,
        )
        out = await registry.call_tool(
            "bsvibe_graph_shortest_path",
            {"src_id": "py:a.py::caller", "dst_id": "py:a.py::util"},
            ctx,
        )
    assert out["hops"] == 1
    assert out["path"][0]["id"] == "py:a.py::caller"
    assert out["path"][-1]["id"] == "py:a.py::util"


async def test_shortest_path_no_path(db, workspace_id, user_id, registry, seeded_graph) -> None:
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",)),
            session=s,
        )
        out = await registry.call_tool(
            "bsvibe_graph_shortest_path",
            {"src_id": "py:a.py::util", "dst_id": "py:b.py::Box"},
            ctx,
        )
    assert out["hops"] == -1


async def test_community_overview_and_members(
    db, workspace_id, user_id, registry, seeded_graph
) -> None:
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",)),
            session=s,
        )
        overview = await registry.call_tool("bsvibe_graph_community", {}, ctx)
        members = await registry.call_tool("bsvibe_graph_community", {"community_id": 0}, ctx)
    # 2 communities seeded.
    cids = {c["community_id"] for c in overview["communities"]}
    assert cids == {0, 1}
    # Members of community 0 all have community_id=0 in raw data.
    assert all(m["community_id"] == 0 for m in members["members"])


async def test_community_overview_excludes_unlabeled_singletons(
    db, workspace_id, user_id, registry, seeded_graph
) -> None:
    """The overview must surface only the NAVIGABLE (labeled) communities.
    Leiden + subdivision leave a long tail of singleton fragments with no label;
    listing all ~2500 raw ids buried the meaningful ones. Add an unlabeled
    singleton (community 99) to the graph and assert it is NOT in the overview,
    and that the labeled communities come back biggest-first."""

    from backend.knowledge.code_graph.graph import load_graph, save_graph

    gpath = seeded_graph / "code_graph" / "graph.json"
    graph: nx.DiGraph = load_graph(gpath)
    graph.add_node(
        "py:z.py::stray",
        id="py:z.py::stray",
        kind="function",
        name="stray",
        path="z.py",
        start_line=1,
        end_line=2,
        language="python",
        community_id=99,  # a singleton — below min_size, so no label entry
    )
    save_graph(graph, gpath)

    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",)),
            session=s,
        )
        overview = await registry.call_tool("bsvibe_graph_community", {}, ctx)

    cids = [c["community_id"] for c in overview["communities"]]
    assert 99 not in cids, "unlabeled singleton must not appear in the overview"
    assert set(cids) == {0, 1}
    # Biggest-first ordering (both size 3 here → tie broken by id, still sorted).
    sizes = [c["size"] for c in overview["communities"]]
    assert sizes == sorted(sizes, reverse=True)


async def test_community_overview_surfaces_subareas(
    db, workspace_id, user_id, registry, seeded_graph
) -> None:
    """The community summary must carry the structured ``subareas`` field
    from the labels sidecar, not only embed it in the prose description —
    MCP-UI parity for the "backend spanning api + mcp" signal."""
    import json as _json

    from backend.knowledge.code_graph.pipeline import community_labels_vault_path

    lpath = community_labels_vault_path(vault_root=seeded_graph)
    lpath.parent.mkdir(parents=True, exist_ok=True)
    lpath.write_text(
        _json.dumps(
            {
                "version": 1,
                "communities": [
                    {
                        "community_id": 0,
                        "label": "backend",
                        "description": "… · spanning backend/api, backend/mcp",
                        "size": 3,
                        "top_symbols": [],
                        "subareas": ["backend/api", "backend/mcp"],
                        "top_paths": [],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",)),
            session=s,
        )
        overview = await registry.call_tool("bsvibe_graph_community", {}, ctx)

    c0 = next(c for c in overview["communities"] if c["community_id"] == 0)
    assert c0["subareas"] == ["backend/api", "backend/mcp"]


async def test_search_finds_node_by_name(db, workspace_id, user_id, registry, seeded_graph) -> None:
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",)),
            session=s,
        )
        out = await registry.call_tool(
            "bsvibe_graph_search",
            {"query": "Box"},
            ctx,
        )
    ids = {r["id"] for r in out["results"]}
    assert "py:b.py::Box" in ids


async def test_search_filters_by_kind(db, workspace_id, user_id, registry, seeded_graph) -> None:
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",)),
            session=s,
        )
        out = await registry.call_tool(
            "bsvibe_graph_search",
            {"query": "util", "kind": "function"},
            ctx,
        )
    assert out["results"]
    assert all(r["kind"] == "function" for r in out["results"])


@pytest_asyncio.fixture
async def graph_with_external(db, workspace_id) -> AsyncIterator[Path]:
    """A graph where an external import stub (BaseModel) shares the query
    substring with an internal node — for the F8 external-exclusion check."""
    async with db() as s:
        ws = WorkspaceRow(id=workspace_id, name="ws", region="us-1")
        s.add(ws)
        await s.commit()
    settings = get_settings()
    vault = Path(settings.knowledge_vault_root) / "us-1" / str(workspace_id)
    vault.mkdir(parents=True, exist_ok=True)
    graph: nx.DiGraph = nx.DiGraph()
    graph.add_node(
        "py:m.py::data_model",
        id="py:m.py::data_model",
        kind="function",
        name="data_model",
        path="m.py",
        start_line=1,
        end_line=5,
        language="python",
        community_id=0,
    )
    graph.add_node(
        "external:pydantic.BaseModel",
        id="external:pydantic.BaseModel",
        kind="external",
        name="BaseModel",
        path="",
        language="python",
    )
    graph.add_edge("py:m.py::data_model", "external:pydantic.BaseModel", kind="imports")
    save_graph(graph, vault / "code_graph" / "graph.json")
    yield vault


async def test_search_excludes_external_import_stubs(
    db, workspace_id, user_id, registry, graph_with_external
) -> None:
    """F8 — graph_search ranks by PageRank and external stubs sink the most
    rank; a plain search must return the codebase's own nodes, not framework
    import stubs that merely match the substring."""
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",)),
            session=s,
        )
        out = await registry.call_tool("bsvibe_graph_search", {"query": "model"}, ctx)
    ids = {r["id"] for r in out["results"]}
    assert "py:m.py::data_model" in ids
    assert "external:pydantic.BaseModel" not in ids


async def test_search_includes_external_when_kind_requested(
    db, workspace_id, user_id, registry, graph_with_external
) -> None:
    """The exclusion is a sensible default, not a hard block: an explicit
    ``kind='external'`` query still surfaces the import stubs."""
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",)),
            session=s,
        )
        out = await registry.call_tool(
            "bsvibe_graph_search", {"query": "model", "kind": "external"}, ctx
        )
    ids = {r["id"] for r in out["results"]}
    assert "external:pydantic.BaseModel" in ids


async def test_no_graph_yet_returns_clean_error(db, workspace_id, user_id, registry) -> None:
    async with db() as s:
        ws = WorkspaceRow(id=workspace_id, name="ws", region="us-1")
        s.add(ws)
        await s.commit()
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",)),
            session=s,
        )
        with pytest.raises(ToolError, match="no code graph"):
            await registry.call_tool(
                "bsvibe_graph_community",
                {},
                ctx,
            )
