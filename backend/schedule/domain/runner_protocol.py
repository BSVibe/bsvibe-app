"""ScheduleRunnerProtocol — the wake-up substrate seam.

The :class:`~backend.schedule.infrastructure.workers.schedule_worker.ScheduleWorker`
depends on this Protocol, not the concrete
:class:`~backend.schedule.infrastructure.db_poll_runner.DbPollScheduleRunner`.
Status §5 is explicit that Redis Streams is a Phase-1 *honest defer*
(DB-poll is the real mode today); the Protocol is the **promotion
seam** so a future ``RedisStreamScheduleRunner`` can be dropped in
without touching the worker or its callers.

The Workflow context's :class:`SafeModeExpirySweepRunner` also satisfies
this Protocol so a SECOND :class:`ScheduleWorker` instance can drive
the system-wide expiry sweep on the same polling cadence — honest reuse
of the seam without bending the ``workspace_schedules`` invariant.
That cross-context import (workflow → schedule.domain) is acceptable
because the Protocol is the published interface — domain Protocols are
exactly what one context exposes to others per DDD.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


class ScheduleRunnerProtocol(Protocol):
    """The wake-up substrate the :class:`ScheduleWorker` delegates to.

    Contract: one call drives ONE wake-up batch. The session factory is
    the runner's persistence handle (the runner opens + commits its own
    session(s) — the worker doesn't manage that). Returns the count of
    schedule rows that fired (NOT trigger events created — a duplicate
    window still counts as 'attempted to fire' for observability).
    """

    async def fire_due(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        now: datetime,
    ) -> int: ...


__all__ = ["ScheduleRunnerProtocol"]
