"""The per-run round budget an agent may declare via ``declare_verification``.

Split out of ``_drive_loop.py`` for the same reason as ``undeclared_verification.py``:
that module is kept under the 600 LOC v8 §17.1 ceiling, and this concern — computing the
run's effective cap and making a (re-)declaration observable — is self-contained.

The ceiling (``orch._max_cycles`` — the global ``execution_work_round_budget`` default, or
an explicit caller/test override) never disappears: a declared ``round_budget`` only ever
NARROWS it (``min(declared, ceiling)``), never widens it. An agent that never declares one
gets today's behaviour unchanged.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from backend.identity.workspaces_db import load_workspace_language
from backend.workflow.application.audit_events import DecisionPending, RoundBudgetDeclared
from backend.workflow.domain.round_cap_outcome import round_cap_outcome_choice
from backend.workflow.infrastructure.db import Decision, ExecutionRun, RunAttempt, WorkStep

if TYPE_CHECKING:
    from backend.workflow.application.agent_loop import RunOrchestrator


def round_budget_stats(orch: RunOrchestrator, registry: Any, cycles_used: int) -> dict[str, int]:
    """Declared / used / ceiling, as plain SYSTEM-recorded ints — never LLM prose.

    ``declared`` falls back to the ceiling when the agent never called
    ``declare_verification`` with a ``round_budget``: the run then always ran at the
    ceiling, so reporting the ceiling as what was "declared" describes what actually
    happened, not a null. This fallback is load-bearing for the round-cap Decision
    rationale, which reads ONLY ``stats["declared"]``/``stats["used"]`` and must keep
    reading the same numbers it always has — do not change it.

    ``declared_explicitly`` is the separate bit the settle record's *estimation* use
    needs, and is the reason this function exists: True only when the agent actually
    called ``declare_verification`` with a ``round_budget`` this run. Without it, a
    never-declared run (fallback to ceiling) and a run that explicitly declared the
    ceiling's exact value are indistinguishable in the settle payload, so a future
    "how many rounds does this kind of work usually take" query would silently average
    in guesses nobody made. Same rule as ``capture_run_changed_paths``'s ``None`` (missing
    value) vs ``[]`` (an answer) split — keep "no estimate" and "estimated the ceiling"
    apart here too.
    """
    declared = getattr(registry, "declared_round_budget", None)
    return {
        "declared": declared if declared is not None else orch._max_cycles,
        "declared_explicitly": declared is not None,
        "used": cycles_used,
        "ceiling": orch._max_cycles,
    }


def round_budget_cap(orch: RunOrchestrator, registry: Any) -> int:
    """This run's EFFECTIVE round cap for the current cycle — re-evaluated every
    iteration, since a re-declaration can change it mid-run."""
    declared: int | None = getattr(registry, "declared_round_budget", None)
    if declared is None:
        return orch._max_cycles
    return min(declared, orch._max_cycles)


async def announce_round_budget_change(
    orch: RunOrchestrator,
    run: ExecutionRun,
    attempt: RunAttempt,
    *,
    previous: int | None,
    declared: int,
) -> None:
    """Make a (re-)declared round budget an OBSERVABLE event — not a silent state change.

    Fires once per VALUE the agent actually declares (first declaration, or any
    re-declaration that changes the number), whether the change came from the in-process
    tool call or was learned back from the MCP transport's per-run state — both paths
    funnel through the same call site in ``_drive_loop``, so an executor-driven run gets
    the same observability as the native loop.
    """
    ceiling = orch._max_cycles
    effective = min(declared, ceiling)
    clamped = declared > ceiling
    payload = {
        "declared": declared,
        "previous": previous,
        "ceiling": ceiling,
        "effective": effective,
        "clamped": clamped,
    }
    await orch._record(run, attempt, "round_budget_declared", payload)
    await orch._audit(run, attempt, RoundBudgetDeclared, payload)


async def attempt_round_budget_lift(
    orch: RunOrchestrator,
    run: ExecutionRun,
    attempt: RunAttempt,
    registry: Any,
    tracker: RoundBudgetTracker,
    messages: list[dict[str, Any]],
) -> bool:
    """A declared (< ceiling) round_budget just ran out — the loop's OWN recovery, not a
    founder Decision (founder ruling 2026-09-07: a round-cap exhaustion is an INTERNAL
    failure, not a bad request, so it must not page the founder while the run still has
    ceiling left to spend). Gets one review of the run's failure history from
    ``request_review``'s existing handler — the loop calls it directly, bypassing the
    tool-call plumbing entirely, because the same prod run that proved this ruling also
    proved the agent will NOT call the tool itself even with it on the menu and even
    having just edited that tool's own code — then raises the declared budget straight
    to the ceiling so the cycle keeps going.

    Returns False, telling the caller to fall through to the existing
    ``round_cap_reached`` Decision, in exactly two cases: the declared budget already IS
    the (clamped) ceiling — a genuine ceiling exhaustion, unrelated to this feature — or
    this run's ``request_review`` call budget (``MAX_STUCK_REVIEWS_PER_RUN``) is already
    spent. The second case escalates rather than raising the budget UNREVIEWED: an
    unreviewed raise cannot be told apart from a silent unbounded extension, so once a
    real second opinion is no longer available the founder is the right next stop — the
    same place the tool's own hint sends an agent whose budget is spent.
    """
    from backend.workflow.domain import request_review as _review  # noqa: PLC0415

    ceiling = orch._max_cycles
    declared = getattr(registry, "declared_round_budget", None)
    if declared is None or declared >= ceiling:
        return False
    used = int((run.payload or {}).get(_review.STUCK_REVIEW_STATE_KEY, 0))
    if used >= _review.MAX_STUCK_REVIEWS_PER_RUN:
        return False
    feedback = await _review.handle_request_review(orch._session, run, orch._llm, {})
    messages.append(
        {
            "role": "user",
            "content": (
                f"Your declared round_budget ({declared}) was reached before "
                f"verification passed. Raising it to this run's ceiling ({ceiling}) "
                "instead of stopping here — a reviewer with no attachment to your prior "
                f"attempts read the full failure history first:\n\n{feedback}"
            ),
        }
    )
    registry.declared_round_budget = ceiling
    await tracker.sync(orch, run, attempt, registry)
    return True


async def should_continue_round(
    orch: RunOrchestrator,
    run: ExecutionRun,
    attempt: RunAttempt,
    registry: Any,
    tracker: RoundBudgetTracker,
    messages: list[dict[str, Any]],
    cycle: int,
) -> bool:
    """``drive_loop``'s own while-condition. Bounded by construction, not by a counter:
    a lift only ever raises the declared budget up to the ceiling (never past it, see
    ``attempt_round_budget_lift``), and ``_cycle`` in the caller's own loop keeps counting
    against that SAME ceiling regardless of how many lifts happen — once ``_cycle`` reaches
    it, ``round_budget_cap`` returns that ceiling too, so a further lift is a no-op
    (``declared >= ceiling`` short-circuits ``attempt_round_budget_lift`` to False) and the
    loop falls through to the ``round_cap_reached`` Decision. This is NOT because every
    lift spends one of ``request_review``'s own per-run call slots: a lift on a run with NO
    verification history yet costs no slot at all — ``handle_request_review`` returns its
    ``_NO_HISTORY_MESSAGE`` and returns BEFORE incrementing ``STUCK_REVIEW_STATE_KEY`` (see
    ``backend.workflow.domain.request_review``) — so an early, history-less lift is free.
    The real, always-true bound is the ceiling on ``_cycle`` itself.
    """
    if cycle < round_budget_cap(orch, registry):
        return True
    return await attempt_round_budget_lift(orch, run, attempt, registry, tracker, messages)


async def finalize_round_cap_decision(
    orch: RunOrchestrator,
    *,
    run: ExecutionRun,
    work_step: WorkStep,
    attempt: RunAttempt,
    written_paths: list[str],
    stats: dict[str, int],
) -> Decision:
    """The Decision ``drive_loop`` raises once the round cap is reached with no
    passing verdict — the founder ruling 2026-09-08 half of this feature.

    Prefers an ``ask_user_question`` Decision carrying a grounded DELIVERABLE choice
    (built by :func:`~backend.workflow.domain.round_cap_outcome.round_cap_outcome_choice`
    from the run's own ``VerificationResult`` history) over the plain
    ``verification_failed`` Decision this used to always raise — the founder is asked
    what to build, never why it failed. Falls back to the plain Decision, reworded but
    otherwise unchanged, only when nothing measurable supports a choice.
    """
    rationale = (
        f"agent loop exhausted its round budget (declared {stats['declared']}, "
        f"used {stats['used']}) without a passing verification"
    )
    base_payload: dict[str, Any] = {
        "reason": "round_cap_reached",
        "written_paths": written_paths,
        "round_budget_declared": stats["declared"],
        "round_budget_used": stats["used"],
    }
    language = await load_workspace_language(orch._session, run.workspace_id)
    choice = await round_cap_outcome_choice(
        orch._session, run, written_paths=written_paths, language=language
    )
    if choice is not None:
        question, options = choice
        decision = await orch._create_decision(
            run,
            work_step,
            kind="ask_user_question",
            payload={**base_payload, "question": question, "options": options},
            rationale=rationale,
        )
        audit_kind = "ask_user_question"
    else:
        decision = await orch._create_decision(
            run, work_step, kind="verification_failed", payload=base_payload, rationale=rationale
        )
        audit_kind = "verification_failed"
    await orch._audit(
        run,
        attempt,
        DecisionPending,
        {
            "kind": audit_kind,
            "decision_id": str(decision.id),
            "reason": "round_cap_reached",
            "round_budget_declared": stats["declared"],
            "round_budget_used": stats["used"],
        },
    )
    return decision  # type: ignore[no-any-return]


class RoundBudgetTracker:
    """Fires :func:`announce_round_budget_change` exactly once per VALUE the agent
    declares this run — whether learned from the native path's own tool call or synced
    back from the MCP transport's per-run state. A value re-seen (a redundant sync, or a
    re-declare that repeats the same number) does not re-fire.
    """

    def __init__(self) -> None:
        self._last_seen: int | None = None

    async def sync(
        self, orch: RunOrchestrator, run: ExecutionRun, attempt: RunAttempt, registry: Any
    ) -> None:
        current = getattr(registry, "declared_round_budget", None)
        if current is not None and current != self._last_seen:
            await announce_round_budget_change(
                orch, run, attempt, previous=self._last_seen, declared=current
            )
            self._last_seen = current


__all__ = [
    "RoundBudgetTracker",
    "announce_round_budget_change",
    "attempt_round_budget_lift",
    "finalize_round_cap_decision",
    "round_budget_cap",
    "round_budget_stats",
    "should_continue_round",
]
