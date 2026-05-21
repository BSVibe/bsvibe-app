"""AgentRunner — drive the execution layer for one Request.

Workflow §12.5 #8 (Bundle G — Orchestrator). The agent runner is the
bridge to the execution layer (Bundle X): it mints a ``run_attempt``,
hands off to the agent loop, and rolls the resulting deliverable state
back into the workflow state machine.
"""

from __future__ import annotations

import uuid

import structlog

logger = structlog.get_logger(__name__)


class AgentRunner:
    """Spawn + supervise one execution run for a Request."""

    async def run_request(self, *, request_id: uuid.UUID) -> None:
        """Run the request to a settled state.

        Returns when the underlying ``run_attempt`` reaches a terminal
        status (completed / failed / timed_out). Compensation is
        handled by :class:`backend.delivery.CompensationHandler`, not
        here.
        """
        # TODO(bundle-g-integration): wire backend.execution.orchestrator.
        # Specifically: load Request, mint run_attempt via
        # backend.execution.run_attempts.RunAttemptService.start, then
        # await backend.execution.run_attempt_executor.RunAttemptExecutor.execute.
        logger.debug(
            "agent_runner_stub",
            request_id=str(request_id),
        )
        raise NotImplementedError("AgentRunner.run_request pending Bundle G integration")


__all__ = ["AgentRunner"]
