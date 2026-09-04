"""Auto-resolve a run's paused review Decision when its deliverable ships.

Root-caused in a live soak: a run that finishes with WEAK verification evidence
raises a ``human_review_required`` Decision (reason ``no_verification_declared``)
and PAUSES at ``RUNNING`` while its Deliverable travels the OUTPUT path
independently (Safe Mode queue → founder approve → dispatch, OR direct
delivery). Once the deliverable ships (a PR opens) the run's Decision stayed
``pending`` forever — the run sat ``RUNNING`` reviewing work that already
shipped (observed: runs shipped their PRs yet sat RUNNING for 6-18h; pending
``human_review_required`` rows piled up + poisoned the autodeploy in-flight
guard).

Founder choice B: when the Deliverable is DELIVERED, auto-resolve the run's
pending review Decision as moot and ship the run — WITHOUT re-delivering and
WITHOUT minting a duplicate Deliverable. These tests pin that behaviour at the
:func:`dispatch_delivery` seam every delivery-success path funnels through.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.config import get_settings
from backend.storage.product_workspace import run_worktree_path
from backend.workflow.application.run_delivery_resolution import (
    AUTO_RESOLVED_DELIVERABLE_SHIPPED,
    SYSTEM_AUTO_RESOLVE_ACTOR_ID,
    auto_resolve_run_on_delivery,
)
from backend.workflow.domain.delivery import ActionResult, DeliveryResult
from backend.workflow.infrastructure.db import (
    Decision,
    DecisionStatus,
    Deliverable,
    DeliverableType,
    ExecutionBase,
    ExecutionRun,
    ExecutionRunHistory,
    ProofState,
    RunStatus,
    WorkStep,
    WorkStepStatus,
)
from backend.workflow.infrastructure.workers.delivery_worker import dispatch_delivery

from .._support import db_engine

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def sf():
    async with db_engine(ExecutionBase) as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
def _isolate_run_root(tmp_path, monkeypatch):
    """Github-bound-ship tests provision a run worktree on disk — isolate it
    from the real run-workspace root (mirrors ``tests/glue/test_auto_ship_gate.py``)."""
    monkeypatch.setattr(get_settings(), "run_workspace_root", str(tmp_path / "runs"), raising=False)


async def _seed_github_binding(sf_: async_sessionmaker, workspace_id: uuid.UUID) -> None:
    """Seed an active github ``ConnectorAccountRow`` so
    ``delivers_via_local_product_repo`` resolves ``False`` for the workspace
    (mirrors ``tests/glue/test_auto_ship_gate.py``)."""
    from backend.connectors.db import ConnectorAccountRow
    from backend.router.accounts.crypto import CredentialCipher

    cipher = CredentialCipher(b"0" * 32)
    async with sf_() as s:
        s.add(
            ConnectorAccountRow(
                id=uuid.uuid4(),
                workspace_id=workspace_id,
                connector="github",
                webhook_token=uuid.uuid4().hex,
                signing_secret_ciphertext=cipher.encrypt("ghp_test_token"),
                delivery_config={"repo": "owner/name"},
                is_active=True,
            )
        )
        await s.commit()


class _RecordingDispatcher:
    """A :class:`PluginDispatchAdapter` counting calls + returning a canned result."""

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


async def _seed_paused_run(
    sf_: async_sessionmaker,
    *,
    run_status: RunStatus = RunStatus.RUNNING,
    decision_kind: str | None = "human_review_required",
    decision_status: DecisionStatus = DecisionStatus.PENDING,
    product_id: uuid.UUID | None = None,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID | None]:
    """Seed a run + WorkStep + Deliverable (+ optional Decision).

    Returns ``(workspace_id, run_id, deliverable_id, decision_id)``. PG-FK insert
    order: run first (flush), then the run-scoped children.
    """
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
                payload={},
                created_at=datetime.now(tz=UTC),
                updated_at=datetime.now(tz=UTC),
            )
        )
        await s.flush()
        s.add(
            WorkStep(
                id=uuid.uuid4(),
                run_id=run_id,
                workspace_id=workspace_id,
                title="do the thing",
                status=WorkStepStatus.RUNNING,
                proof_state=ProofState.UNTESTED,
                payload={},
                created_at=datetime.now(tz=UTC),
                updated_at=datetime.now(tz=UTC),
            )
        )
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
                    status=decision_status,
                    created_at=datetime.now(tz=UTC),
                )
            )
        await s.commit()
    return workspace_id, run_id, deliverable_id, decision_id


async def test_delivery_resolves_pending_review_decision_and_ships_run(sf) -> None:
    """The headline fix: a RUNNING run paused on human_review_required, whose
    deliverable ships → decision resolved (status + resolution + resolved_at +
    resolved_by), run → SHIPPED, WorkStep → VERIFIED/PROVED, no duplicate
    Deliverable, dispatcher called exactly once (no re-delivery)."""
    workspace_id, run_id, deliverable_id, decision_id = await _seed_paused_run(sf)
    dispatcher = _RecordingDispatcher(_success_result(workspace_id, deliverable_id))

    await dispatch_delivery(
        dispatcher,
        workspace_id=workspace_id,
        deliverable_id=deliverable_id,
        artifact_type="pr",
        session_factory=sf,
    )

    assert dispatcher.calls == 1  # NO re-delivery

    async with sf() as s:
        decision = await s.get(Decision, decision_id)
        assert decision is not None
        assert decision.status is DecisionStatus.RESOLVED
        assert decision.resolution == AUTO_RESOLVED_DELIVERABLE_SHIPPED
        assert decision.resolved_at is not None
        assert decision.resolved_by == SYSTEM_AUTO_RESOLVE_ACTOR_ID

        run = await s.get(ExecutionRun, run_id)
        assert run is not None
        assert run.status is RunStatus.SHIPPED

        work_step = (
            (await s.execute(select(WorkStep).where(WorkStep.run_id == run_id))).scalars().first()
        )
        assert work_step is not None
        assert work_step.status is WorkStepStatus.VERIFIED
        assert work_step.proof_state is ProofState.PROVED

        deliverables = (
            (await s.execute(select(Deliverable).where(Deliverable.run_id == run_id)))
            .scalars()
            .all()
        )
        assert len(deliverables) == 1  # NO duplicate minted


async def test_two_hop_transition_uses_valid_state_machine_path(sf) -> None:
    """The run reaches SHIPPED via the valid RUNNING → REVIEW_READY → SHIPPED
    hops (both history rows present) — no invented illegal transition."""
    workspace_id, run_id, deliverable_id, _ = await _seed_paused_run(sf)
    dispatcher = _RecordingDispatcher(_success_result(workspace_id, deliverable_id))

    await dispatch_delivery(
        dispatcher,
        workspace_id=workspace_id,
        deliverable_id=deliverable_id,
        artifact_type="pr",
        session_factory=sf,
    )

    async with sf() as s:
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
        assert (RunStatus.RUNNING, RunStatus.REVIEW_READY) in edges
        assert (RunStatus.REVIEW_READY, RunStatus.SHIPPED) in edges


async def test_verification_failed_kind_also_resolved(sf) -> None:
    """The ``verification_failed`` review kind is auto-resolved too."""
    workspace_id, run_id, deliverable_id, decision_id = await _seed_paused_run(
        sf, decision_kind="verification_failed"
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
        assert run is not None and run.status is RunStatus.SHIPPED


async def test_verified_run_without_pending_decision_is_untouched(sf) -> None:
    """Regression: a run with NO pending review Decision (normal verified path)
    is not touched — its status is left as-is and no decision is invented."""
    workspace_id, run_id, deliverable_id, _ = await _seed_paused_run(
        sf, run_status=RunStatus.REVIEW_READY, decision_kind=None
    )
    resolved = await _run_helper(sf, deliverable_id)
    assert resolved is False
    async with sf() as s:
        run = await s.get(ExecutionRun, run_id)
        assert run is not None
        assert run.status is RunStatus.REVIEW_READY  # unchanged
        decisions = (
            (await s.execute(select(Decision).where(Decision.run_id == run_id))).scalars().all()
        )
        assert decisions == []


def _provision_worktree(run_id: uuid.UUID) -> None:
    """A minimal on-disk worktree with a directory ``.git`` — enough for
    ``delivers_via_local_product_repo`` to proceed past its "was a workspace
    provisioned at all" filesystem check."""
    path = run_worktree_path(run_id)
    path.mkdir(parents=True)
    (path / ".git").mkdir()


