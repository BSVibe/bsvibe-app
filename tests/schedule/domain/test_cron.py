"""Standard 5-field cron evaluator + CronScheduleAdvancer unit tests."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from backend.schedule.domain.advancer import CronScheduleAdvancer
from backend.schedule.domain.cron import CronParseError, next_cron_time, parse_cron


def test_every_monday_0900_is_next_monday() -> None:
    # 2026-07-22 is a Wednesday; next Monday 09:00 UTC is 2026-07-27.
    after = datetime(2026, 7, 22, 10, 0, tzinfo=UTC)
    nxt = next_cron_time("0 9 * * 1", after)
    assert nxt == datetime(2026, 7, 27, 9, 0, tzinfo=UTC)
    assert nxt.weekday() == 0  # Monday


def test_every_monday_advancer_matches_next_cron_time() -> None:
    advancer = CronScheduleAdvancer()
    after = datetime(2026, 7, 27, 9, 0, tzinfo=UTC)  # a Monday 09:00
    nxt = advancer.next_after(cron_expr="0 9 * * 1", after=after)
    # Strictly after ⇒ the FOLLOWING Monday, not the same instant.
    assert nxt == datetime(2026, 8, 3, 9, 0, tzinfo=UTC)


def test_step_expression_advances_by_step() -> None:
    after = datetime(2026, 7, 22, 10, 2, tzinfo=UTC)
    assert next_cron_time("*/5 * * * *", after) == datetime(2026, 7, 22, 10, 5, tzinfo=UTC)


def test_sunday_accepts_both_0_and_7() -> None:
    after = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)  # Wednesday
    via0 = next_cron_time("0 0 * * 0", after)
    via7 = next_cron_time("0 0 * * 7", after)
    assert via0 == via7
    assert via0.weekday() == 6  # Sunday


def test_dom_and_dow_or_semantics() -> None:
    # "0 0 13 * 5" fires on the 13th OR any Friday (standard cron OR-rule when
    # both DOM and DOW are restricted). From Wed 2026-07-22, the next Friday
    # (07-24) comes before the 13th.
    after = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
    nxt = next_cron_time("0 0 13 * 5", after)
    assert nxt == datetime(2026, 7, 24, 0, 0, tzinfo=UTC)  # Friday


def test_range_and_list_fields() -> None:
    after = datetime(2026, 7, 22, 8, 30, tzinfo=UTC)
    # minutes 0 and 30, hours 9-10
    nxt = next_cron_time("0,30 9-10 * * *", after)
    assert nxt == datetime(2026, 7, 22, 9, 0, tzinfo=UTC)


def test_result_is_strictly_after_input() -> None:
    after = datetime(2026, 7, 22, 9, 0, 0, tzinfo=UTC)
    nxt = next_cron_time("0 9 * * *", after)  # daily 09:00
    assert nxt == datetime(2026, 7, 23, 9, 0, tzinfo=UTC)


def test_naive_input_treated_as_utc() -> None:
    after = datetime(2026, 7, 22, 10, 0)  # naive
    nxt = next_cron_time("*/5 * * * *", after)
    assert nxt.tzinfo is not None
    assert nxt == datetime(2026, 7, 22, 10, 5, tzinfo=UTC)


@pytest.mark.parametrize(
    "expr",
    [
        "not a cron",  # wrong field count
        "* * * *",  # 4 fields
        "60 * * * *",  # minute out of range
        "* 24 * * *",  # hour out of range
        "* * 0 * *",  # dom below range
        "* * * 13 *",  # month out of range
        "*/0 * * * *",  # zero step
        "5-1 * * * *",  # reversed range
        "abc * * * *",  # junk value
    ],
)
def test_invalid_expressions_rejected(expr: str) -> None:
    with pytest.raises(CronParseError):
        parse_cron(expr)


def test_dst_boundary_uses_utc_no_gap() -> None:
    # UTC has no DST, so a daily 02:30 schedule around a US spring-forward date
    # (2026-03-08) still lands at exactly 02:30 UTC each day — no skipped hour.
    after = datetime(2026, 3, 8, 1, 0, tzinfo=UTC)
    nxt = next_cron_time("30 2 * * *", after)
    assert nxt == datetime(2026, 3, 8, 2, 30, tzinfo=UTC)
