"""WorkflowStateMachine — Workflow §1 (3+ε) transition engine.

Workflow §12.5 #8 (Bundle G — Orchestrator). The state machine is the
*only* legal stage-mutator for a Request. Callers signal stage events
(``framed``, ``agent_started``, ``settled``, ``cleaned``) and the SM
returns the new :class:`WorkflowState`.

Stages (Workflow §1):

* ``receive`` — TriggerEvent landed; Request row created
* ``frame``  — skill match + artifact-type hint resolved
* ``agent_loop`` — RunAttempt running through Bundle X
* ``epsilon`` — DeliveryResult emitted, Request closed

Transitions are total (every event has either a legal new stage or raises
``InvalidTransitionError``). The SM is stateless; pass in the current
:class:`WorkflowState` and one ``event`` string.
"""

from __future__ import annotations

from typing import Final, cast

import structlog

from backend.orchestrator.schema import Stage, WorkflowState

logger = structlog.get_logger(__name__)


# Stage transition table — keyed by (current_stage, event). The new stage
# is the value. Anything not in this map is an InvalidTransitionError.
_TRANSITIONS: Final[dict[tuple[str, str], str]] = {
    ("receive", "framed"): "frame",
    ("frame", "agent_started"): "agent_loop",
    ("agent_loop", "settled"): "epsilon",
    ("epsilon", "cleaned"): "epsilon",
    # Recovery: a Request can re-enter agent_loop from epsilon if a
    # follow-up RunAttempt is required (e.g. canon decision deferred).
    ("epsilon", "agent_restarted"): "agent_loop",
}


class InvalidTransitionError(RuntimeError):
    """No legal transition for ``(stage, event)``."""


class WorkflowStateMachine:
    """3+ε stage transitions — receive → frame → agent_loop → epsilon."""

    async def transition(
        self,
        *,
        state: WorkflowState,
        event: str,
    ) -> WorkflowState:
        """Apply ``event`` to ``state`` and return the next state."""
        key = (state.stage, event)
        if key not in _TRANSITIONS:
            raise InvalidTransitionError(f"No transition for stage={state.stage!r} event={event!r}")
        new_stage = _TRANSITIONS[key]
        logger.info(
            "workflow_sm_transition",
            from_stage=state.stage,
            to_stage=new_stage,
            trigger_event=event,
            request_id=str(state.request_id),
        )
        return WorkflowState(
            stage=cast(Stage, new_stage),
            request_id=state.request_id,
            run_id=state.run_id,
        )


__all__ = ["InvalidTransitionError", "WorkflowStateMachine"]
