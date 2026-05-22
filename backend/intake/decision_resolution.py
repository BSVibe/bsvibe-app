"""DecisionResolutionTrigger — re-dispatch on resolved decision.

Workflow §12.5 #8 (Bundle G — Intake / Triggers). When a founder
resolves a ``needs_decision`` checkpoint (Workflow §5 #4) the workflow
needs to continue — this trigger is the re-entry point.
"""

from __future__ import annotations

import uuid

import structlog

from backend.intake.schema import TriggerEvent

logger = structlog.get_logger(__name__)


class DecisionResolutionTrigger:
    """Re-dispatch a paused Request on decision resolution.

    The trigger envelope carries the originating decision_id in the
    payload so the orchestrator can wire the resolved choice into the
    next ``run_attempt``.
    """

    async def re_dispatch(
        self,
        *,
        workspace_id: uuid.UUID,
        decision_id: uuid.UUID,
    ) -> TriggerEvent:
        """Produce a TriggerEvent that resumes the paused Request."""
        # TODO(bundle-g-integration): concrete lift from BSNexus
        # backend/execution/decisions.py.on_resolve — joins decision row
        # → request_id, then emits TriggerEvent(trigger_kind="decision_resolution").
        logger.debug(
            "decision_resolution_trigger_stub",
            workspace_id=str(workspace_id),
            decision_id=str(decision_id),
        )
        raise NotImplementedError(
            "DecisionResolutionTrigger.re_dispatch pending Bundle G integration"
        )


__all__ = ["DecisionResolutionTrigger"]
