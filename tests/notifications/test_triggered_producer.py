"""Producer-existence proof for the ``triggered`` outbox event (Notifier N3).

[P-triggered] is the anti-"unwired stub" gate (handoff §5): it drives the REAL
production effect chain — a durable TriggerEvent drained by the ``IntakeWorker``
into a Request — against a real DB, with NO mock of the emit, and asserts a real
:class:`NotificationEventRow(event="triggered")` lands.

The SOURCE distinction is the substance of this event: ``triggered`` fires only
for autonomous / external-origin triggers ("밖에서 뭔가 들어와서 일이 시작됨" — a
webhook or a scheduled tick), NOT for a founder-initiated DIRECT run (the founder
started it; they don't need telling). So the direct-trigger case asserts NO
``triggered`` row.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

import backend.notifications.db  # noqa: F401 — register table on the shared Base
import backend.workflow.infrastructure.db  # noqa: F401
from backend.notifications.db import NotificationEventRow
from backend.workflow.infrastructure.intake.db import (
    RequestRow,
    TriggerEventRow,
    TriggerKind,
)
from backend.workflow.infrastructure.workers.intake_worker import IntakeWorker

from .._support import shared_file_sessionmaker

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def sessionmaker() -> AsyncIterator:
    async with shared_file_sessionmaker() as sm:
        yield sm


async def _seed_trigger(
    sm: async_sessionmaker, *, kind: TriggerKind, source: str, ws: uuid.UUID
) -> None:
    async with sm() as s:
        s.add(
            TriggerEventRow(
                id=uuid.uuid4(),
                workspace_id=ws,
                source=source,
                trigger_kind=kind,
                idempotency_key=str(uuid.uuid4()),
                payload={"hello": "world"},
            )
        )
        await s.commit()


async def _triggered_rows(sm: async_sessionmaker, ws: uuid.UUID) -> list[NotificationEventRow]:
    async with sm() as s:
        return list(
            (
                await s.execute(
                    select(NotificationEventRow).where(
                        NotificationEventRow.workspace_id == ws,
                        NotificationEventRow.event == "triggered",
                    )
                )
            )
            .scalars()
            .all()
        )


async def _request_id(sm: async_sessionmaker, ws: uuid.UUID) -> uuid.UUID:
    async with sm() as s:
        return (
            await s.execute(select(RequestRow.id).where(RequestRow.workspace_id == ws))
        ).scalar_one()


async def test_webhook_trigger_emits_triggered(sessionmaker) -> None:
    """[P-triggered] an external webhook trigger, once drained, queues ``triggered``."""
    ws = uuid.uuid4()
    await _seed_trigger(sessionmaker, kind=TriggerKind.WEBHOOK, source="sentry", ws=ws)

    worker = IntakeWorker(session_factory=sessionmaker)
    assert await worker.drain_once() == 1

    request_id = await _request_id(sessionmaker, ws)
    rows = await _triggered_rows(sessionmaker, ws)
    assert len(rows) == 1, "the founder was never told an external trigger started work"
    row = rows[0]
    assert row.dedupe_key == f"triggered:{request_id}"
    assert row.payload["run_id"] is None or "run_id" not in row.payload
    assert row.payload["link"] == "/brief"
    assert "sentry" in row.payload["body"]


async def test_schedule_trigger_emits_triggered(sessionmaker) -> None:
    """A scheduled (autonomous) tick is also "work started from outside" → ``triggered``."""
    ws = uuid.uuid4()
    await _seed_trigger(sessionmaker, kind=TriggerKind.SCHEDULE, source="cron-summary", ws=ws)

    worker = IntakeWorker(session_factory=sessionmaker)
    assert await worker.drain_once() == 1

    rows = await _triggered_rows(sessionmaker, ws)
    assert len(rows) == 1


async def test_direct_trigger_does_not_emit_triggered(sessionmaker) -> None:
    """A founder-initiated DIRECT run does NOT queue ``triggered`` — they started it."""
    ws = uuid.uuid4()
    await _seed_trigger(sessionmaker, kind=TriggerKind.DIRECT, source="direct", ws=ws)

    worker = IntakeWorker(session_factory=sessionmaker)
    assert await worker.drain_once() == 1

    # The Request was still minted (the run runs) — only the notification is absent.
    request_id = await _request_id(sessionmaker, ws)
    assert request_id is not None
    rows = await _triggered_rows(sessionmaker, ws)
    assert rows == []


async def test_re_draining_does_not_double_notify(sessionmaker) -> None:
    """Re-running the drain is a no-op (the trigger is already drained) → one row."""
    ws = uuid.uuid4()
    await _seed_trigger(sessionmaker, kind=TriggerKind.WEBHOOK, source="github", ws=ws)

    worker = IntakeWorker(session_factory=sessionmaker)
    await worker.drain_once()
    await worker.drain_once()

    rows = await _triggered_rows(sessionmaker, ws)
    assert len(rows) == 1
