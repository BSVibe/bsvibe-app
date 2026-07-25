"""DeliveryWorker + VerifierWorker drain loops against real PG."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.workflow.domain.delivery import ActionResult, DeliveryResult
from backend.workflow.infrastructure.db import (
    ExecutionBase,
    ExecutionRun,
    ProofState,
    RunStatus,
    VerificationOutcome,
    VerificationResult,
    WorkStep,
    WorkStepStatus,
)
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


class _SelectiveDispatcher:
    """Dispatcher that raises for a chosen set of deliverable_ids, succeeds otherwise.

    Lets a single drain batch mix succeeded + failed rows so the at-least-once
    DELETE-scoping contract (§ module docstring) can be exercised: a transient
    dispatch failure must LEAVE the row in place for the next tick, never delete it.
    """

    def __init__(self, fail_ids: set[uuid.UUID]) -> None:
        self.fail_ids = fail_ids
        self.calls: list[uuid.UUID] = []

    async def dispatch(self, **kwargs: Any) -> DeliveryResult:
        did = kwargs["deliverable_id"]
        self.calls.append(did)
        if did in self.fail_ids:
            raise RuntimeError("transient dispatch failure")
        return DeliveryResult(
            workspace_id=kwargs["workspace_id"],
            deliverable_id=did,
            artifact_type=kwargs["artifact_type"],
            actions=[ActionResult(action="noop", succeeded=True)],
            delivered_at=datetime.now(tz=UTC),
        )


async def _seed_delivery_events(sf, *, ws: uuid.UUID, count: int) -> list[uuid.UUID]:
    """Seed ``count`` pending delivery events, returning their event-row ids."""
    event_ids = [uuid.uuid4() for _ in range(count)]
    async with sf() as s:
        for eid in event_ids:
            s.add(
                DeliveryEventRow(
                    id=eid,
                    workspace_id=ws,
                    deliverable_id=uuid.uuid4(),
                    artifact_type="pr",
                    payload={},
                    created_at=datetime.now(tz=UTC),
                )
            )
        await s.commit()
    return event_ids


async def test_delivery_worker_partial_failure_leaves_failed_row_for_retry(sf) -> None:
    """RED — one dispatch fails in a batch: only the SUCCEEDED rows are deleted.

    Honors the module's at-least-once contract: a transiently-failed delivery
    event survives so the next tick retries it, instead of being silently
    deleted (at-most-once data loss).
    """
    ws = uuid.uuid4()
    event_ids = await _seed_delivery_events(sf, ws=ws, count=3)
    # Read back the deliverable_ids per event so we can pick one to fail.
    async with sf() as s:
        rows = (await s.execute(select(DeliveryEventRow))).scalars().all()
        by_event = {r.id: r.deliverable_id for r in rows}
    failing_event = event_ids[1]
    dispatcher = _SelectiveDispatcher(fail_ids={by_event[failing_event]})

    worker = DeliveryWorker(
        session_factory=sf,
        dispatcher=dispatcher,
        config=DeliveryWorkerConfig(batch_size=10, poll_interval_s=0.01),
    )
    processed = await worker.drain_once()

    assert processed == 2  # two succeeded, one failed
    async with sf() as s:
        remaining = (await s.execute(select(DeliveryEventRow))).scalars().all()
        remaining_ids = {r.id for r in remaining}
    # The failed row survives for retry; the two succeeded rows are deleted.
    assert remaining_ids == {failing_event}


async def test_delivery_worker_all_success_deletes_all(sf) -> None:
    """No regression — every dispatch succeeds: every row deleted, processed == len."""
    ws = uuid.uuid4()
    event_ids = await _seed_delivery_events(sf, ws=ws, count=3)
    dispatcher = _SelectiveDispatcher(fail_ids=set())

    worker = DeliveryWorker(
        session_factory=sf,
        dispatcher=dispatcher,
        config=DeliveryWorkerConfig(batch_size=10, poll_interval_s=0.01),
    )
    processed = await worker.drain_once()

    assert processed == len(event_ids)
    async with sf() as s:
        remaining = (await s.execute(select(DeliveryEventRow))).scalars().all()
        assert remaining == []


async def test_delivery_worker_all_fail_deletes_nothing(sf) -> None:
    """Every dispatch fails: NO row deleted (all survive), processed == 0."""
    ws = uuid.uuid4()
    event_ids = await _seed_delivery_events(sf, ws=ws, count=3)
    async with sf() as s:
        rows = (await s.execute(select(DeliveryEventRow))).scalars().all()
        all_deliv_ids = {r.deliverable_id for r in rows}
    dispatcher = _SelectiveDispatcher(fail_ids=all_deliv_ids)

    worker = DeliveryWorker(
        session_factory=sf,
        dispatcher=dispatcher,
        config=DeliveryWorkerConfig(batch_size=10, poll_interval_s=0.01),
    )
    processed = await worker.drain_once()

    assert processed == 0
    async with sf() as s:
        remaining = (await s.execute(select(DeliveryEventRow))).scalars().all()
        assert {r.id for r in remaining} == set(event_ids)


async def test_delivery_worker_failed_row_retries_on_next_tick(sf) -> None:
    """End-to-end retry: a row that failed one tick succeeds + is drained the next."""
    ws = uuid.uuid4()
    event_ids = await _seed_delivery_events(sf, ws=ws, count=2)
    async with sf() as s:
        rows = (await s.execute(select(DeliveryEventRow))).scalars().all()
        by_event = {r.id: r.deliverable_id for r in rows}
    failing_event = event_ids[0]
    failing_deliverable = by_event[failing_event]
    dispatcher = _SelectiveDispatcher(fail_ids={failing_deliverable})

    worker = DeliveryWorker(
        session_factory=sf,
        dispatcher=dispatcher,
        config=DeliveryWorkerConfig(batch_size=10, poll_interval_s=0.01),
    )
    # First tick: one fails and survives.
    assert await worker.drain_once() == 1
    async with sf() as s:
        remaining = (await s.execute(select(DeliveryEventRow))).scalars().all()
        assert {r.id for r in remaining} == {failing_event}

    # The transient failure clears; the previously-failed row now succeeds.
    dispatcher.fail_ids = set()
    assert await worker.drain_once() == 1
    async with sf() as s:
        remaining = (await s.execute(select(DeliveryEventRow))).scalars().all()
        assert remaining == []
    assert failing_deliverable in dispatcher.calls


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
