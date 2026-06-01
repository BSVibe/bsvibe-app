"""Unit coverage for the workspace-scoping contextvar + ORM auto-filter."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.data.scoping import (
    current_workspace_id,
    reset_current_workspace_id,
    set_current_workspace_id,
)
from backend.identity.workspaces_db import ProductRow, WorkspaceRow

pytestmark = pytest.mark.asyncio


async def _seed(session: AsyncSession) -> tuple[uuid.UUID, uuid.UUID]:
    ws_a, ws_b = uuid.uuid4(), uuid.uuid4()
    session.add(WorkspaceRow(id=ws_a, name="a", region="us-1", safe_mode=True))
    session.add(WorkspaceRow(id=ws_b, name="b", region="us-1", safe_mode=True))
    await session.flush()
    session.add(ProductRow(id=uuid.uuid4(), workspace_id=ws_a, name="A", slug="a"))
    session.add(ProductRow(id=uuid.uuid4(), workspace_id=ws_b, name="B", slug="b"))
    await session.commit()
    return ws_a, ws_b


async def test_no_contextvar_means_no_filter(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # Default contextvar is unset → the auto-filter is a no-op (existing tests
    # that never set it keep seeing all rows).
    assert current_workspace_id.get() is None
    async with session_factory() as s:
        await _seed(s)
        rows = (await s.execute(select(ProductRow))).scalars().all()
        assert len(rows) == 2


async def test_contextvar_scopes_queries(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as s:
        ws_a, _ws_b = await _seed(s)

    token = set_current_workspace_id(ws_a)
    try:
        async with session_factory() as s:
            rows = (await s.execute(select(ProductRow))).scalars().all()
            assert len(rows) == 1
            assert rows[0].workspace_id == ws_a
    finally:
        reset_current_workspace_id(token)

    assert current_workspace_id.get() is None
