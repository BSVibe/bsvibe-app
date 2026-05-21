"""AgentWorker — consume Requests off the agent stream, run the agent loop.

Workflow §12.5 #8 (Bundle G — Workers). The agent worker is the
production execution surface for the orchestrator's
:class:`backend.orchestrator.AgentRunner`.
"""

from __future__ import annotations

import structlog

logger = structlog.get_logger(__name__)


class AgentWorker:
    """Consumer-group worker for the ``agent`` Redis Stream."""

    async def start(self) -> None:
        """Spin up the consumer loop and drive ``AgentRunner.run_request``."""
        # TODO(bundle-g-integration): lift from BSNexus
        # backend/workers/agent_worker.py — RedisStreamConsumer.consume
        # bound to handler=AgentRunner.run_request.
        logger.debug("agent_worker_start_stub")
        raise NotImplementedError("AgentWorker.start pending Bundle G integration")

    async def stop(self) -> None:
        """Graceful drain — finish in-flight requests, then exit."""
        # TODO(bundle-g-integration): cancel consume task, wait for
        # XACK on in-flight, close redis client.
        logger.debug("agent_worker_stop_stub")
        raise NotImplementedError("AgentWorker.stop pending Bundle G integration")


__all__ = ["AgentWorker"]
