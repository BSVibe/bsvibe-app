"""Schedule infrastructure — persistence row, v1 DB-poll runner, worker shell.

* :mod:`backend.schedule.infrastructure.schedule_db` —
  :class:`WorkspaceScheduleRow` (``workspace_schedules`` table).
* :mod:`backend.schedule.infrastructure.db_poll_runner` — v1
  :class:`DbPollScheduleRunner` impl of :class:`ScheduleRunnerProtocol`.
* :mod:`backend.schedule.infrastructure.workers.schedule_worker` —
  :class:`ScheduleWorker` (the actual worker daemon).
"""

from __future__ import annotations

# Lift N defensive pattern #1 (v8 §22) — namespace-only, no re-exports.
__all__: list[str] = []
