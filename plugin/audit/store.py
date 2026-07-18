"""Outbox CRUD façade — lifted from ``bsvibe_audit.outbox.store``.

The relay half (``select_undelivered`` etc.) is included so a follow-up
bundle can wire it up without re-lifting the file.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Select, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from plugin.audit.channels import AUDIT_OUTBOX
from plugin.audit.models import AuditOutboxRecord


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _backoff_delta(retry_count: int) -> timedelta:
    seconds = min(60.0, 2.0 ** max(0, retry_count - 1))
    return timedelta(seconds=seconds)


class OutboxStore:
    """CRUD over :class:`AuditOutboxRecord`. Never commits — caller decides."""

    async def enqueue(
        self,
        session: AsyncSession,
        *,
        producer_id: str,
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
        AUDIT_OUTBOX.emit(session, record, producer_id=producer_id)
        await session.flush()
        return record

    def build_select_undelivered_stmt(
        self, *, batch_size: int, now: datetime | None = None
    ) -> Select[tuple[AuditOutboxRecord]]:
        """Lift J — multi-server safe claim of un-relayed outbox rows.

        ``FOR UPDATE SKIP LOCKED`` makes the SELECT atomic w.r.t. a
        second RelayWorker on the same DB: each instance claims a
        disjoint set of rows and one row is never relayed twice. The
        lock releases when the worker's ``mark_delivered`` or
        ``record_failure`` commit closes the transaction.

        Extracted as a builder so the multi-server safety unit test
        pins the rendered SQL carries ``FOR UPDATE SKIP LOCKED``.
        """
        cutoff = now or _utcnow()
        return (
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
            .with_for_update(skip_locked=True)
        )

    async def select_undelivered(
        self,
        session: AsyncSession,
        *,
        batch_size: int,
        now: datetime | None = None,
    ) -> list[AuditOutboxRecord]:
        stmt = self.build_select_undelivered_stmt(batch_size=batch_size, now=now)
        result = await session.execute(stmt)
        return list(result.scalars().all())

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
