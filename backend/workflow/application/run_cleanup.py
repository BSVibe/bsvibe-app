"""Run cleanup — cancel / discard a run, and cascade-cancel a product's runs.

The canonical primitives behind three surfaces:

* ``bsvibe_runs_cancel`` (MCP) — mirrors ``POST /api/v1/runs/{id}/cancel``:
  only an in-flight (OPEN / RUNNING) run can be cancelled.
* ``bsvibe_runs_discard`` (MCP) — the ``폐기`` cleanup primitive: transition ANY
  non-terminal run (incl. ``review_ready``) → CANCELLED, best-effort tombstone
  its handle-less deliverables, and best-effort remove its worktree.
* Product delete cascade — :func:`cancel_product_runs` cancels every non-terminal
  run of a product before the product row is hard-deleted, so runs are never
  orphaned (``ExecutionRun.product_id`` is a loose reference, no FK cascade).

Every status flip appends an :class:`ExecutionRunHistory` audit row — the same
record :meth:`AgentRunner.transition` writes. We replicate that minimal
transition inline (rather than importing ``AgentRunner``) so this cleanup
service — reachable from the lightweight MCP leaf surface — does not drag the
whole agent-execution engine (agent_loop → skill loader → router) into MCP's
import graph. The only target here is CANCELLED, so none of ``transition``'s
REVIEW_READY-only side effects (auto-ship, impl-spawn) apply. The caller owns
the transaction boundary (these functions ``flush`` but never ``commit``).

Deliverable retraction here is a **best-effort tombstone**, NOT plugin
compensation: a deliverable with captured ``compensation_handles`` (a delivered
external artifact) is surfaced in ``deliverables_need_compensation`` rather than
silently marked retracted — undoing the real artifact stays on the explicit
``POST /api/v1/deliverables/{id}/retract`` path (which runs ``@p.compensate``).
A handle-less deliverable (never delivered externally — the common case for an
abandoned / never-shipped run) has nothing to revert, so it is tombstoned.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.workflow.infrastructure.db import (
    ExecutionRun,
    ExecutionRunHistory,
    RunStatus,
)
from backend.workflow.infrastructure.repositories import (
    SqlAlchemyDeliverableRepository,
    SqlAlchemyRunRepository,
)

logger = structlog.get_logger(__name__)

#: A run in one of these states is finished — nothing to cancel / discard.
_TERMINAL: frozenset[RunStatus] = frozenset(
    {RunStatus.SHIPPED, RunStatus.FAILED, RunStatus.CANCELLED}
)
#: Only an in-flight run can be *cancelled* (mirrors the REST /cancel guard).
_CANCELLABLE: frozenset[RunStatus] = frozenset({RunStatus.OPEN, RunStatus.RUNNING})


async def _cancel(session: AsyncSession, run: ExecutionRun, *, reason: str) -> bool:
    """Flip a run to CANCELLED + append the audit-history row. Returns ``False``
    if the run is already terminal (no-op), mirroring ``AgentRunner.transition``."""
    if run.status in _TERMINAL:
        return False
    from_status = run.status
    run.status = RunStatus.CANCELLED
    run.updated_at = datetime.now(tz=UTC)
    session.add(
        ExecutionRunHistory(
            id=uuid.uuid4(),
            run_id=run.id,
            workspace_id=run.workspace_id,
            from_status=from_status,
            to_status=RunStatus.CANCELLED,
            reason=reason,
            created_at=datetime.now(tz=UTC),
        )
    )
    await session.flush()
    logger.info(
        "run_cancelled",
        run_id=str(run.id),
        workspace_id=str(run.workspace_id),
        from_status=from_status.value,
        reason=reason,
    )
    return True


@dataclass
class CancelOutcome:
    """Result of :func:`cancel_run`."""

    found: bool
    cancelled: bool
    status: str | None  # final run status value, or None when not found


@dataclass
class DiscardOutcome:
    """Result of :func:`discard_run`."""

    run_id: uuid.UUID
    status: str
    cancelled: bool
    deliverables_retracted: list[str] = field(default_factory=list)
    deliverables_need_compensation: list[str] = field(default_factory=list)


async def cancel_run(
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
    workspace_id: uuid.UUID,
    reason: str,
) -> CancelOutcome:
    """Cancel an OPEN / RUNNING run (mirrors ``POST /runs/{id}/cancel``).

    ``found=False`` for an unknown / cross-workspace id; ``cancelled=False`` with
    the current status for a run that is not in-flight (terminal or review_ready
    — use :func:`discard_run` for the latter).
    """
    runs = SqlAlchemyRunRepository(session)
    run = await runs.get(run_id)
    if run is None or run.workspace_id != workspace_id:
        return CancelOutcome(found=False, cancelled=False, status=None)
    if run.status not in _CANCELLABLE:
        return CancelOutcome(found=True, cancelled=False, status=run.status.value)
    await _cancel(session, run, reason=reason)
    return CancelOutcome(found=True, cancelled=True, status=RunStatus.CANCELLED.value)


async def discard_run(
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
    workspace_id: uuid.UUID,
    reason: str,
) -> DiscardOutcome | None:
    """Discard a run — cancel it (if non-terminal) + best-effort tombstone.

    Returns ``None`` for an unknown / cross-workspace id. Transitions a
    non-terminal run → CANCELLED (a terminal run is left as-is but its deliverables
    are still evaluated). Handle-less deliverables are tombstoned; deliverables
    with compensation handles are surfaced for an explicit compensating retract.
    Worktree removal is best-effort and never fails the discard.
    """
    runs = SqlAlchemyRunRepository(session)
    run = await runs.get(run_id)
    if run is None or run.workspace_id != workspace_id:
        return None

    cancelled = await _cancel(session, run, reason=reason)

    deliverables = SqlAlchemyDeliverableRepository(session)
    now = datetime.now(tz=UTC)
    retracted: list[str] = []
    need_compensation: list[str] = []
    for d in await deliverables.list_by_run(run.id, workspace_id):
        if d.retracted_at is not None:
            continue
        if d.compensation_handles:
            # A delivered external artifact — don't fake a rollback.
            need_compensation.append(str(d.id))
            continue
        d.retracted_at = now
        retracted.append(str(d.id))

    await session.flush()
    await _remove_worktree_best_effort(run)

    logger.info(
        "run_discarded",
        run_id=str(run.id),
        workspace_id=str(workspace_id),
        cancelled=cancelled,
        deliverables_retracted=len(retracted),
        deliverables_need_compensation=len(need_compensation),
    )
    return DiscardOutcome(
        run_id=run.id,
        status=run.status.value,
        cancelled=cancelled,
        deliverables_retracted=retracted,
        deliverables_need_compensation=need_compensation,
    )


async def cancel_product_runs(
    session: AsyncSession,
    *,
    product_id: uuid.UUID,
    workspace_id: uuid.UUID,
    reason: str,
) -> int:
    """Cancel every non-terminal run bound to a product; return the count.

    Called from ``delete_product`` before the product row is deleted so its runs
    (loose ``product_id`` reference, no FK cascade) are never orphaned.
    """
    stmt = select(ExecutionRun).where(
        ExecutionRun.workspace_id == workspace_id,
        ExecutionRun.product_id == product_id,
        ExecutionRun.status.not_in(tuple(_TERMINAL)),
    )
    rows = (await session.execute(stmt)).scalars().all()
    cancelled = 0
    for run in rows:
        if await _cancel(session, run, reason=reason):
            cancelled += 1
    return cancelled


async def _remove_worktree_best_effort(run: ExecutionRun) -> None:
    """Remove a run's git worktree without ever failing the caller."""
    if run.product_id is None:
        return
    try:
        from backend.storage.product_workspace import remove_run_worktree  # noqa: PLC0415

        await remove_run_worktree(run.product_id, run.id)
    except Exception:  # noqa: BLE001 — cleanup is best-effort
        logger.warning(
            "run_discard_worktree_cleanup_failed",
            run_id=str(run.id),
            product_id=str(run.product_id),
            exc_info=True,
        )


__all__ = [
    "CancelOutcome",
    "DiscardOutcome",
    "cancel_product_runs",
    "cancel_run",
    "discard_run",
]
