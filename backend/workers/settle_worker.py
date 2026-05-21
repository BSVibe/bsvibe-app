"""SettleWorker — roll verified deliverables into ``shipped`` state.

Workflow §12.5 #8 (Bundle G — Workers). Settlement is the transition
between ``verified`` deliverable + the orchestrator's
:class:`backend.orchestrator.SafeModeBoundary`.
"""

from __future__ import annotations

import structlog

logger = structlog.get_logger(__name__)


class SettleWorker:
    """Consumer-group worker for the ``settle`` Redis Stream."""

    async def start(self) -> None:
        """Flip verified→shipped; then queue or dispatch."""
        # TODO(bundle-g-integration): lift from BSNexus
        # backend/workers/settle_worker.py — wraps
        # backend.execution.deliverables.settle + SafeModeBoundary.gate.
        logger.debug("settle_worker_start_stub")
        raise NotImplementedError("SettleWorker.start pending Bundle G integration")

    async def stop(self) -> None:
        """Graceful drain."""
        # TODO(bundle-g-integration): cancel + close.
        logger.debug("settle_worker_stop_stub")
        raise NotImplementedError("SettleWorker.stop pending Bundle G integration")


__all__ = ["SettleWorker"]
