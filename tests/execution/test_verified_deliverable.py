"""Unit test for the shared verified-deliverable write helper.

Lift 5b extracts the verified terminal's artifact writes from
``RunOrchestrator._finish_verified`` into ONE source of truth so the native
loop and the new ExecutorOrchestrator land an identical Deliverable contract.
This test pins that contract directly (no orchestrator, no loop): given a run,
the helper writes a CODE Deliverable + a DeliveryEventRow + a settle activity.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from backend.workflow.domain.verified_deliverable import write_verified_deliverable
from backend.workflow.infrastructure.db import (
    DeliverableType,
    ExecutionRun,
    ExecutionRunActivity,
    RunStatus,
)
from backend.workflow.infrastructure.delivery.db import DeliveryEventRow

from .._support import memory_session

pytestmark = pytest.mark.asyncio


async def _seed_run(s, *, intent: str) -> ExecutionRun:
    run = ExecutionRun(
        id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        product_id=None,
        request_id=uuid.uuid4(),
        status=RunStatus.RUNNING,
        payload={"intent_text": intent},
    )
    s.add(run)
    await s.flush()
    return run


async def test_write_verified_deliverable_emits_full_contract() -> None:
    async with memory_session() as s:
        run = await _seed_run(s, intent="build it")
        attempt_id = uuid.uuid4()

        deliverable = await write_verified_deliverable(
            s,
            run,
            attempt_id=attempt_id,
            artifact_refs=["src/foo.py"],
            summary="all green",
        )

        assert deliverable.deliverable_type is DeliverableType.CODE
        assert deliverable.payload == {"artifact_refs": ["src/foo.py"], "summary": "all green"}

        event = (await s.execute(select(DeliveryEventRow))).scalar_one()
        assert event.deliverable_id == deliverable.id
        assert event.artifact_type == DeliverableType.CODE.value
        assert event.payload == {"artifact_refs": ["src/foo.py"], "summary": "all green"}

        settle = (
            (
                await s.execute(
                    select(ExecutionRunActivity).where(
                        ExecutionRunActivity.activity_type == "settle"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(settle) == 1
        assert settle[0].payload["verified"] is True
        assert settle[0].payload["artifact_refs"] == ["src/foo.py"]
        assert settle[0].payload["summary"] == "all green"
        assert settle[0].payload["intent_text"] == "build it"
        assert settle[0].payload["attempt_id"] == str(attempt_id)


async def test_write_verified_deliverable_truncates_summary_in_event() -> None:
    async with memory_session() as s:
        run = await _seed_run(s, intent="x")
        long_summary = "z" * 900
        await write_verified_deliverable(
            s,
            run,
            attempt_id=uuid.uuid4(),
            artifact_refs=[],
            summary=long_summary,
        )
        event = (await s.execute(select(DeliveryEventRow))).scalar_one()
        assert len(event.payload["summary"]) == 500
        settle = (
            (
                await s.execute(
                    select(ExecutionRunActivity).where(
                        ExecutionRunActivity.activity_type == "settle"
                    )
                )
            )
            .scalars()
            .one()
        )
        assert len(settle.payload["summary"]) == 500
