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

The two ``Advancer`` impls shipped today are deliberately small:
:class:`OneShotScheduleAdvancer` (honestly one-shot — the M1 deferral
of the cron parser) and :class:`FixedIntervalScheduleAdvancer` (fixed
``timedelta``, for callers that want recurrence without full cron
semantics yet).
"""
