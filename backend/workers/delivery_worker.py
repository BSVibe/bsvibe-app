"""DeliveryWorker — drain shipped DeliveryEventRow rows into the dispatcher.

Workflow §12.5 #8 (Bundle G — Workers). The orchestrator writes a
DeliveryEventRow whenever a Deliverable lands in a ``shipped`` state.
This worker:

1. Polls ``delivery_events`` for unprocessed rows (LEFT JOIN-like check
   via a paired ``processed_at`` column would be ideal; for Phase 1 we
   simply delete the row after dispatch — the schema is append-only on
   the orchestrator side, but the worker treats it as a queue).
2. Consults the deliverable's workspace Safe Mode (Workflow §10.5). When
   ``workspaces.safe_mode`` is True the delivery is **enqueued** into the
   :class:`backend.delivery.safe_mode_queue.SafeModeQueue` (status
   ``pending``) instead of dispatching — the founder approves/denies via
   the ``/api/v1/safemode`` routes, and approval re-uses the *same*
   :func:`dispatch_delivery` helper this worker calls. When Safe Mode is
   off (or no workspace row exists — e.g. an unseeded test workspace) the
   delivery dispatches straight out exactly as before.
3. Hands each event to a caller-supplied :class:`PluginDispatchAdapter`
   so the test sink + the real PluginRunner share one shape.

DB-polling, not Redis Streams. Same justification as AgentWorker.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Protocol

import structlog
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.delivery.db import DeliveryEventRow
from backend.delivery.safe_mode_queue import SafeModeQueue
from backend.delivery.schema import ActionResult, DeliveryResult
from backend.execution.db import Deliverable
from backend.workers.base import BaseWorker
from backend.workspaces.db import WorkspaceRow

logger = structlog.get_logger(__name__)


class PluginDispatchAdapter(Protocol):
    """Adapter the worker calls to actually deliver."""

    async def dispatch(
        self,
        *,
        workspace_id: uuid.UUID,
        deliverable_id: uuid.UUID,
        artifact_type: str,
        plugins: Iterable[object] = (),
        context: object = None,
        event: object = None,
    ) -> DeliveryResult: ...


async def _workspace_safe_mode(session: AsyncSession, workspace_id: uuid.UUID) -> bool:
    """Return the workspace's Safe Mode flag.

    Policy v1 (Workflow §10.5): the gate is purely ``workspace.safe_mode``.
    Trigger-source is intentionally NOT threaded through — founder-direct
    bypass is a later refinement. A missing workspace row defaults to direct
    dispatch (``False``) so an unseeded test workspace behaves as today.
    """
    row = await session.get(WorkspaceRow, workspace_id)
    return bool(row.safe_mode) if row is not None else False


def extract_compensation_handles(
    actions: list[ActionResult],
) -> list[dict[str, Any]]:
    """B12b — pull ``compensation_handle`` from successful actions.

    Each :class:`ActionResult.action` is shaped ``"<plugin>:outbound:<artifact>"``
    (see :class:`backend.delivery.dispatcher.DeliveryDispatcher` and
    :class:`backend.delivery.connector_dispatch.ConnectorDeliveryAdapter`). A
    successful action whose ``output`` carries ``compensation_handle`` (a
    plugin-private revert token — Workflow §3.1) produces one entry:
    ``{"plugin": "<name>", "artifact_type": "<type>", "handle": {...}}``.
    Failed actions and successful actions WITHOUT a handle are skipped — the
    retract endpoint reads this list and 400s an empty one rather than
    inventing fake entries.
    """
    entries: list[dict[str, Any]] = []
    for action in actions:
        if not action.succeeded or not action.output:
            continue
        handle = action.output.get("compensation_handle")
        if not isinstance(handle, dict) or not handle:
            continue
        plugin_name, _, rest = action.action.partition(":outbound:")
        if not plugin_name:
            continue
        artifact_type = rest or str(action.output.get("artifact_type") or "")
        entries.append(
            {
                "plugin": plugin_name,
                "artifact_type": artifact_type,
                "handle": dict(handle),
            }
        )
    return entries


async def persist_compensation_handles(
    session: AsyncSession,
    *,
    deliverable_id: uuid.UUID,
    result: DeliveryResult,
) -> int:
    """B12b — append captured handles onto the Deliverable.

    Workflow §1.2 + §3.1 + §9. Reads every successful action whose ``output``
    carries a ``compensation_handle`` (plugin-private revert token) and
    persists ``{"plugin", "artifact_type", "handle"}`` entries onto the
    Deliverable's ``compensation_handles`` column. The row may already carry
    handles from a prior dispatch of the same deliverable (Safe Mode approve
    re-runs the same code path); append rather than overwrite so re-dispatches
    keep accumulating. A missing Deliverable (purged run) is a silent no-op —
    the dispatch already succeeded, the lost handle just means retract is
    unavailable for that target. Returns the count of entries appended.
    """
    entries = extract_compensation_handles(result.actions)
    if not entries:
        return 0
    row = await session.get(Deliverable, deliverable_id)
    if row is None:
        return 0
    existing = list(row.compensation_handles or [])
    existing.extend(entries)
    # SQLAlchemy treats list assignment as a mutation; setting the attribute
    # explicitly avoids the dirty-tracking blind spot of in-place ``.extend``.
    row.compensation_handles = existing
    await session.commit()
    return len(entries)


async def dispatch_delivery(
    dispatcher: PluginDispatchAdapter,
    *,
    workspace_id: uuid.UUID,
    deliverable_id: uuid.UUID,
    artifact_type: str,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> DeliveryResult:
    """The single outbound-dispatch code path shared by the worker + approve.

    Extracted so :meth:`DeliveryWorker.drain_once` (Safe Mode off) and the
    ``POST /api/v1/safemode/{item_id}/approve`` route dispatch through one
    helper rather than duplicating the call shape.

    B12b — when ``session_factory`` is provided, any ``compensation_handle``
    returned by a successful outbound action is persisted on the Deliverable
    row so the retract endpoint can later read it (Workflow §1.2 + §3.1 + §9).
    Callers that already hold an open session (e.g. an HTTP route handler)
    should omit the factory and call :func:`persist_compensation_handles`
    directly on that session — keeps the persist step inside the caller's
    transaction.
    """
    result = await dispatcher.dispatch(
        workspace_id=workspace_id,
        deliverable_id=deliverable_id,
        artifact_type=artifact_type,
    )
    logger.info(
        "delivery_dispatched",
        deliverable_id=str(deliverable_id),
        workspace_id=str(workspace_id),
        actions=len(result.actions),
    )
    if session_factory is not None:
        async with session_factory() as session:
            await persist_compensation_handles(
                session,
                deliverable_id=deliverable_id,
                result=result,
            )
    return result


@dataclass(slots=True)
class DeliveryWorkerConfig:
    batch_size: int = 50
    poll_interval_s: float = 5.0


class DeliveryWorker(BaseWorker):
    """Periodic drain of ``delivery_events`` into the plugin dispatcher."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        dispatcher: PluginDispatchAdapter,
        config: DeliveryWorkerConfig | None = None,
    ) -> None:
        self._cfg = config or DeliveryWorkerConfig()
        super().__init__(name="delivery_worker", poll_interval_s=self._cfg.poll_interval_s)
        self._session_factory = session_factory
        self._dispatcher = dispatcher

    async def _tick(self) -> int:
        return await self.drain_once()

    async def drain_once(self) -> int:
        """Pull a batch of pending events + dispatch each. Returns count delivered."""
        async with self._session_factory() as session:
            stmt = (
                select(DeliveryEventRow)
                .order_by(DeliveryEventRow.created_at.asc())
                .limit(self._cfg.batch_size)
            )
            rows = (await session.execute(stmt)).scalars().all()
            if not rows:
                return 0
            queue = SafeModeQueue(session)
            processed = 0
            for row in rows:
                try:
                    if await _workspace_safe_mode(session, row.workspace_id):
                        # Safe Mode ON — hold for founder approval instead of
                        # dispatching. The /api/v1/safemode routes drive it the
                        # rest of the way. ``run_id`` (B12a) threads the
                        # originating run onto the queue item so the founder
                        # can approve all of a run's accumulated partial
                        # Deliver events as ONE transaction (Workflow §1.2).
                        await queue.enqueue(
                            workspace_id=row.workspace_id,
                            deliverable_id=row.deliverable_id,
                            run_id=row.run_id,
                        )
                        logger.info(
                            "delivery_worker_enqueued_safe_mode",
                            event_id=str(row.id),
                            deliverable_id=str(row.deliverable_id),
                        )
                    else:
                        await dispatch_delivery(
                            self._dispatcher,
                            workspace_id=row.workspace_id,
                            deliverable_id=row.deliverable_id,
                            artifact_type=row.artifact_type,
                            # B12b — capture compensation_handle onto the
                            # Deliverable so the retract endpoint can later
                            # revert through @p.compensate.
                            session_factory=self._session_factory,
                        )
                except Exception:  # noqa: BLE001 — record + move on
                    logger.exception(
                        "delivery_worker_dispatch_failed",
                        event_id=str(row.id),
                        deliverable_id=str(row.deliverable_id),
                    )
                    continue
                processed += 1
            await session.execute(
                delete(DeliveryEventRow).where(DeliveryEventRow.id.in_([r.id for r in rows]))
            )
            await session.commit()
            return processed


__all__ = [
    "DeliveryWorker",
    "DeliveryWorkerConfig",
    "PluginDispatchAdapter",
    "dispatch_delivery",
]
