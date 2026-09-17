"""Redelivery belongs to the awaiter, not to a background sweeper (#965).

A sweeper cannot rebuild the dispatch payload. Two of its fields are withheld
from the row **on purpose**:

* ``mcp`` — a run-scoped OAuth token minted at dispatch time by
  ``ExecutorAdapter._work_tool_surface``; it is ephemeral and belongs to that
  dispatch only;
* ``env`` — an exec task's secrets, kept beside the command precisely so they
  are never written to a database or left on a stream nobody trims.

So a sweeper could only redeliver by persisting secrets — paying for a
reliability fix with a security regression. The awaiter has both values in
memory at the moment it starts waiting (both call sites XADD and then await on
the very next statement), so it can simply re-send what it already holds.

Two more things fall out for free:

* **F10 (the remaining deadline).** ``timeout_s`` means "how long the awaiting
  caller will actually wait", measured from when the worker starts. A sweeper
  re-sending the original value would let the second attempt outlive its
  awaiter. The awaiter knows its own deadline, so it sends what is left.
* **A dead awaiter redelivers nothing** — which is correct, not a gap. Nobody
  is waiting for that result; reviving it would burn a worker slot to report
  into the void.

Redelivery fires only when the row is positively known **unreceived**:
``dispatched`` + ``claimed_at IS NULL``. A worker that does not claim (an older
build) must be excluded, because it would execute the duplicate — ``run_once``
has no dedupe of its own. That gate is enforced here rather than at the call
sites so a caller cannot forget it.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest

pytestmark = pytest.mark.asyncio


class _SilentRedis:
    """A pub/sub that never delivers — forces the DB-poll safety net path."""

    class _PubSub:
        async def subscribe(self, *_a: Any) -> None:
            return None

        async def get_message(self, **_kw: Any) -> None:
            await asyncio.sleep(0.01)
            return None

        async def unsubscribe(self, *_a: Any) -> None:
            return None

        async def aclose(self) -> None:
            return None

    def pubsub(self) -> _PubSub:
        return self._PubSub()


async def _engine_and_sessions() -> Any:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from backend.data import Base

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, sm


async def _seed(
    sm: Any, *, claimed: bool = False, protocol_version: int = 2, status: str = "dispatched"
) -> tuple[uuid.UUID, uuid.UUID]:
    from datetime import UTC, datetime

    from backend.executors.db import ExecutorTaskRow, WorkerRow

    worker_id = uuid.uuid4()
    workspace_id = uuid.uuid4()
    task_id = uuid.uuid4()
    async with sm() as session:
        session.add(
            WorkerRow(
                id=worker_id,
                workspace_id=workspace_id,
                name="w",
                labels=[],
                capabilities=["claude_code"],
                status="online",
                token_hash="h",
                is_active=True,
                protocol_version=protocol_version,
            )
        )
        session.add(
            ExecutorTaskRow(
                id=task_id,
                workspace_id=workspace_id,
                executor_type="claude_code",
                prompt="p",
                system="",
                workspace_dir="/srv/run",
                status=status,
                worker_id=worker_id,
                claimed_at=datetime.now(UTC) if claimed else None,
            )
        )
        await session.commit()
    return task_id, worker_id


async def _await_with_redelivery(
    sm: Any, task_id: uuid.UUID, *, timeout_s: float, redeliver_after_s: float
) -> list[float]:
    """Run ``await_completion`` to its timeout, recording each redelivery."""
    from backend.executors import dispatch

    sent: list[float] = []

    async def _redeliver(remaining_s: float) -> None:
        sent.append(remaining_s)

    async with sm() as session:
        with pytest.raises(dispatch.TaskTimeout):
            await dispatch.await_completion(
                _SilentRedis(),
                session=session,
                task_id=task_id,
                timeout_s=timeout_s,
                session_factory=sm,
                redeliver=_redeliver,
                redeliver_after_s=redeliver_after_s,
            )
    return sent


async def test_an_unclaimed_dispatched_task_is_redelivered() -> None:
    engine, sm = await _engine_and_sessions()
    try:
        task_id, _ = await _seed(sm)
        sent = await _await_with_redelivery(sm, task_id, timeout_s=1.0, redeliver_after_s=0.2)
        assert sent, (
            "the row was dispatched and never claimed for the whole wait — that is "
            "the delivery gap, and nothing else in the system will ever retry it"
        )
    finally:
        await engine.dispose()


async def test_redelivery_carries_the_remaining_deadline_not_the_original() -> None:
    """F10 — ``timeout_s`` is measured from the worker's start, so re-sending
    the original number lets the second attempt outlive the awaiter waiting on it.
    """
    engine, sm = await _engine_and_sessions()
    try:
        task_id, _ = await _seed(sm)
        sent = await _await_with_redelivery(sm, task_id, timeout_s=1.0, redeliver_after_s=0.2)
        assert sent
        assert sent[0] < 1.0, (
            f"redelivered with {sent[0]}s of a 1.0s budget — the worker would keep "
            "running after this awaiter had already given up"
        )
        assert sent[0] > 0.0, "a non-positive budget would be dispatched already-expired"
    finally:
        await engine.dispose()


async def test_a_claimed_task_is_never_redelivered() -> None:
    """The load-bearing case: a legitimately long turn must not be duplicated.

    This is where a lease/heartbeat design has to guess and this one does not —
    a claimed row simply is not a redelivery candidate, however long it runs.
    """
    engine, sm = await _engine_and_sessions()
    try:
        task_id, _ = await _seed(sm, claimed=True)
        sent = await _await_with_redelivery(sm, task_id, timeout_s=1.0, redeliver_after_s=0.2)
        assert sent == [], "a claimed task is being executed; a second copy is a duplicate"
    finally:
        await engine.dispose()


async def test_a_worker_that_cannot_claim_is_never_redelivered_to() -> None:
    """Fail-closed on an older worker — it would run the duplicate.

    ``run_once`` on such a build spawns every execute message it receives; its
    ``_RUNNING_TASKS`` map is consulted only on the cancel path. So an unclaimed
    row from a pre-claim worker is ambiguous ("lost" vs "running") and the safe
    reading is the one that preserves today's behaviour.
    """
    engine, sm = await _engine_and_sessions()
    try:
        task_id, _ = await _seed(sm, protocol_version=1)
        sent = await _await_with_redelivery(sm, task_id, timeout_s=1.0, redeliver_after_s=0.2)
        assert sent == [], "redelivering to a worker with no claim support executes the task twice"
    finally:
        await engine.dispose()


async def test_redelivery_happens_at_most_once() -> None:
    """Every poll tick sees the same unclaimed row; resending on each would pile
    up copies of one task on the worker's stream.
    """
    engine, sm = await _engine_and_sessions()
    try:
        task_id, _ = await _seed(sm)
        sent = await _await_with_redelivery(sm, task_id, timeout_s=3.0, redeliver_after_s=0.2)
        assert len(sent) == 1, f"redelivered {len(sent)} times: {sent}"
    finally:
        await engine.dispose()


async def test_no_redeliver_callback_keeps_the_old_behaviour() -> None:
    """Callers that pass nothing must be untouched — the timeout still raises."""
    from backend.executors import dispatch

    engine, sm = await _engine_and_sessions()
    try:
        task_id, _ = await _seed(sm)
        async with sm() as session:
            with pytest.raises(dispatch.TaskTimeout):
                await dispatch.await_completion(
                    _SilentRedis(),
                    session=session,
                    task_id=task_id,
                    timeout_s=0.5,
                    session_factory=sm,
                )
    finally:
        await engine.dispose()


async def test_the_receipt_is_checked_once_not_on_every_poll(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A claimed row must not be re-interrogated for the rest of the turn.

    Nothing about the answer can change — a claimed row never becomes unclaimed
    — so re-asking each tick only doubles the awaiter's query rate, for the
    whole of exactly the longest waits (an hour-long ``act`` turn polls ~1800
    times). Pinned because it is a property of the control flow that no
    behavioural assertion above would notice being lost.
    """
    from backend.executors import dispatch

    engine, sm = await _engine_and_sessions()
    try:
        task_id, _ = await _seed(sm, claimed=True)
        checks = 0
        real = dispatch._is_unreceived_isolated

        async def _counting(*a: Any, **kw: Any) -> bool:
            nonlocal checks
            checks += 1
            return await real(*a, **kw)

        monkeypatch.setattr(dispatch, "_is_unreceived_isolated", _counting)
        sent = await _await_with_redelivery(sm, task_id, timeout_s=3.0, redeliver_after_s=0.2)

        assert sent == []
        assert checks == 1, (
            f"asked {checks} times across the wait — the receipt was settled on the first"
        )
    finally:
        await engine.dispose()
