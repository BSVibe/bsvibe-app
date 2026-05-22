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
from typing import Protocol

import structlog
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.delivery.db import DeliveryEventRow
from backend.delivery.safe_mode_queue import SafeModeQueue
from backend.delivery.schema import DeliveryResult
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


async def dispatch_delivery(
    dispatcher: PluginDispatchAdapter,
    *,
    workspace_id: uuid.UUID,
    deliverable_id: uuid.UUID,
    artifact_type: str,
) -> DeliveryResult:
    """The single outbound-dispatch code path shared by the worker + approve.

    Extracted so :meth:`DeliveryWorker.drain_once` (Safe Mode off) and the
    ``POST /api/v1/safemode/{item_id}/approve`` route dispatch through one
    helper rather than duplicating the call shape.
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
                        # rest of the way.
                        await queue.enqueue(
                            workspace_id=row.workspace_id,
                            deliverable_id=row.deliverable_id,
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
