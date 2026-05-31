"""Outbox CRUD façade — lifted from ``bsvibe_audit.outbox.store``.

The relay half (``select_undelivered`` etc.) is included so a follow-up
bundle can wire it up without re-lifting the file.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from plugin.audit.models import AuditOutboxRecord


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _backoff_delta(retry_count: int) -> timedelta:
    seconds = min(60.0, 2.0 ** max(0, retry_count - 1))
    return timedelta(seconds=seconds)


class OutboxStore:
    """CRUD over :class:`AuditOutboxRecord`. Never commits — caller decides."""

    async def insert(
        self,
        session: AsyncSession,
        *,
        event_id: str,
        event_type: str,
        occurred_at: datetime,
        payload: dict[str, Any],
    ) -> AuditOutboxRecord:
        record = AuditOutboxRecord(
            event_id=event_id,
            event_type=event_type,
            occurred_at=occurred_at,
            payload=payload,
        )
        session.add(record)
        await session.flush()
        return record

    async def select_undelivered(
        self,
        session: AsyncSession,
        *,
        batch_size: int,
        now: datetime | None = None,
    ) -> Sequence[AuditOutboxRecord]:
        cutoff = now or _utcnow()
        stmt = (
            select(AuditOutboxRecord)
            .where(
                AuditOutboxRecord.delivered_at.is_(None),
                AuditOutboxRecord.dead_letter.is_(False),
            )
            .where(
                (AuditOutboxRecord.next_attempt_at.is_(None))
                | (AuditOutboxRecord.next_attempt_at <= cutoff)
            )
            .order_by(AuditOutboxRecord.id.asc())
            .limit(batch_size)
        )
        result = await session.execute(stmt)
        return result.scalars().all()

    async def mark_delivered(
        self,
        session: AsyncSession,
        ids: Sequence[int],
        *,
        now: datetime | None = None,
    ) -> None:
        if not ids:
            return
        stmt = (
            update(AuditOutboxRecord)
            .where(AuditOutboxRecord.id.in_(list(ids)))
            .values(delivered_at=now or _utcnow(), last_error=None)
        )
        await session.execute(stmt)

    async def record_failure(
        self,
        session: AsyncSession,
        record_id: int,
        *,
        error: str,
        max_retries: int = 5,
        next_attempt_at: datetime | None = None,
        now: datetime | None = None,
    ) -> None:
        cutoff = now or _utcnow()
        record = await session.get(AuditOutboxRecord, record_id)
        if record is None:
            return
        record.retry_count += 1
        record.last_error = error
        if next_attempt_at is not None:
            record.next_attempt_at = next_attempt_at
        else:
            record.next_attempt_at = cutoff + _backoff_delta(record.retry_count)
        if record.retry_count >= max_retries:
            record.dead_letter = True
        await session.flush()
