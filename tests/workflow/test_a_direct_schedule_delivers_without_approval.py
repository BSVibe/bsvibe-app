"""#1072 — a schedule the founder marked ``direct`` delivers without a Safe Mode card.

Measured 2026-09-29: the founder's own weekly BStockReport schedule
(``kind=instruction``) queued its deliverable for approval EVERY week — the
workspace Safe Mode flag is the gate's first rule and it always wins. The only
choices were "turn Safe Mode off for the whole workspace" or "approve the report
every Monday"; both are wrong.

A recurring instruction the founder wrote is approved when it is written. The
schedule now carries ``output_mode`` (``safe`` default · ``direct``); a
``direct`` instruction schedule's deliverable skips the queue even with the
workspace flag on. ``product_tick`` — BSVibe deciding the work itself — still
ALWAYS queues (PT3), and cannot be made ``direct``.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

import backend.identity.workspaces_db  # noqa: F401
import backend.schedule.infrastructure.schedule_db  # noqa: F401
from backend.config import get_settings
from backend.mcp.api import McpPrincipal, ToolContext, ToolRegistry
from backend.mcp.tools import register_all_tools
from backend.schedule.application.emitter import ScheduleTrigger
from backend.schedule.application.schedule_service import (
    ScheduleService,
    ScheduleValidationError,
)
from backend.schedule.serialization import schedule_view_from_row
from backend.workflow.application.agent_runner import _delivery_gate_keys
from backend.workflow.infrastructure.intake.db import TriggerEventRow
from backend.workflow.infrastructure.workers.delivery_worker import resolve_output_mode_gate

from .._support import db_engine

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def db() -> AsyncIterator[Any]:
    get_settings.cache_clear()
    async with db_engine() as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)
    get_settings.cache_clear()


async def _create(db_, ws: uuid.UUID, **kw: Any):
    args = {"kind": "instruction", "text": "주간 리포트를 발행해줘", "cron_expr": "30 0 * * 1"}
    args.update(kw)
    async with db_() as s:
        row = await ScheduleService(s).create(workspace_id=ws, **args)
        await s.commit()
        return row


# ---------------------------------------------------------------------------
# Authoring
# ---------------------------------------------------------------------------


async def test_a_schedule_is_safe_unless_told_otherwise(db) -> None:
    row = await _create(db, uuid.uuid4())

    assert schedule_view_from_row(row).output_mode == "safe"


async def test_an_instruction_schedule_can_be_direct(db) -> None:
    row = await _create(db, uuid.uuid4(), output_mode="direct")

    assert schedule_view_from_row(row).output_mode == "direct"


async def test_a_product_tick_cannot_be_direct(db) -> None:
    """PT3 — BSVibe deciding the work itself always waits for the founder."""
    with pytest.raises(ScheduleValidationError):
        await _create(
            db, uuid.uuid4(), kind="product_tick", product_id=uuid.uuid4(), output_mode="direct"
        )


async def test_an_unknown_mode_is_refused(db) -> None:
    with pytest.raises(ScheduleValidationError):
        await _create(db, uuid.uuid4(), output_mode="yolo")


async def test_an_existing_schedule_can_be_switched(db) -> None:
    ws = uuid.uuid4()
    row = await _create(db, ws)

    async with db() as s:
        updated = await ScheduleService(s).set_output_mode(
            schedule_id=row.id, workspace_id=ws, output_mode="direct"
        )
        await s.commit()

    assert updated is not None
    assert schedule_view_from_row(updated).output_mode == "direct"
    assert schedule_view_from_row(updated).text == "주간 리포트를 발행해줘"  # text kept


async def test_the_mcp_surface_can_switch_it_too(db) -> None:
    ws = uuid.uuid4()
    row = await _create(db, ws)
    reg = ToolRegistry()
    register_all_tools(reg)

    async with db() as s:
        ctx = ToolContext(
            principal=McpPrincipal(
                user_id=uuid.uuid4(),
                workspace_id=ws,
                client_id="t",
                scopes=frozenset({"mcp:read", "mcp:write"}),
                jti=uuid.uuid4(),
            ),
            session=s,
        )
        out = await reg.call_tool(
            "bsvibe_schedules_set_output_mode",
            {"schedule_id": str(row.id), "output_mode": "direct"},
            ctx,
        )

    assert out["output_mode"] == "direct"


# ---------------------------------------------------------------------------
# Firing → run → gate
# ---------------------------------------------------------------------------


async def test_the_fired_trigger_carries_the_schedules_mode(db) -> None:
    ws = uuid.uuid4()
    row = await _create(db, ws, output_mode="direct")

    async with db() as s:
        await ScheduleTrigger(s).fire(
            workspace_id=ws,
            schedule_id=row.id,
            kind=row.kind,
            schedule_payload=row.payload,
            cron_expr=row.cron_expr,
            fired_at=datetime.now(tz=UTC),
        )
        await s.commit()
        trig = (await s.execute(select(TriggerEventRow))).scalar_one()

    assert trig.payload["schedule_output_mode"] == "direct"


def test_the_run_carries_it_to_the_delivery_gate() -> None:
    keys = _delivery_gate_keys({"kind": "instruction", "schedule_output_mode": "direct"})

    assert keys["schedule_output_mode"] == "direct"


def test_a_direct_schedule_skips_workspace_safe_mode() -> None:
    assert (
        resolve_output_mode_gate(workspace_safe_mode=True, output_mode=None, schedule_direct=True)
        is False
    )


def test_a_safe_schedule_still_queues_under_workspace_safe_mode() -> None:
    """Control."""
    assert (
        resolve_output_mode_gate(workspace_safe_mode=True, output_mode=None, schedule_direct=False)
        is True
    )


def test_an_autonomous_run_queues_even_if_marked_direct() -> None:
    """PT3 outranks it — the mark can never make BSVibe's own decision unattended."""
    assert (
        resolve_output_mode_gate(
            workspace_safe_mode=True,
            output_mode=None,
            autonomous_origin=True,
            schedule_direct=True,
        )
        is True
    )