async def test_github_bound_run_with_no_pending_decision_ships_on_delivery(sf) -> None:
    """The prod shape this fix closes (live run ``10e8b5f1``): github-bound
    product run, verification PASSED, zero pending Decisions, Safe Mode
    approved → dispatch succeeds (PR opened) → the run must reach a
    terminal status, not sit at REVIEW_READY forever."""
    product_id = uuid.uuid4()
    workspace_id, run_id, deliverable_id, _ = await _seed_paused_run(
        sf,
        run_status=RunStatus.REVIEW_READY,
        decision_kind=None,
        product_id=product_id,
    )
    await _seed_github_binding(sf, workspace_id)
    _provision_worktree(run_id)
    dispatcher = _RecordingDispatcher(_success_result(workspace_id, deliverable_id))

    await dispatch_delivery(
        dispatcher,
        workspace_id=workspace_id,
        deliverable_id=deliverable_id,
        artifact_type="pr",
        session_factory=sf,
    )

    assert dispatcher.calls == 1
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
        # Already at REVIEW_READY — only the final hop fires (no illegal jump).
        edges = [(h.from_status, h.to_status) for h in hops]
        assert (RunStatus.REVIEW_READY, RunStatus.SHIPPED) in edges
        assert (RunStatus.RUNNING, RunStatus.REVIEW_READY) not in edges


