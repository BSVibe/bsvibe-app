"""Tests for AuditEmitter — outbox insert + trace_id propagation."""

from __future__ import annotations

import structlog
from sqlalchemy import select

from backend.extensions.implementations.audit.emitter import AuditEmitter
from backend.extensions.implementations.audit.events import AuditActor, AuditEventBase
from backend.extensions.implementations.audit.models import AuditOutboxRecord


class _Event(AuditEventBase):
    DEFAULT_EVENT_TYPE = "test.emitter.fired"


def _actor() -> AuditActor:
    return AuditActor(type="user", id="user-1")


class TestEmitInsertsOutboxRow:
    async def test_basic_emit_writes_outbox_row(self, session):
        emitter = AuditEmitter()
        event = _Event(actor=_actor(), tenant_id="ten-1")
        await emitter.emit(event, session=session)
        await session.commit()

        result = await session.execute(select(AuditOutboxRecord))
        rows = result.scalars().all()
        assert len(rows) == 1
        row = rows[0]
        assert row.event_type == "test.emitter.fired"
        assert row.event_id == str(event.event_id)
        assert row.payload["tenant_id"] == "ten-1"
        assert row.delivered_at is None
        assert row.dead_letter is False

    async def test_emit_does_not_commit(self, session):
        """Emitter only flushes; the caller's tx is the unit of atomicity."""
        emitter = AuditEmitter()
        await emitter.emit(_Event(actor=_actor()), session=session)
        # Without commit, a rollback should drop the row.
        await session.rollback()

        result = await session.execute(select(AuditOutboxRecord))
        assert result.scalars().all() == []


class TestTraceIdPropagation:
    async def test_ambient_trace_id_fills_when_missing(self, session):
        structlog.contextvars.bind_contextvars(trace_id="trace-abc")
        try:
            emitter = AuditEmitter()
            event = _Event(actor=_actor())
            await emitter.emit(event, session=session)
            await session.commit()
            result = await session.execute(select(AuditOutboxRecord))
            row = result.scalar_one()
            assert row.payload["trace_id"] == "trace-abc"
        finally:
            structlog.contextvars.unbind_contextvars("trace_id")

    async def test_explicit_trace_id_wins(self, session):
        structlog.contextvars.bind_contextvars(trace_id="ambient")
        try:
            emitter = AuditEmitter()
            event = _Event(actor=_actor(), trace_id="explicit")
            await emitter.emit(event, session=session)
            await session.commit()
            row = (await session.execute(select(AuditOutboxRecord))).scalar_one()
            assert row.payload["trace_id"] == "explicit"
        finally:
            structlog.contextvars.unbind_contextvars("trace_id")
