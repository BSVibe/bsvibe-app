"""A nested ``workspace_session_scope`` must not strip the outer scope's tenant.

Prod 2026-10-23 09:21Z: the intake worker scopes each trigger with
``workspace_session_scope`` and called a helper (``intake.pr_comment``) that opened its
OWN scope for the same workspace. The inner scope's exit cleared the GUC, so the
worker's next write (``INSERT INTO requests``) ran with no tenant and hit the RLS
policy — the trigger failed on every iteration and intake stopped for everyone.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.data import rls
from tests._support import can_reach_pg, migration_pg_url

pytestmark = pytest.mark.asyncio


async def test_inner_exit_restores_the_outer_workspace(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, Any]] = []

    async def _set(_conn: Any, workspace_id: uuid.UUID) -> None:
        calls.append(("set", workspace_id))

    async def _clear(_conn: Any) -> None:
        calls.append(("clear", None))

    monkeypatch.setattr(rls, "set_workspace_guc", _set)
    monkeypatch.setattr(rls, "clear_workspace_guc", _clear)

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with async_sessionmaker(engine)() as session:
            outer, inner = uuid.uuid4(), uuid.uuid4()
            async with rls.workspace_session_scope(session, outer):
                async with rls.workspace_session_scope(session, inner):
                    pass
                # the outer scope is still open: its tenant must be back
                assert calls[-1] == ("set", outer)
            assert calls[-1] == ("clear", None)
    finally:
        await engine.dispose()

    assert calls == [("set", outer), ("set", inner), ("set", outer), ("clear", None)]


async def test_on_postgres_the_guc_survives_a_nested_scope() -> None:
    import os

    if not os.environ.get("BSVIBE_DATABASE_URL"):
        pytest.skip("BSVIBE_DATABASE_URL not set — needs real Postgres")
    url = migration_pg_url()
    if not can_reach_pg(url):
        pytest.skip(f"Postgres not reachable at {url}")

    engine = create_async_engine(url, future=True)
    try:
        async with async_sessionmaker(engine)() as session:
            ws = uuid.uuid4()

            async def _guc(s: AsyncSession) -> str:
                row = (
                    await s.execute(
                        text("SELECT current_setting('app.current_workspace_id', true)")
                    )
                ).first()
                return row[0] if row else ""

            async with rls.workspace_session_scope(session, ws):
                async with rls.workspace_session_scope(session, ws):
                    pass
                assert await _guc(session) == str(ws)
            assert await _guc(session) in ("", None)
            await session.rollback()
    finally:
        await engine.dispose()
