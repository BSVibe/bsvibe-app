"""Cancel / out-of-band-question checkpoints of the drive loop (split out of ``_drive_loop``)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from backend.workflow.application.audit_events import LoopTerminal
from backend.workflow.infrastructure.db import (
    Decision,
    DecisionStatus,
    ExecutionRun,
    RunAttempt,
    WorkStep,
)

if TYPE_CHECKING:
    from backend.workflow.application.agent_loop import LoopResult, RunOrchestrator

logger = structlog.get_logger(__name__)


async def pending_question(session: Any, run: ExecutionRun) -> Decision | None:
    """A question the agent asked the founder OUT OF BAND — i.e. not via a tool call the loop
    could see, but by calling the MCP work tool, which records the Decision server-side.

    That is the only way an executor's CLI can ask anything (parity audit #20: no tool call
    other than ``declare_verification`` can come back from an executor), so without this the
    run would keep working while the founder's question sat pending, unanswered."""
    from sqlalchemy import select  # noqa: PLC0415

    rows = await session.execute(
        select(Decision).where(
            Decision.run_id == run.id,
            Decision.decision == "ask_user_question",
            Decision.status == DecisionStatus.PENDING,
        )
    )
    return rows.scalars().first()  # type: ignore[no-any-return]


async def stop_cancelled(
    orch: RunOrchestrator,
    run: ExecutionRun,
    work_step: WorkStep,
    attempt: RunAttempt,
    written_paths: list[str],
    final_text: str,
    checkpoint: str,
    cycle: int,
) -> LoopResult:
    """End a drive whose run was cancelled / discarded under it — no more work, no artifacts."""
    logger.warning(
        "drive_loop_stopped_run_cancelled",
        run_id=str(run.id),
        checkpoint=checkpoint,
        cycle=cycle,
    )
    await orch._audit(
        run, attempt, LoopTerminal, {"outcome": "cancelled", "cycle": cycle, "at": checkpoint}
    )
    return orch._cancelled_result(run, work_step, attempt, written_paths, final_text)


async def stop_if_cancelled(
    orch: RunOrchestrator,
    run: ExecutionRun,
    work_step: WorkStep,
    attempt: RunAttempt,
    written_paths: list[str],
    final_text: str,
    checkpoint: str,
    cycle: int,
) -> LoopResult | None:
    """Re-read the run's status; a cancelled run ends the drive, otherwise ``None``.

    Commits after the read so its transaction is not held open across the external work
    (verification) that follows."""
    cancelled = await orch._run_cancelled(run)
    await orch._session.commit()
    if not cancelled:
        return None
    return await stop_cancelled(
        orch, run, work_step, attempt, written_paths, final_text, checkpoint, cycle
    )
