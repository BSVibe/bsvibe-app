"""#1112 — "Approve & ship" on a GitHub product delivers through the PR path.

``ship`` (a ``verification_failed`` / ``human_review_required`` Decision, PWA
only) is the founder accepting work past a failed or human-gated verification.
It minted a Deliverable, force-merged the run onto the LOCAL product main and
marked the run ``shipped`` — but wrote no ``delivery_events`` row. For a
GitHub-bound product that is the whole delivery: the DeliveryWorker had nothing
to drain, no PR was ever opened, the run read ``shipped`` (breaking #1109's
"shipped only after the merge"), and the local main drifted from GitHub's.

Now, for a GitHub-bound product, ``ship`` writes what the verified path writes —
a Deliverable AND its delivery event — leaves the run at ``review_ready``, and
the delivery path takes it from there (push + PR → merge watch → ``shipped`` on
merge). No local force-merge.

형님 ruled (2026-10-06): the ship click IS the founder's approval, so the event
is marked founder-approved and the DeliveryWorker dispatches it straight out
instead of queueing a second Safe Mode approval.

A local-repo product keeps its old path (force-merge, ``shipped``): there the
local main IS the delivery.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.connectors.db import ConnectorAccountRow
from backend.data.rls import workspace_session_scope
from backend.identity.workspaces_db import ProductRow, WorkspaceRow
from backend.router.accounts.crypto import CredentialCipher
from backend.workflow.application.checkpoint_resolution import resolve_checkpoint
from backend.workflow.domain.delivery import ActionResult, DeliveryResult
from backend.workflow.infrastructure.db import (
    Decision,
    DecisionStatus,
    Deliverable,
    ExecutionRun,
    ProofState,
    RunStatus,
    WorkStep,
    WorkStepStatus,
)
from backend.workflow.infrastructure.delivery.db import (
    DeliveryEventRow,
    SafeModeQueueItemRow,
)
from backend.workflow.infrastructure.workers.delivery_worker import (
    FOUNDER_APPROVED_KEY,
    DeliveryWorker,
)

from .._support import db_engine

TEST_KEY = b"0123456789abcdef0123456789abcdef"


@pytest_asyncio.fixture
async def sf():
    async with db_engine() as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
def storage(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record the product-workspace git operations ``ship`` performs."""
    from backend.storage import product_workspace as pw

    calls: list[str] = []

    async def _commit(product_id, run_id, *, message: str) -> None:
        calls.append("commit_worktree")

    async def _force(product_id, run_id) -> None:
        calls.append("force_merge_theirs")

    async def _publish(product_id) -> None:
        calls.append("publish_product_bundle")

    async def _remove(product_id, run_id) -> None:
        calls.append("remove_run_worktree")

    @asynccontextmanager
    async def _lock(session, product_id):
        yield

    monkeypatch.setattr(pw, "commit_worktree", _commit)
    monkeypatch.setattr(pw, "force_merge_theirs", _force)
    monkeypatch.setattr(pw, "publish_product_bundle", _publish)
    monkeypatch.setattr(pw, "remove_run_worktree", _remove)
    monkeypatch.setattr(pw, "product_workspace_lock", _lock)
    return calls


