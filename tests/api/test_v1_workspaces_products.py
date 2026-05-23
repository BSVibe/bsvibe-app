"""/api/v1/workspaces + /api/v1/products — full CRUD against real PG."""

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
from backend.workspaces.db import WorkspacesBase

from .._support import db_engine, fake_current_user

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def db():
    async with db_engine(WorkspacesBase) as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


@pytest_asyncio.fixture
async def client_with_ws(db):
    """Client + a pre-created workspace + dep override."""
    app = create_app()
    workspace_id = uuid.uuid4()

    def _ws() -> uuid.UUID:
        return workspace_id

    async def _session():
        async with db() as s:
            yield s

    app.dependency_overrides[get_current_user] = fake_current_user()
    app.dependency_overrides[get_workspace_id] = _ws
    app.dependency_overrides[get_db_session] = _session

    # Seed the workspace row so /api/v1/products has a parent, plus an owner
    # membership for the fake principal so role-gated routes (product DELETE
    # requires admin+) resolve a real Membership.role.
    from backend.workspaces.db import WorkspaceRow

    async with db() as s:
        s.add(WorkspaceRow(id=workspace_id, name="test", region="us-1", safe_mode=True))
        user = UserRow(id=uuid.uuid4(), supabase_user_id="test-user", email="t@example.com")
        s.add(user)
        await s.flush()
        s.add(
            MembershipRow(id=uuid.uuid4(), user_id=user.id, workspace_id=workspace_id, role="owner")
        )
        await s.commit()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c, workspace_id


async def test_workspaces_full_lifecycle(db) -> None:
    app = create_app()

    async def _session():
        async with db() as s:
            yield s

    # Seed a real user row so the FK from memberships → users holds on PG
    # (SQLite doesn't enforce it, but real Postgres does).
    async with db() as s:
        s.add(UserRow(id=uuid.uuid4(), supabase_user_id="test-user", email="t@x"))
        await s.commit()

    app.dependency_overrides[get_db_session] = _session
    app.dependency_overrides[get_current_user] = fake_current_user("test-user")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        # Initially empty
        r = await c.get("/api/v1/workspaces")
        assert r.status_code == 200
        assert r.json() == []

        # Create
        r = await c.post(
            "/api/v1/workspaces",
            json={"name": "Acme", "region": "us-1", "safe_mode": False},
        )
        assert r.status_code == 201, r.text
        created = r.json()
        ws_id = created["id"]
        assert created["safe_mode"] is False

        # List
        r = await c.get("/api/v1/workspaces")
        assert len(r.json()) == 1

        # Get
        r = await c.get(f"/api/v1/workspaces/{ws_id}")
        assert r.status_code == 200

        # Patch
        r = await c.patch(f"/api/v1/workspaces/{ws_id}", json={"region": "eu-1"})
        assert r.status_code == 200
        assert r.json()["region"] == "eu-1"

        # Delete
        r = await c.delete(f"/api/v1/workspaces/{ws_id}")
        assert r.status_code == 204
        r = await c.get(f"/api/v1/workspaces/{ws_id}")
        assert r.status_code == 404


async def test_products_full_lifecycle(client_with_ws) -> None:
    c, workspace_id = client_with_ws
    # Initial empty
    r = await c.get("/api/v1/products")
    assert r.status_code == 200
    assert r.json() == []

    # Create
    r = await c.post(
        "/api/v1/products",
        json={"name": "My Blog", "slug": "my-blog", "repo_url": "https://x/y"},
    )
    assert r.status_code == 201, r.text
    product_id = r.json()["id"]

    # Slug conflict
    r = await c.post("/api/v1/products", json={"name": "Other", "slug": "my-blog"})
    assert r.status_code == 409

    # Invalid slug format
    r = await c.post("/api/v1/products", json={"name": "X", "slug": "Bad Slug"})
    assert r.status_code == 422

    # List
    r = await c.get("/api/v1/products")
    assert len(r.json()) == 1

    # Patch
    r = await c.patch(f"/api/v1/products/{product_id}", json={"name": "Renamed"})
    assert r.status_code == 200
    assert r.json()["name"] == "Renamed"

    # Delete
    r = await c.delete(f"/api/v1/products/{product_id}")
    assert r.status_code == 204


async def test_product_workspace_isolation(db) -> None:
    """A product in workspace A is NOT visible / patchable from workspace B."""
    app = create_app()
    ws_a = uuid.uuid4()
    ws_b = uuid.uuid4()

    async def _session():
        async with db() as s:
            yield s

    app.dependency_overrides[get_db_session] = _session
    app.dependency_overrides[get_current_user] = fake_current_user()
    from backend.workspaces.db import ProductRow, WorkspaceRow

    product_id = uuid.uuid4()
    async with db() as s:
        s.add(WorkspaceRow(id=ws_a, name="a", region="us-1", safe_mode=True))
        s.add(WorkspaceRow(id=ws_b, name="b", region="us-1", safe_mode=True))
        await s.flush()
        s.add(ProductRow(id=product_id, workspace_id=ws_a, name="A's blog", slug="a-blog"))
        await s.commit()

    transport = httpx.ASGITransport(app=app)
    # Workspace B's view
    app.dependency_overrides[get_workspace_id] = lambda: ws_b
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.get("/api/v1/products")
        assert r.json() == []
        r = await c.get(f"/api/v1/products/{product_id}")
        assert r.status_code == 404
