"""The two LOOP-owned effects behind the MCP work tools (T1b).

They live here, not in :mod:`backend.mcp`, because the MCP import contract forbids that
context from importing :mod:`backend.api` — and ``handle_emit_deliverable`` reaches
``backend.api.v1.live_events`` for the live bus. The composition root wires these in, so
``backend.mcp`` stays a transport: it decides who may act on which run, never what the act is.

Both COMMIT. The MCP dispatcher opens the request session and never commits it
(``backend/mcp/server.py``), so an uncommitted write is rolled back when the request ends and
the effect silently never happens — the founder is never asked, the deliverable never appears.
Every MCP write tool commits for itself; these are no different.
"""

from __future__ import annotations

import uuid
from typing import Any

from backend.mcp.api import ToolContext
from backend.mcp.tools.work_registry import load_run
from backend.workflow.application.run_persistence import create_decision, record_activity
from backend.workflow.domain.emit_deliverable import handle_emit_deliverable


def _ask_decision_kind(run: Any) -> str:
    """The Decision kind for an agent-raised blocking question.

    PR7 — when the run is resolving a re-dispatched merge conflict (the drive
    loop set ``payload["merge_conflict_resolving"]`` after surfacing the
    conflict), an ``ask_user_question`` the agent raises IS the founder's
    clear-vs-ambiguous merge decision. Mint it as ``merge_conflict_review`` so
    the checkpoint surface offers the retry/discard one-click actions (and the
    calm merge-conflict copy) instead of a vanilla free-text ask. Otherwise the
    kind is the plain ``ask_user_question`` (unchanged)."""
    payload = run.payload if isinstance(run.payload, dict) else {}
    if payload.get("merge_conflict_resolving"):
        return "merge_conflict_review"
    return "ask_user_question"


async def record_question(run_id: uuid.UUID, ctx: ToolContext, payload: dict[str, Any]) -> str:
    """Create the Decision the run pauses on when the agent asks the founder.

    Normally an ``ask_user_question`` Decision; a question raised while the run
    is resolving a re-dispatched merge conflict becomes a ``merge_conflict_review``
    (PR7) so the founder gets the retry/discard actions."""
    run = await load_run(run_id, ctx)
    kind = _ask_decision_kind(run)
    decision = await create_decision(
        ctx.session,
        run,
        None,  # work_step is unused by the Decision row
        kind=kind,
        payload=payload,
        rationale="the working agent asked the founder a blocking question",
    )
    await ctx.session.commit()
    return str(decision.id)


async def record_deliverable(run_id: uuid.UUID, ctx: ToolContext, arguments: dict[str, Any]) -> str:
    """Persist a mid-run Deliver event — the same domain handler the loop calls."""
    run = await load_run(run_id, ctx)
    result = await handle_emit_deliverable(ctx.session, run, arguments)
    await ctx.session.commit()
    return result


async def record_progress(run_id: uuid.UUID, ctx: ToolContext, payload: dict[str, Any]) -> None:
    """Record ONE work-tool call as run activity — the executor's half of the trail.

    The in-process loop writes a ``tool_call`` activity per call; an executor reaches the
    same registry through MCP, and that path wrote nothing. The consequence was not a
    missing nicety: for a whole 28-minute turn the founder's timeline was blank and a
    working run was indistinguishable from a wedged one (fix backlog #1).

    Same ``activity_type`` and same payload keys as the loop, so the founder-facing
    timeline (``_tool_call_label`` → "Delivered X") needs no second code path.

    ``attempt`` is ``None``: this transport acts OUTSIDE any loop WorkStep, exactly as
    ``record_question`` passes ``None`` for its work step.
    """
    run = await load_run(run_id, ctx)
    await record_activity(ctx.session, run, None, "tool_call", payload)
    await ctx.session.commit()


__all__ = ["_ask_decision_kind", "record_deliverable", "record_progress", "record_question"]
