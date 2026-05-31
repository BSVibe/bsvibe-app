"""ScheduleWorker — DB-poll runner for ``workspace_schedules`` (M1).

Workflow §12.5 #8 (Bundle G — Intake / Triggers) / Status §5 medium-term.
:mod:`backend.intake.schedule` already turns a *fire time* into a
:class:`~backend.workflow.infrastructure.intake.db.TriggerEventRow` — but nothing in production told
it WHEN to fire. ``ScheduleWorker`` is that "when": a
:class:`~backend.workers.base.BaseWorker` that DB-polls
:class:`~backend.intake.schedule_db.WorkspaceScheduleRow` for rows where
``enabled=True AND next_run_at <= now`` and calls
:class:`~backend.intake.schedule.ScheduleTrigger` on each one. The downstream
:class:`~backend.workflow.infrastructure.workers.intake_worker.IntakeWorker` drains the new
TriggerEvent into a Request, and the agent loop takes it from there — the
same path a Direct or Connector trigger walks.

Two design seams worth calling out:

* :class:`ScheduleRunnerProtocol` — the wake-up substrate. The worker depends
  on this interface, not the concrete :class:`DbPollScheduleRunner`. Status §5
  is explicit that Redis Streams is a Phase-1 *honest defer* (DB-poll is the
  real mode today); the Protocol is the **promotion seam** so a future
  ``RedisStreamScheduleRunner`` can be dropped in without touching the worker
  or its callers. The deliverable in this lift is the seam — NOT a new impl.
* :class:`ScheduleAdvancer` — the cron-algebra seam. The runner asks the
  advancer for the *next* ``next_run_at`` after firing; a noop / fixed-interval
  test impl can hold the clock steady, and a real-cron impl (croniter / a
  hand-rolled standard-5-field evaluator) can be swapped in later without
  rewriting the runner. v1 ships only the test-facing impls — real-cron is the
  next lift, and the spec is explicit that this PR ships the runner topology,
  not the cron parser.

Idempotence is asserted at the **spawn site**: ``ScheduleTrigger.fire()`` keys
on ``<plugin>:<fire_iso>`` and the :class:`TriggerEventRow` unique constraint
is ``(workspace_id, source, idempotency_key)``. A second tick in the same
window (clock hasn't crossed the new ``next_run_at``) computes the same key,
hits the unique constraint, and returns ``duplicate=True`` — never a second
Request. This is **stronger** than "the worker log says no" — even a row that
slips past ``SELECT … FOR UPDATE SKIP LOCKED`` (CI scheduler quirks, two
workers racing) is collapsed at the DB.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.intake.schedule import ScheduleTrigger
from backend.intake.schedule_db import WorkspaceScheduleRow
from backend.workers.base import BaseWorker

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Seams — the swappable bits
# ---------------------------------------------------------------------------


class ScheduleAdvancer(Protocol):
    """Compute the *next* fire time after a successful fire.

    Pure: takes the just-fired window time + cron expression, returns the next
    fire time. Implementations may use a real cron library (croniter) or a
    hand-rolled standard-5-field evaluator. Test impls hold the clock steady
    (so a follow-up tick is verifiably idempotent) or advance by a fixed
    interval (so the second-window delta is real).
    """

    def next_after(self, *, cron_expr: str, after: datetime) -> datetime: ...


class ScheduleRunnerProtocol(Protocol):
    """The wake-up substrate the :class:`ScheduleWorker` delegates to.

    The Protocol is the **promotion seam** (Status §5 — Redis Streams is a
    Phase-1 honest defer). ``DbPollScheduleRunner`` is the v1 impl; a future
    ``RedisStreamScheduleRunner`` can satisfy the same Protocol so the worker
    + every caller stay unchanged.

    Contract: one call drives ONE wake-up batch. The session factory is the
    runner's persistence handle (the runner opens + commits its own session(s)
    — the worker doesn't manage that). Returns the count of schedule rows
    that fired (NOT trigger events created — a duplicate window still counts
    as 'attempted to fire' for observability).
    """

    async def fire_due(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        now: datetime,
    ) -> int: ...


# ---------------------------------------------------------------------------
# v1 DB-polling runner
# ---------------------------------------------------------------------------


class DbPollScheduleRunner:
    """v1 ``ScheduleRunnerProtocol`` impl: DB-poll + fire + advance.

    One call selects every ``WorkspaceScheduleRow`` where ``enabled=True AND
    next_run_at <= now`` (``SKIP LOCKED`` so two workers don't fight for the
    same row), fires :class:`ScheduleTrigger` for each, then advances the
    row's ``next_run_at`` via the injected :class:`ScheduleAdvancer`. All in
    ONE transaction per session — a partial failure rolls the whole batch
    back so a half-advanced row can never silently skip a window.

    The ``now_fn`` lets the worker inject a deterministic clock under tests
    (the ``BaseWorker`` poll loop calls :meth:`ScheduleWorker.fire_due_once`
    with ``datetime.now(UTC)`` in prod).
    """

    def __init__(
        self,
        *,
        advancer: ScheduleAdvancer,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self._advancer = advancer
        self._now_fn = now_fn or (lambda: datetime.now(tz=UTC))

    async def fire_due(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        now: datetime,
    ) -> int:
        # The runner's own ``now_fn`` wins over the worker's ``now`` — tests
        # inject a deterministic clock at the runner level, and the worker
        # itself doesn't need to thread one in. In prod (no injection) the
        # default ``now_fn`` reads the wall clock once per tick, so a single
        # batch still uses ONE consistent clock across every row.
        effective_now = self._now_fn()
        async with session_factory() as session:
            rows = await self._claim_due(session, effective_now)
            fired = 0
            for sched in rows:
                if await self._fire_one(session, sched, effective_now):
                    fired += 1
            await session.commit()
            return fired

    async def _claim_due(self, session: AsyncSession, now: datetime) -> list[WorkspaceScheduleRow]:
        """Select every due, enabled schedule for this tick.

        ``with_for_update(skip_locked=True)`` matters under multi-worker prod
        (the launchd ``com.bsvibe.worker`` + any future replicas) — a second
        worker that lands on the same row at the same time skips it rather
        than blocking, and the next tick catches it. On SQLite the
        ``skip_locked`` hint is a no-op (the dialect ignores it), which is
        fine for tests that drive ``fire_due_once`` from a single coroutine.
        """
        stmt = (
            select(WorkspaceScheduleRow)
            .where(
                WorkspaceScheduleRow.enabled.is_(True),
                WorkspaceScheduleRow.next_run_at <= now,
            )
            .order_by(WorkspaceScheduleRow.next_run_at.asc())
            .with_for_update(skip_locked=True)
        )
        return list((await session.execute(stmt)).scalars().all())

    async def _fire_one(
        self,
        session: AsyncSession,
        sched: WorkspaceScheduleRow,
        now: datetime,
    ) -> bool:
        """Fire the emitter for ONE schedule + advance its ``next_run_at``.

        The fire time passed to :class:`ScheduleTrigger` is the schedule's
        OWN ``next_run_at`` — NOT the wall clock — so the idempotency key is
        ``<plugin>:<next_run_at_iso>``: two ticks in the same window key the
        same way and collide at the unique constraint. (Using the wall clock
        would re-key per tick and the constraint couldn't help us.)
        """
        trigger = ScheduleTrigger(session)
        outcome = await trigger.fire(
            workspace_id=sched.workspace_id,
            plugin_name=sched.plugin_name,
            cron_expr=sched.cron_expr,
            fired_at=sched.next_run_at,
            product_id=sched.product_id,
        )

        # Advance regardless of duplicate — the goal is to move past this
        # window so the next tick targets a NEW one. A duplicate at the spawn
        # site just means another worker already fired the SAME window, and
        # advancing here is still correct (we've consumed our turn).
        sched.next_run_at = self._advancer.next_after(
            cron_expr=sched.cron_expr, after=sched.next_run_at
        )
        sched.last_fired_at = now
        # ``flush`` keeps the row update inside the same transaction as the
        # trigger insert — partial failure rolls both back.
        await session.flush()

        logger.info(
            "schedule_runner_fired",
            schedule_id=str(sched.id),
            workspace_id=str(sched.workspace_id),
            plugin_name=sched.plugin_name,
            fired_at=sched.next_run_at.isoformat() if outcome.duplicate else None,
            duplicate=outcome.duplicate,
        )
        return True


# ---------------------------------------------------------------------------
# Worker shell
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ScheduleWorkerConfig:
    """Tunables for the schedule poll loop.

    The default ``poll_interval_s=10`` is deliberately coarser than intake /
    delivery: schedule windows are minute-grained (cron), so a 10-second poll
    is the longest sleep that still keeps fire-latency below the smallest
    real window. Tests can drive the worker via :meth:`ScheduleWorker.fire_due_once`
    without touching the loop.
    """

    poll_interval_s: float = 10.0


class ScheduleWorker(BaseWorker):
    """The schedule end of the OS: a poll-loop that fires due schedules.

    Depends on the :class:`ScheduleRunnerProtocol` rather than the concrete
    :class:`DbPollScheduleRunner` so the wake-up substrate is swappable — a
    future Redis-Streams runner can be dropped in without touching this class
    or any caller. Mirrors the rest of the worker family:
    :class:`IntakeWorker`, :class:`AgentWorker`, etc. — same
    ``BaseWorker``-shell + per-tick batch contract.
    """

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        runner: ScheduleRunnerProtocol,
        config: ScheduleWorkerConfig | None = None,
        name: str = "schedule_worker",
    ) -> None:
        # The default ``name="schedule_worker"`` preserves every existing
        # caller's behaviour; D3a (Safe Mode expiry sweep) instantiates a
        # SECOND ScheduleWorker against the SAME Protocol seam but a
        # different runner, so the ``name`` override prevents the two
        # workers from sharing a task name + log prefix in the runtime.
        self._cfg = config or ScheduleWorkerConfig()
        super().__init__(name=name, poll_interval_s=self._cfg.poll_interval_s)
        self._session_factory = session_factory
        self._runner = runner

    async def _tick(self) -> int:
        return await self.fire_due_once()

    async def fire_due_once(self) -> int:
        """One polling tick — fire every schedule due at ``now``.

        Public (mirrors :meth:`IntakeWorker.drain_once`) so tests can drive a
        single tick deterministically without starting the loop.
        """
        now = datetime.now(tz=UTC)
        return await self._runner.fire_due(session_factory=self._session_factory, now=now)


# ---------------------------------------------------------------------------
# Production advancer — honest deferral of the cron parser
# ---------------------------------------------------------------------------


# Practical sentinel: "the far future" — a row whose advancer returns this is
# effectively one-shot (it won't be due again on any realistic clock). Avoids
# ``datetime.max`` which trips some DB drivers' tzinfo edge cases.
_FAR_FUTURE: datetime = datetime(9999, 1, 1, tzinfo=UTC)


class OneShotScheduleAdvancer:
    """A no-real-cron-algebra advancer for v1 prod — schedules fire ONCE.

    Status §5 / M1 spec: this lift ships the **runner topology** + the
    **Protocol seam**, NOT the cron parser. Real cron-expression evaluation
    (croniter / a hand-rolled standard-5-field evaluator) is a follow-up impl
    that drops into the :class:`ScheduleAdvancer` Protocol with zero changes
    elsewhere.

    Until then, the v1 production advancer is honestly one-shot: after a fire
    it pushes ``next_run_at`` so far into the future that no real clock will
    ever cross it. An operator who wants the schedule to recur re-arms the
    row (or registers a fresh row) — which is exactly the dogfood-driven
    seam the next lift will replace.

    This is **deliberately honest**: a production system that silently fires
    a cron expression it cannot actually evaluate would be lying. The runner
    topology is real; the cron algebra is deferred — and visibly so.
    """

    def next_after(self, *, cron_expr: str, after: datetime) -> datetime:
        return _FAR_FUTURE


class FixedIntervalScheduleAdvancer:
    """Advance ``next_run_at`` by a fixed ``timedelta`` after each fire.

    Useful for callers that *do* want recurrence but don't yet need full
    cron-expression semantics (e.g. "every 5 minutes regardless of wall-clock
    alignment"). Provided alongside :class:`OneShotScheduleAdvancer` so the
    in-process integration path the production daemon wires can pick a
    sensible default once the operator wires up a recurring schedule, without
    waiting for the full cron parser.
    """

    def __init__(self, *, interval: timedelta) -> None:
        self._interval = interval

    def next_after(self, *, cron_expr: str, after: datetime) -> datetime:
        return after + self._interval


def build_db_poll_schedule_runner(
    *,
    advancer: ScheduleAdvancer | None = None,
) -> DbPollScheduleRunner:
    """Production :class:`DbPollScheduleRunner` factory.

    Defaults to :class:`OneShotScheduleAdvancer` — the honest M1 deferral
    (Status §5 calls Redis Streams a Phase-1 honest defer; the same applies
    to the cron parser here). A future implementation lift can swap in a
    real cron-expression advancer without touching this factory's callers.
    """
    return DbPollScheduleRunner(advancer=advancer or OneShotScheduleAdvancer())


__all__ = [
    "DbPollScheduleRunner",
    "FixedIntervalScheduleAdvancer",
    "OneShotScheduleAdvancer",
    "ScheduleAdvancer",
    "ScheduleRunnerProtocol",
    "ScheduleWorker",
    "ScheduleWorkerConfig",
    "build_db_poll_schedule_runner",
]
