"""A failed delivery asks the founder ONCE -- however many times the worker retries it.

The DeliveryWorker re-claims a failed event up to ``max_attempts`` and calls
``dispatch_delivery`` each time; every call used to mint another ``delivery_failed`` Decision
(and founder notification). One incident, one question.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.data.rls import workspace_session_scope
from backend.data.scoping import workspace_scope
from backend.workflow.domain.delivery import ActionResult, DeliveryResult
from backend.workflow.infrastructure.db import (
    Decision,
    DecisionStatus,
    Deliverable,
    DeliverableType,
    ExecutionBase,
    ExecutionRun,
    RunStatus,
)
from backend.workflow.infrastructure.workers.delivery_worker import (
    dispatch_delivery,
    set_decision_factory,
)

from .._support import db_engine

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def sf():
    async with db_engine(ExecutionBase) as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


async def _decision_row(
    session: AsyncSession, run: ExecutionRun, work_step: object, *, kind: str, **fields: Any
) -> Decision:
    decision = Decision(
        id=uuid.uuid4(),
        run_id=run.id,
        workspace_id=run.workspace_id,
        decision=kind,
        payload=fields["payload"],
        rationale=fields["rationale"],
    )
    session.add(decision)
    await session.flush()
    return decision


@pytest.fixture(autouse=True)
def _wired_decision_factory():
    set_decision_factory(_decision_row)
    yield
    set_decision_factory(None)


class _Dispatcher:
    def __init__(self, result: DeliveryResult) -> None:
        self._result = result

    async def dispatch(self, **kwargs: Any) -> DeliveryResult:
        return self._result


async def _seed(sf_: async_sessionmaker) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    workspace_id, run_id, deliverable_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    now = datetime.now(tz=UTC)
    async with sf_() as s, workspace_session_scope(s, workspace_id):
        s.add(
            ExecutionRun(
                id=run_id,
                workspace_id=workspace_id,
                product_id=None,
                status=RunStatus.REVIEW_READY,
                payload={},
                created_at=now,
                updated_at=now,
            )
        )
        await s.flush()
        s.add(
            Deliverable(
                id=deliverable_id,
                run_id=run_id,
                workspace_id=workspace_id,
                deliverable_type=DeliverableType.PR,
                payload={"summary": "ship it"},
                created_at=now,
            )
        )
        await s.commit()
    return workspace_id, run_id, deliverable_id


def _failed(workspace_id: uuid.UUID, deliverable_id: uuid.UUID) -> DeliveryResult:
    return DeliveryResult(
        workspace_id=workspace_id,
        deliverable_id=deliverable_id,
        artifact_type="pr",
        actions=[
            ActionResult(action="github:outbound:pr", succeeded=False, error="non-fast-forward"),
            ActionResult(action="telegram:outbound:pr", succeeded=True),
        ],
        delivered_at=datetime.now(tz=UTC),
    )


async def _dispatch(sf_, workspace_id, deliverable_id, times: int) -> None:
    dispatcher = _Dispatcher(_failed(workspace_id, deliverable_id))
    for _ in range(times):
        with workspace_scope(workspace_id):
            await dispatch_delivery(
                dispatcher,
                workspace_id=workspace_id,
                deliverable_id=deliverable_id,
                artifact_type="pr",
                session_factory=sf_,
            )


async def _pending(sf_, workspace_id) -> list[Decision]:
    async with sf_() as s, workspace_session_scope(s, workspace_id):
        rows = await s.execute(
            select(Decision).where(
                Decision.decision == "delivery_failed",
                Decision.status == DecisionStatus.PENDING,
            )
        )
        return list(rows.scalars().all())


async def test_same_failure_retried_three_times_raises_one_decision(sf) -> None:
    workspace_id, _run_id, deliverable_id = await _seed(sf)
    await _dispatch(sf, workspace_id, deliverable_id, 3)
    assert len(await _pending(sf, workspace_id)) == 1


async def test_resolved_decision_does_not_block_a_new_failure(sf) -> None:
    workspace_id, _run_id, deliverable_id = await _seed(sf)
    await _dispatch(sf, workspace_id, deliverable_id, 1)
    async with sf() as s, workspace_session_scope(s, workspace_id):
        for decision in (await s.execute(select(Decision))).scalars():
            decision.status = DecisionStatus.RESOLVED
        await s.commit()
    await _dispatch(sf, workspace_id, deliverable_id, 1)
    assert len(await _pending(sf, workspace_id)) == 1
