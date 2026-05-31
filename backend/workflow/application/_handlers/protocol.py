"""The TransitionHandler protocol — the shape every H2c handler implements.

v8 §7.3. A handler is a *thin* application-layer adapter: given a Run,
the current coarse ``WorkflowState``, and the triggering
``WorkflowEvent``, it carries out the stage's side effect (typically by
delegating to one of the canonical Workflow application services) and
returns the next coarse state.

The driver in :mod:`backend.workflow.application.state_machine_driver`
looks the handler up by name (the matrix slot's ``handler_name``) and
invokes it. The handler MUST return the matrix's ``to_state`` — the
driver compares the two to guard against mismatched wiring.

Handlers do NOT mutate the matrix or compose other handlers. Cross-stage
recovery (``fail``/``abandon``/``expire``) is encoded directly in the
:data:`backend.workflow.domain.transitions.CROSS_STAGE_TRANSITIONS` table
and each cross-stage handler is invoked exactly like a per-(state,event)
handler.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from backend.workflow.domain.state import WorkflowEvent, WorkflowState

if TYPE_CHECKING:
    pass


@runtime_checkable
class TransitionHandler(Protocol):
    """The contract every H2c handler class implements."""

    async def handle(
        self,
        *,
        run: Any,
        current_state: WorkflowState,
        event: WorkflowEvent,
    ) -> WorkflowState:
        """Perform the side effect for this transition and return the new state.

        ``run`` is intentionally typed ``Any`` at the protocol layer — the
        Workflow context spans multiple persistence rows (``RequestRow``,
        ``ExecutionRun``, ``WorkStep``, ``Deliverable``) and the concrete
        handler narrows the type at the implementation site. Tests pass a
        ``MagicMock``; production callers pass the canonical row for the
        stage.
        """
        ...


__all__ = ["TransitionHandler"]
