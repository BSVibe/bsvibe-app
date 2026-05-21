"""DirectTrigger — founder-direct text submission to TriggerEvent.

Workflow §12.5 #8 (Bundle G — Intake / Triggers). This is the
``source="direct"`` path: a founder pastes / types a request into the
web UI or CLI and we want it on the workflow exactly the same way an
inbound webhook would land.
"""

from __future__ import annotations

import uuid

import structlog

from backend.intake.schema import TriggerEvent

logger = structlog.get_logger(__name__)


class DirectTrigger:
    """Convert a founder's typed input into a :class:`TriggerEvent`.

    The idempotency_key is typically a content hash of ``text`` so an
    accidental double-submit collapses.
    """

    async def submit(
        self,
        *,
        workspace_id: uuid.UUID,
        founder_id: uuid.UUID,
        text: str,
    ) -> TriggerEvent:
        """Adapt one direct submission into a TriggerEvent."""
        # TODO(bundle-g-integration): concrete lift from BSNexus
        # backend/api/direct_submit.py + content-hash idempotency.
        logger.debug(
            "direct_trigger_stub",
            workspace_id=str(workspace_id),
            founder_id=str(founder_id),
            text_chars=len(text),
        )
        raise NotImplementedError("DirectTrigger.submit pending Bundle G integration")


__all__ = ["DirectTrigger"]
