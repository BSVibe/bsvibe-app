"""ScheduleAdvancer — the cron-algebra seam + v1 no-cron implementations.

The runner asks the advancer for the *next* ``next_run_at`` after firing;
a noop / fixed-interval test impl can hold the clock steady, and a
real-cron impl (croniter / a hand-rolled standard-5-field evaluator) can
be swapped in later without rewriting the runner.

v1 ships only :class:`OneShotScheduleAdvancer` (the honest M1 deferral
of the cron parser — schedules fire ONCE) and
:class:`FixedIntervalScheduleAdvancer` (advance by a fixed
``timedelta``). Real cron-expression evaluation is the next lift and
drops into the :class:`ScheduleAdvancer` Protocol with zero changes
elsewhere.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol

from backend.schedule.domain.cron import next_cron_time


class ScheduleAdvancer(Protocol):
    """Compute the *next* fire time after a successful fire.

    Pure: takes the just-fired window time + cron expression, returns the
    next fire time. Implementations may use a real cron library (croniter)
    or a hand-rolled standard-5-field evaluator. Test impls hold the
    clock steady (so a follow-up tick is verifiably idempotent) or
    advance by a fixed interval (so the second-window delta is real).
    """

    def next_after(self, *, cron_expr: str, after: datetime) -> datetime: ...


# Practical sentinel: "the far future" — a row whose advancer returns this
# is effectively one-shot (it won't be due again on any realistic clock).
# Avoids ``datetime.max`` which trips some DB drivers' tzinfo edge cases.
_FAR_FUTURE: datetime = datetime(9999, 1, 1, tzinfo=UTC)


class OneShotScheduleAdvancer:
    """A no-real-cron-algebra advancer for v1 prod — schedules fire ONCE.

    Status §5 / M1 spec: this lift ships the **runner topology** + the
    **Protocol seam**, NOT the cron parser. Real cron-expression
    evaluation (croniter / a hand-rolled standard-5-field evaluator) is
    a follow-up impl that drops into the :class:`ScheduleAdvancer`
    Protocol with zero changes elsewhere.

    Until then, the v1 production advancer is honestly one-shot: after a
    fire it pushes ``next_run_at`` so far into the future that no real
    clock will ever cross it. An operator who wants the schedule to
    recur re-arms the row (or registers a fresh row) — which is exactly
    the dogfood-driven seam the next lift will replace.

    This is **deliberately honest**: a production system that silently
    fires a cron expression it cannot actually evaluate would be lying.
    The runner topology is real; the cron algebra is deferred — and
    visibly so.
    """

    def next_after(self, *, cron_expr: str, after: datetime) -> datetime:
        return _FAR_FUTURE


class FixedIntervalScheduleAdvancer:
    """Advance ``next_run_at`` by a fixed ``timedelta`` after each fire.

    Useful for callers that *do* want recurrence but don't yet need full
    cron-expression semantics (e.g. "every 5 minutes regardless of
    wall-clock alignment"). Provided alongside
    :class:`OneShotScheduleAdvancer` so the in-process integration path
    the production daemon wires can pick a sensible default once the
    operator wires up a recurring schedule, without waiting for the
    full cron parser.
    """

    def __init__(self, *, interval: timedelta) -> None:
        self._interval = interval

    def next_after(self, *, cron_expr: str, after: datetime) -> datetime:
        return after + self._interval


class CronScheduleAdvancer:
    """Advance ``next_run_at`` to the next match of the row's cron expression.

    The real recurrence impl (S1): drops into the :class:`ScheduleAdvancer`
    Protocol with zero changes to the runner. Computes the first fire time
    strictly after ``after`` via the dependency-free
    :func:`~backend.schedule.domain.cron.next_cron_time` evaluator (standard 5
    fields, UTC — matching how :class:`OneShotScheduleAdvancer` treats the
    clock). This is what the production
    :class:`~backend.schedule.infrastructure.db_poll_runner.DbPollScheduleRunner`
    wires, so a row with ``cron_expr='0 9 * * 1'`` recurs every Monday 09:00
    instead of firing once.
    """

    def next_after(self, *, cron_expr: str, after: datetime) -> datetime:
        return next_cron_time(cron_expr, after)


__all__ = [
    "CronScheduleAdvancer",
    "FixedIntervalScheduleAdvancer",
    "OneShotScheduleAdvancer",
    "ScheduleAdvancer",
]
