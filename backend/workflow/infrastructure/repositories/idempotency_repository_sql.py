"""SqlAlchemyIdempotencyRepository — concrete :class:`IdempotencyRepository`.

v8 D44/D45. Adapts the existing
:mod:`backend.workflow.infrastructure.idempotency` module-level helpers
(`is_duplicate` / `record`) onto a Repository surface so application code
depends on the Protocol, not the legacy functional API. The Schedule
context still imports the legacy helpers directly (out of scope for the
Workflow-context lift) — both surfaces share the same underlying SQL.
"""

from __future__ import annotations

import uuid

import structlog
from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.workflow.infrastructure.intake.db import RequestRow, TriggerEventRow

logger = structlog.get_logger(__name__)


class SqlAlchemyIdempotencyRepository:
    """SQLAlchemy-backed :class:`IdempotencyRepository`."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def is_duplicate(
        self,
        *,
        workspace_id: uuid.UUID,
        source: str,
        idempotency_key: str,
    ) -> bool:
        stmt = select(TriggerEventRow.id).where(
            TriggerEventRow.workspace_id == workspace_id,
            TriggerEventRow.source == source,
            TriggerEventRow.idempotency_key == idempotency_key,
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none() is not None

    async def record(self, row: TriggerEventRow) -> None:
        self._session.add(row)
        await self._session.flush()
        logger.debug(
            "idempotency_recorded",
            workspace_id=str(row.workspace_id),
            source=row.source,
            idempotency_key=row.idempotency_key,
            trigger_event_id=str(row.id),
        )

    async def list_undrained(self, *, limit: int = 50) -> list[TriggerEventRow]:
        # A row is "drained" when a RequestRow references it. The filter-rejected
        # path (RECEIVE_FILTERED_KEY marker on payload) is enforced by the
        # IntakeWorker in-process — JSON key-presence semantics drift across the
        # Postgres/SQLite test tiers so we keep the WHERE simple.
        already_drained = exists().where(RequestRow.trigger_event_id == TriggerEventRow.id)
        stmt = (
            select(TriggerEventRow)
            .where(~already_drained)
            .order_by(TriggerEventRow.received_at.asc())
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())


__all__ = ["SqlAlchemyIdempotencyRepository"]
