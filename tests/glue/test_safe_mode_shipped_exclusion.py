"""Prelaunch finding A-2 — a ``shipped`` run's held item must not surface in
the founder-facing "Needs you" (pending) queue.

Normally the approve path resolves a Safe Mode item before its run ships. But
a stale/legacy ship path can leave a ``pending`` item behind a ``shipped``
run; that item would then show up on Brief as an already-shipped deliverable
still begging for approval (founder confusion + double-approval risk).
``list_pending_by_workspace`` defensively excludes items whose run is shipped,
while keeping items with no ``run_id`` (legacy single-emit).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.workflow.infrastructure.db import ExecutionRun, RunStatus
from backend.workflow.infrastructure.delivery.db import (
    SafeModeQueueItemRow,
    SafeModeStatus,
)
from backend.workflow.infrastructure.repositories.safe_mode_queue_repository_sql import (
    SqlAlchemySafeModeQueueRepository,
)

from .._support import db_engine

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def sf():
    async with db_engine() as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


def _run(workspace_id: uuid.UUID, status: RunStatus) -> ExecutionRun:
    return ExecutionRun(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        product_id=None,
        request_id=None,
        status=status,
        payload={},
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


def _item(workspace_id: uuid.UUID, run_id: uuid.UUID | None) -> SafeModeQueueItemRow:
    return SafeModeQueueItemRow(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        deliverable_id=uuid.uuid4(),
        run_id=run_id,
        status=SafeModeStatus.PENDING,
        expires_at=datetime.now(UTC),
        created_at=datetime.now(UTC),
    )


async def test_shipped_run_item_excluded_others_kept(sf):
    ws = uuid.uuid4()
    review_run = _run(ws, RunStatus.REVIEW_READY)
    shipped_run = _run(ws, RunStatus.SHIPPED)
    review_item = _item(ws, review_run.id)
    shipped_item = _item(ws, shipped_run.id)
    legacy_item = _item(ws, None)  # no run_id — must always be kept

    async with sf() as session:
        session.add_all([review_run, shipped_run, review_item, shipped_item, legacy_item])
        await session.commit()

    async with sf() as session:
        repo = SqlAlchemySafeModeQueueRepository(session)
        pending = await repo.list_pending_by_workspace(ws)

    ids = {i.id for i in pending}
    assert review_item.id in ids  # held behind a review_ready run — surfaced
    assert legacy_item.id in ids  # no run_id — always surfaced
    assert shipped_item.id not in ids  # run already shipped — excluded
