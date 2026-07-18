"""SqlAlchemyRequestRepository — concrete :class:`RequestRepository`.

v8 D44/D45. One instance per request / worker tick, sharing the session
that owns the transaction boundary. All sqlalchemy concerns live here;
callers see only the Protocol.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.workflow.channels import REQUESTS
from backend.workflow.infrastructure.intake.db import RequestRow, RequestStatus


class SqlAlchemyRequestRepository:
    """SQLAlchemy-backed :class:`RequestRepository`."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, request_id: uuid.UUID) -> RequestRow | None:
        return await self._session.get(RequestRow, request_id)

    async def list_by_workspace(self, workspace_id: uuid.UUID) -> list[RequestRow]:
        stmt = select(RequestRow).where(RequestRow.workspace_id == workspace_id)
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def list_open_for_claim(self, *, limit: int = 50) -> list[RequestRow]:
        # ``with_for_update(skip_locked=True)`` is intentional — multiple
        # AgentWorker instances may race for the same head of the queue, and
        # SKIP LOCKED lets each worker claim a disjoint batch. SQLite ignores
        # the clause (no row-level locking), so the test tier still works.
        stmt = (
            select(RequestRow)
            .where(RequestRow.status == RequestStatus.OPEN)
            .order_by(RequestRow.created_at.asc())
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def enqueue(self, request: RequestRow, *, producer_id: str) -> None:
        REQUESTS.emit(self._session, request, producer_id=producer_id)


__all__ = ["SqlAlchemyRequestRepository"]
