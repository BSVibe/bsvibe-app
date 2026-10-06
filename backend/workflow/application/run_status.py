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

And it is where a move is checked against the transition table (#1109,
:func:`is_allowed_move`): ``shipped`` is final, and ``failed`` / ``cancelled``
leave only by an explicit retry to ``open``.

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

#: The only exit from each CLOSED status (#1109). ``shipped`` has none: it means the
#: work landed — merged, for a GitHub-delivered run — and every reaper, the run cap
#: and the dashboards treat it as final. ``failed`` / ``cancelled`` re-open only by
#: an explicit retry. Open statuses (``open`` / ``running`` / ``review_ready``) may
#: move anywhere; who may move them is the caller's guard, not this table's.
_EXITS_FROM_CLOSED: dict[RunStatus, frozenset[RunStatus]] = {
    RunStatus.SHIPPED: frozenset(),
    RunStatus.FAILED: frozenset({RunStatus.OPEN}),
    RunStatus.CANCELLED: frozenset({RunStatus.OPEN}),
}


def is_allowed_move(from_status: RunStatus, to_status: RunStatus) -> bool:
    """Whether the transition table permits ``from_status → to_status`` (#1109)."""
    exits = _EXITS_FROM_CLOSED.get(from_status)
    return exits is None or to_status in exits


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
    if not is_allowed_move(from_status, to_status):
        logger.warning(
            "run_status_move_not_allowed",
            run_id=str(run.id),
            from_status=from_status.value,
            to_status=to_status.value,
            reason=reason,
        )
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
    if to_status is RunStatus.SHIPPED:
        await _announce_shipped(session, run)
    return True


async def _announce_shipped(session: AsyncSession, run: ExecutionRun) -> None:
    """Say "shipped" when the run SHIPS (#1111) — merged, or shipped locally.

    Every move to SHIPPED passes through :func:`move_run_status`, so this is the one
    honest place. It used to be said at VERIFY time, before Safe Mode approval and
    before any delivery; that card is now ``review_ready``. Staged in the SAME
    transaction as the move (confirmed iff the move commits); deduped per run. The
    body is the run's latest deliverable title line; no buttons — nothing is left
    to approve.
    """
    from sqlalchemy import select  # noqa: PLC0415

    from backend.identity.workspaces_db import load_workspace_language  # noqa: PLC0415
    from backend.notifications.copy import notification_copy  # noqa: PLC0415
    from backend.notifications.emit import emit_notification  # noqa: PLC0415
    from backend.workflow.domain.verified_deliverable import _shipped_detail  # noqa: PLC0415
    from backend.workflow.infrastructure.db import Deliverable  # noqa: PLC0415

    latest = (
        await session.execute(
            select(Deliverable)
            .where(Deliverable.run_id == run.id)
            .order_by(Deliverable.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    summary = ""
    if latest is not None and isinstance(latest.payload, dict):
        summary = str(latest.payload.get("summary") or "")
    language = await load_workspace_language(session, run.workspace_id)
    copy = notification_copy("shipped", language, detail=_shipped_detail(summary))
    payload: dict[str, object] = {
        "title": copy.title,
        "body": copy.body,
        "run_id": str(run.id),
    }
    if latest is not None:
        payload["link"] = f"/deliverables/{latest.id}"
    await emit_notification(
        session,
        workspace_id=run.workspace_id,
        product_id=run.product_id,
        event="shipped",
        dedupe_key=f"shipped:{run.id}",
        payload=payload,
        producer_id="workflow:run_status",
    )


__all__ = ["is_allowed_move", "move_run_status"]
