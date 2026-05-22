"""ExecutorDispatchWorker — dispatch run_attempts to executor pools.

Workflow §12.5 #8 (Bundle G — Workers). Reads run_attempt records in
``running`` phase=``prepare`` and dispatches them to the matching
executor pool (DinD sandbox, claude-code CLI, local agent).
"""

from __future__ import annotations

import structlog

logger = structlog.get_logger(__name__)


class ExecutorDispatchWorker:
    """Consumer-group worker for the ``executor_dispatch`` Redis Stream."""

    async def start(self) -> None:
        """Match run_attempt → executor pool → start sandbox."""
        # TODO(bundle-g-integration): lift from BSNexus
        # backend/workers/executor_dispatch.py — joins
        # backend.execution.run_attempt_executor + DinD sandbox provisioning.
        logger.debug("executor_dispatch_worker_start_stub")
        raise NotImplementedError("ExecutorDispatchWorker.start pending Bundle G integration")

    async def stop(self) -> None:
        """Graceful drain."""
        # TODO(bundle-g-integration): cancel + close.
        logger.debug("executor_dispatch_worker_stop_stub")
        raise NotImplementedError("ExecutorDispatchWorker.stop pending Bundle G integration")


__all__ = ["ExecutorDispatchWorker"]
