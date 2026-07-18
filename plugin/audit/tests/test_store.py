"""Tests for OutboxStore — undelivered selection, mark_delivered, backoff."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from plugin.audit.models import AuditOutboxRecord
from plugin.audit.store import OutboxStore


def _now() -> datetime:
    return datetime.now(UTC)


async def _insert(store: OutboxStore, session, *, event_id: str) -> AuditOutboxRecord:
    return await store.enqueue(
        session,
        producer_id="audit:emitter",
        event_id=event_id,
        event_type="t.x",
        occurred_at=_now(),
        payload={"x": 1},
    )


class TestSelectUndelivered:
    async def test_returns_undelivered_rows(self, session):
        store = OutboxStore()
        await _insert(store, session, event_id="e-1")
        await _insert(store, session, event_id="e-2")
        await session.commit()

        rows = await store.select_undelivered(session, batch_size=10)
        assert len(rows) == 2

    async def test_excludes_delivered(self, session):
        store = OutboxStore()
        r1 = await _insert(store, session, event_id="e-1")
        await _insert(store, session, event_id="e-2")
        await session.commit()
        await store.mark_delivered(session, [r1.id])
        await session.commit()

        rows = await store.select_undelivered(session, batch_size=10)
        assert {r.event_id for r in rows} == {"e-2"}

    async def test_excludes_dead_letter(self, session):
        store = OutboxStore()
        r1 = await _insert(store, session, event_id="e-1")
        r1.dead_letter = True
        await _insert(store, session, event_id="e-2")
        await session.commit()

        rows = await store.select_undelivered(session, batch_size=10)
        assert {r.event_id for r in rows} == {"e-2"}

    async def test_respects_backoff(self, session):
        store = OutboxStore()
        r1 = await _insert(store, session, event_id="e-1")
        r1.next_attempt_at = _now() + timedelta(hours=1)
        await _insert(store, session, event_id="e-2")
        await session.commit()

        rows = await store.select_undelivered(session, batch_size=10)
        assert {r.event_id for r in rows} == {"e-2"}


class TestMarkDelivered:
    async def test_sets_delivered_at_and_clears_last_error(self, session):
        store = OutboxStore()
        r1 = await _insert(store, session, event_id="e-1")
        r1.last_error = "old error"
        await session.commit()

        await store.mark_delivered(session, [r1.id])
        await session.commit()

        row = (
            await session.execute(select(AuditOutboxRecord).where(AuditOutboxRecord.id == r1.id))
        ).scalar_one()
        assert row.delivered_at is not None
        assert row.last_error is None

    async def test_empty_ids_is_noop(self, session):
        store = OutboxStore()
        await store.mark_delivered(session, [])
        # No exception, nothing inserted.
        rows = (await session.execute(select(AuditOutboxRecord))).scalars().all()
        assert rows == []


class TestRecordFailure:
    async def test_increments_retry_count_and_schedules_backoff(self, session):
        store = OutboxStore()
        r1 = await _insert(store, session, event_id="e-1")
        await session.commit()

        await store.record_failure(session, r1.id, error="500")
        await session.commit()

        row = (
            await session.execute(select(AuditOutboxRecord).where(AuditOutboxRecord.id == r1.id))
        ).scalar_one()
        assert row.retry_count == 1
        assert row.last_error == "500"
        assert row.next_attempt_at is not None
        assert row.dead_letter is False

    async def test_dead_letters_at_max_retries(self, session):
        store = OutboxStore()
        r1 = await _insert(store, session, event_id="e-1")
        await session.commit()

        for _ in range(5):
            await store.record_failure(session, r1.id, error="500", max_retries=5)
        await session.commit()

        row = (
            await session.execute(select(AuditOutboxRecord).where(AuditOutboxRecord.id == r1.id))
        ).scalar_one()
        assert row.dead_letter is True

    async def test_missing_record_id_is_noop(self, session):
        store = OutboxStore()
        await store.record_failure(session, record_id=99_999, error="ghost")
        # No raise.
