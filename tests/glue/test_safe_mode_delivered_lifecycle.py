"""승인된 Safe Mode 항목의 수명 — 배송이 성공해야 ``approved → delivered`` 로 넘어간다.

``approved`` 를 "아직 살아 있음"으로 읽는 판정(``_release_run_if_nothing_left``)은 승인 이력이 있는
런에서 늘 틀렸다: 배송 성공을 항목에 남기는 호출자가 없어 항목이 영원히 ``approved`` 였다.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.data.rls import workspace_session_scope
from backend.workflow.application.safe_mode_queue import DenyKind, SafeModeQueue
from backend.workflow.domain.delivery import ActionResult, DeliveryResult
from backend.workflow.infrastructure.db import (
    Deliverable,
    DeliverableType,
    ExecutionBase,
    ExecutionRun,
    RunStatus,
)
from backend.workflow.infrastructure.delivery.db import SafeModeQueueItemRow, SafeModeStatus
from backend.workflow.infrastructure.workers.delivery_worker import dispatch_delivery

from .._support import db_engine

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def sf():
    async with db_engine(ExecutionBase) as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


class _Dispatcher:
    def __init__(self, *, succeeded: bool) -> None:
        self._succeeded = succeeded

    async def dispatch(self, **kwargs: Any) -> DeliveryResult:
        return DeliveryResult(
            workspace_id=kwargs["workspace_id"],
            deliverable_id=kwargs["deliverable_id"],
            artifact_type="pr",
            actions=[
                ActionResult(
                    action="github:outbound:pr",
                    succeeded=self._succeeded,
                    error=None if self._succeeded else "boom",
                )
            ],
            delivered_at=datetime.now(tz=UTC),
            error=None if self._succeeded else "boom",
        )


async def _seed_run(
    sf_: async_sessionmaker, workspace_id: uuid.UUID, *, status: RunStatus
) -> uuid.UUID:
    run_id = uuid.uuid4()
    async with sf_() as s, workspace_session_scope(s, workspace_id):
        s.add(
            ExecutionRun(
                id=run_id,
                workspace_id=workspace_id,
                product_id=None,
                status=status,
                payload={},
                created_at=datetime.now(tz=UTC),
                updated_at=datetime.now(tz=UTC),
            )
        )
        await s.commit()
    return run_id


async def _seed_item(
    sf_: async_sessionmaker,
    workspace_id: uuid.UUID,
    run_id: uuid.UUID,
    *,
    approve: bool,
) -> tuple[uuid.UUID, uuid.UUID]:
    """A deliverable + its queue item (``approved`` or left ``pending``)."""
    deliverable_id = uuid.uuid4()
    async with sf_() as s, workspace_session_scope(s, workspace_id):
        s.add(
            Deliverable(
                id=deliverable_id,
                run_id=run_id,
                workspace_id=workspace_id,
                deliverable_type=DeliverableType.PR,
                payload={},
                created_at=datetime.now(tz=UTC),
            )
        )
        await s.flush()
        queue = SafeModeQueue(s)
        item_id = await queue.enqueue(
            workspace_id=workspace_id, deliverable_id=deliverable_id, run_id=run_id
        )
        if approve:
            assert await queue.approve(
                workspace_id=workspace_id, item_id=item_id, actor_id=uuid.uuid4()
            )
        await s.commit()
    return deliverable_id, item_id


async def _status(sf_: async_sessionmaker, workspace_id: uuid.UUID, item_id: uuid.UUID):
    async with sf_() as s, workspace_session_scope(s, workspace_id):
        return (await s.get(SafeModeQueueItemRow, item_id)).status


async def _run_status(sf_: async_sessionmaker, workspace_id: uuid.UUID, run_id: uuid.UUID):
    async with sf_() as s, workspace_session_scope(s, workspace_id):
        return (await s.get(ExecutionRun, run_id)).status


async def test_approved_item_becomes_delivered_when_delivery_succeeds(sf) -> None:
    workspace_id = uuid.uuid4()
    run_id = await _seed_run(sf, workspace_id, status=RunStatus.REVIEW_READY)
    deliverable_id, item_id = await _seed_item(sf, workspace_id, run_id, approve=True)

    async with sf() as s, workspace_session_scope(s, workspace_id):
        await dispatch_delivery(
            _Dispatcher(succeeded=True),
            workspace_id=workspace_id,
            deliverable_id=deliverable_id,
            artifact_type="pr",
            session=s,
        )

    assert await _status(sf, workspace_id, item_id) is SafeModeStatus.DELIVERED


async def test_approved_item_stays_approved_when_delivery_fails(sf) -> None:
    workspace_id = uuid.uuid4()
    run_id = await _seed_run(sf, workspace_id, status=RunStatus.REVIEW_READY)
    deliverable_id, item_id = await _seed_item(sf, workspace_id, run_id, approve=True)

    async with sf() as s, workspace_session_scope(s, workspace_id):
        await dispatch_delivery(
            _Dispatcher(succeeded=False),
            workspace_id=workspace_id,
            deliverable_id=deliverable_id,
            artifact_type="pr",
            session=s,
        )

    assert await _status(sf, workspace_id, item_id) is SafeModeStatus.APPROVED


async def test_review_ready_run_with_approved_history_is_released_when_last_item_denied(
    sf,
) -> None:
    workspace_id = uuid.uuid4()
    run_id = await _seed_run(sf, workspace_id, status=RunStatus.REVIEW_READY)
    first_deliverable, first_item = await _seed_item(sf, workspace_id, run_id, approve=True)
    async with sf() as s, workspace_session_scope(s, workspace_id):
        await dispatch_delivery(
            _Dispatcher(succeeded=True),
            workspace_id=workspace_id,
            deliverable_id=first_deliverable,
            artifact_type="pr",
            session=s,
        )
    # 첫 배송으로 런이 shipped 되지 않도록 되돌려 둔다 — 이 가드는 "마지막 항목 거절" 경로만 본다.
    async with sf() as s, workspace_session_scope(s, workspace_id):
        run = await s.get(ExecutionRun, run_id)
        run.status = RunStatus.REVIEW_READY
        await s.commit()
    _, last_item = await _seed_item(sf, workspace_id, run_id, approve=False)

    async with sf() as s, workspace_session_scope(s, workspace_id):
        assert await SafeModeQueue(s).deny(
            workspace_id=workspace_id,
            item_id=last_item,
            actor_id=uuid.uuid4(),
            reason="",
            kind=DenyKind.QUEUE_CLEANUP,
        )
        await s.commit()

    assert await _status(sf, workspace_id, first_item) is SafeModeStatus.DELIVERED
    assert await _run_status(sf, workspace_id, run_id) is RunStatus.CANCELLED


async def test_approved_but_undelivered_item_still_holds_the_run(sf) -> None:
    """배송이 아직 안 된(또는 실패한) 승인 항목은 진짜로 살아 있다 — 런을 정리하면 안 된다."""
    workspace_id = uuid.uuid4()
    run_id = await _seed_run(sf, workspace_id, status=RunStatus.REVIEW_READY)
    await _seed_item(sf, workspace_id, run_id, approve=True)
    _, last_item = await _seed_item(sf, workspace_id, run_id, approve=False)

    async with sf() as s, workspace_session_scope(s, workspace_id):
        await SafeModeQueue(s).deny(
            workspace_id=workspace_id,
            item_id=last_item,
            actor_id=uuid.uuid4(),
            reason="",
            kind=DenyKind.QUEUE_CLEANUP,
        )
        await s.commit()

    assert await _run_status(sf, workspace_id, run_id) is RunStatus.REVIEW_READY
