"""One turn of the drive loop — or ``None`` when the run was cancelled.

Split out of ``_drive_loop`` (v8 §17.1 600-LOC ceiling) when #1106 gave a turn a
second way to end in cancel. Both live here so they cannot drift apart:

* at the turn boundary — the run was cancelled between turns (dogfood dd2bd3a3);
* mid-turn — the adapter saw the cancel while the executor session ran, told the
  worker to kill it, and raised :class:`RunCancelledDuringTurn` (#1106).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from backend.dispatch.adapter import RunCancelledDuringTurn
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
    except RunCancelledDuringTurn:
        # #1106 — the cancel landed MID-turn; the adapter has already told the
        # worker to kill the session.
        return None
    return turn
