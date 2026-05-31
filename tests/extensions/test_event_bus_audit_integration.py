"""Lift R2a — end-to-end: producer publishes ``audit.emit`` on the in-process
EventBus → the audit plugin's subscriber persists an ``audit_outbox`` row
inside the producer's session (transactional outbox preserved).

Covers the rewire seam itself (NOT the producer call sites, which are
covered by their existing unit tests).
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import plugin.audit  # noqa: F401 — import triggers subscriber registration
import plugin.audit.models  # noqa: F401 — register tables on Base.metadata
from backend.extensions.eventbus import get_event_bus
from bsvibe_sdk import Event
from plugin.audit.events import AuditActor, AuditEventBase
from plugin.audit.models import AuditOutboxRecord
from plugin.audit.service import safe_emit
from plugin.audit.subscriber import AUDIT_EMIT_KIND, AUDIT_KIND_PREFIX
from tests._support import memory_session


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    async with memory_session() as s:
        yield s


class _Event(AuditEventBase):
    DEFAULT_EVENT_TYPE = "test.r2a.event_bus_audit"


def _actor() -> AuditActor:
    return AuditActor(type="user", id="user-1")


async def test_audit_subscriber_registered_on_module_import() -> None:
    bus = get_event_bus()
    assert AUDIT_KIND_PREFIX in bus.registered_prefixes()


async def test_publish_audit_emit_event_persists_outbox_row(session: AsyncSession) -> None:
    bus = get_event_bus()
    event = _Event(actor=_actor())
    await bus.publish(
        Event(
            kind=AUDIT_EMIT_KIND,
            payload={"event": event, "session": session},
        )
    )
    await session.commit()
    rows = (await session.execute(select(AuditOutboxRecord))).scalars().all()
    assert len(rows) == 1
    assert rows[0].event_type == "test.r2a.event_bus_audit"


async def test_safe_emit_routes_through_event_bus(session: AsyncSession) -> None:
    # The producer-side ``safe_emit`` call publishes onto the bus, and the
    # bus-registered subscriber writes the outbox row inside the producer
    # session — no direct emitter import on the call site.
    event = _Event(actor=_actor())
    await safe_emit(event, session=session)
    await session.commit()
    rows = (await session.execute(select(AuditOutboxRecord))).scalars().all()
    assert len(rows) == 1
    assert rows[0].event_type == "test.r2a.event_bus_audit"


async def test_subscriber_failure_does_not_propagate(session: AsyncSession) -> None:
    """A buggy subscriber registered after the audit one should not break
    the audit persistence — the bus catches all subscriber exceptions."""
    bus = get_event_bus()

    class _Boom:
        async def on_event(self, event: Event) -> None:
            raise RuntimeError("boom")

    unsubscribe = bus.subscribe(AUDIT_KIND_PREFIX, _Boom())
    try:
        await safe_emit(_Event(actor=_actor()), session=session)
        await session.commit()
    finally:
        await unsubscribe()
    # Despite _Boom raising, the audit subscriber still wrote the row.
    rows = (await session.execute(select(AuditOutboxRecord))).scalars().all()
    assert len(rows) == 1


async def test_subscriber_unsubscribe_handle_works(session: AsyncSession) -> None:
    bus = get_event_bus()

    seen: list[Event] = []

    class _Sink:
        async def on_event(self, event: Event) -> None:
            seen.append(event)

    sink = _Sink()
    unsubscribe = bus.subscribe(AUDIT_KIND_PREFIX, sink)
    await safe_emit(_Event(actor=_actor()), session=session)
    assert len(seen) == 1
    await unsubscribe()
    await safe_emit(_Event(actor=_actor()), session=session)
    assert len(seen) == 1  # unchanged after unsubscribe

    # Drop test rows so subsequent tests in the module see a clean table.
    await session.rollback()


@pytest.mark.parametrize("kind", ["audit.action.dispatched", "audit.run.terminal"])
async def test_other_audit_prefix_events_reach_subscriber(kind: str, session: AsyncSession) -> None:
    """The audit subscriber matches the WHOLE ``audit.`` prefix family —
    not just ``audit.emit`` — so future event variants can route through
    the same plugin without an extra subscription."""
    bus = get_event_bus()
    event = _Event(actor=_actor())
    await bus.publish(Event(kind=kind, payload={"event": event, "session": session}))
    await session.commit()
    rows = (await session.execute(select(AuditOutboxRecord))).scalars().all()
    assert len(rows) == 1
