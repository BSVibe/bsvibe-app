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
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

pytestmark = pytest.mark.asyncio


class _Boom(RuntimeError):
    """Whatever a drive can raise — a timed-out turn, a dead worker, a bug."""


async def test_one_runs_failure_does_not_kill_the_rest_of_the_batch() -> None:
    """The batch loop must not be an all-or-nothing. A single bad run took every
    other run claimed in the same pass down with it, and those runs then had to
    wait out the stale lease before anyone looked at them again."""
    from backend.workflow.infrastructure.workers.agent_worker import AgentWorker

    ids = [uuid.uuid4() for _ in range(3)]
    driven: list[uuid.UUID] = []

    worker = _worker()
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


async def test_a_failed_drive_gives_its_claim_back() -> None:
    """Holding a claim it is not using is what makes the run invisible for the
    length of the stale lease. It has already stopped — say so now."""
    run_id = uuid.uuid4()
    worker = _worker()
    worker._claim_runs_for_drive = _returns([run_id])  # type: ignore[method-assign]
    worker._frame_and_drive_run = _raises(_Boom("dead worker"))  # type: ignore[method-assign]

    await worker.drive_once()

    assert worker.released == [run_id], "a crashed drive must release its claim"


async def test_repeated_failures_reach_the_founder_and_stop_retrying() -> None:
    """The bound. A run that has failed to drive this many times is not going to
    fix itself on the next tick, and re-driving it forever is how the platform
    burns a machine while saying nothing."""
    run_id = uuid.uuid4()
    worker = _worker(max_drive_failures=2)
    worker._claim_runs_for_drive = _returns([run_id])  # type: ignore[method-assign]
    worker._frame_and_drive_run = _raises(_Boom("turn timed out"))  # type: ignore[method-assign]

    await worker.drive_once()
    assert worker.escalated == [], "one failure is not a pattern"

    await worker.drive_once()

    assert worker.escalated == [run_id], "the founder is told once the bound is hit"


async def test_a_drive_that_works_clears_the_count() -> None:
    """The counter is about CONSECUTIVE failures. A run that recovers must not
    carry a grudge into its next hiccup — otherwise a long-lived run accumulates
    unrelated failures and escalates for no reason."""
    run_id = uuid.uuid4()
    worker = _worker(max_drive_failures=2)
    worker._claim_runs_for_drive = _returns([run_id])  # type: ignore[method-assign]

    worker._frame_and_drive_run = _raises(_Boom("blip"))  # type: ignore[method-assign]
    await worker.drive_once()

    worker._frame_and_drive_run = _noop  # type: ignore[method-assign]
    await worker.drive_once()

    worker._frame_and_drive_run = _raises(_Boom("blip"))  # type: ignore[method-assign]
    await worker.drive_once()

    assert worker.escalated == [], "the successful drive reset the count"


async def test_the_capacity_yield_is_not_a_failure() -> None:
    """Saturation is the platform working as designed — every worker busy. It
    already has its own yield-back and must not count toward the bound, or a
    busy afternoon escalates healthy runs to the founder."""
    from backend.dispatch.adapter import ExecutorCapacitySaturated

    run_id = uuid.uuid4()
    worker = _worker(max_drive_failures=2)
    worker._claim_runs_for_drive = _returns([run_id])  # type: ignore[method-assign]
    worker._frame_and_drive_run = _raises(ExecutorCapacitySaturated("all busy"))  # type: ignore[method-assign]

    await worker.drive_once()
    await worker.drive_once()
    await worker.drive_once()

    assert worker.escalated == []
    assert worker._drive_failures.get(run_id, 0) == 0


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


def _worker(*, max_drive_failures: int = 3) -> Any:
    """An AgentWorker with its DB edges replaced by in-memory recorders."""
    from backend.config import get_settings
    from backend.workflow.infrastructure.workers.agent_worker import (
        AgentWorker,
        AgentWorkerConfig,
    )

    settings = get_settings().model_copy(update={"agent_max_drive_failures": max_drive_failures})
    worker = AgentWorker(
        session_factory=None,  # type: ignore[arg-type]
        config=AgentWorkerConfig(poll_interval_s=30.0),
        settings=settings,
    )
    worker._execution = _EXECUTION
    worker.released = []  # type: ignore[attr-defined]
    worker.escalated = []  # type: ignore[attr-defined]

    async def _release(run_id: uuid.UUID) -> None:
        worker.released.append(run_id)  # type: ignore[attr-defined]

    async def _clear(_run_id: uuid.UUID) -> None:
        return None

    async def _reap(*_a: Any, **_k: Any) -> int:
        return 0

    async def _escalate(run_id: uuid.UUID, *, failures: int, error: str) -> None:
        worker.escalated.append(run_id)  # type: ignore[attr-defined]

    worker._release_claim_to_open = _release  # type: ignore[method-assign]
    worker._clear_claim = _clear  # type: ignore[method-assign]
    worker._reap_stale_claims = _reap  # type: ignore[method-assign]
    worker._reap_terminal_run_workspaces = _reap  # type: ignore[method-assign]
    worker._escalate_drive_failure = _escalate  # type: ignore[method-assign]
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
