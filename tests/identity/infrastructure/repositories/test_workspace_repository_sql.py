"""Lift I-Repo-Identity — SqlAlchemyWorkspaceRepository tests."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from backend.identity.db import MembershipRow, UserRow
from backend.identity.infrastructure.repositories import SqlAlchemyWorkspaceRepository
from backend.identity.workspaces_db import WorkspaceRow
from tests._support import memory_session


@pytest.mark.asyncio
async def test_add_get_roundtrip() -> None:
    async with memory_session() as session:
        repo = SqlAlchemyWorkspaceRepository(session)
        ws = WorkspaceRow(id=uuid.uuid4(), name="alpha", region="us-1", safe_mode=True)
        await repo.add(ws)
        await session.flush()
        loaded = await repo.get(ws.id)
        assert loaded is not None
        assert loaded.name == "alpha"
        assert loaded.region == "us-1"
        assert loaded.safe_mode is True


@pytest.mark.asyncio
async def test_get_missing_returns_none() -> None:
    async with memory_session() as session:
        repo = SqlAlchemyWorkspaceRepository(session)
        assert await repo.get(uuid.uuid4()) is None


@pytest.mark.asyncio
async def test_get_live_skips_soft_deleted() -> None:
    async with memory_session() as session:
        repo = SqlAlchemyWorkspaceRepository(session)
        live = WorkspaceRow(id=uuid.uuid4(), name="live")
        dead = WorkspaceRow(id=uuid.uuid4(), name="dead", deleted_at=datetime.now(UTC))
        await repo.add(live)
        await repo.add(dead)
        await session.flush()

        assert (await repo.get_live(live.id)) is not None
        assert (await repo.get_live(dead.id)) is None
        assert (await repo.get_live(uuid.uuid4())) is None


@pytest.mark.asyncio
async def test_list_for_user_returns_only_active_membership_live_workspaces() -> None:
    async with memory_session() as session:
        repo = SqlAlchemyWorkspaceRepository(session)
        user = UserRow(id=uuid.uuid4(), supabase_user_id="sub-1", email="u@example.com")
        session.add(user)

        ws_member_live = WorkspaceRow(id=uuid.uuid4(), name="member-live")
        ws_member_deleted = WorkspaceRow(
            id=uuid.uuid4(), name="member-deleted", deleted_at=datetime.now(UTC)
        )
        ws_left = WorkspaceRow(id=uuid.uuid4(), name="left")
        ws_nonmember = WorkspaceRow(id=uuid.uuid4(), name="nonmember")
        for ws in (ws_member_live, ws_member_deleted, ws_left, ws_nonmember):
            await repo.add(ws)
        await session.flush()

        session.add(
            MembershipRow(
                id=uuid.uuid4(),
                user_id=user.id,
                workspace_id=ws_member_live.id,
                role="owner",
            )
        )
        session.add(
            MembershipRow(
                id=uuid.uuid4(),
                user_id=user.id,
                workspace_id=ws_member_deleted.id,
                role="owner",
            )
        )
        session.add(
            MembershipRow(
                id=uuid.uuid4(),
                user_id=user.id,
                workspace_id=ws_left.id,
                role="owner",
                left_at=datetime.now(UTC),
            )
        )
        await session.flush()

        rows = await repo.list_for_user(user.id)
        ids = {r.id for r in rows}
        assert ws_member_live.id in ids
        assert ws_member_deleted.id not in ids
        assert ws_left.id not in ids
        assert ws_nonmember.id not in ids


@pytest.mark.asyncio
async def test_list_active_regions_excludes_soft_deleted() -> None:
    async with memory_session() as session:
        repo = SqlAlchemyWorkspaceRepository(session)
        a = WorkspaceRow(id=uuid.uuid4(), name="a", region="us-1", safe_mode=True)
        b = WorkspaceRow(id=uuid.uuid4(), name="b", region="eu-1", safe_mode=False)
        gone = WorkspaceRow(
            id=uuid.uuid4(), name="gone", region="us-1", deleted_at=datetime.now(UTC)
        )
        for ws in (a, b, gone):
            await repo.add(ws)
        await session.flush()

        triples = await repo.list_active_regions()
        ids = {wid for wid, _, _ in triples}
        assert a.id in ids
        assert b.id in ids
        assert gone.id not in ids
        a_triple = next(t for t in triples if t[0] == a.id)
        assert a_triple == (a.id, "us-1", True)


@pytest.mark.asyncio
async def test_repository_does_not_commit() -> None:
    """Repository :meth:`add` must NOT commit — the caller owns the txn."""
    async with memory_session() as session:
        repo = SqlAlchemyWorkspaceRepository(session)
        ws = WorkspaceRow(id=uuid.uuid4(), name="x")
        await repo.add(ws)
        # No flush yet — the row should NOT be visible via a fresh get on
        # the same session until flush happens.
        assert session.in_transaction() is True
