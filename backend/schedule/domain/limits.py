"""Schedule limits as pure functions — one definition for the authoring check, the list
flags and the worker warning.

Existing schedules that violate a limit are never switched off (they are user data); the
limits are only *reported* for them (``below_minimum_interval`` / ``over_workspace_limit``).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import Protocol

from backend.schedule.domain.cron import CronParseError, next_cron_time

#: How many consecutive fires are sampled to find a cron's tightest gap (covers the
#: wrap-around of patterns like ``*/7`` whose smallest gap is at the hour boundary).
INTERVAL_SAMPLE = 120


class _LimitRow(Protocol):
    id: uuid.UUID
    enabled: bool
    cron_expr: str


def tightest_gap_below(cron_expr: str, after: datetime, min_minutes: int) -> int | None:
    """Minutes between the first two consecutive fires closer than ``min_minutes``, else None.

    An unparseable cron or one with no further match is not "too often" → None."""
    limit = timedelta(minutes=min_minutes)
    try:
        previous = next_cron_time(cron_expr, after)
        for _ in range(INTERVAL_SAMPLE):
            following = next_cron_time(cron_expr, previous)
            if following - previous < limit:
                return int((following - previous).total_seconds() // 60)
            previous = following
    except CronParseError:
        return None
    return None


def over_limit_ids(rows: Iterable[_LimitRow], cap: int) -> set[uuid.UUID]:
    """Ids of enabled schedules beyond the first ``cap`` active ones.

    ``rows`` must be ordered oldest first, so the newest active schedules are the ones
    reported as over the limit."""
    active = [r.id for r in rows if r.enabled]
    return set(active[cap:])
