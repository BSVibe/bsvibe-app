"""Authentication + workspace-scoping on the v1 routers.

Covers the four required proofs:
  * unauthenticated request → 401
  * authenticated request → succeeds and is workspace-scoped
  * cross-workspace isolation — user A cannot read user B's rows
  * the workspaces router is scoped to the caller's memberships
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.execution.db import ExecutionRun, RunStatus
from backend.identity.db import MembershipRow, UserRow
from backend.workspaces.db import ProductRow

from .conftest import seed_user_workspace

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Unauthenticated → 401
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/products",
        "/api/v1/runs",
        "/api/v1/accounts",
        "/api/v1/decisions",
        "/api/v1/workspaces",
        "/api/v1/settings",
        "/api/v1/presets",
        "/api/v1/skills",
    ],
)
async def test_unauthenticated_request_is_rejected(client, path: str) -> None:
    r = await client.get(path)
    assert r.status_code == 401, f"{path} → {r.status_code}: {r.text}"


# ---------------------------------------------------------------------------
# Authenticated + workspace-scoped success
# ---------------------------------------------------------------------------
async def test_authenticated_request_is_workspace_scoped(
    authed_client_factory, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    async with session_factory() as s:
        _user, ws, _m = await seed_user_workspace(s, supabase_user_id="user-a")
        s.add(ProductRow(id=uuid.uuid4(), workspace_id=ws.id, name="A blog", slug="a-blog"))
        await s.commit()

    async with authed_client_factory("user-a") as c:
        r = await c.get("/api/v1/products")
        assert r.status_code == 200, r.text
        rows = r.json()
        assert len(rows) == 1
        assert rows[0]["slug"] == "a-blog"
        assert rows[0]["workspace_id"] == str(ws.id)


async def test_authenticated_user_without_membership_is_forbidden(
    authed_client_factory, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    # A verified principal that has no membership cannot resolve a workspace.
    async with session_factory() as s:
        s.add(UserRow(id=uuid.uuid4(), supabase_user_id="orphan", email="o@example.com"))
        await s.commit()

    async with authed_client_factory("orphan") as c:
        r = await c.get("/api/v1/products")
        assert r.status_code == 403, r.text


# ---------------------------------------------------------------------------
# Cross-workspace isolation
# ---------------------------------------------------------------------------
async def test_cross_workspace_isolation_products(
    authed_client_factory, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    async with session_factory() as s:
        _ua, ws_a, _ma = await seed_user_workspace(s, supabase_user_id="user-a")
        _ub, ws_b, _mb = await seed_user_workspace(s, supabase_user_id="user-b")
        a_prod = uuid.uuid4()
        b_prod = uuid.uuid4()
        s.add(ProductRow(id=a_prod, workspace_id=ws_a.id, name="A", slug="a"))
        s.add(ProductRow(id=b_prod, workspace_id=ws_b.id, name="B", slug="b"))
        await s.commit()

    async with authed_client_factory("user-a") as c:
        # List shows only A's product.
        r = await c.get("/api/v1/products")
        assert r.status_code == 200, r.text
        slugs = {p["slug"] for p in r.json()}
        assert slugs == {"a"}

        # B's product is invisible to A → 404.
        r = await c.get(f"/api/v1/products/{b_prod}")
        assert r.status_code == 404, r.text

        # A's own product is reachable.
        r = await c.get(f"/api/v1/products/{a_prod}")
        assert r.status_code == 200, r.text


async def test_cross_workspace_isolation_runs(
    authed_client_factory, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    async with session_factory() as s:
        _ua, ws_a, _ma = await seed_user_workspace(s, supabase_user_id="user-a")
        _ub, ws_b, _mb = await seed_user_workspace(s, supabase_user_id="user-b")
        a_run = uuid.uuid4()
        b_run = uuid.uuid4()
        for rid, ws in ((a_run, ws_a), (b_run, ws_b)):
            s.add(
                ExecutionRun(
                    id=rid,
                    workspace_id=ws.id,
                    status=RunStatus.OPEN,
                    payload={},
                    created_at=datetime.now(tz=UTC),
                    updated_at=datetime.now(tz=UTC),
                )
            )
        await s.commit()

    async with authed_client_factory("user-a") as c:
        r = await c.get("/api/v1/runs")
        assert r.status_code == 200, r.text
        ids = {row["id"] for row in r.json()}
        assert ids == {str(a_run)}

        r = await c.get(f"/api/v1/runs/{b_run}")
        assert r.status_code == 404, r.text


# ---------------------------------------------------------------------------
# Workspaces router — membership-scoped
# ---------------------------------------------------------------------------
async def test_workspaces_list_only_my_memberships(
    authed_client_factory, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    async with session_factory() as s:
        _ua, ws_a, _ma = await seed_user_workspace(s, supabase_user_id="user-a")
        _ub, ws_b, _mb = await seed_user_workspace(s, supabase_user_id="user-b")
        await s.commit()

    async with authed_client_factory("user-a") as c:
        r = await c.get("/api/v1/workspaces")
        assert r.status_code == 200, r.text
        ids = {w["id"] for w in r.json()}
        assert ids == {str(ws_a.id)}

        # B's workspace is not directly readable by A.
        r = await c.get(f"/api/v1/workspaces/{ws_b.id}")
        assert r.status_code == 404, r.text


async def test_create_workspace_grants_owner_membership(
    authed_client_factory, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    # Bare user (post-login) with no workspace yet.
    async with session_factory() as s:
        s.add(UserRow(id=uuid.uuid4(), supabase_user_id="user-a", email="a@example.com"))
        await s.commit()

    async with authed_client_factory("user-a") as c:
        r = await c.post("/api/v1/workspaces", json={"name": "Acme", "region": "us-1"})
        assert r.status_code == 201, r.text
        ws_id = r.json()["id"]

        # The new workspace is now visible to its creator.
        r = await c.get("/api/v1/workspaces")
        assert {w["id"] for w in r.json()} == {ws_id}

    async with session_factory() as s:
        membership = (await s.execute(select(MembershipRow))).scalars().one()
        assert membership.role == "owner"
        assert str(membership.workspace_id) == ws_id