# ---------------------------------------------------------------------------
# Through the real seams — the run is opened from the request, the worker drains
# ---------------------------------------------------------------------------


async def test_opening_the_run_keeps_the_schedules_mode(db) -> None:
    from backend.workflow.application.agent_runner import AgentRunner
    from backend.workflow.infrastructure.db import ExecutionRun
    from backend.workflow.infrastructure.intake.db import RequestRow, RequestStatus, TriggerKind

    ws = uuid.uuid4()
    async with db() as s:
        trig = TriggerEventRow(
            id=uuid.uuid4(),
            workspace_id=ws,
            source="schedule",
            trigger_kind=TriggerKind.SCHEDULE,
            idempotency_key=uuid.uuid4().hex,
            payload={},
            received_at=datetime.now(tz=UTC),
        )
        s.add(trig)
        await s.flush()
        req = RequestRow(
            id=uuid.uuid4(),
            workspace_id=ws,
            trigger_event_id=trig.id,
            status=RequestStatus.OPEN,
            payload={"kind": "instruction", "schedule_output_mode": "direct", "text": "x"},
        )
        s.add(req)
        await s.flush()
        run_id = await AgentRunner(s).open_run(request=req)
        run = await s.get(ExecutionRun, run_id)

    assert run is not None
    assert run.payload["schedule_output_mode"] == "direct"


class _Dispatcher:
    def __init__(self) -> None:
        self.dispatched: list[uuid.UUID] = []

    async def dispatch(self, *, workspace_id, deliverable_id, artifact_type, **_: Any):
        from backend.workflow.domain.delivery import ActionResult, DeliveryResult

        self.dispatched.append(deliverable_id)
        return DeliveryResult(
            workspace_id=workspace_id,
            deliverable_id=deliverable_id,
            artifact_type=artifact_type,
            actions=[ActionResult(action="telegram.send", succeeded=True)],
        )


@pytest.mark.parametrize(("mode", "queued"), [("direct", False), (None, True)])
async def test_the_delivery_worker_honours_it_under_workspace_safe_mode(
    db, mode: str | None, queued: bool
) -> None:
    from backend.identity.workspaces_db import WorkspaceRow
    from backend.workflow.infrastructure.db import Deliverable, ExecutionRun, RunStatus
    from backend.workflow.infrastructure.delivery.db import (
        DeliveryEventRow,
        SafeModeQueueItemRow,
    )
    from backend.workflow.infrastructure.workers.delivery_worker import DeliveryWorker

    ws, run_id, deliverable_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    payload: dict[str, Any] = {"kind": "instruction"}
    if mode:
        payload["schedule_output_mode"] = mode
    async with db() as s:
        s.add(WorkspaceRow(id=ws, name="ws-1072", safe_mode=True))
        await s.flush()
        s.add(
            ExecutionRun(id=run_id, workspace_id=ws, status=RunStatus.REVIEW_READY, payload=payload)
        )
        await s.flush()
        s.add(
            Deliverable(
                id=deliverable_id,
                run_id=run_id,
                workspace_id=ws,
                deliverable_type="direct_output",
                payload={"summary": "주간 리포트"},
            )
        )
        s.add(
            DeliveryEventRow(
                id=uuid.uuid4(),
                workspace_id=ws,
                run_id=run_id,
                deliverable_id=deliverable_id,
                artifact_type="direct_output",
                payload={"summary": "주간 리포트"},
            )
        )
        await s.commit()

    dispatcher = _Dispatcher()
    await DeliveryWorker(session_factory=db, dispatcher=dispatcher).drain_once()

    async with db() as s:
        items = list((await s.execute(select(SafeModeQueueItemRow))).scalars().all())
    assert (len(items) == 1) is queued
    assert (dispatcher.dispatched == [deliverable_id]) is (not queued)
