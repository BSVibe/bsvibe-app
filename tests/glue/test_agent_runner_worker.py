"""AgentRunner + AgentWorker — Request lifecycle wiring against real PG."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.workflow.application.agent_runner import AgentRunner
from backend.workflow.infrastructure.db import (
    Decision,
    ExecutionBase,
    ExecutionRun,
    ExecutionRunHistory,
    RunStatus,
)
from backend.workflow.infrastructure.intake.db import (
    IntakeBase,
    RequestRow,
    RequestStatus,
    TriggerEventRow,
    TriggerKind,
)
from backend.workflow.infrastructure.workers.agent_worker import AgentWorker, AgentWorkerConfig

from .._support import db_engine

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def session_factory():
    async with db_engine(IntakeBase, ExecutionBase) as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


async def _seed_request(sm: async_sessionmaker[AsyncSession]) -> RequestRow:
    """Insert a TriggerEvent + Request and return the Request."""
    async with sm() as s:
        ws = uuid.uuid4()
        trig = TriggerEventRow(
            id=uuid.uuid4(),
            workspace_id=ws,
            source="direct",
            trigger_kind=TriggerKind.DIRECT,
            idempotency_key=f"k-{uuid.uuid4()}",
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
            payload={"text": "hi"},
            created_at=datetime.now(tz=UTC),
            updated_at=datetime.now(tz=UTC),
        )
        s.add(req)
        await s.commit()
        return req


async def test_open_run_creates_execution_run(session_factory) -> None:
    req = await _seed_request(session_factory)
    async with session_factory() as s:
        runner = AgentRunner(s)
        run_id = await runner.open_run(request=req)
        await s.commit()

    async with session_factory() as s:
        run = await s.get(ExecutionRun, run_id)
        assert run is not None
        assert run.status is RunStatus.OPEN
        assert run.request_id == req.id
        # History row was written
        history = (
            (
                await s.execute(
                    select(ExecutionRunHistory).where(ExecutionRunHistory.run_id == run_id)
                )
            )
            .scalars()
            .all()
        )
        assert len(history) == 1
        assert history[0].to_status is RunStatus.OPEN


async def test_open_run_propagates_product_id_from_request(session_factory) -> None:
    """L-P1: AgentRunner.open_run must carry product_id from the Request to
    the new ExecutionRun. Previously it was hardcoded ``None``, which is what
    dropped product binding on every founder-direct run."""
    product_id = uuid.uuid4()
    ws = uuid.uuid4()
    async with session_factory() as s:
        trig = TriggerEventRow(
            id=uuid.uuid4(),
            workspace_id=ws,
            product_id=product_id,
            source="direct",
            trigger_kind=TriggerKind.DIRECT,
            idempotency_key=f"k-{uuid.uuid4()}",
            payload={},
            received_at=datetime.now(tz=UTC),
        )
        s.add(trig)
        await s.flush()
        req = RequestRow(
            id=uuid.uuid4(),
            workspace_id=ws,
            trigger_event_id=trig.id,
            product_id=product_id,
            status=RequestStatus.OPEN,
            payload={"text": "hi"},
            created_at=datetime.now(tz=UTC),
            updated_at=datetime.now(tz=UTC),
        )
        s.add(req)
        await s.commit()

    async with session_factory() as s:
        runner = AgentRunner(s)
        run_id = await runner.open_run(request=req)
        await s.commit()

    async with session_factory() as s:
        run = await s.get(ExecutionRun, run_id)
        assert run is not None
        assert run.product_id == product_id


async def test_open_run_propagates_tick_origin_kind_from_request(session_factory) -> None:
    """PT3: a product_tick-origin marker (``payload["kind"]``) rides from the
    Request onto the ExecutionRun payload — the same propagation seam that
    carries ``binding_id`` — so the DeliveryWorker can force Safe Mode for
    autonomous tick deliverables."""
    ws = uuid.uuid4()
    async with session_factory() as s:
        trig = TriggerEventRow(
            id=uuid.uuid4(),
            workspace_id=ws,
            source="schedule",
            trigger_kind=TriggerKind.SCHEDULE,
            idempotency_key=f"k-{uuid.uuid4()}",
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
            payload={"text": "decide + do", "kind": "product_tick"},
            created_at=datetime.now(tz=UTC),
            updated_at=datetime.now(tz=UTC),
        )
        s.add(req)
        await s.commit()

    async with session_factory() as s:
        runner = AgentRunner(s)
        run_id = await runner.open_run(request=req)
        await s.commit()

    async with session_factory() as s:
        run = await s.get(ExecutionRun, run_id)
        assert run is not None
        assert isinstance(run.payload, dict)
        assert run.payload.get("kind") == "product_tick"


async def test_open_run_is_idempotent(session_factory) -> None:
    req = await _seed_request(session_factory)
    async with session_factory() as s:
        runner = AgentRunner(s)
        first = await runner.open_run(request=req)
        await s.commit()
    async with session_factory() as s:
        runner = AgentRunner(s)
        second = await runner.open_run(request=req)
        await s.commit()
    assert first == second


async def test_transition_history(session_factory) -> None:
    req = await _seed_request(session_factory)
    async with session_factory() as s:
        runner = AgentRunner(s)
        run_id = await runner.open_run(request=req)
        await runner.transition(run_id=run_id, to_status=RunStatus.RUNNING, reason="claim")
        await runner.transition(run_id=run_id, to_status=RunStatus.SHIPPED, reason="settled")
        await s.commit()
    async with session_factory() as s:
        run = await s.get(ExecutionRun, run_id)
        assert run.status is RunStatus.SHIPPED
        history = (
            (
                await s.execute(
                    select(ExecutionRunHistory)
                    .where(ExecutionRunHistory.run_id == run_id)
                    .order_by(ExecutionRunHistory.created_at)
                )
            )
            .scalars()
            .all()
        )
        assert [h.to_status for h in history] == [
            RunStatus.OPEN,
            RunStatus.RUNNING,
            RunStatus.SHIPPED,
        ]


async def test_transition_to_review_ready_does_not_mint_a_decision(
    session_factory,
) -> None:
    """W1: the L-P2 ``ship_or_discard`` synthesis is retired. Verified runs
    will be auto-merged in W2; W1 just leaves them at REVIEW_READY with
    no founder-facing Decision attached."""
    req = await _seed_request(session_factory)
    async with session_factory() as s:
        runner = AgentRunner(s)
        run_id = await runner.open_run(request=req)
        await runner.transition(run_id=run_id, to_status=RunStatus.RUNNING, reason="claim")
        await runner.transition(run_id=run_id, to_status=RunStatus.REVIEW_READY, reason="verified")
        await s.commit()

    async with session_factory() as s:
        decisions = (
            (await s.execute(select(Decision).where(Decision.run_id == run_id))).scalars().all()
        )
        assert len(decisions) == 0


async def test_transition_returns_false_for_same_status(session_factory) -> None:
    req = await _seed_request(session_factory)
    async with session_factory() as s:
        runner = AgentRunner(s)
        run_id = await runner.open_run(request=req)
        await s.commit()
    async with session_factory() as s:
        runner = AgentRunner(s)
        # OPEN → OPEN is a no-op
        ok = await runner.transition(run_id=run_id, to_status=RunStatus.OPEN)
        assert ok is False


async def test_agent_worker_claim_once_advances_open_requests(session_factory) -> None:
    # Seed 3 OPEN requests
    requests = [await _seed_request(session_factory) for _ in range(3)]
    worker = AgentWorker(
        session_factory=session_factory,
        config=AgentWorkerConfig(batch_size=10, poll_interval_s=0.01),
    )
    claimed = await worker.claim_once()
    assert claimed == 3
    # Each Request flipped to RUNNING and has a paired ExecutionRun
    async with session_factory() as s:
        for r in requests:
            fresh = await s.get(RequestRow, r.id)
            assert fresh.status is RequestStatus.RUNNING
            runs = (
                (await s.execute(select(ExecutionRun).where(ExecutionRun.request_id == r.id)))
                .scalars()
                .all()
            )
            assert len(runs) == 1


async def test_agent_worker_idle_returns_zero(session_factory) -> None:
    worker = AgentWorker(session_factory=session_factory)
    claimed = await worker.claim_once()
    assert claimed == 0


async def test_agent_worker_batch_size_respects_limit(session_factory) -> None:
    for _ in range(5):
        await _seed_request(session_factory)
    worker = AgentWorker(
        session_factory=session_factory,
        config=AgentWorkerConfig(batch_size=2, poll_interval_s=0.01),
    )
    assert await worker.claim_once() == 2
    # Second call picks up the remaining ones
    assert await worker.claim_once() == 2
    assert await worker.claim_once() == 1
    assert await worker.claim_once() == 0


def _minimal_execution_deps() -> object:
    """A truthy AgentExecutionDeps so ``drive_once`` reaches ``_frame_and_drive``.

    ``_frame_and_drive`` is patched in the yield-back test, so the deps' own
    factories are never invoked — they just have to satisfy the constructor.
    """
    import tempfile
    from pathlib import Path

    from backend.workflow.infrastructure.workers.agent_worker import AgentExecutionDeps

    return AgentExecutionDeps(
        skill_loader_for=lambda _ws: None,  # type: ignore[arg-type,return-value]
        orchestrator_factory=lambda _s, _r: None,  # type: ignore[arg-type,return-value]
        workspace_root=Path(tempfile.mkdtemp(prefix="bsvibe-yieldback-")),
    )


async def _seed_open_run(sm: async_sessionmaker[AsyncSession]) -> ExecutionRun:
    async with sm() as s:
        run = ExecutionRun(
            id=uuid.uuid4(),
            workspace_id=uuid.uuid4(),
            status=RunStatus.OPEN,
            payload={"frame": {"skill_match": None}},  # pre-framed → skip framing
            created_at=datetime.now(tz=UTC),
            updated_at=datetime.now(tz=UTC),
        )
        s.add(run)
        await s.commit()
        return run


async def test_drive_once_yields_saturated_run_open_and_continues(session_factory) -> None:
    """A saturated run-drive executor call raises ``ExecutorCapacitySaturated``
    out of ``_frame_and_drive``. ``drive_once`` catches it at the per-run call
    site, leaves that run OPEN (NOT failed, no partial decision state), and
    continues to the next run — so the next tick re-picks the saturated one."""
    from backend.dispatch.adapter import ExecutorCapacitySaturated

    # Two OPEN runs. The FIRST (oldest) saturates; the SECOND processes.
    run1 = await _seed_open_run(session_factory)
    # Ensure a deterministic created_at ordering (drive_once orders by asc).
    import asyncio

    await asyncio.sleep(0.01)
    run2 = await _seed_open_run(session_factory)

    worker = AgentWorker(
        session_factory=session_factory,
        config=AgentWorkerConfig(batch_size=10, poll_interval_s=0.01),
        execution=_minimal_execution_deps(),
    )

    processed: list[uuid.UUID] = []

    async def _fake_frame_and_drive(session, run, execution) -> None:  # type: ignore[no-untyped-def]
        if run.id == run1.id:
            raise ExecutorCapacitySaturated("all live workers at capacity")
        processed.append(run.id)

    worker._frame_and_drive = _fake_frame_and_drive  # type: ignore[method-assign]

    driven = await worker.drive_once()

    # The saturated run did NOT abort the batch: the second run still processed.
    assert run2.id in processed
    assert run1.id not in processed
    # drive_once counted both runs (it did not crash on the saturated one).
    assert driven == 2

    # The saturated run is left OPEN — NOT failed, no decision state — so the
    # next drive_once re-picks it.
    async with session_factory() as s:
        fresh1 = await s.get(ExecutionRun, run1.id)
        assert fresh1.status is RunStatus.OPEN
        # No failure history row was written for the yielded run.
        hist = (
            (
                await s.execute(
                    select(ExecutionRunHistory).where(ExecutionRunHistory.run_id == run1.id)
                )
            )
            .scalars()
            .all()
        )
        assert all(h.to_status is not RunStatus.FAILED for h in hist)
