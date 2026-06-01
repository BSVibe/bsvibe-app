"""SqlAlchemyMembershipRepository — concrete over one AsyncSession.

v8 D44/D45. One instance per request / worker tick.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.identity.db import MembershipRow


class SqlAlchemyMembershipRepository:
    """SQLAlchemy-backed :class:`MembershipRepository`."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def first_active_for_user(self, user_id: uuid.UUID) -> MembershipRow | None:
        stmt = (
            select(MembershipRow)
            .where(MembershipRow.user_id == user_id, MembershipRow.left_at.is_(None))
            .order_by(MembershipRow.joined_at.asc())
        )
        result = await self._session.execute(stmt)
        return result.scalars().first()

    async def active_for_user_in_workspace(
        self, user_id: uuid.UUID, workspace_id: uuid.UUID
    ) -> MembershipRow | None:
        stmt = select(MembershipRow).where(
            MembershipRow.user_id == user_id,
            MembershipRow.workspace_id == workspace_id,
            MembershipRow.left_at.is_(None),
        )
        result = await self._session.execute(stmt)
        return result.scalars().first()

    async def add(self, membership: MembershipRow) -> None:
        self._session.add(membership)


__all__ = ["SqlAlchemyMembershipRepository"]
