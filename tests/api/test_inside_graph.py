"""/api/v1/inside/graph — the founder's force-directed knowledge-graph view.

The Knowledge surface needs nodes + edges, not just the concept/observation
LISTS the other two ``/inside`` endpoints serve. This endpoint sources its
graph from the SAME per-workspace knowledge store the rest of the stack uses —
a :class:`~backend.knowledge.graph.vault_backend.VaultBackend` rooted at the
caller's ``<vault_root>/<region>/<workspace_id>/`` vault (FS-as-SoT; it loads
the ``.bsage/graph_cache.json`` snapshot the GraphSubscriber persists from vault
writes). It is strictly read-only — there is no graph WRITE path here.

These tests seed a REAL per-workspace graph by driving the production
``VaultBackend`` (upsert entities + a relationship, then ``close()`` persists
the cache), so the endpoint loads exactly the snapshot the subscriber would have
left. Workspace isolation is structural: a graph seeded in another workspace's
vault is never read. A fresh workspace yields ``{nodes: [], edges: []}`` — 200,
never an error.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

from backend.api.deps import get_current_user, get_workspace_id
from backend.api.main import create_app
from backend.api.v1.inside import build_inside_storage
from backend.knowledge.graph.graph_models import GraphEntity, GraphRelationship
from backend.knowledge.graph.storage import FileSystemStorage
from backend.knowledge.graph.vault_backend import VaultBackend

from .._support import fake_current_user

pytestmark = pytest.mark.asyncio

_REGION = "us-1"


# ---------------------------------------------------------------------------
# Seed helper — drive the REAL VaultBackend, persisting its cache to the vault.
# ---------------------------------------------------------------------------
async def _seed_graph(storage: FileSystemStorage) -> tuple[str, str]:
    """Seed two entities + one relationship into the workspace graph cache.

    Returns (auth_id, jwks_id) — the resolved entity ids so the test can assert
    on the edge's source/target.
    """
    backend = VaultBackend(storage)
    await backend.initialize()
    auth_id = await backend.upsert_entity(
        GraphEntity(name="Auth", entity_type="concept", source_path="concepts/active/auth.md")
    )
    jwks_id = await backend.upsert_entity(
        GraphEntity(name="JWKS", entity_type="concept", source_path="concepts/active/jwks.md")
    )
    await backend.upsert_relationship(
        GraphRelationship(
            source_id=auth_id,
            target_id=jwks_id,
            rel_type="relates_to",
            source_path="concepts/active/auth.md",
            weight=0.8,
            edge_type="strong",
        )
    )
    await backend.close()  # persists .bsage/graph_cache.json
    return auth_id, jwks_id


# ---------------------------------------------------------------------------
# Fixtures — mirror tests/api/test_inside.py
# ---------------------------------------------------------------------------
@pytest.fixture
def workspace_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def vault_root(tmp_path: Path) -> Path:
    return tmp_path / "vault"


@pytest.fixture
def workspace_storage(vault_root: Path, workspace_id: uuid.UUID) -> FileSystemStorage:
    root = vault_root / _REGION / str(workspace_id)
    root.mkdir(parents=True, exist_ok=True)
    return FileSystemStorage(root)


@pytest_asyncio.fixture
async def client(vault_root: Path, workspace_id: uuid.UUID):
    app = create_app()

    def _ws() -> uuid.UUID:
        return workspace_id

    async def _storage(ws: uuid.UUID = workspace_id) -> FileSystemStorage:
        root = vault_root / _REGION / str(ws)
        root.mkdir(parents=True, exist_ok=True)
        return FileSystemStorage(root)

    app.dependency_overrides[get_current_user] = fake_current_user()
    app.dependency_overrides[get_workspace_id] = _ws
    app.dependency_overrides[build_inside_storage] = _storage

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ---------------------------------------------------------------------------
# graph
# ---------------------------------------------------------------------------
async def test_graph_returns_nodes_and_edges(client, workspace_storage) -> None:
    """A workspace with graph data → nodes + edges with the wire shape."""
    auth_id, jwks_id = await _seed_graph(workspace_storage)

    r = await client.get("/api/v1/inside/graph")
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"nodes", "edges"}

    node_ids = {n["id"] for n in body["nodes"]}
    assert {auth_id, jwks_id} <= node_ids
    labels = {n["label"] for n in body["nodes"]}
    assert {"Auth", "JWKS"} <= labels

    # The node shape carries id + label (+ optional kind/weight).
    auth_node = next(n for n in body["nodes"] if n["id"] == auth_id)
    assert auth_node["label"] == "Auth"
    assert auth_node["kind"] == "concept"

    assert len(body["edges"]) == 1
    edge = body["edges"][0]
    assert edge["source"] == auth_id
    assert edge["target"] == jwks_id
    assert edge["type"] == "relates_to"


async def test_graph_empty_workspace(client) -> None:
    """A fresh workspace → empty nodes + edges, 200 (never an error)."""
    r = await client.get("/api/v1/inside/graph")
    assert r.status_code == 200, r.text
    assert r.json() == {"nodes": [], "edges": []}


async def test_graph_sparse_nodes_no_edges(client, workspace_storage) -> None:
    """Nodes with zero relationships still return — edges empty, no error."""
    backend = VaultBackend(workspace_storage)
    await backend.initialize()
    await backend.upsert_entity(
        GraphEntity(name="Lonely", entity_type="concept", source_path="concepts/active/lonely.md")
    )
    await backend.close()

    r = await client.get("/api/v1/inside/graph")
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["nodes"]) == 1
    assert body["nodes"][0]["label"] == "Lonely"
    assert body["edges"] == []


async def test_graph_workspace_isolation(client, vault_root) -> None:
    """A graph in a DIFFERENT workspace's vault is never read."""
    other_ws = uuid.uuid4()
    other_root = vault_root / _REGION / str(other_ws)
    other_root.mkdir(parents=True, exist_ok=True)
    await _seed_graph(FileSystemStorage(other_root))

    r = await client.get("/api/v1/inside/graph")
    assert r.status_code == 200, r.text
    assert r.json() == {"nodes": [], "edges": []}


async def test_graph_caps_large_graph(client, workspace_storage) -> None:
    """A large graph is capped to a sensible top-N node budget."""
    backend = VaultBackend(workspace_storage)
    await backend.initialize()
    # Seed a hub + many leaves so degree/centrality has a clear ordering.
    hub = await backend.upsert_entity(
        GraphEntity(name="Hub", entity_type="concept", source_path="concepts/active/hub.md")
    )
    for i in range(300):
        leaf = await backend.upsert_entity(
            GraphEntity(
                name=f"Leaf {i}",
                entity_type="concept",
                source_path=f"concepts/active/leaf-{i}.md",
            )
        )
        await backend.upsert_relationship(
            GraphRelationship(
                source_id=hub,
                target_id=leaf,
                rel_type="relates_to",
                source_path="concepts/active/hub.md",
            )
        )
    await backend.close()

    r = await client.get("/api/v1/inside/graph")
    assert r.status_code == 200, r.text
    body = r.json()
    # Capped — not all 301 nodes returned.
    assert len(body["nodes"]) <= 200
    # The hub (highest centrality) survives the cap.
    assert any(n["id"] == hub for n in body["nodes"])
    # Edges only reference surviving nodes.
    surviving = {n["id"] for n in body["nodes"]}
    for edge in body["edges"]:
        assert edge["source"] in surviving
        assert edge["target"] in surviving
