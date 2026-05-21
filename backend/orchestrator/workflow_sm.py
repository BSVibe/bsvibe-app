"""WorkflowStateMachine — Workflow §1 (3+ε) transition engine.

Workflow §12.5 #8 (Bundle G — Orchestrator). The state machine is the
*only* legal stage-mutator for a Request. Callers signal stage events
(``framed``, ``agent_started``, ``settled``, ``cleaned``) and the SM
returns the new :class:`WorkflowState`.
"""

from __future__ import annotations

import structlog

from backend.orchestrator.schema import WorkflowState

logger = structlog.get_logger(__name__)


class WorkflowStateMachine:
    """3+ε stage transitions — receive → frame → agent_loop → epsilon."""

    async def transition(
        self,
        *,
        state: WorkflowState,
        event: str,
    ) -> WorkflowState:
        """Apply ``event`` to ``state`` and return the next state."""
        # TODO(bundle-g-integration): concrete transition map. Anchored
        # in Workflow §1 — three required stages plus ε terminal.
        # Lift target: backend/execution/state_machine.py transition
        # patterns, but operating at request granularity rather than
        # run_attempt granularity.
        logger.debug(
            "workflow_sm_transition_stub",
            stage=state.stage,
            request_id=str(state.request_id),
            event=event,
        )
        raise NotImplementedError("WorkflowStateMachine.transition pending Bundle G integration")


__all__ = ["WorkflowStateMachine"]
