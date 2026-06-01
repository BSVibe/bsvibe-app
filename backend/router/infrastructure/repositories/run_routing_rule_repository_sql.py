"""SqlAlchemyRunRoutingRuleRepository — concrete RunRoutingRuleRepository.

v8 D44/D45 — infrastructure-layer SQL adapter for the Router context's
RunRoutingRule aggregate.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.router.routing.run_routing.db import RunRoutingRuleRow


class SqlAlchemyRunRoutingRuleRepository:
    """SQLAlchemy-backed :class:`RunRoutingRuleRepository`."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_by_workspace(self, *, workspace_id: uuid.UUID) -> list[RunRoutingRuleRow]:
        stmt = (
            select(RunRoutingRuleRow)
            .where(RunRoutingRuleRow.workspace_id == workspace_id)
            .order_by(RunRoutingRuleRow.priority.asc())
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def get(self, *, workspace_id: uuid.UUID, rule_id: uuid.UUID) -> RunRoutingRuleRow | None:
        stmt = select(RunRoutingRuleRow).where(
            RunRoutingRuleRow.id == rule_id,
            RunRoutingRuleRow.workspace_id == workspace_id,
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def has_any(self, *, workspace_id: uuid.UUID) -> bool:
        stmt = (
            select(RunRoutingRuleRow.id)
            .where(RunRoutingRuleRow.workspace_id == workspace_id)
            .limit(1)
        )
        result = await self._session.execute(stmt)
        return result.first() is not None

    async def add(self, row: RunRoutingRuleRow) -> None:
        self._session.add(row)
        await self._session.flush()

    async def delete(self, row: RunRoutingRuleRow) -> None:
        await self._session.delete(row)
        await self._session.flush()


__all__ = ["SqlAlchemyRunRoutingRuleRepository"]
