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
    judge_indeterminate: bool = False,
) -> HonestyGrade | None:
    """Grade a PASSING verdict A–D by evidence strength (see module docstring).

    ``applicable`` — the run is a product run with a real worktree (the durable
    repo diff the ladder is about); ``False`` → ``None`` (ladder N/A).
    ``gate_passed`` — the target's own gate RAN and passed (I1). ``gate_discovered``
    — a gate was found even if it could not run here. ``demonstrated`` — the
    outcome demonstration observed the intended result (I2).
    judge_indeterminate — the LLM judge declared it could not see the relevant
    code (cannot_determine); the run still passes (command evidence stands) but
    the maximum attainable grade is B, never A, since the judge verification was
    incomplete."""
    if not applicable:
        return None
    if gate_passed and demonstrated:
        grade: HonestyGrade = "A"
    elif gate_passed or demonstrated:
        grade = "B"
    elif gate_discovered:
        grade = "C"
    else:
        grade = "D"
    if judge_indeterminate and grade == "A":
        grade = "B"
    return grade


#: File shapes no command can meaningfully gate — the deliverable IS the prose.
#: Deliberately a small, closed list: anything not named here counts as gateable
#: (fail-CLOSED), because a config, a schema, a Dockerfile can all be checked by
#: some command and only a shape we KNOW is prose should buy the exemption.
_PROSE_EXTS: frozenset[str] = frozenset({".md", ".txt", ".rst", ".adoc", ".markdown"})


def work_is_gateable(written_paths: list[str]) -> bool:
    """Could a command have verified what THIS work produced?

    The grade is a judgement about a piece of work, so the question that decides
    whether a weak grade is worth the founder's eyes has to be about the work —
    not about the repo it landed in. ``gate_expected`` answers "is this a real
    project that should declare a definition of done", which is the right
    question for the fail-CLOSED gate path and the WRONG one here: it made the
    same report call the founder or not depending on whether the repo happened
    to carry a ``pyproject.toml``.

    A run that produced NOTHING is gateable by this definition — deliberately.
    "Wrote no files" must never read as "legitimately gateless": that is the
    prose-answer-instead-of-work case, and it keeps its review."""
    return not written_paths or any(not _is_prose(p) for p in written_paths)


def _is_prose(path: str) -> bool:
    dot = path.rfind(".")
    return dot != -1 and path[dot:].lower() in _PROSE_EXTS


def needs_founder_review(
    grade: str | None, *, gate_expected: bool, work_gateable: bool = True
) -> bool:
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

    ``work_gateable`` (:func:`work_is_gateable`) applies that SAME distinction to
    the work instead of the repo. A report, a plan, a piece of research is
    legitimately gateless no matter how well-equipped its repo is: there is no
    command to run, so "couldn't verify" is not a weakness of the work. Without
    this, every non-dev deliverable in a real project called the founder — the
    complaint that opened this track.

    ``None`` (ladder N/A — non-product / Direct run) never needs review here."""
    return grade == "D" and gate_expected and work_gateable


__all__ = [
    "HonestyGrade",
    "compute_honesty_grade",
    "needs_founder_review",
    "work_is_gateable",
]
