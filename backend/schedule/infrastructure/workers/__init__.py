"""Schedule infrastructure / workers.

* :mod:`backend.schedule.infrastructure.workers.schedule_worker` —
  :class:`ScheduleWorker` (the actual worker daemon shell + tunables).
"""

from __future__ import annotations

# Lift N defensive pattern #1 (v8 §22) — namespace-only, no re-exports.
__all__: list[str] = []
