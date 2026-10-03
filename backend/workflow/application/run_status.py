"""The one place a run's status changes (#1110).

Before this, ``AgentRunner.transition`` and six other places each wrote
``run.status`` plus their own ``ExecutionRunHistory`` row: local auto-ship,
delivery auto-resolve, cancel, reopen, the Safe-Mode-deny reopen and the
drive-failure escalation. #1102 made ``transition`` a compare-and-set; the other
six still overwrote whatever the database held — a cancel committed by another
session could be undone through any of them.

:func:`move_run_status` is the move, for all of them:

* a conditional ``UPDATE … WHERE id = :id AND status = :from`` — ``from`` is the
  held object's status, so a run another session moved first is refused (zero
  rows) instead of overwritten;
* the history row is written only when the move landed — the history is what
  #1102 was diagnosed from, and it must not record a move that did not happen;
* the held object is mirrored with ``set_committed_value`` (a dirty ``status``
  would flush a second, unconditional UPDATE), or refreshed on refusal so the
  caller sees the truth.

What a move is ALLOWED to be (``shipped`` must not go back to ``open``) is #1109's
transition table; this module is the single seam it will live in.

Leaf-level on purpose (sqlalchemy + the ORM rows only): callers include the
inbound-webhook / MCP side, which import-linter keeps off the loop graph.
``tests/glue/test_one_run_state_machine.py`` pins that nothing else writes a run's
status.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import update
from sqlalchemy.orm.attributes import set_committed_value

from backend.workflow.infrastructure.db import ExecutionRun, ExecutionRunHistory, RunStatus

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger(__name__)


async def move_run_status(
    session: AsyncSession,
    run: ExecutionRun,
    to_status: RunStatus,
    *,
    reason: str | None,
) -> bool:
    """Move ``run`` from its held status to ``to_status``; ``False`` if it did not land.

    ``False`` when the run already is ``to_status`` (no-op, no history) or when the
    database no longer holds the status this object thinks it has (another writer
    moved it first — the object is refreshed to the real status).
    """
    from_status = run.status
    if from_status is to_status:
        return False
    now = datetime.now(tz=UTC)
    result = await session.execute(
        update(ExecutionRun)
        .where(ExecutionRun.id == run.id, ExecutionRun.status == from_status)
        .values(status=to_status, updated_at=now)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:  # type: ignore[attr-defined]
        await session.refresh(run, attribute_names=["status", "updated_at"])
        logger.info(
            "run_status_move_lost_race",
            run_id=str(run.id),
            expected_status=from_status.value,
            actual_status=run.status.value,
            to_status=to_status.value,
        )
        return False
    set_committed_value(run, "status", to_status)
    set_committed_value(run, "updated_at", now)
    session.add(
        ExecutionRunHistory(
            id=uuid.uuid4(),
            run_id=run.id,
            workspace_id=run.workspace_id,
            from_status=from_status,
            to_status=to_status,
            reason=reason,
            created_at=now,
        )
    )
    await session.flush()
    return True


__all__ = ["move_run_status"]
