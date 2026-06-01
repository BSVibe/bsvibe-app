"""Lift I-Repo-Identity — SqlAlchemyMembershipRepository tests."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from backend.identity.db import MembershipRow, UserRow
from backend.identity.infrastructure.repositories import (
    SqlAlchemyMembershipRepository,
)
from backend.workspaces.db import WorkspaceRow
from tests._support import memory_session


@pytest.mark.asyncio
async def test_add_then_first_active_for_user() -> None:
    async with memory_session() as session:
        repo = SqlAlchemyMembershipRepository(session)
        user = UserRow(id=uuid.uuid4(), supabase_user_id="sub-1")
        ws = WorkspaceRow(id=uuid.uuid4(), name="alpha")
        session.add(user)
        session.add(ws)
        await session.flush()

        m = MembershipRow(
            id=uuid.uuid4(),
            user_id=user.id,
            workspace_id=ws.id,
            role="owner",
        )
        await repo.add(m)
        await session.flush()

        got = await repo.first_active_for_user(user.id)
        assert got is not None
        assert got.id == m.id


@pytest.mark.asyncio
async def test_first_active_for_user_oldest_first() -> None:
    async with memory_session() as session:
        repo = SqlAlchemyMembershipRepository(session)
        user = UserRow(id=uuid.uuid4(), supabase_user_id="sub-2")
        ws_old = WorkspaceRow(id=uuid.uuid4(), name="old")
        ws_new = WorkspaceRow(id=uuid.uuid4(), name="new")
        session.add(user)
        session.add(ws_old)
        session.add(ws_new)
        await session.flush()

        now = datetime.now(UTC)
        await repo.add(
            MembershipRow(
                id=uuid.uuid4(),
                user_id=user.id,
                workspace_id=ws_old.id,
                role="owner",
                joined_at=now - timedelta(days=2),
            )
        )
        await repo.add(
            MembershipRow(
                id=uuid.uuid4(),
                user_id=user.id,
                workspace_id=ws_new.id,
                role="owner",
                joined_at=now,
            )
        )
        await session.flush()

        got = await repo.first_active_for_user(user.id)
        assert got is not None
        assert got.workspace_id == ws_old.id


@pytest.mark.asyncio
async def test_first_active_for_user_skips_left_membership() -> None:
    async with memory_session() as session:
        repo = SqlAlchemyMembershipRepository(session)
        user = UserRow(id=uuid.uuid4(), supabase_user_id="sub-3")
        ws_left = WorkspaceRow(id=uuid.uuid4(), name="left")
        ws_active = WorkspaceRow(id=uuid.uuid4(), name="active")
        session.add(user)
        session.add(ws_left)
        session.add(ws_active)
        await session.flush()

        now = datetime.now(UTC)
        await repo.add(
            MembershipRow(
                id=uuid.uuid4(),
                user_id=user.id,
                workspace_id=ws_left.id,
                role="owner",
                joined_at=now - timedelta(days=2),
                left_at=now,
            )
        )
        await repo.add(
            MembershipRow(
                id=uuid.uuid4(),
                user_id=user.id,
                workspace_id=ws_active.id,
                role="owner",
                joined_at=now,
            )
        )
        await session.flush()

        got = await repo.first_active_for_user(user.id)
        assert got is not None
        assert got.workspace_id == ws_active.id


@pytest.mark.asyncio
async def test_first_active_for_user_empty_returns_none() -> None:
    async with memory_session() as session:
        repo = SqlAlchemyMembershipRepository(session)
        assert await repo.first_active_for_user(uuid.uuid4()) is None


@pytest.mark.asyncio
async def test_active_for_user_in_workspace_scoped() -> None:
    async with memory_session() as session:
        repo = SqlAlchemyMembershipRepository(session)
        user = UserRow(id=uuid.uuid4(), supabase_user_id="sub-4")
        ws_a = WorkspaceRow(id=uuid.uuid4(), name="a")
        ws_b = WorkspaceRow(id=uuid.uuid4(), name="b")
        session.add(user)
        session.add(ws_a)
        session.add(ws_b)
        await session.flush()

        await repo.add(
            MembershipRow(
                id=uuid.uuid4(),
                user_id=user.id,
                workspace_id=ws_a.id,
                role="owner",
            )
        )
        await session.flush()

        got_a = await repo.active_for_user_in_workspace(user.id, ws_a.id)
        assert got_a is not None
        got_b = await repo.active_for_user_in_workspace(user.id, ws_b.id)
        assert got_b is None


@pytest.mark.asyncio
async def test_active_for_user_in_workspace_excludes_left() -> None:
    async with memory_session() as session:
        repo = SqlAlchemyMembershipRepository(session)
        user = UserRow(id=uuid.uuid4(), supabase_user_id="sub-5")
        ws = WorkspaceRow(id=uuid.uuid4(), name="x")
        session.add(user)
        session.add(ws)
        await session.flush()

        await repo.add(
            MembershipRow(
                id=uuid.uuid4(),
                user_id=user.id,
                workspace_id=ws.id,
                role="owner",
                left_at=datetime.now(UTC),
            )
        )
        await session.flush()

        assert (await repo.active_for_user_in_workspace(user.id, ws.id)) is None
