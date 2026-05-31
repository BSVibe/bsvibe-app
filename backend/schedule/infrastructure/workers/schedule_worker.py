"""ScheduleWorker — the schedule end of the OS (poll-loop worker shell).

Workflow §12.5 #8 (Bundle G — Intake / Triggers) / Status §5 medium-term.
:mod:`backend.schedule.application.emitter` turns a *fire time* into a
:class:`~backend.workflow.infrastructure.intake.db.TriggerEventRow` — but
nothing in production told it WHEN to fire. ``ScheduleWorker`` is that
"when": a :class:`~backend.workers.base.BaseWorker` that delegates each
tick to a :class:`~backend.schedule.domain.runner_protocol.ScheduleRunnerProtocol`
implementation. The default v1 impl
(:class:`~backend.schedule.infrastructure.db_poll_runner.DbPollScheduleRunner`)
DB-polls :class:`~backend.schedule.infrastructure.schedule_db.WorkspaceScheduleRow`
for rows where ``enabled=True AND next_run_at <= now`` and calls
:class:`~backend.schedule.application.emitter.ScheduleTrigger` on each
one. The downstream
:class:`~backend.workflow.infrastructure.workers.intake_worker.IntakeWorker`
drains the new TriggerEvent into a Request, and the agent loop takes it
from there — the same path a Direct or Connector trigger walks.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.schedule.domain.runner_protocol import ScheduleRunnerProtocol
from backend.workers.base import BaseWorker


@dataclass(slots=True)
class ScheduleWorkerConfig:
    """Tunables for the schedule poll loop.

    The default ``poll_interval_s=10`` is deliberately coarser than
    intake / delivery: schedule windows are minute-grained (cron), so a
    10-second poll is the longest sleep that still keeps fire-latency
    below the smallest real window. Tests can drive the worker via
    :meth:`ScheduleWorker.fire_due_once` without touching the loop.
    """

    poll_interval_s: float = 10.0


class ScheduleWorker(BaseWorker):
    """The schedule end of the OS: a poll-loop that fires due schedules.

    Depends on the :class:`ScheduleRunnerProtocol` rather than the concrete
    :class:`DbPollScheduleRunner` so the wake-up substrate is swappable
    — a future Redis-Streams runner can be dropped in without touching
    this class or any caller. Mirrors the rest of the worker family:
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

        Public (mirrors :meth:`IntakeWorker.drain_once`) so tests can
        drive a single tick deterministically without starting the loop.
        """
        now = datetime.now(tz=UTC)
        return await self._runner.fire_due(session_factory=self._session_factory, now=now)


__all__ = [
    "ScheduleWorker",
    "ScheduleWorkerConfig",
]
