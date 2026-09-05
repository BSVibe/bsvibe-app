"""#886-redo — a github-bound product run must reach SHIPPED.

Prod incident: a github-bound run finishes verified, transitions to
REVIEW_READY (``AgentRunner.transition``'s ``delivers_via_local_product_repo``
gate correctly skips the local ``merge_to_main`` for it — issue #362, it
delivers via push+PR instead), gets approved, its PR opens (delivery SUCCESS)
— and then never moves again. ``auto_resolve_run_on_delivery``'s
``if not pending_review: return False`` treated it exactly like an
already-locally-shipped run. Runs ``10e8b5f1`` / ``96edde43`` sat REVIEW_READY
for hours after their PR opened.

The fix: ``AgentRunner.transition`` records its ``delivers_via_local_product_repo``
verdict onto ``run.payload`` at the REVIEW_READY transition; this module reads
(never recomputes) that recorded answer to complete the SHIPPED hop on
delivery success. These tests pin the fixed behaviour + its negative controls.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.workflow.application.agent_runner import AgentRunner
from backend.workflow.application.run_delivery_resolution import auto_resolve_run_on_delivery
from backend.workflow.domain.delivery import ActionResult, DeliveryResult
from backend.workflow.infrastructure.db import (
    Decision,
    DecisionStatus,
    Deliverable,
    DeliverableType,
    ExecutionBase,
    ExecutionRun,
    ExecutionRunHistory,
    RunStatus,
)
from backend.workflow.infrastructure.workers.delivery_worker import dispatch_delivery

from .._support import db_engine

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def sf():
    async with db_engine(ExecutionBase) as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


class _RecordingDispatcher:
    def __init__(self, result: DeliveryResult) -> None:
        self._result = result
        self.calls = 0

    async def dispatch(self, **kwargs: Any) -> DeliveryResult:
        self.calls += 1
        return self._result


def _success_result(workspace_id: uuid.UUID, deliverable_id: uuid.UUID) -> DeliveryResult:
    return DeliveryResult(
        workspace_id=workspace_id,
        deliverable_id=deliverable_id,
        artifact_type="pr",
        actions=[
            ActionResult(
                action="github:outbound:pr",
                succeeded=True,
                output={"url": "https://github.com/acme/site/pull/9"},
            )
        ],
        delivered_at=datetime.now(tz=UTC),
    )


def _failed_result(workspace_id: uuid.UUID, deliverable_id: uuid.UUID) -> DeliveryResult:
    return DeliveryResult(
        workspace_id=workspace_id,
        deliverable_id=deliverable_id,
        artifact_type="pr",
        actions=[ActionResult(action="github:outbound:pr", succeeded=False, error="boom")],
        delivered_at=datetime.now(tz=UTC),
        error="boom",
    )


async def _seed_run(
    sf_: async_sessionmaker,
    *,
    run_status: RunStatus,
    product_id: uuid.UUID | None,
    payload: dict[str, Any],
    decision_kind: str | None = None,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID | None]:
    """Seed a run (+ Deliverable, + optional pending review Decision) directly
    (bypassing ``AgentRunner.transition``) with a caller-chosen ``payload`` —
    used by the tests that must control the recorded
    ``delivers_via_local_product_repo`` verdict directly rather than deriving
    it through a real github binding + worktree."""
    workspace_id = uuid.uuid4()
    run_id = uuid.uuid4()
    deliverable_id = uuid.uuid4()
    decision_id: uuid.UUID | None = None
    async with sf_() as s:
        s.add(
            ExecutionRun(
                id=run_id,
                workspace_id=workspace_id,
                product_id=product_id,
                status=run_status,
                payload=payload,
                created_at=datetime.now(tz=UTC),
                updated_at=datetime.now(tz=UTC),
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
                created_at=datetime.now(tz=UTC),
            )
        )
        if decision_kind is not None:
            decision_id = uuid.uuid4()
            s.add(
                Decision(
                    id=decision_id,
                    run_id=run_id,
                    workspace_id=workspace_id,
                    decision=decision_kind,
                    payload={"reason": "no_verification_declared"},
                    status=DecisionStatus.PENDING,
                    created_at=datetime.now(tz=UTC),
                )
            )
        await s.commit()
    return workspace_id, run_id, deliverable_id, decision_id


async def _run_helper(sf_: async_sessionmaker, deliverable_id: uuid.UUID) -> bool:
    async with sf_() as s:
        resolved = await auto_resolve_run_on_delivery(s, deliverable_id=deliverable_id)
        await s.commit()
    return resolved


# ---------------------------------------------------------------------------
# Prod-shape reproduction
# ---------------------------------------------------------------------------


async def test_github_bound_run_reaches_shipped_after_delivery(sf) -> None:
    """github-bound, verified (no pending Decision), Safe Mode already
    resolved, delivery SUCCEEDS → the run reaches SHIPPED. The REVIEW_READY
    transition is driven through the REAL ``AgentRunner.transition`` (via a
    directly-seeded payload verdict, since a real git/github binding is
    exercised separately by ``test_auto_ship_gate.py``), so the recorded
    payload key is the ACTUAL one ``AgentRunner`` writes — not a test
    double's guess at its shape."""
    workspace_id = uuid.uuid4()
    run_id = uuid.uuid4()
    product_id = uuid.uuid4()
    async with sf() as s:
        s.add(
            ExecutionRun(
                id=run_id,
                workspace_id=workspace_id,
                product_id=product_id,
                status=RunStatus.RUNNING,
                payload={},
                created_at=datetime.now(tz=UTC),
                updated_at=datetime.now(tz=UTC),
            )
        )
        await s.commit()

    # Drive the REAL REVIEW_READY transition. No worktree exists on disk for
    # this run, so ``delivers_via_local_product_repo`` answers False purely
    # from the "no product_id" / "no worktree" leaves of its own gate (the
    # github-binding branch is exercised directly by test_auto_ship_gate.py) —
    # what matters here is that AgentRunner records WHATEVER it computed.
    async with sf() as s:
        runner = AgentRunner(s)
        await runner.transition(run_id=run_id, to_status=RunStatus.REVIEW_READY, reason="verified")
        await s.commit()

    async with sf() as s:
        run = await s.get(ExecutionRun, run_id)
        assert run is not None
        assert run.status is RunStatus.REVIEW_READY  # not auto-shipped locally
        assert run.payload.get("delivers_via_local_product_repo") is False
        deliverable_id = uuid.uuid4()
        s.add(
            Deliverable(
                id=deliverable_id,
                run_id=run_id,
                workspace_id=workspace_id,
                deliverable_type=DeliverableType.PR,
                payload={"summary": "ship it"},
                created_at=datetime.now(tz=UTC),
            )
        )
        await s.commit()

    dispatcher = _RecordingDispatcher(_success_result(workspace_id, deliverable_id))
    await dispatch_delivery(
        dispatcher,
        workspace_id=workspace_id,
        deliverable_id=deliverable_id,
        artifact_type="pr",
        session_factory=sf,
    )

    async with sf() as s:
        run = await s.get(ExecutionRun, run_id)
        assert run is not None
        assert run.status is RunStatus.SHIPPED

        hops = (
            (
                await s.execute(
                    select(ExecutionRunHistory)
                    .where(ExecutionRunHistory.run_id == run_id)
                    .order_by(ExecutionRunHistory.created_at.asc())
                )
            )
            .scalars()
            .all()
        )
        edges = [(h.from_status, h.to_status) for h in hops]
        # The only hop THIS resolution performs is REVIEW_READY → SHIPPED —
        # RUNNING → REVIEW_READY already happened earlier via the real
        # ``AgentRunner.transition`` call above, not as part of delivery
        # resolution.
        assert (RunStatus.REVIEW_READY, RunStatus.SHIPPED) in edges
        assert edges.count((RunStatus.RUNNING, RunStatus.REVIEW_READY)) == 1


