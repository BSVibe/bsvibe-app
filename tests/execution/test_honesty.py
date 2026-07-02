"""Unit tests for the pure honesty ladder (redesign §4)."""

from __future__ import annotations

from backend.workflow.domain.honesty import compute_honesty_grade, is_auto_trusted


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


def test_is_auto_trusted() -> None:
    assert is_auto_trusted("A")
    assert is_auto_trusted("B")
    assert is_auto_trusted("C")
    assert not is_auto_trusted("D")  # only D is withheld from the ratchet
    assert is_auto_trusted(None)  # ladder N/A → governed by its own checks
