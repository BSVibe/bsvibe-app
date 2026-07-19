"""Minimal, dependency-free standard 5-field cron evaluator.

The schedule runner asks the :class:`~backend.schedule.domain.advancer.CronScheduleAdvancer`
for the *next* fire time strictly after a given instant. That advancer delegates
the algebra here.

Why hand-rolled rather than ``croniter``: the S1 surface needs exactly two
operations — validate an expression at authoring time, and compute the next
match after an instant — over the standard 5 fields
(``minute hour day-of-month month day-of-week``) with ``*``, ``,`` lists, ``-``
ranges, and ``*/step`` steps. That is a small, fully-testable grammar; pulling a
dependency (plus a typing stub) for it would be more surface, not less. The
founder's canonical example — "매주 월요일" / "every Monday 09:00" → ``0 9 * * 1``
— is covered by the day-of-week handling + the standard cron OR-semantics
between day-of-month and day-of-week.

All computation is in UTC (naive-vs-aware handled by the caller); this matches
the existing :class:`~backend.schedule.domain.advancer.OneShotScheduleAdvancer`,
which treats the clock as UTC.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

# Standard cron field bounds. Day-of-week is parsed with an upper bound of 7
# (both 0 and 7 mean Sunday), then normalized in :func:`parse_cron`.
_MINUTE = (0, 59)
_HOUR = (0, 23)
_DOM = (1, 31)
_MONTH = (1, 12)

# A safety bound on the forward search — no standard 5-field expression can go
# more than ~5 years without matching (e.g. Feb 29 constraints), so a 4-year
# minute-by-minute ceiling (via day-stepping) is comfortably safe.
_MAX_SEARCH_DAYS = 366 * 5


class CronParseError(ValueError):
    """A cron expression is malformed (wrong field count / out-of-range / junk)."""


@dataclass(frozen=True)
class CronExpr:
    """A parsed standard 5-field cron expression (matchable sets per field)."""

    minutes: frozenset[int]
    hours: frozenset[int]
    days_of_month: frozenset[int]
    months: frozenset[int]
    days_of_week: frozenset[int]
    # Standard cron semantics: when BOTH day-of-month and day-of-week are
    # restricted (neither is ``*``), a datetime matches if EITHER matches.
    # When one is ``*`` the other is the sole gate (AND). We record whether
    # each field was the wildcard to reproduce that rule.
    dom_restricted: bool
    dow_restricted: bool


def _parse_field(spec: str, low: int, high: int, *, field: str) -> frozenset[int]:
    """Parse one cron field into the set of matching integers."""
    values: set[int] = set()
    for part in spec.split(","):
        if not part:
            raise CronParseError(f"empty term in {field!r} field")
        step = 1
        body = part
        if "/" in part:
            body, _, step_s = part.partition("/")
            if not step_s.isdigit() or int(step_s) < 1:
                raise CronParseError(f"invalid step {step_s!r} in {field!r} field")
            step = int(step_s)
        if body == "*":
            start, end = low, high
        elif "-" in body:
            start_s, _, end_s = body.partition("-")
            if not (start_s.isdigit() and end_s.isdigit()):
                raise CronParseError(f"invalid range {body!r} in {field!r} field")
            start, end = int(start_s), int(end_s)
        else:
            if not body.isdigit():
                raise CronParseError(f"invalid value {body!r} in {field!r} field")
            start = end = int(body)
        if start > end:
            raise CronParseError(f"reversed range {body!r} in {field!r} field")
        if start < low or end > high:
            raise CronParseError(f"{field!r} value out of range [{low},{high}]: {body!r}")
        values.update(range(start, end + 1, step))
    if not values:
        raise CronParseError(f"no values matched in {field!r} field")
    return frozenset(values)


def parse_cron(expr: str) -> CronExpr:
    """Parse a standard 5-field cron expression. Raises :class:`CronParseError`.

    Day-of-week accepts ``0``-``7`` where both ``0`` and ``7`` mean Sunday; ``7``
    is normalized to ``0`` so the match test is uniform.
    """
    if not isinstance(expr, str):
        raise CronParseError("cron expression must be a string")
    fields = expr.split()
    if len(fields) != 5:
        raise CronParseError(
            f"expected 5 cron fields (min hour dom month dow); got {len(fields)}: {expr!r}"
        )
    minute_s, hour_s, dom_s, month_s, dow_s = fields
    minutes = _parse_field(minute_s, *_MINUTE, field="minute")
    hours = _parse_field(hour_s, *_HOUR, field="hour")
    days_of_month = _parse_field(dom_s, *_DOM, field="day-of-month")
    months = _parse_field(month_s, *_MONTH, field="month")
    # Allow 7 as Sunday, then normalize to 0.
    raw_dow = _parse_field(dow_s, 0, 7, field="day-of-week")
    days_of_week = frozenset(0 if d == 7 else d for d in raw_dow)
    return CronExpr(
        minutes=minutes,
        hours=hours,
        days_of_month=days_of_month,
        months=months,
        days_of_week=days_of_week,
        dom_restricted=dom_s != "*",
        dow_restricted=dow_s != "*",
    )


def _day_matches(parsed: CronExpr, moment: datetime) -> bool:
    """Standard cron day match: DOM and DOW OR-combine when both restricted."""
    if moment.month not in parsed.months:
        return False
    # Python: Monday=0..Sunday=6; cron: Sunday=0..Saturday=6.
    cron_dow = (moment.weekday() + 1) % 7
    dom_ok = moment.day in parsed.days_of_month
    dow_ok = cron_dow in parsed.days_of_week
    if parsed.dom_restricted and parsed.dow_restricted:
        return dom_ok or dow_ok
    return dom_ok and dow_ok


def next_cron_time(expr: str, after: datetime) -> datetime:
    """Return the first UTC instant strictly after ``after`` matching ``expr``.

    ``after`` may be naive or tz-aware; the result is tz-aware UTC. The search
    steps minute-by-minute within a matching day and day-by-day otherwise, so it
    is bounded and cheap for standard expressions.
    """
    parsed = parse_cron(expr)
    if after.tzinfo is None:
        after = after.replace(tzinfo=UTC)
    else:
        after = after.astimezone(UTC)
    # Start at the next whole minute strictly after ``after``.
    candidate = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
    horizon = candidate + timedelta(days=_MAX_SEARCH_DAYS)
    while candidate <= horizon:
        if not _day_matches(parsed, candidate):
            # Jump to the start of the next day — no minute in this day matches.
            candidate = (candidate + timedelta(days=1)).replace(hour=0, minute=0)
            continue
        if candidate.hour in parsed.hours and candidate.minute in parsed.minutes:
            return candidate
        candidate += timedelta(minutes=1)
    raise CronParseError(f"no cron match within {_MAX_SEARCH_DAYS} days for {expr!r}")


__all__ = ["CronExpr", "CronParseError", "next_cron_time", "parse_cron"]
