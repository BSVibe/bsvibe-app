"""A run whose drive keeps crashing stops retrying in silence.

``drive_once`` catches exactly one exception — ``ExecutorCapacitySaturated``,
the yield-back. Everything else (an executor turn that timed out, a dead worker,
a bug in a stage) leaves through the top of the batch loop, and three things
follow that nobody chose:

* **the batch dies with it.** The remaining runs claimed in that same pass are
  never driven, and sit RUNNING holding claims they are not using.
* **the failing run keeps its claim.** Nothing releases it, so it waits out the
  full stale lease (2× the executor timeout — two hours by default) before the
  reaper can even look at it.
* **it comes back forever.** The reaper resets it to OPEN, it is re-driven, it
  fails the same way, and there is no counter anywhere that notices. The only
  thing that has ever ended one of these is the founder cancelling it by hand
  (prod runs ``0bbf72eb`` and ``010bbdd8``).

``BaseWorker`` swallows the exception with ``logger.exception`` and continues,
which is right for a worker shell and is also the whole reason none of this is
visible from outside.

So: contain the failure to its own run, give the claim back, count, and when the
count says this is not going to fix itself, tell the founder — the same shape
#746 gave the merge-watch terminals.

The count itself lived in a per-process dict until 2026-09-08. That worked only
because prod happens to run exactly ONE AgentWorker: runs are claimed with
``FOR UPDATE SKIP LOCKED`` precisely so several workers can, and with two, each
process counts only the failures it personally saw. Neither ever reaches the
bound, nobody is ever told, and the run retries forever — which is the exact
failure this counter was built to end, re-created by the counter. So these tests
now drive real workers against a real database, and one of them runs TWO worker
instances over the same rows.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

import backend.notifications.db  # noqa: F401 — register the table on the shared Base
from backend.workflow.infrastructure.db import (
    Decision,
    ExecutionRun,
    ExecutionRunHistory,
    RunStatus,
)
from backend.workflow.infrastructure.workers.agent_worker import (
    DRIVE_FAILED_KIND,
    DRIVE_FAILURES_KEY,
)

from .._support import db_engine

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def sf():
    async with db_engine() as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


class _Boom(RuntimeError):
    """Whatever a drive can raise — a timed-out turn, a dead worker, a bug."""


async def test_one_runs_failure_does_not_kill_the_rest_of_the_batch(sf) -> None:
    """The batch loop must not be an all-or-nothing. A single bad run took every
    other run claimed in the same pass down with it, and those runs then had to
    wait out the stale lease before anyone looked at them again."""
    from backend.workflow.infrastructure.workers.agent_worker import AgentWorker

    ids = [await _seed_run(sf) for _ in range(3)]
    driven: list[uuid.UUID] = []

    worker = _worker(sf)
    worker._claim_runs_for_drive = _returns(ids)  # type: ignore[method-assign]

    async def _drive(run_id: uuid.UUID, _execution: Any) -> None:
        if run_id == ids[0]:
            raise _Boom("turn timed out")
        driven.append(run_id)

    worker._frame_and_drive_run = _drive  # type: ignore[method-assign]

    count = await worker.drive_once()

    assert driven == ids[1:], "the healthy runs in the batch must still be driven"
    assert count == 2, "the failed run is not counted as driven"
    assert isinstance(worker, AgentWorker)


async def test_a_failed_drive_gives_its_claim_back(sf) -> None:
    """Holding a claim it is not using is what makes the run invisible for the
    length of the stale lease. It has already stopped — say so now."""
    run_id = await _seed_run(sf)
    worker = _worker(sf)
    worker._claim_runs_for_drive = _returns([run_id])  # type: ignore[method-assign]
    worker._frame_and_drive_run = _raises(_Boom("dead worker"))  # type: ignore[method-assign]

    await worker.drive_once()

    async with sf() as session:
        run = await session.get(ExecutionRun, run_id)
        assert run is not None
        assert run.claimed_at is None, "a crashed drive must release its claim"
        assert run.status is RunStatus.OPEN, "and hand the run back to the OPEN scan"

    # The history row must say what happened. Both callers of
    # ``_release_claim_to_open`` used to share one hard-coded sentence, so every
    # retried crash logged "yielded back on executor capacity saturation" into
    # its own run history — a false statement about a run nothing was saturating.
    reason = await _last_history_reason(sf, run_id)
    assert "saturation" not in reason, f"a crashed drive is not a capacity yield: {reason!r}"
    assert "_Boom" in reason, f"the history row must name what crashed: {reason!r}"


async def test_repeated_failures_reach_the_founder_and_stop_retrying(sf) -> None:
    """The bound. A run that has failed to drive this many times is not going to
    fix itself on the next tick, and re-driving it forever is how the platform
    burns a machine while saying nothing."""
    run_id = await _seed_run(sf)
    worker = _worker(sf, max_drive_failures=2)
    worker._claim_runs_for_drive = _returns([run_id])  # type: ignore[method-assign]
    worker._frame_and_drive_run = _raises(_Boom("turn timed out"))  # type: ignore[method-assign]

    await worker.drive_once()
    assert await _decision_kinds(sf, run_id) == [], "one failure is not a pattern"

    await worker.drive_once()

    assert await _decision_kinds(sf, run_id) == [DRIVE_FAILED_KIND], (
        "the founder is told once the bound is hit"
    )


async def test_two_workers_share_one_bound(sf) -> None:
    """The reason the count is on the run and not in a dict.

    Two AgentWorker INSTANCES are two processes as far as this counter is
    concerned — they share nothing but the database, which is exactly the
    arrangement ``FOR UPDATE SKIP LOCKED`` exists to support. Alternating the
    same failing run between them must still reach the bound. With a per-process
    count each worker sits at 1 forever, the bound is never crossed, and the run
    retries in silence for as long as the deployment lives."""
    run_id = await _seed_run(sf)
    one = _worker(sf, max_drive_failures=2)
    two = _worker(sf, max_drive_failures=2)
    for worker in (one, two):
        worker._claim_runs_for_drive = _returns([run_id])  # type: ignore[method-assign]
        worker._frame_and_drive_run = _raises(_Boom("turn timed out"))  # type: ignore[method-assign]

    await one.drive_once()
    await two.drive_once()

    assert await _decision_kinds(sf, run_id) == [DRIVE_FAILED_KIND], (
        "the second worker must see the first worker's failure"
    )


async def test_a_drive_that_works_clears_the_count(sf) -> None:
    """The counter is about CONSECUTIVE failures. A run that recovers must not
    carry a grudge into its next hiccup — otherwise a long-lived run accumulates
    unrelated failures and escalates for no reason."""
    run_id = await _seed_run(sf)
    worker = _worker(sf, max_drive_failures=2)
    worker._claim_runs_for_drive = _returns([run_id])  # type: ignore[method-assign]

    worker._frame_and_drive_run = _raises(_Boom("blip"))  # type: ignore[method-assign]
    await worker.drive_once()

    worker._frame_and_drive_run = _noop  # type: ignore[method-assign]
    await worker.drive_once()

    worker._frame_and_drive_run = _raises(_Boom("blip"))  # type: ignore[method-assign]
    await worker.drive_once()

    # Read the count itself, not just its absence of consequences: "no Decision"
    # is also what a counter that never increments at all would produce, and the
    # proposition here is specifically that the successful drive zeroed it.
    assert await _persisted_failures(sf, run_id) == 1, "the failure count restarted at 1"
    assert await _decision_kinds(sf, run_id) == [], "the successful drive reset the count"


async def test_the_capacity_yield_is_not_a_failure(sf) -> None:
    """Saturation is the platform working as designed — every worker busy. It
    already has its own yield-back and must not count toward the bound, or a
    busy afternoon escalates healthy runs to the founder."""
    from backend.dispatch.adapter import ExecutorCapacitySaturated

    run_id = await _seed_run(sf)
    worker = _worker(sf, max_drive_failures=2)
    worker._claim_runs_for_drive = _returns([run_id])  # type: ignore[method-assign]
    worker._frame_and_drive_run = _raises(ExecutorCapacitySaturated("all busy"))  # type: ignore[method-assign]

    await worker.drive_once()
    await worker.drive_once()
    await worker.drive_once()

    assert await _decision_kinds(sf, run_id) == []


# ── the minimum of the worker these tests drive ──────────────────────────────

_EXECUTION: Any = object()


def _returns(ids: list[uuid.UUID]) -> Any:
    async def _f(*_a: Any, **_k: Any) -> list[uuid.UUID]:
        return list(ids)

    return _f


def _raises(exc: BaseException) -> Any:
    async def _f(*_a: Any, **_k: Any) -> None:
        raise exc

    return _f


async def _noop(*_a: Any, **_k: Any) -> None:
    return None


async def _seed_run(sf: Any) -> uuid.UUID:
    """A claimed RUNNING run — what ``drive_once`` hands to ``_frame_and_drive_run``."""
    async with sf() as session:
        run = ExecutionRun(
            id=uuid.uuid4(),
            workspace_id=uuid.uuid4(),
            product_id=None,
            request_id=None,
            status=RunStatus.RUNNING,
            payload={},
            claimed_at=datetime.now(tz=UTC),
            claimed_by=uuid.uuid4(),
        )
        session.add(run)
        await session.commit()
        return run.id


async def _decision_kinds(sf: Any, run_id: uuid.UUID) -> list[str]:
    async with sf() as session:
        rows = await session.execute(select(Decision).where(Decision.run_id == run_id))
        return [d.decision for d in rows.scalars().all()]


async def _persisted_failures(sf: Any, run_id: uuid.UUID) -> int:
    """The consecutive-failure count as it actually sits on the run row."""
    async with sf() as session:
        run = await session.get(ExecutionRun, run_id)
        assert run is not None
        return (run.payload or {}).get(DRIVE_FAILURES_KEY, 0)


async def _last_history_reason(sf: Any, run_id: uuid.UUID) -> str:
    async with sf() as session:
        rows = await session.execute(
            select(ExecutionRunHistory)
            .where(ExecutionRunHistory.run_id == run_id)
            .order_by(ExecutionRunHistory.created_at)
        )
        history = list(rows.scalars().all())
        return history[-1].reason or "" if history else ""


def _worker(sf: Any, *, max_drive_failures: int = 3) -> Any:
    """A real AgentWorker over a real database, with only the two edges these
    tests are not about — claiming and driving — replaced."""
    from backend.config import get_settings
    from backend.workflow.infrastructure.workers.agent_worker import (
        AgentWorker,
        AgentWorkerConfig,
    )

    settings = get_settings().model_copy(update={"agent_max_drive_failures": max_drive_failures})
    worker = AgentWorker(
        session_factory=sf,
        config=AgentWorkerConfig(poll_interval_s=30.0),
        settings=settings,
    )
    worker._execution = _EXECUTION

    async def _reap(*_a: Any, **_k: Any) -> int:
        return 0

    worker._reap_stale_claims = _reap  # type: ignore[method-assign]
    worker._reap_terminal_run_workspaces = _reap  # type: ignore[method-assign]
    return worker


# ── what the founder actually sees ───────────────────────────────────────────


async def test_the_phone_says_which_way_it_failed() -> None:
    """An unlisted reason falls back to the generic needs-you body, which reads
    "something needs you" and tells the founder nothing about what to do. This
    Decision means the work never ran — worth its own sentence."""
    from backend.notifications.copy import needs_you_reason_body

    for lang in ("en", "ko"):
        body = needs_you_reason_body("drive_failed_repeatedly", lang)
        generic = needs_you_reason_body("something-not-in-the-catalog", lang)
        assert body != generic, f"{lang}: the reason must have its own body"


async def test_the_brief_line_is_not_blank_or_jargon() -> None:
    from types import SimpleNamespace

    from backend.workflow.application._checkpoint_shared import _question_text
    from backend.workflow.infrastructure.workers.agent_worker import DRIVE_FAILED_KIND

    decision = SimpleNamespace(
        decision=DRIVE_FAILED_KIND, payload={"reason": "drive_failed_repeatedly"}
    )
    for lang in ("en", "ko"):
        line = _question_text(decision, lang)  # type: ignore[arg-type]
        assert line, f"{lang}: the Brief item must not be blank"
        assert "drive_failed" not in line, "machine jargon must not reach the founder"


async def test_the_founder_can_retry_or_discard_but_not_ship() -> None:
    """``ship`` would approve output that was never produced — this run crashed
    before it made anything. ``retry`` re-opens it for a fresh attempt (a failed
    drive is recoverable) and ``discard`` abandons it."""
    from types import SimpleNamespace

    from backend.workflow.application._checkpoint_shared import _decision_actions
    from backend.workflow.infrastructure.workers.agent_worker import DRIVE_FAILED_KIND

    actions = _decision_actions(SimpleNamespace(decision=DRIVE_FAILED_KIND))  # type: ignore[arg-type]
    keys = {a.key for a in (actions or [])}

    assert "retry" in keys and "discard" in keys
    assert "ship" not in keys, "there is nothing to ship — the work never ran"