async def test_github_bound_failed_dispatch_does_not_ship_run(sf) -> None:
    """Negative control 1: a failed dispatch for a github-bound run with no
    pending Decision must NOT ship the run either."""
    product_id = uuid.uuid4()
    workspace_id, run_id, deliverable_id, _ = await _seed_paused_run(
        sf,
        run_status=RunStatus.REVIEW_READY,
        decision_kind=None,
        product_id=product_id,
    )
    await _seed_github_binding(sf, workspace_id)
    _provision_worktree(run_id)

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
        assert run.status is RunStatus.REVIEW_READY  # unchanged, not shipped


async def test_local_product_run_is_untouched_by_this_fix(sf) -> None:
    """Negative control 2: a local product run (no github binding — its OWN
    ``_auto_ship_product_run`` owns getting it to SHIPPED) must NOT be shipped
    by this helper, even with delivery success and zero pending Decisions."""
    product_id = uuid.uuid4()
    workspace_id, run_id, deliverable_id, _ = await _seed_paused_run(
        sf,
        run_status=RunStatus.REVIEW_READY,
        decision_kind=None,
        product_id=product_id,
    )
    _provision_worktree(run_id)  # no github binding seeded

    resolved = await _run_helper(sf, deliverable_id)

    assert resolved is False
    async with sf() as s:
        run = await s.get(ExecutionRun, run_id)
        assert run is not None
        assert run.status is RunStatus.REVIEW_READY  # untouched


