"""Schedule domain — published Protocols + cron-algebra implementations.

Two seams live here, both as :mod:`typing.Protocol`:

* :class:`~backend.schedule.domain.runner_protocol.ScheduleRunnerProtocol`
  — the wake-up substrate. The worker shell depends on this Protocol,
  not on the concrete v1 :class:`DbPollScheduleRunner`, so a future
  Redis-Streams runner (Status §5 honest defer) can drop in unchanged.
* :class:`~backend.schedule.domain.advancer.ScheduleAdvancer` — the
  cron-algebra seam. The runner asks the advancer for the *next*
  ``next_run_at`` after firing; a real-cron implementation can be
  swapped in without rewriting the runner.

The only ``Advancer`` impl is :class:`CronScheduleAdvancer` (standard
5-field cron, UTC). The one-shot / fixed-interval placeholders that predated
the cron parser had zero callers and were deleted 2026-08-21.
"""

from __future__ import annotations

# Lift N defensive pattern #1 (v8 §22) — namespace-only, no re-exports.
__all__: list[str] = []
