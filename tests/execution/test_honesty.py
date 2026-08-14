"""Unit tests for the pure honesty ladder (redesign §4)."""

from __future__ import annotations

from backend.workflow.domain.honesty import (
    compute_honesty_grade,
    needs_founder_review,
    work_is_gateable,
)


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


# ── the ratchet asks about the WORK, not the repo ────────────────────────────
#
# `gate_expected` was `manifest_present(repo)` — a REPO property deciding a
# judgement about a piece of WORK. So the same report, written in a repo that
# happens to carry a pyproject.toml, called the founder; written in one that
# doesn't, it didn't. Nothing about the deliverable's verifiability changed.
# The question the ratchet actually needs is: could THIS work have been gated?


def test_prose_only_work_could_not_have_been_gated() -> None:
    assert not work_is_gateable(["docs/plan.md", "notes/research.txt"])


def test_any_non_prose_file_makes_the_work_gateable() -> None:
    assert work_is_gateable(["docs/plan.md", "backend/thing.py"])
    # Unknown extensions count as gateable — fail-CLOSED. A config, a schema, a
    # dockerfile can all be checked by a command; only a shape we KNOW is prose
    # buys the exemption.
    assert work_is_gateable(["deploy/compose.yaml"])
    assert work_is_gateable(["Makefile"])


def test_a_run_that_produced_nothing_is_not_exempt() -> None:
    # "Wrote no files" must NOT read as "legitimately gateless" — that is the
    # prose-answer-instead-of-work case, and it keeps calling the founder.
    assert work_is_gateable([])


def test_review_is_withheld_for_prose_work_even_in_a_manifest_repo() -> None:
    # The whole point: a real project's repo (gate_expected=True) no longer
    # forces review on a deliverable no command could have verified.
    assert not needs_founder_review("D", gate_expected=True, work_gateable=False)
    # …while code work in that same repo still routes to review on grade D.
    assert needs_founder_review("D", gate_expected=True, work_gateable=True)


def test_a_gateless_repo_stays_exempt_regardless_of_the_work() -> None:
    assert not needs_founder_review("D", gate_expected=False, work_gateable=True)
