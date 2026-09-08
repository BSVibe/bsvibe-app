"""Round-cap-reached outcome choice — ask WHAT TO BUILD, never WHY it failed.

Founder ruling 2026-09-08: the ``round_cap_reached`` question this loop used to raise
(a ``verification_failed`` Decision, see ``_checkpoint_shared.py``) asked the founder to
judge internal facts they never observed — "is this possible as scoped, or does the
approach need to change?" — in BSVibe's own words ("budget", "rounds", "approach") the
founder never used. Diagnosing a stuck run is BSVibe's job, not the founder's; the founder
should only ever be asked to pick a DELIVERABLE outcome.

This module makes that diagnosis itself, from the run's own measured ``VerificationResult``
history, and reduces it to a plain choice between concrete outcomes — never a "why":

* the run failed on several DIFFERENT things across its attempts (a broad scope) — offer
  to ship what's done and keep going on the rest, or split the work differently.
* the run failed on the SAME thing every attempt (one blocker) — offer to skip that part
  and ship the rest, or keep going as-is.
* neither is measurable (nothing written AND no verification attempt happened at all) —
  :func:`round_cap_outcome_choice` returns ``None`` and the caller keeps today's plain
  Decision (reworded separately, without the same jargon, in ``_checkpoint_shared.py``).
  An empty or invented menu would be worse than that honest fallback.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.workflow.infrastructure.db import (
    ExecutionRun,
    VerificationOutcome,
    VerificationResult,
)


def _failing_signature(result: Mapping[str, Any]) -> frozenset[str] | None:
    """What is CURRENTLY failing in one verification attempt, as a set of stable
    identifiers — command strings, or a fixed marker for a non-command surface.

    Mirrors ``verification_feedback._sections``'s authority order but returns IDENTITY,
    not rendered text: this only needs to tell "the same thing failed again" apart from
    "a different thing failed this time", never to explain the failure to anyone. Returns
    ``None`` when nothing failed here is identifiable (an empty/malformed result) — that
    attempt then contributes no signal either way, rather than a false "nothing changed".
    """
    names: set[str] = set()
    gate = result.get("derived_gate")
    if isinstance(gate, Mapping):
        for cmd in gate.get("commands") or []:
            if isinstance(cmd, Mapping) and (cmd.get("status") == "failed" or cmd.get("timed_out")):
                names.add(str(cmd.get("command") or "gate"))
    for cmd in result.get("command_results") or []:
        if isinstance(cmd, Mapping) and (cmd.get("passed") is False or cmd.get("timed_out")):
            names.add(str(cmd.get("command") or "declared"))
    demo = result.get("outcome_demonstration")
    if isinstance(demo, Mapping) and demo.get("verdict") == "failed":
        names.add("outcome_demonstration")
    judge = result.get("judge")
    if isinstance(judge, Mapping) and judge.get("passed") is False:
        names.add("judge")
    scope = result.get("scope")
    if isinstance(scope, Mapping) and scope.get("flagged_paths"):
        names.add("scope")
    return frozenset(names) or None


# Every line below is deliberately free of the words a founder never chose — no
# "budget", "round(s)", "attempt N", "approach", "verify/verification", "ceiling",
# "reviewer" (en) / 예산·라운드·시도·접근·검증·천장·검토자 (ko). Each pair is
# (question, options) — a choice between OUTCOMES, never a diagnosis.
_TOO_BIG_KO = (
    "지금까지 일부는 만들어졌고 나머지는 아직이에요 — 어떻게 할까요?",
    (
        "지금까지 만든 것까지만 완성해서 보여줘",
        "나머지도 계속 만들어줘",
        "다르게 나눠서 알려줄게",
    ),
)
_TOO_BIG_EN = (
    "Some of this is built and the rest isn't yet — how should it proceed?",
    (
        "Finish just what's built so far",
        "Keep going on the rest too",
        "Let me split it differently",
    ),
)
_STUCK_KO = (
    "한 부분에서 계속 막히고 있어요 — 어떻게 할까요?",
    (
        "그 부분은 빼고 나머지부터 보여줘",
        "그대로 계속 진행해줘",
        "다르게 알려줄게",
    ),
)
_STUCK_EN = (
    "One part keeps blocking this — how should it proceed?",
    (
        "Skip that part and show me the rest first",
        "Keep going as is",
        "Let me tell you something different",
    ),
)
_GENERIC_KO = (
    "지금까지 만든 걸 어떻게 할까요?",
    (
        "지금까지 만든 것 보여줘",
        "계속 진행해줘",
        "다르게 알려줄게",
    ),
)
_GENERIC_EN = (
    "What should happen with what's been built so far?",
    (
        "Show me what's been built so far",
        "Keep going",
        "Let me tell you something different",
    ),
)


async def round_cap_outcome_choice(
    session: AsyncSession, run: ExecutionRun, *, written_paths: list[str], language: str
) -> tuple[str, list[str]] | None:
    """The ``(question, options)`` pair for a round-cap-reached Decision, or ``None``
    when nothing measurable supports one — the caller then falls back to the plain
    Decision.

    Classification, in order (each branch requires only what it actually measures):

    1. Two or more DISTINCT failing signatures across this run's failed verification
       attempts → the run touched several different problems → "too big" framing.
    2. Exactly one distinct failing signature (even from a single failed attempt) →
       the run kept hitting the SAME thing → "stuck on one part" framing.
    3. No signature could be extracted at all, but the agent DID write something → a
       generic "what should happen with it" framing — honest about not knowing WHY,
       but still grounded in the one measured fact (files were touched).
    4. Nothing written and no verification history → ``None``: there is nothing this
       function can honestly base a choice on.
    """
    rows = list(
        (
            await session.execute(
                select(VerificationResult)
                .where(VerificationResult.run_id == run.id)
                .order_by(VerificationResult.created_at.asc())
            )
        )
        .scalars()
        .all()
    )
    failed = [r for r in rows if r.outcome is VerificationOutcome.FAILED]
    signatures = [
        sig
        for row in failed
        if (sig := _failing_signature(row.result if isinstance(row.result, Mapping) else {}))
        is not None
    ]
    distinct = set(signatures)
    ko = language == "ko"

    if len(distinct) >= 2:
        question, options = _TOO_BIG_KO if ko else _TOO_BIG_EN
        return question, list(options)
    if len(distinct) == 1:
        question, options = _STUCK_KO if ko else _STUCK_EN
        return question, list(options)
    if written_paths:
        question, options = _GENERIC_KO if ko else _GENERIC_EN
        return question, list(options)
    return None


__all__ = ["round_cap_outcome_choice"]
