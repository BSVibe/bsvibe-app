"""SafeModeBoundary — gate outbound delivery on workspace Safe Mode.

Workflow §12.5 #8 (Bundle G — Orchestrator) and Workflow §10.5. The
boundary is the choke point between ``shipped`` deliverables and the
delivery dispatcher: in Safe Mode the deliverable goes into the
:class:`backend.delivery.SafeModeQueue`; out of Safe Mode it
auto-dispatches.
"""

from __future__ import annotations

import uuid

import structlog

logger = structlog.get_logger(__name__)


class SafeModeBoundary:
    """Per-deliverable decision: auto-dispatch vs queue for approval."""

    async def gate(self, *, deliverable_id: uuid.UUID) -> bool:
        """Return ``True`` to auto-dispatch, ``False`` to queue.

        The decision joins workspace ``safe_mode`` flag + per-artifact-type
        policy. When the flag is on (founder-set) every deliverable is
        queued. When off, the policy decides.
        """
        # TODO(bundle-g-integration): SELECT workspace.safe_mode + apply
        # per-artifact policy from backend.workflow.application.safe_mode_queue.
        logger.debug(
            "safe_mode_gate_stub",
            deliverable_id=str(deliverable_id),
        )
        raise NotImplementedError("SafeModeBoundary.gate pending Bundle G integration")


__all__ = ["SafeModeBoundary"]
