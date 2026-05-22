"""DeliveryWorker — drain shipped DeliveryEventRow rows into the dispatcher.

Workflow §12.5 #8 (Bundle G — Workers). The orchestrator writes a
DeliveryEventRow whenever a Deliverable lands in a ``shipped`` state.
This worker:

1. Polls ``delivery_events`` for unprocessed rows (LEFT JOIN-like check
   via a paired ``processed_at`` column would be ideal; for Phase 1 we
   simply delete the row after dispatch — the schema is append-only on
   the orchestrator side, but the worker treats it as a queue).
2. Hands each event to a caller-supplied :class:`PluginDispatchAdapter`
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
from backend.delivery.schema import DeliveryResult
from backend.workers.base import BaseWorker

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
            processed = 0
            for row in rows:
                try:
                    result = await self._dispatcher.dispatch(
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
                logger.info(
                    "delivery_worker_dispatched",
                    event_id=str(row.id),
                    deliverable_id=str(row.deliverable_id),
                    actions=len(result.actions),
                )
                processed += 1
            if rows:
                await session.execute(
                    delete(DeliveryEventRow).where(DeliveryEventRow.id.in_([r.id for r in rows]))
                )
                await session.commit()
            return processed


__all__ = ["DeliveryWorker", "DeliveryWorkerConfig", "PluginDispatchAdapter"]
