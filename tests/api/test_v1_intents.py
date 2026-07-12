"""/api/v1/intents — author intent definitions + examples (NL routing Lift N2).

The embedder is monkeypatched at the endpoint's build seam so no real embedding
API is ever called: the default fake returns a fixed vector; the
no-embedding-config case patches the seam to return ``None``.
"""

from __future__ import annotations

import uuid

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker

import backend.embedding.db  # noqa: F401 — register intent tables
import backend.router.accounts.account_models  # noqa: F401 — register accounts table
from backend.api.deps import (
    get_current_user,
    get_db_session,
    get_workspace_id,
    require_account_id,
)
from backend.api.main import create_app
from backend.embedding.service import EmbeddedExample

from .._support import db_engine, fake_current_user

pytestmark = pytest.mark.asyncio


class _StubEmbedder:
    model = "stub/embed-1"

    async def embed_one(self, text: str) -> EmbeddedExample:
        return EmbeddedExample(text=text, embedding=[1.0, 0.0, 0.0], model=self.model)


@pytest_asyncio.fixture
async def db():
    async with db_engine() as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


@pytest_asyncio.fixture
async def account_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest_asyncio.fixture
async def client(db, account_id, monkeypatch):
    # Default: a stub embedder is configured (no real API touched).
    import backend.api.v1.intents as intents_mod

    async def _stub_build(session, *, workspace_id, account_id):
        return _StubEmbedder()

    monkeypatch.setattr(intents_mod, "build_account_embedder", _stub_build)

    app = create_app()
    workspace_id = uuid.uuid4()

    async def _session():
        async with db() as s:
            yield s

    app.dependency_overrides[get_current_user] = fake_current_user()
    app.dependency_overrides[get_workspace_id] = lambda: workspace_id
    app.dependency_overrides[require_account_id] = lambda: account_id
    app.dependency_overrides[get_db_session] = _session

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_list_empty(client) -> None:
    r = await client.get("/api/v1/intents")
    assert r.status_code == 200
    assert r.json() == []


async def test_create_list_delete_round_trip(client) -> None:
    body = {
        "name": "marketing",
        "threshold": 0.7,
        "examples": ["write a launch tweet", "draft a blog post"],
    }
    r = await client.post("/api/v1/intents", json=body)
    assert r.status_code == 201, r.text
    created = r.json()
    assert created["name"] == "marketing"
    assert created["threshold"] == 0.7
    intent_id = created["id"]

    r = await client.get("/api/v1/intents")
    assert r.status_code == 200
    names = {i["name"] for i in r.json()}
    assert names == {"marketing"}

    r = await client.delete(f"/api/v1/intents/{intent_id}")
    assert r.status_code == 204

    r = await client.get("/api/v1/intents")
    assert r.json() == []


async def test_create_defaults_threshold(client) -> None:
    r = await client.post("/api/v1/intents", json={"name": "design", "examples": ["make a logo"]})
    assert r.status_code == 201, r.text
    assert r.json()["threshold"] == 0.65


async def test_create_duplicate_name_conflicts(client) -> None:
    body = {"name": "dup", "examples": []}
    assert (await client.post("/api/v1/intents", json=body)).status_code == 201
    r = await client.post("/api/v1/intents", json=body)
    assert r.status_code == 409


async def test_create_without_embedding_config_is_graceful(client, monkeypatch) -> None:
    """No embedding model configured -> 201 with the intent + examples still
    created (embedding=None), NOT a hard failure."""
    import backend.api.v1.intents as intents_mod

    async def _no_embedder(session, *, workspace_id, account_id):
        return None

    monkeypatch.setattr(intents_mod, "build_account_embedder", _no_embedder)

    r = await client.post(
        "/api/v1/intents", json={"name": "nomodel", "examples": ["some work phrase"]}
    )
    assert r.status_code == 201, r.text
    assert r.json()["name"] == "nomodel"


async def test_create_rejects_extra_fields(client) -> None:
    r = await client.post("/api/v1/intents", json={"name": "x", "examples": [], "bogus": 1})
    assert r.status_code == 422


async def test_create_rejects_empty_name(client) -> None:
    r = await client.post("/api/v1/intents", json={"name": "", "examples": []})
    assert r.status_code == 422


async def test_delete_unknown_intent_404(client) -> None:
    r = await client.delete(f"/api/v1/intents/{uuid.uuid4()}")
    assert r.status_code == 404
