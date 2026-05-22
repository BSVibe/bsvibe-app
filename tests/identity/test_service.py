"""Unit tests for the identity bootstrap + workspace resolution (§10.1)."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from backend.identity.db import MembershipRow, UserRow
from backend.identity.service import (
    active_membership_for_user,
    ensure_user_bootstrapped,
    get_user_by_supabase_id,
    resolve_workspace_id,
)
from backend.workspaces.db import WorkspaceRow

from .._support import memory_session

pytestmark = pytest.mark.asyncio


async def test_bootstrap_creates_user_workspace_owner_membership() -> None:
    async with memory_session() as s:
        user, membership = await ensure_user_bootstrapped(
            s, supabase_user_id="sb-1", email="founder@acme.io"
        )
        assert user.supabase_user_id == "sb-1"
        assert membership.role == "owner"
        assert membership.user_id == user.id

        ws = (await s.execute(select(WorkspaceRow))).scalars().one()
        assert ws.id == membership.workspace_id
        assert ws.name == "founder's workspace"
        assert ws.region == "us-1"


async def test_bootstrap_default_name_without_email() -> None:
    async with memory_session() as s:
        await ensure_user_bootstrapped(s, supabase_user_id="sb-1", email=None)
        ws = (await s.execute(select(WorkspaceRow))).scalars().one()
        assert ws.name == "My workspace"


async def test_bootstrap_idempotent_and_reuses_workspace() -> None:
    async with memory_session() as s:
        u1, m1 = await ensure_user_bootstrapped(s, supabase_user_id="sb-1", email="a@x.io")
        u2, m2 = await ensure_user_bootstrapped(s, supabase_user_id="sb-1", email="a@x.io")
        assert u1.id == u2.id
        assert m1.workspace_id == m2.workspace_id
        assert len((await s.execute(select(UserRow))).scalars().all()) == 1
        assert len((await s.execute(select(WorkspaceRow))).scalars().all()) == 1
        assert len((await s.execute(select(MembershipRow))).scalars().all()) == 1


async def test_bootstrap_updates_email_on_change() -> None:
    async with memory_session() as s:
        await ensure_user_bootstrapped(s, supabase_user_id="sb-1", email="old@x.io")
        await ensure_user_bootstrapped(s, supabase_user_id="sb-1", email="new@x.io")
        user = (await s.execute(select(UserRow))).scalars().one()
        assert user.email == "new@x.io"


async def test_bootstrap_custom_region() -> None:
    async with memory_session() as s:
        await ensure_user_bootstrapped(s, supabase_user_id="sb-1", email=None, region="eu-1")
        ws = (await s.execute(select(WorkspaceRow))).scalars().one()
        assert ws.region == "eu-1"


async def test_resolve_workspace_id_returns_none_for_unknown_user() -> None:
    async with memory_session() as s:
        assert await resolve_workspace_id(s, supabase_user_id="ghost") is None


async def test_resolve_workspace_id_after_bootstrap() -> None:
    async with memory_session() as s:
        _user, membership = await ensure_user_bootstrapped(
            s, supabase_user_id="sb-1", email="a@x.io"
        )
        resolved = await resolve_workspace_id(s, supabase_user_id="sb-1")
        assert resolved == membership.workspace_id


async def test_resolve_workspace_id_none_when_user_has_no_membership() -> None:
    async with memory_session() as s:
        s.add(UserRow(id=uuid.uuid4(), supabase_user_id="lonely", email=None))
        await s.commit()
        assert await resolve_workspace_id(s, supabase_user_id="lonely") is None


async def test_helpers_lookup() -> None:
    async with memory_session() as s:
        user, _m = await ensure_user_bootstrapped(s, supabase_user_id="sb-1", email="a@x.io")
        found = await get_user_by_supabase_id(s, "sb-1")
        assert found is not None and found.id == user.id
        membership = await active_membership_for_user(s, user.id)
        assert membership is not None
        # A left membership is ignored.
        membership.left_at = membership.joined_at
        await s.commit()
        assert await active_membership_for_user(s, user.id) is None