# ---------------------------------------------------------------------------
# Negative controls
# ---------------------------------------------------------------------------


async def test_failed_delivery_does_not_ship_github_bound_run(sf) -> None:
    """Negative control 1: delivery FAILS → the github-bound run stays put."""
    workspace_id, run_id, deliverable_id, _ = await _seed_run(
        sf,
        run_status=RunStatus.REVIEW_READY,
        product_id=uuid.uuid4(),
        payload={"delivers_via_local_product_repo": False},
    )
    await dispatch_delivery(
        _RecordingDispatcher(_failed_result(workspace_id, deliverable_id)),
        workspace_id=workspace_id,
        deliverable_id=deliverable_id,
        artifact_type="pr",
        session_factory=sf,
    )
    async with sf() as s:
        run = await s.get(ExecutionRun, run_id)
        assert run is not None
        assert run.status is RunStatus.REVIEW_READY


async def test_local_auto_ship_run_is_left_alone(sf) -> None:
    """Negative control 2: a run recorded as auto-shipping LOCALLY
    (``delivers_via_local_product_repo`` True) is never touched by this
    delivery-triggered path — its own auto-ship path owns it."""
    workspace_id, run_id, deliverable_id, _ = await _seed_run(
        sf,
        run_status=RunStatus.REVIEW_READY,
        product_id=uuid.uuid4(),
        payload={"delivers_via_local_product_repo": True},
    )
    resolved = await _run_helper(sf, deliverable_id)
    assert resolved is False
    async with sf() as s:
        run = await s.get(ExecutionRun, run_id)
        assert run is not None
        assert run.status is RunStatus.REVIEW_READY  # unchanged


