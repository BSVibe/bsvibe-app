"""#1115 — a run whose result will never be delivered stops holding a run slot.

Prod: 73 of 226 cancellations (a third) read "abandoned review backlog cleared
before the free-plan run cap". The concurrent-run cap counts ``review_ready``
on purpose (``run_caps``), so a backlog blocks new work — but most of that
backlog was not waiting on anyone. When a run's Safe Mode item ended WITHOUT a
delivery, the run itself was left at ``review_ready`` forever:

* ``deny`` re-opens the run only for a ``rejected_approach`` WITH a reason; a
  ``queue_cleanup`` deny, or a reasonless one, left it untouched;
* ``mark_expired`` (the 90-day sweep) never touched the run at all.

형님 ruled (2026-10-06): plug the leak; the cap policy stays. When a run's last
live item ends undelivered, the run is ``cancelled``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.data.rls import workspace_session_scope
from backend.workflow.application.safe_mode_queue import DenyKind, SafeModeQueue
from backend.workflow.infrastructure.db import ExecutionRun, RunStatus
from backend.workflow.infrastructure.delivery.db import SafeModeQueueItemRow, SafeModeStatus

from .._support import db_engine


@pytest_asyncio.fixture
async def sf():
    async with db_engine() as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


async def _seed(
    sf_,
    workspace_id: uuid.UUID,
    *,
    items: int = 1,
    status: RunStatus = RunStatus.REVIEW_READY,
    payload: dict | None = None,
) -> tuple[uuid.UUID, list[uuid.UUID]]:
    run_id = uuid.uuid4()
    item_ids: list[uuid.UUID] = []
    async with sf_() as s, workspace_session_scope(s, workspace_id):
        s.add(
            ExecutionRun(
                id=run_id,
                workspace_id=workspace_id,
                status=status,
                payload=payload or {"intent_text": "weekly report"},
                created_at=datetime.now(tz=UTC),
                updated_at=datetime.now(tz=UTC),
            )
        )
        await s.flush()
        for _ in range(items):
            item_id = uuid.uuid4()
            item_ids.append(item_id)
            s.add(
                SafeModeQueueItemRow(
                    id=item_id,
                    workspace_id=workspace_id,
                    deliverable_id=uuid.uuid4(),
                    run_id=run_id,
                    status=SafeModeStatus.PENDING,
                    expires_at=datetime.now(tz=UTC) + timedelta(days=30),
                )
            )
        await s.commit()
    return run_id, item_ids


async def _status(sf_, workspace_id: uuid.UUID, run_id: uuid.UUID) -> RunStatus:
    async with sf_() as s, workspace_session_scope(s, workspace_id):
        run = await s.get(ExecutionRun, run_id)
        assert run is not None
        return run.status


async def _deny(sf_, workspace_id, item_id, *, kind: DenyKind, reason: str = "") -> None:
    async with sf_() as s, workspace_session_scope(s, workspace_id):
        assert await SafeModeQueue(s).deny(
            workspace_id=workspace_id,
            item_id=item_id,
            actor_id=uuid.uuid4(),
            reason=reason,
            kind=kind,
        )
        await s.commit()


async def test_a_cleanup_deny_of_the_last_item_releases_the_run(sf) -> None:
    ws = uuid.uuid4()
    run_id, (item,) = await _seed(sf, ws)

    await _deny(sf, ws, item, kind=DenyKind.QUEUE_CLEANUP, reason="duplicate")

    assert await _status(sf, ws, run_id) is RunStatus.CANCELLED


async def test_a_reasonless_rejection_releases_the_run(sf) -> None:
    """No reason → nothing to re-drive with, so the run is not re-opened either."""
    ws = uuid.uuid4()
    run_id, (item,) = await _seed(sf, ws)

    await _deny(sf, ws, item, kind=DenyKind.REJECTED_APPROACH, reason="")

    assert await _status(sf, ws, run_id) is RunStatus.CANCELLED


async def test_expiry_of_the_last_item_releases_the_run(sf) -> None:
    ws = uuid.uuid4()
    run_id, (item,) = await _seed(sf, ws)

    async with sf() as s, workspace_session_scope(s, ws):
        assert await SafeModeQueue(s).mark_expired(workspace_id=ws, item_id=item)
        await s.commit()

    assert await _status(sf, ws, run_id) is RunStatus.CANCELLED


async def test_a_rejection_with_a_reason_still_re_drives_the_run(sf) -> None:
    """Control — the founder's guided rejection re-opens the run (A-1c), not cancels it."""
    ws = uuid.uuid4()
    run_id, (item,) = await _seed(sf, ws)

    await _deny(sf, ws, item, kind=DenyKind.REJECTED_APPROACH, reason="use the v2 API")

    assert await _status(sf, ws, run_id) is RunStatus.OPEN


async def test_a_run_with_another_live_item_keeps_waiting(sf) -> None:
    """Control — one item gone, another still waiting on the founder."""
    ws = uuid.uuid4()
    run_id, (first, _second) = await _seed(sf, ws, items=2)

    await _deny(sf, ws, first, kind=DenyKind.QUEUE_CLEANUP, reason="duplicate")

    assert await _status(sf, ws, run_id) is RunStatus.REVIEW_READY


async def test_a_run_waiting_on_its_merge_is_left_alone(sf) -> None:
    """Control — #1109: an awaiting-merge run's work is out; its merge decides."""
    ws = uuid.uuid4()
    run_id, (item,) = await _seed(
        sf, ws, payload={"awaiting_merge": {"repo": "a/b", "pr_number": 1}}
    )

    await _deny(sf, ws, item, kind=DenyKind.QUEUE_CLEANUP, reason="stale")

    assert await _status(sf, ws, run_id) is RunStatus.REVIEW_READY


@pytest.mark.parametrize("status", [RunStatus.RUNNING, RunStatus.OPEN])
async def test_a_run_still_in_flight_is_left_alone(sf, status: RunStatus) -> None:
    """Control — only a run whose work is done (review_ready) is released."""
    ws = uuid.uuid4()
    run_id, (item,) = await _seed(sf, ws, status=status)

    await _deny(sf, ws, item, kind=DenyKind.QUEUE_CLEANUP, reason="stale")

    assert await _status(sf, ws, run_id) is status
