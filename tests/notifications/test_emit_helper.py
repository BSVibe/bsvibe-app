"""Shared notification-outbox emit helper — dedupe + savepoint isolation.

All four notification producers (needs_you / triggered / shipped / failed) stage
their outbox row through :func:`emit_notification`, so the dedupe-savepoint logic
lives here in ONE place. These tests pin that shared contract directly:

* [D] the UNIQUE ``dedupe_key`` makes a re-emit of the same moment a DB-level
  no-op — exactly one row survives.
* the SAVEPOINT isolates the duplicate: only the outbox insert rolls back, a
  sibling row flushed before the dup emit is intact.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import select

import backend.notifications.db  # noqa: F401 — register table on the shared Base
from backend.notifications.db import NotificationEventRow, NotificationStatus
from backend.notifications.emit import emit_notification

from .._support import memory_session

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def session() -> AsyncIterator:
    async with memory_session() as s:
        yield s


async def test_emit_notification_stages_a_pending_row(session) -> None:
    ws = uuid.uuid4()
    await emit_notification(
        session,
        workspace_id=ws,
        event="shipped",
        dedupe_key="shipped:abc",
        payload={"title": "t", "body": "b", "link": "/deliverables/abc"},
        producer_id="workflow:verified_deliverable",
    )
    await session.commit()

    row = (await session.execute(select(NotificationEventRow))).scalar_one()
    assert row.event == "shipped"
    assert row.dedupe_key == "shipped:abc"
    assert row.workspace_id == ws
    assert row.status is NotificationStatus.PENDING
    assert row.payload["link"] == "/deliverables/abc"


async def test_re_emit_same_dedupe_key_is_deduped_to_one_row(session) -> None:
    """[D] a re-emit of the same moment is a DB-level no-op via the UNIQUE key."""
    ws = uuid.uuid4()
    for _ in range(2):
        await emit_notification(
            session,
            workspace_id=ws,
            event="failed",
            dedupe_key="failed:run-1",
            payload={"title": "t", "body": "b"},
            producer_id="workflow:run_failed",
        )
    await session.commit()

    rows = (
        (
            await session.execute(
                select(NotificationEventRow).where(
                    NotificationEventRow.dedupe_key == "failed:run-1"
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1


async def test_savepoint_isolates_the_duplicate(session) -> None:
    """A sibling row flushed before the dup emit survives — only the dup rolls back."""
    ws = uuid.uuid4()
    await emit_notification(
        session,
        workspace_id=ws,
        event="triggered",
        dedupe_key="triggered:req-1",
        payload={"title": "t", "body": "b"},
        producer_id="worker:intake_worker",
    )
    # Duplicate of the same moment inside the same transaction.
    await emit_notification(
        session,
        workspace_id=ws,
        event="triggered",
        dedupe_key="triggered:req-1",
        payload={"title": "t", "body": "b"},
        producer_id="worker:intake_worker",
    )
    await session.commit()

    rows = (await session.execute(select(NotificationEventRow))).scalars().all()
    assert len(rows) == 1
