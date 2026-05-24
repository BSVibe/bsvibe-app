"""/api/v1/products/{product_id}/resources — per-product resource CRUD.

A product Resource is a named pointer to something the product works
with — a repo, a doc, a deploy, a free note. Workspace-scoped exactly like
the parent product: a resource for a product in workspace A is invisible /
404 from workspace B, and adding to a product the caller can't see 404s.
"""

from __future__ import annotations

import uuid

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.api.deps import (
    get_current_user,
    get_db_session,
    get_workspace_id,
)
from backend.api.main import create_app
from backend.identity.db import MembershipRow, UserRow  # noqa: F401 — register tables
from backend.workspaces.db import ProductResourceRow, ProductRow, WorkspaceRow, WorkspacesBase

from .._support import db_engine, fake_current_user

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def db():
    async with db_engine(WorkspacesBase) as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


@pytest_asyncio.fixture
async def client_with_product(db):
    """Client + a seeded workspace + a product to hang resources on."""
    app = create_app()
    workspace_id = uuid.uuid4()
    product_id = uuid.uuid4()

    async def _session():
        async with db() as s:
            yield s

    app.dependency_overrides[get_current_user] = fake_current_user()
    app.dependency_overrides[get_workspace_id] = lambda: workspace_id
    app.dependency_overrides[get_db_session] = _session

    async with db() as s:
        s.add(WorkspaceRow(id=workspace_id, name="test", region="us-1", safe_mode=True))
        s.add(ProductRow(id=product_id, workspace_id=workspace_id, name="Blog", slug="blog"))
        await s.commit()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c, workspace_id, product_id


async def test_add_resource_persists_and_lists(client_with_product) -> None:
    c, _ws, product_id = client_with_product

    # Initially empty.
    r = await c.get(f"/api/v1/products/{product_id}/resources")
    assert r.status_code == 200
    assert r.json() == []

    # Add one.
    r = await c.post(
        f"/api/v1/products/{product_id}/resources",
        json={
            "kind": "repo",
            "title": "Main repo",
            "url": "https://github.com/acme/blog",
            "note": "the source of truth",
        },
    )
    assert r.status_code == 201, r.text
    created = r.json()
    assert created["title"] == "Main repo"
    assert created["kind"] == "repo"
    assert created["url"] == "https://github.com/acme/blog"
    assert created["product_id"] == str(product_id)

    # Now listed.
    r = await c.get(f"/api/v1/products/{product_id}/resources")
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 1
    assert rows[0]["id"] == created["id"]


async def test_add_resource_minimal_url_and_note_optional(client_with_product) -> None:
    c, _ws, product_id = client_with_product
    r = await c.post(
        f"/api/v1/products/{product_id}/resources",
        json={"kind": "note", "title": "A bare note"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["url"] is None
    assert body["note"] is None


async def test_add_resource_rejects_blank_title(client_with_product) -> None:
    c, _ws, product_id = client_with_product
    r = await c.post(
        f"/api/v1/products/{product_id}/resources",
        json={"kind": "link", "title": "   "},
    )
    assert r.status_code == 422


async def test_add_resource_rejects_extra_field(client_with_product) -> None:
    c, _ws, product_id = client_with_product
    r = await c.post(
        f"/api/v1/products/{product_id}/resources",
        json={"kind": "link", "title": "ok", "bogus": "nope"},
    )
    assert r.status_code == 422


async def test_add_resource_rejects_bad_url(client_with_product) -> None:
    c, _ws, product_id = client_with_product
    r = await c.post(
        f"/api/v1/products/{product_id}/resources",
        json={"kind": "link", "title": "ok", "url": "not a url"},
    )
    assert r.status_code == 422


async def test_add_resource_unknown_product_404(client_with_product) -> None:
    c, _ws, _product_id = client_with_product
    missing = uuid.uuid4()
    r = await c.post(
        f"/api/v1/products/{missing}/resources",
        json={"kind": "link", "title": "ok"},
    )
    assert r.status_code == 404
    r = await c.get(f"/api/v1/products/{missing}/resources")
    assert r.status_code == 404


async def test_delete_resource(client_with_product) -> None:
    c, _ws, product_id = client_with_product
    r = await c.post(
        f"/api/v1/products/{product_id}/resources",
        json={"kind": "doc", "title": "Plan"},
    )
    resource_id = r.json()["id"]

    r = await c.delete(f"/api/v1/products/{product_id}/resources/{resource_id}")
    assert r.status_code == 204

    r = await c.get(f"/api/v1/products/{product_id}/resources")
    assert r.json() == []

    # Deleting again 404s.
    r = await c.delete(f"/api/v1/products/{product_id}/resources/{resource_id}")
    assert r.status_code == 404


async def test_resource_workspace_isolation(db) -> None:
    """A resource on a product in workspace A is invisible from workspace B."""
    app = create_app()
    ws_a = uuid.uuid4()
    ws_b = uuid.uuid4()
    product_id = uuid.uuid4()
    resource_id = uuid.uuid4()

    async def _session():
        async with db() as s:
            yield s

    app.dependency_overrides[get_db_session] = _session
    app.dependency_overrides[get_current_user] = fake_current_user()

    async with db() as s:
        s.add(WorkspaceRow(id=ws_a, name="a", region="us-1", safe_mode=True))
        s.add(WorkspaceRow(id=ws_b, name="b", region="us-1", safe_mode=True))
        await s.flush()
        s.add(ProductRow(id=product_id, workspace_id=ws_a, name="A blog", slug="a-blog"))
        await s.flush()
        s.add(
            ProductResourceRow(
                id=resource_id,
                workspace_id=ws_a,
                product_id=product_id,
                kind="repo",
                title="A's repo",
            )
        )
        await s.commit()

    transport = httpx.ASGITransport(app=app)

    # Workspace B can't see workspace A's product at all → 404 on its resources.
    app.dependency_overrides[get_workspace_id] = lambda: ws_b
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.get(f"/api/v1/products/{product_id}/resources")
        assert r.status_code == 404
        r = await c.delete(f"/api/v1/products/{product_id}/resources/{resource_id}")
        assert r.status_code == 404

    # Workspace A sees its own resource.
    app.dependency_overrides[get_workspace_id] = lambda: ws_a
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.get(f"/api/v1/products/{product_id}/resources")
        assert r.status_code == 200
        assert len(r.json()) == 1