async def test_pending_review_decision_takes_precedence(sf) -> None:
    """Negative control 3: a github-bound run that ALSO has a pending review
    Decision follows the EXISTING resolve-the-decision path unchanged — the
    new no-pending-review branch never fires for it."""
    workspace_id, run_id, deliverable_id, decision_id = await _seed_run(
        sf,
        run_status=RunStatus.RUNNING,
        product_id=uuid.uuid4(),
        payload={"delivers_via_local_product_repo": False},
        decision_kind="human_review_required",
    )
    await dispatch_delivery(
        _RecordingDispatcher(_success_result(workspace_id, deliverable_id)),
        workspace_id=workspace_id,
        deliverable_id=deliverable_id,
        artifact_type="pr",
        session_factory=sf,
    )
    async with sf() as s:
        decision = await s.get(Decision, decision_id)
        assert decision is not None
        assert decision.status is DecisionStatus.RESOLVED
        run = await s.get(ExecutionRun, run_id)
        assert run is not None
        assert run.status is RunStatus.SHIPPED


async def test_run_with_no_recorded_verdict_is_not_shipped(sf) -> None:
    """Negative control 4: a pre-fix run (REVIEW_READY, product-bound, no
    ``delivers_via_local_product_repo`` key at all) FAILS CLOSED — delivery
    success does not ship it. There is no recorded answer to trust."""
    workspace_id, run_id, deliverable_id, _ = await _seed_run(
        sf,
        run_status=RunStatus.REVIEW_READY,
        product_id=uuid.uuid4(),
        payload={},
    )
    resolved = await _run_helper(sf, deliverable_id)
    assert resolved is False
    async with sf() as s:
        run = await s.get(ExecutionRun, run_id)
        assert run is not None
        assert run.status is RunStatus.REVIEW_READY  # unchanged


async def test_non_product_run_without_pending_decision_is_untouched(sf) -> None:
    """Existing invariant preserved: a non-product run (``product_id is
    None``) is never touched by this branch, even if it happens to carry a
    recorded ``False`` verdict (every non-product run does, since the
    predicate short-circuits False on ``product_id is None``)."""
    workspace_id, run_id, deliverable_id, _ = await _seed_run(
        sf,
        run_status=RunStatus.REVIEW_READY,
        product_id=None,
        payload={"delivers_via_local_product_repo": False},
    )
    resolved = await _run_helper(sf, deliverable_id)
    assert resolved is False
    async with sf() as s:
        run = await s.get(ExecutionRun, run_id)
        assert run is not None
        assert run.status is RunStatus.REVIEW_READY  # unchanged


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


async def test_second_delivery_of_already_shipped_github_bound_run_is_noop(sf) -> None:
    workspace_id, run_id, deliverable_id, _ = await _seed_run(
        sf,
        run_status=RunStatus.REVIEW_READY,
        product_id=uuid.uuid4(),
        payload={"delivers_via_local_product_repo": False},
    )
    first = await _run_helper(sf, deliverable_id)
    second = await _run_helper(sf, deliverable_id)
    assert first is True
    assert second is False
    async with sf() as s:
        run = await s.get(ExecutionRun, run_id)
        assert run is not None
        assert run.status is RunStatus.SHIPPED
        hops = (
            (
                await s.execute(
                    select(ExecutionRunHistory).where(ExecutionRunHistory.run_id == run_id)
                )
            )
            .scalars()
            .all()
        )
        shipped_hops = [h for h in hops if h.to_status is RunStatus.SHIPPED]
        assert len(shipped_hops) == 1  # not doubled by the second call
