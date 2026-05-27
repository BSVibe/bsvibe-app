"""AgentRunner + AgentWorker — Request lifecycle wiring against real PG."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.execution.db import (
    Decision,
    DecisionStatus,
    ExecutionBase,
    ExecutionRun,
    ExecutionRunHistory,
    RunStatus,
)
from backend.intake.db import IntakeBase, RequestRow, RequestStatus, TriggerEventRow, TriggerKind
from backend.orchestrator.agent_runner import SHIP_OR_DISCARD_DECISION_KIND, AgentRunner
from backend.workers.agent_worker import AgentWorker, AgentWorkerConfig

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


async def test_transition_to_review_ready_mints_ship_or_discard_decision(
    session_factory,
) -> None:
    """L-P2: AgentRunner.transition into REVIEW_READY synthesizes a pending
    ``ship_or_discard`` Decision so the verified-but-unshipped run is
    actionable on the founder's Decisions UI (e2e-hello reality-audit fix)."""
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
        assert len(decisions) == 1
        d = decisions[0]
        assert d.decision == SHIP_OR_DISCARD_DECISION_KIND
        assert d.status is DecisionStatus.PENDING


async def test_transition_to_review_ready_is_idempotent_on_decision(
    session_factory,
) -> None:
    """A retry that re-transitions to REVIEW_READY must NOT mint a second
    Decision — the founder would see two duplicate items."""
    req = await _seed_request(session_factory)
    async with session_factory() as s:
        runner = AgentRunner(s)
        run_id = await runner.open_run(request=req)
        await runner.transition(run_id=run_id, to_status=RunStatus.RUNNING, reason="claim")
        await runner.transition(run_id=run_id, to_status=RunStatus.REVIEW_READY, reason="verified")
        # Bounce: REVIEW_READY → RUNNING → REVIEW_READY (rare but legal).
        await runner.transition(run_id=run_id, to_status=RunStatus.RUNNING, reason="resume")
        await runner.transition(
            run_id=run_id, to_status=RunStatus.REVIEW_READY, reason="verified again"
        )
        await s.commit()

    async with session_factory() as s:
        decisions = (
            (
                await s.execute(
                    select(Decision).where(
                        Decision.run_id == run_id,
                        Decision.decision == SHIP_OR_DISCARD_DECISION_KIND,
                        Decision.status == DecisionStatus.PENDING,
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(decisions) == 1


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
