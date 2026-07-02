"""The honesty ladder — grade a passing verdict by the STRENGTH of its evidence.

A "verified" that is honest must say not just *that* it passed but *how strongly*
(redesign SoT §4). Two passing runs are not equal: one whose finished deliverable
was exercised and observed to do the intended thing (I2) against the target's own
gate (I1) is far stronger evidence than one that only satisfied a fuzzy judge with
no gate to run at all.

The ladder (trust ∝ evidence):

- **A** — the deliverable was DEMONSTRATED (I2 observed the intended result) AND
  the target's own gate ran and passed (I1). Strongest, objective.
- **B** — one strong leg: the gate ran and passed, OR the outcome was
  demonstrated, but not both.
- **C** — a gate was discovered but could not run here (all steps unavailable in
  the isolated sandbox) and the outcome was not demonstrated — judgement-shaped,
  weak.
- **D** — no gate declared at all: the target has no definition of done, so
  "verified" rests on nothing runnable. Weakest; the founder should review it.

``None`` when the ladder does not apply — a non-product / non-worktree run (a
Direct-path scratch answer) has no repo gate concept to grade against.

Pure + offline: derive the grade from simple flags the verifier already computed;
the trust ratchet that consumes the grade (A/B/C auto-accumulate, D → founder
review) lives with the loop consumers, not here.
"""

from __future__ import annotations

from typing import Literal

HonestyGrade = Literal["A", "B", "C", "D"]


def compute_honesty_grade(
    *,
    applicable: bool,
    gate_passed: bool,
    gate_discovered: bool,
    demonstrated: bool,
) -> HonestyGrade | None:
    """Grade a PASSING verdict A–D by evidence strength (see module docstring).

    ``applicable`` — the run is a product run with a real worktree (the durable
    repo diff the ladder is about); ``False`` → ``None`` (ladder N/A).
    ``gate_passed`` — the target's own gate RAN and passed (I1). ``gate_discovered``
    — a gate was found even if it could not run here. ``demonstrated`` — the
    outcome demonstration observed the intended result (I2)."""
    if not applicable:
        return None
    if gate_passed and demonstrated:
        return "A"
    if gate_passed or demonstrated:
        return "B"
    if gate_discovered:
        return "C"
    return "D"


def needs_founder_review(grade: str | None, *, gate_expected: bool) -> bool:
    """True when a PASSING verdict must route to founder review instead of
    auto-accumulating trust (PROVED).

    Only grade **D** (no runnable gate + not demonstrated) is ever withheld — and
    even then, only when a gate was reasonably EXPECTED: the repo has a detectable
    stack, so it is a real project that *should* declare a definition of done but
    doesn't. That is the "couldn't verify" weakness worth a founder's eyes.

    An early / greenfield repo with **no detectable stack** (nothing to gate yet)
    is *legitimately* gateless — founder's distinction: "couldn't do it" vs
    "legitimately skipped for a valid reason". Its weak grade is still surfaced,
    but it auto-proceeds rather than nagging review on every early deliverable.

    ``None`` (ladder N/A — non-product / Direct run) never needs review here."""
    return grade == "D" and gate_expected


__all__ = [
    "HonestyGrade",
    "compute_honesty_grade",
    "needs_founder_review",
]
