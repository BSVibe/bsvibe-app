"""SqlAlchemyUserRepository — concrete over one AsyncSession.

v8 D44/D45. One instance per request / worker tick (sharing the session
that owns the transaction boundary).
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.identity.db import UserRow


class SqlAlchemyUserRepository:
    """SQLAlchemy-backed :class:`UserRepository`.

    Constructor-injected with one :class:`AsyncSession`. The session owns
    the transaction; the repository never calls ``commit`` and never opens
    a new transaction.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, user_id: uuid.UUID) -> UserRow | None:
        return await self._session.get(UserRow, user_id)

    async def get_by_supabase_id(self, supabase_user_id: str) -> UserRow | None:
        stmt = select(UserRow).where(UserRow.supabase_user_id == supabase_user_id)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def add(self, user: UserRow) -> None:
        self._session.add(user)

    async def lock_for_update(self, user_id: uuid.UUID) -> UserRow | None:
        # ``SELECT ... FOR UPDATE`` serialises two concurrent first-logins
        # of the same Supabase subject on the user row, so they converge on
        # one workspace + one membership instead of racing.  SQLite ignores
        # ``with_for_update`` (no row-level locking), which is fine for the
        # test tier since it is single-connection.
        stmt = select(UserRow).where(UserRow.id == user_id).with_for_update()
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()


__all__ = ["SqlAlchemyUserRepository"]
