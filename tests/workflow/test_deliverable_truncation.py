"""A deliverable must never quietly become a WRONG number.

Live 2026-08-10, BStockReport M5: the weekly report's deliverable was cut at
exactly 500 chars, mid-number, with nothing to say it had been —

    포지션: 포지션 4개 · 평가액 $1,005,189 · 현금 $922,0

``$922,010`` became ``$922,0``. In a financial report that is worse than
dropping the line: it MANUFACTURES a plausible wrong figure, and the anomaly
warning that followed it vanished without trace. The cap is right to exist (row
size), but a cap that is both too small for a real artifact and silent about
firing is a correctness bug, not a storage policy.
"""

from __future__ import annotations

import pytest

from backend.workflow.domain.verified_deliverable import (
    SETTLE_SUMMARY_CAP,
    capped_summary,
)


def test_a_report_sized_summary_survives_intact() -> None:
    """The cap must clear a real artifact. The M5 weekly report is ~700 chars of
    numbers before any commentary; 500 could not hold even the numbers."""
    report = "포지션: 평가액 $1,005,189 · 현금 $922,010\n" * 30
    assert len(report) > 500
    assert capped_summary(report) == report


def test_short_summary_is_returned_unchanged() -> None:
    assert capped_summary("한 줄 요약") == "한 줄 요약"


def test_over_cap_is_marked_never_silent() -> None:
    """Truncation still happens — but it ANNOUNCES itself, so a reader can tell
    a cut number from a real one."""
    huge = "가" * (SETTLE_SUMMARY_CAP + 500)

    out = capped_summary(huge, language="ko")

    assert len(out) > SETTLE_SUMMARY_CAP, "the notice is appended, not squeezed in"
    assert out.startswith("가" * 100)
    assert str(SETTLE_SUMMARY_CAP) in out and str(len(huge)) in out, "counts are stated"
    assert "잘렸" in out


def test_the_notice_is_localized_like_other_outbound_copy() -> None:
    """This lands in the founder's telegram, so it follows ``workspaces.language``
    (the static-catalog path used by product_tick / notification copy). English
    prose leaking into Korean outbound was a real complaint (PR #610)."""
    huge = "x" * (SETTLE_SUMMARY_CAP + 10)

    assert "truncated" in capped_summary(huge, language="en")
    assert "잘렸" in capped_summary(huge, language="ko")
    # Unknown tag falls back to English rather than emitting a raw key.
    assert "truncated" in capped_summary(huge, language="zz")


def test_boundary_exactly_at_the_cap_is_not_marked() -> None:
    exact = "y" * SETTLE_SUMMARY_CAP
    assert capped_summary(exact) == exact


@pytest.mark.parametrize("bad", ["", "   "])
def test_empty_summary_is_untouched(bad: str) -> None:
    assert capped_summary(bad) == bad
