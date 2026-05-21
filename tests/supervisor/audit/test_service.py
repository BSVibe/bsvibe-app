"""Tests for safe_emit + make_actor — producer-side helpers."""

from __future__ import annotations

from unittest.mock import AsyncMock

from sqlalchemy import select

from backend.supervisor.audit.emitter import AuditEmitter
from backend.supervisor.audit.events import AuditActor, AuditEventBase
from backend.supervisor.audit.models import AuditOutboxRecord
from backend.supervisor.audit.service import make_actor, safe_emit


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
        # Force the underlying store.insert to blow up.
        bad_emitter._store.insert = AsyncMock(  # type: ignore[method-assign]
            side_effect=RuntimeError("disk on fire")
        )

        # The domain handler should NOT see the exception.
        await safe_emit(_Event(actor=_actor()), session=session, emitter=bad_emitter)

    async def test_no_outbox_row_inserted_when_emit_raises(self, session):
        from sqlalchemy import select

        from backend.supervisor.audit.models import AuditOutboxRecord

        bad_emitter = AuditEmitter()
        bad_emitter._store.insert = AsyncMock(  # type: ignore[method-assign]
            side_effect=RuntimeError("bang")
        )
        await safe_emit(_Event(actor=_actor()), session=session, emitter=bad_emitter)
        await session.commit()

        rows = (await session.execute(select(AuditOutboxRecord))).scalars().all()
        assert rows == []
