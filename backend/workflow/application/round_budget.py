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

from backend.workflow.application.audit_events import RoundBudgetDeclared
from backend.workflow.infrastructure.db import ExecutionRun, RunAttempt

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
    "round_budget_cap",
    "round_budget_stats",
]
