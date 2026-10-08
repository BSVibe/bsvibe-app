"""One turn of the drive loop — or ``None`` when the run was cancelled.

Split out of ``_drive_loop`` (v8 §17.1 600-LOC ceiling) when #1106 gave a turn a
second way to end in cancel. Both live here so they cannot drift apart:

* at the turn boundary — the run was cancelled between turns (dogfood dd2bd3a3);
* mid-turn — the adapter saw the cancel while the executor session ran, told the
  worker to kill it, and raised :class:`RunCancelledDuringTurn` (#1106).

Also the hand-over a failed round leaves for the next one (#1114,
:func:`failed_round_messages`).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from backend.dispatch.adapter import ExecutorAdapterUnavailable, RunCancelledDuringTurn
from backend.workflow.infrastructure.db import ExecutionRun

if TYPE_CHECKING:
    from backend.workflow.application.agent_loop import LoopTurn, RunOrchestrator


async def take_turn(
    orch: RunOrchestrator,
    run: ExecutionRun,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
) -> LoopTurn | None:
    """The model's next turn, or ``None`` if the run is cancelled before or during it."""
    # Cooperative cancel — stop at the turn boundary if the run was cancelled
    # mid-flight, instead of dispatching another (expensive) LLM/executor
    # turn and burning the round budget. The transition-time guard alone let
    # a cancelled run keep turning to exhaustion (dogfood dd2bd3a3).
    if await orch._run_cancelled(run):
        return None
    # Drive-session-release (B) — release the pooled DB connection for the
    # duration of the (up to 30-minute) executor turn. Committing at the turn
    # boundary ends the orchestrator session's open transaction, so NO
    # connection is held idle-in-transaction across the ``complete()`` await
    # — the pool-exhaustion outage this refactor fixes. The engine uses
    # ``expire_on_commit=False`` (see runtime/lifecycle.py) so every loaded
    # ORM attribute survives the commit; post-turn writes autobegin a fresh
    # short transaction. Nothing between this commit and ``complete()``
    # touches the DB. ``claimed_at`` is refreshed here as a heartbeat so a
    # legitimately long multi-turn drive is never mistaken for a stale claim
    # and reaped by ``AgentWorker`` (the lease is 2× the executor timeout, so
    # a single turn is always safe; the heartbeat covers many turns).
    run.claimed_at = datetime.now(UTC)
    await orch._session.commit()
    try:
        turn: LoopTurn = await orch._llm.complete(messages=messages, tools=tools)
    except RunCancelledDuringTurn as exc:
        # #1106 — the cancel landed MID-turn; the adapter has already told the
        # worker to kill the session.
        await _accrue_spent(orch, run, exc)
        return None
    except ExecutorAdapterUnavailable as exc:
        await _accrue_spent(orch, run, exc)
        raise
    return turn


async def _accrue_spent(
    orch: RunOrchestrator, run: ExecutionRun, exc: ExecutorAdapterUnavailable
) -> None:
    """#928 — a turn that ended in an error still spent what its worker reported.

    The success path accrues through ``account_and_enforce_token_cap``; this is the same
    meter for the endings that never get there. Committed here, because the error is about
    to unwind past every later write. The ceiling is not checked: the run is stopping
    anyway, and on a cancel it already has.
    """
    if not (exc.usage_prompt_tokens or exc.usage_completion_tokens):
        return
    run.usage_prompt_tokens += exc.usage_prompt_tokens
    run.usage_completion_tokens += exc.usage_completion_tokens
    await orch._session.commit()


def failed_round_messages(
    *, report: str | None, failure: str, written_paths: list[str], hint: str = ""
) -> list[dict[str, Any]]:
    """What a round that failed verification hands to the next round (#1114).

    An executor round is a whole fresh CLI session — no ``--resume``, because
    continuity is BSVibe's and the next round may run on another executor. What
    the next session knows about this one is exactly what lands here. The loop
    used to append only the failure, so the round's own report was dropped and
    nothing named the files already changed: the next session re-explored from
    zero. Now it gets, in conversation order, the round's report, then the
    failure with the run's changed files. The failure stays LAST — it is what
    the agent must act on (``test_verification_feedback``).
    """
    messages: list[dict[str, Any]] = []
    if report and report.strip():
        messages.append({"role": "assistant", "content": report})
    changed = (
        "\nFiles this run has changed so far:\n" + "\n".join(f"- {p}" for p in written_paths)
        if written_paths
        else ""
    )
    messages.append(
        {
            "role": "user",
            "content": (
                f"Verification FAILED. Details:\n{failure}{changed}\n"
                "Fix the problem and try again, then send your summary." + hint
            ),
        }
    )
    return messages
