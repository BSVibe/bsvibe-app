"""Tests for safe_emit + make_actor — producer-side helpers."""

from __future__ import annotations

from unittest.mock import AsyncMock

from sqlalchemy import select
from structlog.testing import capture_logs

from backend.extensions.eventbus import get_event_bus, reset_event_bus_for_testing
from bsvibe_sdk import Event
from plugin.audit.emitter import AuditEmitter
from plugin.audit.events import AuditActor, AuditEventBase
from plugin.audit.models import AuditOutboxRecord
from plugin.audit.service import make_actor, safe_emit


class _Event(AuditEventBase):
    DEFAULT_EVENT_TYPE = "test.safe_emit.fired"


def _actor() -> AuditActor:
    return AuditActor(type="user", id="user-1")


class TestMakeActor:
    def test_returns_audit_actor(self):
        a = make_actor(actor_type="service", actor_id="svc-1", label="bsvibe-gateway")
        assert isinstance(a, AuditActor)
        assert a.type == "service"
        assert a.id == "svc-1"
        assert a.label == "bsvibe-gateway"


class TestSafeEmitHappyPath:
    async def test_writes_row_via_default_emitter(self, session):
        await safe_emit(_Event(actor=_actor()), session=session)
        await session.commit()
        rows = (await session.execute(select(AuditOutboxRecord))).scalars().all()
        assert len(rows) == 1
        assert rows[0].event_type == "test.safe_emit.fired"


class TestSafeEmitSwallowsErrors:
    async def test_returns_none_when_emitter_raises(self, session, caplog):
        bad_emitter = AuditEmitter()
        # Force the underlying store.enqueue to blow up.
        bad_emitter._store.enqueue = AsyncMock(  # type: ignore[method-assign]
            side_effect=RuntimeError("disk on fire")
        )

        # The domain handler should NOT see the exception.
        await safe_emit(_Event(actor=_actor()), session=session, emitter=bad_emitter)

    async def test_no_outbox_row_inserted_when_emit_raises(self, session):
        from sqlalchemy import select

        from plugin.audit.models import AuditOutboxRecord

        bad_emitter = AuditEmitter()
        bad_emitter._store.enqueue = AsyncMock(  # type: ignore[method-assign]
            side_effect=RuntimeError("bang")
        )
        await safe_emit(_Event(actor=_actor()), session=session, emitter=bad_emitter)
        await session.commit()

        rows = (await session.execute(select(AuditOutboxRecord))).scalars().all()
        assert rows == []


class TestSafeEmitSurfacesBusOutcome:
    """The bus path (no ``emitter``) is best-effort but observable: a raising
    sink or a missing subscriber is logged at ERROR with an ``audit_delivery``
    field a metric can key on — and never propagates."""

    async def test_subscriber_raised_is_logged_and_row_still_written(self, session):
        bus = get_event_bus()  # audit subscriber already registered (conftest)

        class _Boom:
            async def on_event(self, event: Event) -> None:
                raise RuntimeError("boom")

        unsubscribe = bus.subscribe("audit.", _Boom())
        try:
            with capture_logs() as logs:
                # No exception into the caller despite the raising sink.
                await safe_emit(_Event(actor=_actor()), session=session)
            await session.commit()
        finally:
            await unsubscribe()

        assert any(
            log.get("audit_delivery") == "subscriber_raised" and log["log_level"] == "error"
            for log in logs
        )
        # The audit subscriber still persisted the row (best-effort preserved).
        rows = (await session.execute(select(AuditOutboxRecord))).scalars().all()
        assert len(rows) == 1

    async def test_no_subscriber_is_logged_and_does_not_raise(self, session):
        # Drop the autouse-registered subscriber so nothing matches.
        reset_event_bus_for_testing()
        try:
            with capture_logs() as logs:
                await safe_emit(_Event(actor=_actor()), session=session)
        finally:
            reset_event_bus_for_testing()

        assert any(
            log.get("audit_delivery") == "no_subscriber" and log["log_level"] == "error"
            for log in logs
        )
        # Nothing was written — but the caller never saw an error.
        await session.commit()
        rows = (await session.execute(select(AuditOutboxRecord))).scalars().all()
        assert rows == []
