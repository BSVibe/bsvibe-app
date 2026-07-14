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
from backend.workflow.application.run_persistence import create_decision
from backend.workflow.domain.emit_deliverable import handle_emit_deliverable


async def record_question(run_id: uuid.UUID, ctx: ToolContext, payload: dict[str, Any]) -> str:
    """Create the ``ask_user_question`` Decision the run pauses on."""
    run = await load_run(run_id, ctx)
    decision = await create_decision(
        ctx.session,
        run,
        None,  # work_step is unused by the Decision row
        kind="ask_user_question",
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


__all__ = ["record_deliverable", "record_question"]
