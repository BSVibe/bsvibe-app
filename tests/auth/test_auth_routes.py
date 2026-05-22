"""/api/auth/* — login, OAuth callback bootstrap, refresh, logout.

Supabase is mocked end-to-end (``FakeSupabaseClient``). The focus is the
§10.1 bootstrap: first successful login upserts a ``User`` and creates a
``Workspace`` + ``Membership(role='owner')`` when the user has none.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.identity.db import MembershipRow, UserRow
from backend.workspaces.db import WorkspaceRow

pytestmark = pytest.mark.asyncio


async def _counts(session: AsyncSession) -> tuple[int, int, int]:
    users = len((await session.execute(select(UserRow))).scalars().all())
    workspaces = len((await session.execute(select(WorkspaceRow))).scalars().all())
    memberships = len((await session.execute(select(MembershipRow))).scalars().all())
    return users, workspaces, memberships


async def test_login_bootstraps_user_workspace_membership(
    client, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    r = await client.post(
        "/api/auth/login", json={"email": "founder@example.com", "password": "pw"}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["access_token"] == "access-token"
    assert body["refresh_token"] == "refresh-token"

    async with session_factory() as s:
        users, workspaces, memberships = await _counts(s)
        assert (users, workspaces, memberships) == (1, 1, 1)
        membership = (await s.execute(select(MembershipRow))).scalars().one()
        assert membership.role == "owner"
        user = (await s.execute(select(UserRow))).scalars().one()
        assert user.supabase_user_id == "sb-user-1"
        assert membership.user_id == user.id


async def test_oauth_callback_bootstraps_on_first_login(
    client, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    r = await client.post("/api/auth/oauth/google/callback", json={"code": "auth-code-123"})
    assert r.status_code == 200, r.text
    assert r.json()["access_token"] == "access-token"

    async with session_factory() as s:
        assert await _counts(s) == (1, 1, 1)


async def test_bootstrap_is_idempotent_across_logins(
    client, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    for _ in range(3):
        r = await client.post(
            "/api/auth/login", json={"email": "founder@example.com", "password": "pw"}
        )
        assert r.status_code == 200, r.text

    async with session_factory() as s:
        # Exactly one of each — repeated logins do not create duplicates.
        assert await _counts(s) == (1, 1, 1)


async def test_refresh_returns_new_session(client, fake_supabase) -> None:
    r = await client.post("/api/auth/refresh", json={"refresh_token": "old-refresh"})
    assert r.status_code == 200, r.text
    assert r.json()["access_token"] == "access-token"
    assert fake_supabase.refresh_calls == ["old-refresh"]


async def test_logout_calls_supabase(client, fake_supabase) -> None:
    r = await client.post("/api/auth/logout", headers={"Authorization": "Bearer some-access-token"})
    assert r.status_code == 204, r.text
    assert fake_supabase.logout_calls == ["some-access-token"]


async def test_login_validates_payload(client) -> None:
    r = await client.post("/api/auth/login", json={"email": "x@example.com"})
    assert r.status_code == 422


async def test_existing_user_keeps_their_workspace(
    client, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """A user that already has a workspace+membership reuses them on login."""
    async with session_factory() as s:
        user = UserRow(id=uuid.uuid4(), supabase_user_id="sb-user-1", email="founder@example.com")
        ws = WorkspaceRow(id=uuid.uuid4(), name="existing", region="us-1", safe_mode=True)
        s.add(user)
        s.add(ws)
        await s.flush()
        s.add(MembershipRow(id=uuid.uuid4(), user_id=user.id, workspace_id=ws.id, role="owner"))
        await s.commit()
        existing_ws_id = ws.id

    r = await client.post(
        "/api/auth/login", json={"email": "founder@example.com", "password": "pw"}
    )
    assert r.status_code == 200, r.text

    async with session_factory() as s:
        assert await _counts(s) == (1, 1, 1)
        ws = (await s.execute(select(WorkspaceRow))).scalars().one()
        assert ws.id == existing_ws_id
        assert ws.name == "existing"
