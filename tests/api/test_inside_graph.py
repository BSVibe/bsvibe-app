"""/api/v1/inside/graph — the founder's force-directed knowledge-graph view.

The Knowledge surface needs nodes + edges, not just the concept/observation
LISTS the other two ``/inside`` endpoints serve. This endpoint builds its graph
**deterministically from the settled canonicalization vault** rooted at the
caller's ``<vault_root>/<region>/<workspace_id>/`` (FS-as-SoT) — active concepts
become nodes, and concepts that co-occur in the same garden observation become
``co-occurs`` edges (see
:func:`backend.knowledge.canonicalization.concept_graph.build_concept_graph`).

It was previously sourced from a ``VaultBackend`` ``.bsage/graph_cache.json``
snapshot the GraphSubscriber persists — but that extractor path is NOT wired in
this deployment, so the graph was always empty even though concepts existed.
These tests now seed REAL active concepts (via a permissive
``CanonicalizationService``, exactly like the promotion e2e helpers) plus garden
observations whose tags reference those concepts, so a workspace with
co-occurring concepts yields non-empty ``nodes`` AND ``edges``. No LLM, no
network. Workspace isolation is structural: a vault seeded in another
workspace's root is never read. A fresh workspace yields ``{nodes: [],
edges: []}`` — 200, never an error.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

from backend.api.deps import get_current_user, get_output_language, get_workspace_id
from backend.api.main import create_app
from backend.api.v1.inside import build_inside_storage
from backend.knowledge.canonicalization.index import InMemoryCanonicalizationIndex
from backend.knowledge.canonicalization.lock import AsyncIOMutationLock
from backend.knowledge.canonicalization.resolver import TagResolver
from backend.knowledge.canonicalization.service import CanonicalizationService
from backend.knowledge.canonicalization.store import NoteStore
from backend.knowledge.graph.storage import FileSystemStorage

from .._support import fake_current_user

pytestmark = pytest.mark.asyncio

_REGION = "us-1"
_FIXED_NOW = datetime(2026, 5, 24, 12, 0, 0)


# ---------------------------------------------------------------------------
# Seed helpers — create REAL active concepts + garden observations.
# ---------------------------------------------------------------------------
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


async def _seed_cooccurrence(storage: FileSystemStorage) -> None:
    """Two concepts that co-occur in one observation (auth + jwks)."""
    await _seed_concepts(storage, ["auth", "jwks"])
    await storage.write(
        "garden/seedling/obs.md", _garden_note("settle", "verified-run", "auth", "jwks")
    )


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
    # Default English; per-test override sets a workspace language.
    app.dependency_overrides[get_output_language] = lambda: "en"

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        c._app = app  # expose for per-test dependency overrides
        yield c


async def test_graph_localizes_node_label_for_workspace_language(client, workspace_storage) -> None:
    """A KO workspace renders concept nodes with their Korean display label while
    the node ``id`` stays the English identifier (founder decision 2026-07)."""
    from backend.knowledge.canonicalization import models

    store = NoteStore(workspace_storage)
    await store.write_concept(
        models.ConceptEntry(
            concept_id="http-client",
            path="concepts/active/http-client.md",
            display="Http client",
            aliases=[],
            created_at=_FIXED_NOW,
            updated_at=_FIXED_NOW,
            display_labels={"ko": "HTTP 클라이언트"},
        )
    )
    client._app.dependency_overrides[get_output_language] = lambda: "ko"

    r = await client.get("/api/v1/inside/graph")
    assert r.status_code == 200, r.text
    node = next(n for n in r.json()["nodes"] if n["id"] == "http-client")
    # Identity unchanged; label localized.
    assert node["id"] == "http-client"
    assert node["label"] == "HTTP 클라이언트"


# ---------------------------------------------------------------------------
# graph
# ---------------------------------------------------------------------------
async def test_graph_returns_nodes_and_edges(client, workspace_storage) -> None:
    """Co-occurring concepts → concept nodes + a ``co-occurs`` edge, wire shape."""
    await _seed_cooccurrence(workspace_storage)

    r = await client.get("/api/v1/inside/graph")
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"nodes", "edges"}

    node_ids = {n["id"] for n in body["nodes"]}
    assert {"auth", "jwks"} <= node_ids
    labels = {n["label"] for n in body["nodes"]}
    assert {"auth", "jwks"} <= labels

    # Node shape carries id + label + kind (entity_type="concept") + community.
    auth_node = next(n for n in body["nodes"] if n["id"] == "auth")
    assert auth_node["label"] == "auth"
    assert auth_node["kind"] == "concept"
    # COMMUNITY legend mode colours by this — every node carries a non-empty id.
    assert isinstance(auth_node["community"], str)
    assert auth_node["community"]
    # Two concepts co-occurring in one observation are in the same community.
    jwks_node = next(n for n in body["nodes"] if n["id"] == "jwks")
    assert auth_node["community"] == jwks_node["community"]

    # One undirected co-occurs edge between the two concepts.
    assert len(body["edges"]) == 1
    edge = body["edges"][0]
    assert {edge["source"], edge["target"]} == {"auth", "jwks"}
    assert edge["type"] == "co-occurs"
    assert edge["weight"] == 1.0


async def test_graph_empty_workspace(client) -> None:
    """A fresh workspace → empty nodes + edges, 200 (never an error)."""
    r = await client.get("/api/v1/inside/graph")
    assert r.status_code == 200, r.text
    assert r.json() == {"nodes": [], "edges": []}


async def test_graph_sparse_nodes_no_edges(client, workspace_storage) -> None:
    """A concept with no co-occurrence still returns — edges empty, no error."""
    await _seed_concepts(workspace_storage, ["lonely"])

    r = await client.get("/api/v1/inside/graph")
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["nodes"]) == 1
    assert body["nodes"][0]["label"] == "lonely"
    assert body["edges"] == []
    # Even an isolated node carries a valid community id (trivial-graph case).
    assert isinstance(body["nodes"][0]["community"], str)
    assert body["nodes"][0]["community"]


async def test_graph_workspace_isolation(client, vault_root) -> None:
    """A graph in a DIFFERENT workspace's vault is never read."""
    other_ws = uuid.uuid4()
    other_root = vault_root / _REGION / str(other_ws)
    other_root.mkdir(parents=True, exist_ok=True)
    await _seed_cooccurrence(FileSystemStorage(other_root))

    r = await client.get("/api/v1/inside/graph")
    assert r.status_code == 200, r.text
    assert r.json() == {"nodes": [], "edges": []}


async def test_graph_caps_large_graph(client, workspace_storage) -> None:
    """A large graph is capped to a sensible top-N node budget; the hub
    (highest degree) survives and edges only reference surviving nodes."""
    # Seed a hub concept + many leaves, each co-occurring with the hub in one
    # observation so the hub has the highest degree.
    leaves = [f"leaf-{i}" for i in range(300)]
    await _seed_concepts(workspace_storage, ["hub", *leaves])
    for i, leaf in enumerate(leaves):
        await workspace_storage.write(
            f"garden/seedling/obs-{i}.md", _garden_note("settle", "hub", leaf)
        )

    r = await client.get("/api/v1/inside/graph")
    assert r.status_code == 200, r.text
    body = r.json()
    # Capped — not all 301 nodes returned.
    assert len(body["nodes"]) <= 200
    # The hub (highest degree) survives the cap.
    assert any(n["id"] == "hub" for n in body["nodes"])
    # Edges only reference surviving nodes.
    surviving = {n["id"] for n in body["nodes"]}
    for edge in body["edges"]:
        assert edge["source"] in surviving
        assert edge["target"] in surviving
