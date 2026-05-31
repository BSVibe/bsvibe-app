"""DeliveryWorker + VerifierWorker drain loops against real PG."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.execution.db import (
    ExecutionBase,
    ExecutionRun,
    ProofState,
    RunStatus,
    VerificationOutcome,
    VerificationResult,
    WorkStep,
    WorkStepStatus,
)
from backend.workflow.domain.delivery import ActionResult, DeliveryResult
from backend.workflow.infrastructure.delivery.db import DeliveryBase, DeliveryEventRow
from backend.workflow.infrastructure.workers.delivery_worker import (
    DeliveryWorker,
    DeliveryWorkerConfig,
)
from backend.workflow.infrastructure.workers.verifier_worker import VerifierConfig, VerifierWorker

from .._support import db_engine

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def sf():
    async with db_engine(DeliveryBase, ExecutionBase) as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


class _FakeDispatcher:
    def __init__(self) -> None:
        self.calls: list[uuid.UUID] = []

    async def dispatch(self, **kwargs: Any) -> DeliveryResult:
        self.calls.append(kwargs["deliverable_id"])
        return DeliveryResult(
            workspace_id=kwargs["workspace_id"],
            deliverable_id=kwargs["deliverable_id"],
            artifact_type=kwargs["artifact_type"],
            actions=[ActionResult(action="noop", succeeded=True)],
            delivered_at=datetime.now(tz=UTC),
        )


async def test_delivery_worker_drains_events(sf) -> None:
    ws = uuid.uuid4()
    deliv_ids = [uuid.uuid4() for _ in range(3)]
    async with sf() as s:
        for did in deliv_ids:
            s.add(
                DeliveryEventRow(
                    id=uuid.uuid4(),
                    workspace_id=ws,
                    deliverable_id=did,
                    artifact_type="pr",
                    payload={},
                    created_at=datetime.now(tz=UTC),
                )
            )
        await s.commit()

    dispatcher = _FakeDispatcher()
    worker = DeliveryWorker(
        session_factory=sf,
        dispatcher=dispatcher,
        config=DeliveryWorkerConfig(batch_size=10, poll_interval_s=0.01),
    )
    processed = await worker.drain_once()
    assert processed == 3
    assert sorted(str(d) for d in dispatcher.calls) == sorted(str(d) for d in deliv_ids)
    # Events removed from queue after processing
    async with sf() as s:
        remaining = (await s.execute(select(DeliveryEventRow))).scalars().all()
        assert remaining == []


async def test_delivery_worker_empty_queue(sf) -> None:
    dispatcher = _FakeDispatcher()
    worker = DeliveryWorker(session_factory=sf, dispatcher=dispatcher)
    assert await worker.drain_once() == 0
    assert dispatcher.calls == []


class _FakeVerifier:
    def __init__(self, outcome: VerificationOutcome) -> None:
        self.outcome = outcome

    async def verify(self, *, work_step: WorkStep) -> tuple[VerificationOutcome, dict]:
        return self.outcome, {"checked_step": str(work_step.id)}


async def _seed_running_step(sf, *, ws: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID]:
    run_id = uuid.uuid4()
    step_id = uuid.uuid4()
    async with sf() as s:
        s.add(
            ExecutionRun(
                id=run_id,
                workspace_id=ws,
                status=RunStatus.RUNNING,
                payload={},
                created_at=datetime.now(tz=UTC),
                updated_at=datetime.now(tz=UTC),
            )
        )
        await s.flush()
        s.add(
            WorkStep(
                id=step_id,
                run_id=run_id,
                workspace_id=ws,
                title="run tests",
                status=WorkStepStatus.RUNNING,
                proof_state=ProofState.UNTESTED,
                payload={},
                created_at=datetime.now(tz=UTC),
                updated_at=datetime.now(tz=UTC),
            )
        )
        await s.commit()
    return run_id, step_id


async def test_verifier_worker_marks_verified_on_pass(sf) -> None:
    ws = uuid.uuid4()
    _, step_id = await _seed_running_step(sf, ws=ws)
    worker = VerifierWorker(
        session_factory=sf,
        verifier=_FakeVerifier(VerificationOutcome.PASSED),
        config=VerifierConfig(batch_size=10, poll_interval_s=0.01),
    )
    processed = await worker.verify_once()
    assert processed == 1
    async with sf() as s:
        step = await s.get(WorkStep, step_id)
        assert step.status is WorkStepStatus.VERIFIED
        assert step.proof_state is ProofState.PROVED
        results = (await s.execute(select(VerificationResult))).scalars().all()
        assert len(results) == 1
        assert results[0].outcome is VerificationOutcome.PASSED


async def test_verifier_worker_marks_rejected_on_fail(sf) -> None:
    ws = uuid.uuid4()
    _, step_id = await _seed_running_step(sf, ws=ws)
    worker = VerifierWorker(session_factory=sf, verifier=_FakeVerifier(VerificationOutcome.FAILED))
    await worker.verify_once()
    async with sf() as s:
        step = await s.get(WorkStep, step_id)
        assert step.status is WorkStepStatus.REJECTED
        assert step.proof_state is ProofState.REFUTED


async def test_verifier_worker_inconclusive_on_exception(sf) -> None:
    ws = uuid.uuid4()
    _, step_id = await _seed_running_step(sf, ws=ws)

    class _Boom:
        async def verify(self, **_: Any):
            raise RuntimeError("sandbox down")

    worker = VerifierWorker(session_factory=sf, verifier=_Boom())
    await worker.verify_once()
    async with sf() as s:
        results = (await s.execute(select(VerificationResult))).scalars().all()
        assert len(results) == 1
        assert results[0].outcome is VerificationOutcome.INCONCLUSIVE
        assert "sandbox down" in results[0].result["error"]


async def test_verifier_worker_empty_queue(sf) -> None:
    worker = VerifierWorker(session_factory=sf, verifier=_FakeVerifier(VerificationOutcome.PASSED))
    assert await worker.verify_once() == 0
