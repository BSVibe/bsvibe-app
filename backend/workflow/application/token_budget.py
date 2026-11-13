"""게이트 1 — per-run LLM token accounting + the runaway token ceiling.

``_drive_loop`` accumulates each turn's usage onto the run and, once the run
crosses ``agent_max_run_tokens``, stops on a founder Decision. Kept out of
``_drive_loop`` (the god-file cap, v8 §17.1) the way ``round_budget`` is: the
turn-count budget cannot bound a single huge turn, so this is the other half of
the runaway guard.

**Who this protects.** Model tokens are BYO (founder decision 2026-10-16): the workspace
pays its own provider, or runs a worker on its own subscription. The platform has no token cost
to guard, so this per-run ceiling exists to protect the USER from a runaway — a run stuck in a
loop would otherwise burn their key's bill or their subscription limit. Nothing in this module
prices anything.

**This is one of TWO ceilings.** The other — the per-WORKSPACE cumulative budget
(``workspaces.monthly_token_budget``, #930 part 1; a spend ceiling the user chooses,
unlimited by default) — lives in
:mod:`backend.workflow.application.workspace_token_budget`, enforced at
ADMISSION rather than mid-run. That module carries the argument for its window,
its moment and its refusal shape. The two are deliberately INDEPENDENT: setting
``agent_max_run_tokens = 0`` must not switch the workspace budget off, and a
workspace over its budget must still get this ceiling on the runs it is already
driving (both directions are pinned by tests).

It lives in its own module and not here because this one is typed against
``RunOrchestrator``, so it carries a ``TYPE_CHECKING`` import of ``agent_loop``
— and import-linter counts ``TYPE_CHECKING`` edges. Every importer of this
module therefore inherits the whole loop graph, and the workspace budget's
callers include ``backend.mcp``, which is contractually forbidden it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from backend.workflow.application.audit_events import LoopTerminal
from backend.workflow.domain import conflict_budget as cb
from backend.workflow.domain.run_token_cap import (
    TOKEN_CAP_DECISION_KIND,
    effective_run_token_cap,
)

if TYPE_CHECKING:
    from backend.workflow.application.agent_loop import LoopResult, LoopTurn, RunOrchestrator
    from backend.workflow.infrastructure.db import ExecutionRun, RunAttempt, WorkStep


async def account_and_enforce_token_cap(
    orch: RunOrchestrator,
    *,
    run: ExecutionRun,
    work_step: WorkStep,
    attempt: RunAttempt,
    turn: LoopTurn,
    written_paths: list[str],
    final_text: str,
) -> LoopResult | None:
    """Add ``turn``'s tokens to the run, then enforce the ceiling.

    Returns a terminal ``LoopResult`` when the run crossed the cap (the caller
    must return it), else ``None`` to keep driving. ``agent_max_run_tokens == 0``
    disables the ceiling (accounting still runs).
    """
    run.usage_prompt_tokens += turn.usage_prompt_tokens
    run.usage_completion_tokens += turn.usage_completion_tokens

    used = run.usage_prompt_tokens + run.usage_completion_tokens

    # Merge-conflict resolution has its OWN, smaller ceiling: spending the whole run cap on a
    # conflict nobody resolved is what this guards. Reaching it hands the run to the founder
    # as ``merge_conflict_review`` (with what was tried), not as ``run_token_cap_reached``.
    budget = cb.read_budget(run.payload)
    ceiling = budget.get("token_ceiling") if cb.is_resolving(run.payload) else None
    run_cap = effective_run_token_cap(orch._settings.agent_max_run_tokens, run.payload)
    if isinstance(ceiling, int) and ceiling > 0 and run_cap > 0:
        ceiling = min(ceiling, run_cap)  # never larger than the run's own cap
    if isinstance(ceiling, int) and ceiling > 0 and used >= ceiling:
        await orch._audit(
            run,
            attempt,
            LoopTerminal,
            {"outcome": "conflict_token_budget_reached", "tokens": used, "ceiling": ceiling},
        )
        # Mark the budget spent: the merge-watch row stays NEEDS_RESOLUTION, so without the
        # mark the worker's next re-dispatch would re-open the run under the pending review
        # with no conflict ceiling. A founder retry clears the whole budget (and the mark).
        spent = dict(budget)
        spent[cb.EXHAUSTED_KEY] = "token_limit"
        payload = dict(run.payload or {})
        payload[cb.CONFLICT_BUDGET_KEY] = spent
        payload.pop("merge_conflict", None)
        payload.pop("merge_conflict_resolving", None)
        run.payload = payload
        decision = await orch._create_decision(
            run,
            work_step,
            kind="merge_conflict_review",
            payload={
                "reason": "conflict_budget_exhausted",
                "written_paths": written_paths,
                **cb.summarize(budget, used_tokens=used, reason="token_limit"),
            },
            rationale=(
                f"merge conflict resolution used {used - int(budget.get('tokens_at_start', used))} "
                f"tokens, reaching its budget; stopped for founder review"
            ),
        )
        await orch._session.commit()
        return orch._decision_result(run, work_step, attempt, decision, written_paths, final_text)

    # #1105 — a founder's "keep going" raises THIS run's ceiling.
    cap = effective_run_token_cap(orch._settings.agent_max_run_tokens, run.payload)
    if cap <= 0 or used < cap:
        return None

    await orch._audit(
        run, attempt, LoopTerminal, {"outcome": "token_cap_reached", "tokens": used, "cap": cap}
    )
    decision = await orch._create_decision(
        run,
        work_step,
        kind=TOKEN_CAP_DECISION_KIND,
        payload={
            "reason": TOKEN_CAP_DECISION_KIND,
            "usage_total_tokens": used,
            "usage_prompt_tokens": run.usage_prompt_tokens,
            "usage_completion_tokens": run.usage_completion_tokens,
            "token_cap": cap,
            "written_paths": written_paths,
        },
        rationale=(
            f"run consumed {used} LLM tokens, reaching the per-run ceiling of "
            f"{cap}; stopped for founder review"
        ),
    )
    await orch._session.commit()
    return orch._decision_result(run, work_step, attempt, decision, written_paths, final_text)


__all__ = ["TOKEN_CAP_DECISION_KIND", "account_and_enforce_token_cap"]