async def _seed(
    sf_: async_sessionmaker[AsyncSession], *, github: bool, safe_mode: bool = True
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    ws, product_id, run_id, decision_id = (uuid.uuid4() for _ in range(4))
    async with sf_() as s, workspace_session_scope(s, ws):
        s.add(WorkspaceRow(id=ws, name="ws-1112", safe_mode=safe_mode))
        await s.flush()
        s.add(
            ProductRow(
                id=product_id,
                workspace_id=ws,
                name="p",
                slug="p",
                repo_url="https://github.com/acme/app" if github else None,
            )
        )
        if github:
            s.add(
                ConnectorAccountRow(
                    id=uuid.uuid4(),
                    workspace_id=ws,
                    connector="github",
                    webhook_token=uuid.uuid4().hex,
                    signing_secret_ciphertext=CredentialCipher(TEST_KEY).encrypt("ghp_x"),
                    delivery_config={"repo": "acme/app", "base_branch": "main"},
                    is_active=True,
                )
            )
        s.add(
            ExecutionRun(
                id=run_id,
                workspace_id=ws,
                product_id=product_id,
                status=RunStatus.RUNNING,
                payload={"intent_text": "fix the thing"},
            )
        )
        await s.flush()
        s.add(
            WorkStep(
                id=uuid.uuid4(),
                run_id=run_id,
                workspace_id=ws,
                title="step",
                status=WorkStepStatus.RUNNING,
                proof_state=ProofState.UNTESTED,
                payload={},
            )
        )
        s.add(
            Decision(
                id=decision_id,
                run_id=run_id,
                workspace_id=ws,
                decision="verification_failed",
                payload={"reason": "contract_failed", "artifact_refs": ["app.py"]},
                status=DecisionStatus.PENDING,
            )
        )
        await s.commit()
    return ws, run_id, decision_id


async def _ship(sf_, ws: uuid.UUID, decision_id: uuid.UUID) -> None:
    async with sf_() as s, workspace_session_scope(s, ws):
        await resolve_checkpoint(
            s,
            workspace_id=ws,
            checkpoint_id=decision_id,
            action_key="ship",
            actor_id=uuid.uuid4(),
        )
        await s.commit()


async def _run(sf_, ws: uuid.UUID, run_id: uuid.UUID) -> ExecutionRun:
    async with sf_() as s, workspace_session_scope(s, ws):
        run = await s.get(ExecutionRun, run_id)
        assert run is not None
        return run


async def _events(sf_, ws: uuid.UUID, run_id: uuid.UUID) -> list[DeliveryEventRow]:
    async with sf_() as s, workspace_session_scope(s, ws):
        return list(
            (await s.execute(select(DeliveryEventRow).where(DeliveryEventRow.run_id == run_id)))
            .scalars()
            .all()
        )


# ---------------------------------------------------------------------------
# GitHub product — ship hands the work to the delivery path
# ---------------------------------------------------------------------------


async def test_ship_writes_the_delivery_event_the_worker_drains(sf, storage) -> None:
    ws, run_id, decision_id = await _seed(sf, github=True)

    await _ship(sf, ws, decision_id)

    events = await _events(sf, ws, run_id)
    assert len(events) == 1
    async with sf() as s, workspace_session_scope(s, ws):
        deliverable = await s.get(Deliverable, events[0].deliverable_id)
    assert deliverable is not None and deliverable.run_id == run_id
    assert events[0].payload["artifact_refs"] == ["app.py"]


async def test_ship_leaves_the_run_for_its_merge_to_ship(sf, storage) -> None:
    """#1109 — shipped only after the merge. The PR does not even exist yet."""
    ws, run_id, decision_id = await _seed(sf, github=True)

    await _ship(sf, ws, decision_id)

    assert (await _run(sf, ws, run_id)).status is RunStatus.REVIEW_READY


async def test_ship_does_not_force_merge_the_local_main(sf, storage) -> None:
    """The PR carries the run branch — the local main must not drift from GitHub's."""
    ws, _run_id, decision_id = await _seed(sf, github=True)

    await _ship(sf, ws, decision_id)

    assert "force_merge_theirs" not in storage
    assert "commit_worktree" in storage  # the branch the PR is cut from has the work


# ---------------------------------------------------------------------------
# The ship click is the approval — no second Safe Mode card
# ---------------------------------------------------------------------------


class _Dispatcher:
    def __init__(self) -> None:
        self.dispatched: list[uuid.UUID] = []

    async def dispatch(self, *, workspace_id, deliverable_id, artifact_type, **_: Any):
        self.dispatched.append(deliverable_id)
        return DeliveryResult(
            workspace_id=workspace_id,
            deliverable_id=deliverable_id,
            artifact_type=artifact_type,
            actions=[ActionResult(action="github.open_pr", succeeded=True)],
        )


async def _queued(sf_, ws: uuid.UUID) -> list[SafeModeQueueItemRow]:
    async with sf_() as s, workspace_session_scope(s, ws):
        return list((await s.execute(select(SafeModeQueueItemRow))).scalars().all())


async def test_a_shipped_event_is_dispatched_past_safe_mode(sf, storage) -> None:
    ws, run_id, decision_id = await _seed(sf, github=True, safe_mode=True)
    await _ship(sf, ws, decision_id)
    (event,) = await _events(sf, ws, run_id)
    assert event.payload.get(FOUNDER_APPROVED_KEY) is True

    dispatcher = _Dispatcher()
    await DeliveryWorker(session_factory=sf, dispatcher=dispatcher).drain_once()

    assert dispatcher.dispatched == [event.deliverable_id]
    assert await _queued(sf, ws) == []


async def test_the_delivered_ship_completes_the_run(sf, storage) -> None:
    """With no merge watch to wait on, a successful delivery ships the run."""
    ws, run_id, decision_id = await _seed(sf, github=True)
    await _ship(sf, ws, decision_id)

    assert (await _run(sf, ws, run_id)).status is RunStatus.REVIEW_READY

    dispatcher = _Dispatcher()
    await DeliveryWorker(session_factory=sf, dispatcher=dispatcher).drain_once()

    assert len(dispatcher.dispatched) == 1
    assert (await _run(sf, ws, run_id)).status is RunStatus.SHIPPED


async def test_an_unapproved_event_still_waits_for_safe_mode(sf) -> None:
    """Control — only the founder's ship is pre-approved; a verified run's event queues."""
    ws, run_id, _decision_id = await _seed(sf, github=True, safe_mode=True)
    deliverable_id = uuid.uuid4()
    async with sf() as s, workspace_session_scope(s, ws):
        s.add(
            Deliverable(
                id=deliverable_id,
                run_id=run_id,
                workspace_id=ws,
                deliverable_type="code",
                payload={"artifact_refs": ["app.py"]},
            )
        )
        s.add(
            DeliveryEventRow(
                id=uuid.uuid4(),
                workspace_id=ws,
                run_id=run_id,
                deliverable_id=deliverable_id,
                artifact_type="code",
                payload={"artifact_refs": ["app.py"]},
            )
        )
        await s.commit()

    dispatcher = _Dispatcher()
    await DeliveryWorker(session_factory=sf, dispatcher=dispatcher).drain_once()

    assert dispatcher.dispatched == []
    assert len(await _queued(sf, ws)) == 1


# ---------------------------------------------------------------------------
# Local-repo product — unchanged: the local main IS the delivery
# ---------------------------------------------------------------------------


async def test_a_local_product_ship_still_force_merges_and_ships(sf, storage) -> None:
    ws, run_id, decision_id = await _seed(sf, github=False)

    await _ship(sf, ws, decision_id)

    assert "force_merge_theirs" in storage
    assert (await _run(sf, ws, run_id)).status is RunStatus.SHIPPED
    assert await _events(sf, ws, run_id) == []
