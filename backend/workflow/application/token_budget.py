"""게이트 1 — per-run LLM token accounting + the runaway token ceiling.

``_drive_loop`` accumulates each turn's usage onto the run and, once the run
crosses ``agent_max_run_tokens``, stops on a founder Decision. Kept out of
``_drive_loop`` (the god-file cap, v8 §17.1) the way ``round_budget`` is: the
turn-count budget cannot bound a single huge turn, so this is the other half of
the runaway guard.

⚠️ **This is a safety ceiling, not a price lever** — but the sentence that used
to justify that here no longer holds. It read "BYO-key means the spend is the
founder's own provider bill, so the ceiling is a safety limit, not a price
lever", and the founder decided on 2026-09-14 that BSVibe opens to public
signup, which breaks the BYO-key premise. The conclusion survives the premise:
the free plan's price lever is ``workspaces.max_concurrent_runs`` and billing is
#928. Nothing in this module prices anything.

**This is one of TWO ceilings.** The other — the per-WORKSPACE cumulative budget
(``workspaces.monthly_token_budget``, #930 part 1) — lives in
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

if TYPE_CHECKING:
    from backend.workflow.application.agent_loop import LoopResult, LoopTurn, RunOrchestrator
    from backend.workflow.infrastructure.db import ExecutionRun, RunAttempt, WorkStep

TOKEN_CAP_DECISION_KIND = "run_token_cap_reached"  # noqa: S105 — decision kind, not a secret


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

    cap = orch._settings.agent_max_run_tokens
    used = run.usage_prompt_tokens + run.usage_completion_tokens
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
