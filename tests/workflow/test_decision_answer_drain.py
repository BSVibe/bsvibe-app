"""The engine applies a queued chat answer — 게이트 3 후속.

The inbound layer records the founder's tap and returns (see
``backend.connectors.decision_answer_queue``). This is the other half: the side
that is allowed to do the heavy work — record the answer, resume the run, run
``ship`` / ``discard`` side effects — through the SAME ``resolve_checkpoint``
the PWA's Brief uses.

Without this the tap is a card that changes nothing, which is exactly the defect
PR #920 closed. Rendering, queueing and draining land together.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.connectors.decision_answer_queue import QUEUED_ANSWER_KEY, queue_answer
from backend.identity.db import MembershipRow, UserRow
from backend.identity.workspaces_db import WorkspaceRow
from backend.workflow.application.decision_answer_drain import drain_queued_answers
from backend.workflow.infrastructure.db import Decision, DecisionStatus, ExecutionRun

from .._support import db_engine

pytestmark = pytest.mark.asyncio


async def _seed(session, *, kind: str, payload: dict) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    ws = uuid.uuid4()
    owner = UserRow(id=uuid.uuid4(), supabase_user_id=f"sub-{uuid.uuid4().hex}")
    session.add(WorkspaceRow(id=ws, name="WS", language="en"))
    session.add(owner)
    # Flush the FK TARGETS before the row that points at them. SQLAlchemy orders
    # inserts per-mapper, not by cross-model FK dependency, so on PostgreSQL the
    # membership can reach the wire before its user — SQLite's lax FK enforcement
    # hides this entirely, which is why it only ever fails in CI.
    await session.flush()
    session.add(MembershipRow(user_id=owner.id, workspace_id=ws, role="owner"))
    run = ExecutionRun(id=uuid.uuid4(), workspace_id=ws, status="running")
    session.add(run)
    await session.flush()
    decision = Decision(
        id=uuid.uuid4(),
        run_id=run.id,
        workspace_id=ws,
        decision=kind,
        payload=payload,
        status=DecisionStatus.PENDING,
    )
    session.add(decision)
    await session.commit()
    return ws, decision.id, owner.id


async def test_a_queued_action_is_applied_and_cleared() -> None:
    """The proposition the whole split rests on: the tap eventually lands."""
    async with db_engine() as (engine, _pg):
        sf = async_sessionmaker(engine, expire_on_commit=False)
        async with sf() as session:
            _ws, did, owner = await _seed(session, kind="human_review_required", payload={})
            decision = await session.get(Decision, did)
            assert decision is not None
            assert queue_answer(
                decision, action_key="discard", answer="", actor_id=owner, connector="telegram"
            )
            await session.commit()

            applied = await drain_queued_answers(session)
            assert applied == 1

            session.expire_all()
            decision = await session.get(Decision, did)
            assert decision is not None
            assert decision.status == DecisionStatus.RESOLVED
            # Cleared — this is what makes a retried drain idempotent.
            assert QUEUED_ANSWER_KEY not in (decision.payload or {})


async def test_a_queued_option_resolves_with_its_text() -> None:
    """The founder's sentence, not the button's index, is what the run resumes on."""
    options = ["이전 지시대로 대기한다", "실제 조사 질문을 새로 주신다"]
    async with db_engine() as (engine, _pg):
        sf = async_sessionmaker(engine, expire_on_commit=False)
        async with sf() as session:
            _ws, did, owner = await _seed(
                session, kind="ask_user_question", payload={"options": options}
            )
            decision = await session.get(Decision, did)
            assert decision is not None
            queue_answer(
                decision, action_key=None, answer=options[1], actor_id=owner, connector="telegram"
            )
            await session.commit()

            assert await drain_queued_answers(session) == 1
            session.expire_all()
            decision = await session.get(Decision, did)
            assert decision is not None
            assert decision.resolution == options[1]


async def test_draining_twice_applies_once() -> None:
    """The drain runs on a schedule; a second pass must find nothing."""
    async with db_engine() as (engine, _pg):
        sf = async_sessionmaker(engine, expire_on_commit=False)
        async with sf() as session:
            _ws, did, owner = await _seed(session, kind="human_review_required", payload={})
            decision = await session.get(Decision, did)
            assert decision is not None
            queue_answer(
                decision, action_key="discard", answer="", actor_id=owner, connector="telegram"
            )
            await session.commit()

            assert await drain_queued_answers(session) == 1
            assert await drain_queued_answers(session) == 0


async def test_a_decision_with_no_queued_answer_is_untouched() -> None:
    """Negative control: the drain must not resolve Decisions nobody answered.

    It scans PENDING Decisions; a scan that acted on all of them would settle the
    founder's whole queue on its first tick.
    """
    async with db_engine() as (engine, _pg):
        sf = async_sessionmaker(engine, expire_on_commit=False)
        async with sf() as session:
            _ws, did, _owner = await _seed(session, kind="human_review_required", payload={})
            assert await drain_queued_answers(session) == 0

            session.expire_all()
            decision = await session.get(Decision, did)
            assert decision is not None
            assert decision.status == DecisionStatus.PENDING


async def test_an_unreadable_queued_answer_is_dropped_not_retried_forever() -> None:
    """A malformed entry must not stall every Decision behind it.

    A poison row that the drain re-reads every tick is how a queue stops moving;
    the honest outcome is to drop it and leave the Decision answerable again.
    """
    async with db_engine() as (engine, _pg):
        sf = async_sessionmaker(engine, expire_on_commit=False)
        async with sf() as session:
            _ws, did, _owner = await _seed(
                session,
                kind="human_review_required",
                payload={QUEUED_ANSWER_KEY: {"action_key": "discard"}},  # no actor_id
            )
            assert await drain_queued_answers(session) == 0

            session.expire_all()
            decision = await session.get(Decision, did)
            assert decision is not None
            assert decision.status == DecisionStatus.PENDING
            assert QUEUED_ANSWER_KEY not in (decision.payload or {})


async def test_the_agent_worker_tick_drains() -> None:
    """The wiring, pinned where it can fail.

    A drain nothing calls is a queue that fills forever — the founder taps, the
    card says "picking it up", and nothing ever does. This is the hop that makes
    the split real, and it is invisible to every test above.
    """
    from backend.workflow.infrastructure.workers.agent_worker import AgentWorker

    async with db_engine() as (engine, _pg):
        sf = async_sessionmaker(engine, expire_on_commit=False)
        async with sf() as session:
            _ws, did, owner = await _seed(session, kind="human_review_required", payload={})
            decision = await session.get(Decision, did)
            assert decision is not None
            queue_answer(
                decision, action_key="discard", answer="", actor_id=owner, connector="telegram"
            )
            await session.commit()

        worker = AgentWorker(session_factory=sf)
        await worker._tick()

        async with sf() as session:
            decision = await session.get(Decision, did)
            assert decision is not None
            assert decision.status == DecisionStatus.RESOLVED
