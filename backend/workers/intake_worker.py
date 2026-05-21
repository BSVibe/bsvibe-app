"""IntakeWorker — drain TriggerEvents into Requests.

Workflow §12.5 #8 (Bundle G — Workers). Consumes the ``intake`` Redis
Stream (which the API surface pushes :class:`TriggerEvent` deliveries
into) and creates the matching :class:`RequestRow`.
"""

from __future__ import annotations

import structlog

logger = structlog.get_logger(__name__)


class IntakeWorker:
    """Consumer-group worker for the ``intake`` Redis Stream."""

    async def start(self) -> None:
        """For each TriggerEvent: idempotency check → RequestRow insert."""
        # TODO(bundle-g-integration): lift from BSNexus
        # backend/workers/request_worker.py (RequestWorker rename).
        logger.debug("intake_worker_start_stub")
        raise NotImplementedError("IntakeWorker.start pending Bundle G integration")

    async def stop(self) -> None:
        """Graceful drain."""
        # TODO(bundle-g-integration): cancel + close.
        logger.debug("intake_worker_stop_stub")
        raise NotImplementedError("IntakeWorker.stop pending Bundle G integration")


__all__ = ["IntakeWorker"]
