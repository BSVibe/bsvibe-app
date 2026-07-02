"""Unit tests for the pure honesty ladder (redesign §4)."""

from __future__ import annotations

from backend.workflow.domain.honesty import compute_honesty_grade, needs_founder_review


def _grade(**kw) -> str | None:
    base = dict(applicable=True, gate_passed=False, gate_discovered=False, demonstrated=False)
    base.update(kw)
    return compute_honesty_grade(**base)  # type: ignore[arg-type]


def test_grade_a_demonstrated_and_gate() -> None:
    assert _grade(gate_passed=True, gate_discovered=True, demonstrated=True) == "A"


def test_grade_b_gate_only() -> None:
    assert _grade(gate_passed=True, gate_discovered=True) == "B"


def test_grade_b_demonstrated_only() -> None:
    # A strong observation leg even without a runnable gate is still B.
    assert _grade(demonstrated=True) == "B"


def test_grade_c_gate_discovered_but_not_runnable() -> None:
    # A gate exists but every step was unavailable in the sandbox, and the
    # outcome wasn't demonstrated → judgement-shaped, weak.
    assert _grade(gate_discovered=True) == "C"


def test_grade_d_no_gate_declared() -> None:
    assert _grade() == "D"


def test_grade_none_when_not_applicable() -> None:
    # Non-product / Direct run — the repo-gate ladder does not apply.
    assert _grade(applicable=False, gate_passed=True, demonstrated=True) is None


def test_needs_review_only_grade_d_with_expected_gate() -> None:
    # Grade D + a gate was expected (real project, has a stack) → review.
    assert needs_founder_review("D", gate_expected=True)
    # Grade D but NO gate expected (early/greenfield, no stack yet) → legitimate
    # skip, auto-proceed (founder: distinguish "couldn't" from "skipped").
    assert not needs_founder_review("D", gate_expected=False)


def test_needs_review_false_for_strong_grades_and_none() -> None:
    for g in ("A", "B", "C", None):
        assert not needs_founder_review(g, gate_expected=True)
        assert not needs_founder_review(g, gate_expected=False)
