"""DbPollScheduleRunner — v1 ``ScheduleRunnerProtocol`` impl: DB-poll + fire + advance.

Status §5 is explicit that Redis Streams is a Phase-1 *honest defer*
(DB-poll is the real mode today). This module is the v1 implementation
of the Schedule context's wake-up substrate seam
(:class:`~backend.schedule.domain.runner_protocol.ScheduleRunnerProtocol`)
— a future ``RedisStreamScheduleRunner`` can satisfy the same Protocol
without touching the worker shell or any caller.

One call selects every
:class:`~backend.schedule.infrastructure.schedule_db.WorkspaceScheduleRow`
where ``enabled=True AND next_run_at <= now`` (``SKIP LOCKED`` so two
workers don't fight for the same row), fires
:class:`~backend.schedule.application.emitter.ScheduleTrigger` for each,
then advances the row's ``next_run_at`` via the injected
:class:`~backend.schedule.domain.advancer.ScheduleAdvancer`. All in ONE
transaction per session — a partial failure rolls the whole batch back
so a half-advanced row can never silently skip a window.

Idempotence is asserted at the **spawn site**: ``ScheduleTrigger.fire()``
keys on ``<plugin>:<fire_iso>`` and the :class:`TriggerEventRow` unique
constraint is ``(workspace_id, source, idempotency_key)``. A second tick
in the same window (clock hasn't crossed the new ``next_run_at``)
computes the same key, hits the unique constraint, and returns
``duplicate=True`` — never a second Request. This is **stronger** than
"the worker log says no" — even a row that slips past
``SELECT … FOR UPDATE SKIP LOCKED`` (CI scheduler quirks, two workers
racing) is collapsed at the DB.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.schedule.application.emitter import ScheduleTrigger
from backend.schedule.domain.advancer import OneShotScheduleAdvancer, ScheduleAdvancer
from backend.schedule.infrastructure.schedule_db import WorkspaceScheduleRow

logger = structlog.get_logger(__name__)


class DbPollScheduleRunner:
    """v1 ``ScheduleRunnerProtocol`` impl: DB-poll + fire + advance.

    One call selects every ``WorkspaceScheduleRow`` where ``enabled=True
    AND next_run_at <= now`` (``SKIP LOCKED`` so two workers don't fight
    for the same row), fires :class:`ScheduleTrigger` for each, then
    advances the row's ``next_run_at`` via the injected
    :class:`ScheduleAdvancer`. All in ONE transaction per session — a
    partial failure rolls the whole batch back so a half-advanced row
    can never silently skip a window.

    The ``now_fn`` lets the worker inject a deterministic clock under
    tests (the ``BaseWorker`` poll loop calls
    :meth:`ScheduleWorker.fire_due_once` with ``datetime.now(UTC)`` in
    prod).
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

        ``with_for_update(skip_locked=True)`` matters under multi-worker
        prod (the launchd ``com.bsvibe.worker`` + any future replicas) —
        a second worker that lands on the same row at the same time
        skips it rather than blocking, and the next tick catches it. On
        SQLite the ``skip_locked`` hint is a no-op (the dialect ignores
        it), which is fine for tests that drive ``fire_due_once`` from a
        single coroutine.
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
        OWN ``next_run_at`` — NOT the wall clock — so the idempotency key
        is ``<plugin>:<next_run_at_iso>``: two ticks in the same window
        key the same way and collide at the unique constraint. (Using
        the wall clock would re-key per tick and the constraint couldn't
        help us.)
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
        # window so the next tick targets a NEW one. A duplicate at the
        # spawn site just means another worker already fired the SAME
        # window, and advancing here is still correct (we've consumed
        # our turn).
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


def build_db_poll_schedule_runner(
    *,
    advancer: ScheduleAdvancer | None = None,
) -> DbPollScheduleRunner:
    """Production :class:`DbPollScheduleRunner` factory.

    Defaults to :class:`OneShotScheduleAdvancer` — the honest M1 deferral
    (Status §5 calls Redis Streams a Phase-1 honest defer; the same
    applies to the cron parser here). A future implementation lift can
    swap in a real cron-expression advancer without touching this
    factory's callers.
    """
    return DbPollScheduleRunner(advancer=advancer or OneShotScheduleAdvancer())


__all__ = [
    "DbPollScheduleRunner",
    "build_db_poll_schedule_runner",
]
