"""#1113 — webhook work is gated at intake, and nothing parked there starves the queue.

Since the GitHub App, every issue / PR / comment on a bound repo becomes a run —
on purpose (형님, 2026-10-06: "전부 받는다 — 단순 질의나 의미 없는 건 알아서
끝난다"). But the concurrent-run cap and the monthly token budget were only
read at the two founder-direct doors, so a webhook ran outside both: the leak
``run_caps``'s docstring predicted ("the gate belongs in intake, with a refusal
the founder can see").

형님 ruled: at the run cap, webhook work WAITS (it starts when a slot frees);
over the monthly token budget, it is REFUSED and the founder is told.

And a defect underneath both: the intake claim (``list_undrained``) took the
OLDEST 50 triggers without a Request and only THEN skipped the filter-rejected
ones in-process. A filtered trigger never gets a Request, so it stayed in that
window forever — once 50 piled up (every paused record-only PR leaves one),
intake would never see a new trigger again. A held trigger would pile up the
same way, so the claim now leaves both out in SQL.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.data.rls import workspace_session_scope
from backend.identity.workspaces_db import WorkspaceRow
from backend.notifications.db import NotificationEventRow
from backend.workflow.application.stages.intake import (
    INTAKE_HELD_KEY,
    RECEIVE_FILTERED_KEY,
)
from backend.workflow.infrastructure.db import ExecutionRun, RunStatus
from backend.workflow.infrastructure.intake.db import RequestRow, TriggerEventRow, TriggerKind
from backend.workflow.infrastructure.workers.intake_worker import (
    IntakeWorker,
    IntakeWorkerConfig,
)

from .._support import db_engine

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def sf() -> Any:
    async with db_engine() as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


async def _workspace(
    sf_, *, run_cap: int | None = 5, token_budget: int | None = None, running: int = 0
) -> uuid.UUID:
    ws = uuid.uuid4()
    async with sf_() as s, workspace_session_scope(s, ws):
        s.add(
            WorkspaceRow(
                id=ws,
                name="ws-1113",
                safe_mode=True,
                max_concurrent_runs=run_cap,
                monthly_token_budget=token_budget,
            )
        )
        await s.flush()
        for _ in range(running):
            s.add(
                ExecutionRun(id=uuid.uuid4(), workspace_id=ws, status=RunStatus.RUNNING, payload={})
            )
        await s.commit()
    return ws


async def _trigger(
    sf_,
    ws: uuid.UUID,
    *,
    kind: TriggerKind = TriggerKind.WEBHOOK,
    payload: dict[str, Any] | None = None,
    age_s: int = 0,
) -> uuid.UUID:
    trig_id = uuid.uuid4()
    async with sf_() as s, workspace_session_scope(s, ws):
        s.add(
            TriggerEventRow(
                id=trig_id,
                workspace_id=ws,
                source="github" if kind is TriggerKind.WEBHOOK else "direct",
                trigger_kind=kind,
                idempotency_key=f"k-{uuid.uuid4().hex}",
                payload=payload or {"intent_text": "fix the bug in #1"},
                received_at=datetime.now(tz=UTC) - timedelta(seconds=age_s),
            )
        )
        await s.commit()
    return trig_id


async def _requests(sf_, ws: uuid.UUID) -> list[RequestRow]:
    async with sf_() as s, workspace_session_scope(s, ws):
        return list((await s.execute(select(RequestRow))).scalars().all())


async def _payload(sf_, ws: uuid.UUID, trig_id: uuid.UUID) -> dict[str, Any]:
    async with sf_() as s, workspace_session_scope(s, ws):
        row = await s.get(TriggerEventRow, trig_id)
        assert row is not None
        return dict(row.payload or {})


async def _finish_runs(sf_, ws: uuid.UUID) -> None:
    async with sf_() as s, workspace_session_scope(s, ws):
        for run in (await s.execute(select(ExecutionRun))).scalars().all():
            run.status = RunStatus.CANCELLED
        await s.commit()


def _worker(sf_, *, batch_size: int = 50) -> IntakeWorker:
    return IntakeWorker(session_factory=sf_, config=IntakeWorkerConfig(batch_size=batch_size))


# ---------------------------------------------------------------------------
# The claim window — parked triggers must not fill it
# ---------------------------------------------------------------------------


async def test_filtered_triggers_do_not_starve_a_new_one(sf) -> None:
    ws = await _workspace(sf)
    for age in range(3):
        await _trigger(
            sf,
            ws,
            payload={RECEIVE_FILTERED_KEY: {"reason": "filter_rejected"}},
            age_s=100 + age,
        )
    await _trigger(sf, ws)

    await _worker(sf, batch_size=3).drain_once()

    assert len(await _requests(sf, ws)) == 1


async def test_held_triggers_do_not_starve_another_workspace(sf) -> None:
    full = await _workspace(sf, run_cap=1, running=1)
    for age in range(3):
        await _trigger(sf, full, age_s=100 + age)
    other = await _workspace(sf)
    await _trigger(sf, other)

    worker = _worker(sf, batch_size=3)
    await worker.drain_once()
    await worker.drain_once()

    assert len(await _requests(sf, other)) == 1
    assert await _requests(sf, full) == []


# ---------------------------------------------------------------------------
# Run cap — webhook work waits, then starts
# ---------------------------------------------------------------------------


async def test_webhook_work_at_the_run_cap_waits(sf) -> None:
    ws = await _workspace(sf, run_cap=1, running=1)
    trig = await _trigger(sf, ws)

    await _worker(sf).drain_once()

    assert await _requests(sf, ws) == []
    payload = await _payload(sf, ws, trig)
    assert INTAKE_HELD_KEY in payload
    assert RECEIVE_FILTERED_KEY not in payload  # waiting, not refused


async def test_held_webhook_work_starts_when_a_slot_frees(sf) -> None:
    ws = await _workspace(sf, run_cap=1, running=1)
    trig = await _trigger(sf, ws)
    worker = _worker(sf)
    await worker.drain_once()

    await _finish_runs(sf, ws)
    await worker.drain_once()

    (request,) = await _requests(sf, ws)
    assert request.trigger_event_id == trig


async def test_only_as_many_held_as_there_are_slots_start(sf) -> None:
    ws = await _workspace(sf, run_cap=2, running=2)
    for age in range(3):
        await _trigger(sf, ws, age_s=100 - age)
    worker = _worker(sf)
    await worker.drain_once()

    async with sf() as s, workspace_session_scope(s, ws):
        first = (await s.execute(select(ExecutionRun))).scalars().first()
        assert first is not None
        first.status = RunStatus.CANCELLED
        await s.commit()
    await worker.drain_once()

    assert len(await _requests(sf, ws)) == 1


async def test_admitted_work_not_yet_a_run_takes_its_slot(sf) -> None:
    """Two issues in one tick, one slot: an admitted Request is not a run yet, but
    it will be — counting only runs would admit both into the one slot."""
    ws = await _workspace(sf, run_cap=2, running=1)
    await _trigger(sf, ws, age_s=10)
    await _trigger(sf, ws)

    await _worker(sf).drain_once()

    assert len(await _requests(sf, ws)) == 1


async def test_direct_work_is_not_held_at_intake(sf) -> None:
    """Control — the founder's own submission is gated at its door (429), not here."""
    ws = await _workspace(sf, run_cap=1, running=1)
    await _trigger(sf, ws, kind=TriggerKind.DIRECT)

    await _worker(sf).drain_once()

    assert len(await _requests(sf, ws)) == 1


# ---------------------------------------------------------------------------
# Token budget — webhook work is refused, and the founder is told
# ---------------------------------------------------------------------------


async def test_webhook_work_over_the_token_budget_is_refused(sf) -> None:
    ws = await _workspace(sf, token_budget=0)
    trig = await _trigger(sf, ws)

    await _worker(sf).drain_once()

    assert await _requests(sf, ws) == []
    assert (await _payload(sf, ws, trig))[RECEIVE_FILTERED_KEY]["reason"] == (
        "token_budget_reached"
    )


async def test_the_founder_is_told_the_work_was_refused(sf) -> None:
    ws = await _workspace(sf, token_budget=0)
    await _trigger(sf, ws)

    await _worker(sf).drain_once()

    async with sf() as s, workspace_session_scope(s, ws):
        events = [r.event for r in (await s.execute(select(NotificationEventRow))).scalars()]
    assert events == ["needs_you"]


async def test_webhook_work_within_both_limits_starts(sf) -> None:
    """Control."""
    ws = await _workspace(sf, run_cap=2, token_budget=1_000_000, running=1)
    await _trigger(sf, ws)

    await _worker(sf).drain_once()

    assert len(await _requests(sf, ws)) == 1