async def test_github_bound_second_delivery_after_ship_is_noop(sf) -> None:
    """Idempotent: a second delivery of the same already-shipped github-bound
    run does nothing."""
    product_id = uuid.uuid4()
    workspace_id, run_id, deliverable_id, _ = await _seed_paused_run(
        sf,
        run_status=RunStatus.REVIEW_READY,
        decision_kind=None,
        product_id=product_id,
    )
    await _seed_github_binding(sf, workspace_id)
    _provision_worktree(run_id)

    first = await _run_helper(sf, deliverable_id)
    second = await _run_helper(sf, deliverable_id)

    assert first is True
    assert second is False
    async with sf() as s:
        run = await s.get(ExecutionRun, run_id)
        assert run is not None and run.status is RunStatus.SHIPPED


async def test_missing_deliverable_is_noop(sf) -> None:
    """A deliverable_id with no row (purged run / no run linkage) → no-op."""
    resolved = await _run_helper(sf, uuid.uuid4())
    assert resolved is False


async def test_already_shipped_run_is_idempotent_noop(sf) -> None:
    """Delivering again for an already-SHIPPED run is a no-op (idempotent)."""
    workspace_id, run_id, deliverable_id, _ = await _seed_paused_run(
        sf, run_status=RunStatus.SHIPPED, decision_kind=None
    )
    resolved = await _run_helper(sf, deliverable_id)
    assert resolved is False
    async with sf() as s:
        run = await s.get(ExecutionRun, run_id)
        assert run is not None and run.status is RunStatus.SHIPPED


async def test_second_delivery_after_resolve_is_noop(sf) -> None:
    """First delivery resolves + ships; a SECOND delivery of the same run does
    nothing (the decision is already resolved, the run already terminal)."""
    workspace_id, run_id, deliverable_id, decision_id = await _seed_paused_run(sf)
    dispatcher = _RecordingDispatcher(_success_result(workspace_id, deliverable_id))
    first = await _run_helper(sf, deliverable_id)
    second = await _run_helper(sf, deliverable_id)
    assert first is True
    assert second is False
    async with sf() as s:
        decision = await s.get(Decision, decision_id)
        assert decision is not None and decision.status is DecisionStatus.RESOLVED
    # keep dispatcher referenced (helper path bypasses it); silence lint
    assert dispatcher.calls == 0


async def test_failed_dispatch_does_not_ship_run(sf) -> None:
    """A wholly-failed dispatch (nothing shipped) must NOT resolve/ship the run."""
    workspace_id, run_id, deliverable_id, decision_id = await _seed_paused_run(sf)
    await dispatch_delivery(
        _RecordingDispatcher(_failed_result(workspace_id, deliverable_id)),
        workspace_id=workspace_id,
        deliverable_id=deliverable_id,
        artifact_type="pr",
        session_factory=sf,
    )
    async with sf() as s:
        decision = await s.get(Decision, decision_id)
        assert decision is not None
        assert decision.status is DecisionStatus.PENDING  # untouched
        run = await s.get(ExecutionRun, run_id)
        assert run is not None and run.status is RunStatus.RUNNING  # not shipped


async def test_caller_session_path_resolves_and_ships(sf) -> None:
    """The REST/MCP/Telegram path (caller-owned ``session=``) also resolves +
    ships within the caller's transaction."""
    workspace_id, run_id, deliverable_id, decision_id = await _seed_paused_run(sf)
    dispatcher = _RecordingDispatcher(_success_result(workspace_id, deliverable_id))
    async with sf() as s:
        await dispatch_delivery(
            dispatcher,
            workspace_id=workspace_id,
            deliverable_id=deliverable_id,
            artifact_type="pr",
            session=s,
        )
    async with sf() as s:
        decision = await s.get(Decision, decision_id)
        assert decision is not None and decision.status is DecisionStatus.RESOLVED
        run = await s.get(ExecutionRun, run_id)
        assert run is not None and run.status is RunStatus.SHIPPED


async def _run_helper(sf_: async_sessionmaker, deliverable_id: uuid.UUID) -> bool:
    """Drive the application helper directly in its own committed session."""
    async with sf_() as s:
        resolved = await auto_resolve_run_on_delivery(s, deliverable_id=deliverable_id)
        await s.commit()
    return resolved
