"""DeliveryWorker — fan shipped deliverables out to outbound adapters.

Workflow §12.5 #8 (Bundle G — Workers). Production surface for
:class:`backend.delivery.DeliveryDispatcher`.
"""

from __future__ import annotations

import structlog

logger = structlog.get_logger(__name__)


class DeliveryWorker:
    """Consumer-group worker for the ``delivery`` Redis Stream."""

    async def start(self) -> None:
        """Drive ``DeliveryDispatcher.dispatch`` per shipped event."""
        # TODO(bundle-g-integration): lift from BSNexus
        # backend/workers/delivery_worker.py.
        logger.debug("delivery_worker_start_stub")
        raise NotImplementedError("DeliveryWorker.start pending Bundle G integration")

    async def stop(self) -> None:
        """Graceful drain."""
        # TODO(bundle-g-integration): cancel + close.
        logger.debug("delivery_worker_stop_stub")
        raise NotImplementedError("DeliveryWorker.stop pending Bundle G integration")


__all__ = ["DeliveryWorker"]
