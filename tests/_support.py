"""Shared test helpers.

``memory_session`` is the one in-memory SQLite session factory every
unit-level conftest builds on. Since Bundle 1's Single Base unification
all module tables register on ``backend.data.Base.metadata``, so a single
``create_all`` materialises whatever models the test module has imported.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.data import Base


@asynccontextmanager
async def memory_session() -> AsyncIterator[AsyncSession]:
    """Yield an ``AsyncSession`` bound to a fresh in-memory SQLite engine.

    Creates every table currently registered on ``Base.metadata`` (i.e.
    every model module the caller has imported) and disposes the engine
    on exit.
    """
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with maker() as session:
            yield session
    finally:
        await engine.dispose()


def fake_current_user(
    supabase_user_id: str = "test-user", email: str | None = "t@example.com"
) -> Callable[[], object]:
    """Dependency override returning an authenticated authz ``User``.

    Lets API tests satisfy the v1 router-level auth dependency without a real
    JWT. Pair with an explicit ``get_workspace_id`` override (these tests scope
    via that, not membership resolution).
    """
    from backend.shared.authz.types import User

    def _user() -> User:
        return User(id=supabase_user_id, email=email)

    return _user


__all__ = ["fake_current_user", "memory_session"]
