"""What an EXECUTOR agent did through the MCP work tools, read back into the loop.

Split out of ``_drive_loop`` to keep that file under the 600 LOC ceiling; behaviour unchanged.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from backend.workflow.application.tool_registry import WORK_TOOL_STATE_KEY
from backend.workflow.infrastructure.db import ExecutionRun

if TYPE_CHECKING:
    from backend.workflow.application.agent_loop import RunOrchestrator


async def remote_work_state(orch: RunOrchestrator, run: ExecutionRun) -> dict[str, Any] | None:
    """The work-tool state the MCP transport committed for this run — read FRESH.

    The MCP handlers run in the API process and commit there, so the loop's own ORM copy of the
    run never learns about it. Select the column rather than refreshing the instance: the loop
    mutates ``run`` itself, and a refresh would clobber its pending changes.
    """
    from sqlalchemy import select  # noqa: PLC0415

    rows = await orch._session.execute(
        select(ExecutionRun.payload).where(ExecutionRun.id == run.id)
    )
    payload = rows.scalar_one_or_none() or {}
    state = payload.get(WORK_TOOL_STATE_KEY)
    return state if isinstance(state, dict) else None


async def sync_remote_tool_state(
    registry: Any, written_paths: list[str], *, state: dict[str, Any] | None
) -> None:
    """Teach the loop's registry what the agent did through the MCP work tools.

    Restores the same per-run latches the MCP transport persists — the declared contract, the
    grounding, the declared knowledge — and merges the paths the agent WROTE into the loop's
    accumulator, which becomes the verified Deliverable's ``artifact_refs``.

    Idempotent: it runs every turn, and both the registry and ``written_paths`` dedupe.
    """
    if not state:
        return
    registry.restore_state(state)
    for path in registry.written_paths:
        if path not in written_paths:
            written_paths.append(path)
