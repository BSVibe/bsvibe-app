"""CompensationHandler — post-delivery revert / supersede / notify.

Workflow §12.5 #8 (Bundle G — Delivery) and Workflow §10.5. When a
shipped deliverable is later found to be wrong (verification regression,
founder rejection, replacement deliverable shipped), compensation
decides whether to revert it, mark it superseded, or just notify.
"""

from __future__ import annotations

import uuid

import structlog

from backend.delivery.schema import CompensationResult

logger = structlog.get_logger(__name__)


class CompensationHandler:
    """Evaluate compensation for one delivered artifact.

    Returns ``None`` when no compensation is warranted (the common
    case); a :class:`CompensationResult` when revert / supersede /
    notify is required.
    """

    async def evaluate(
        self,
        *,
        deliverable_id: uuid.UUID,
    ) -> CompensationResult | None:
        """Decide whether to compensate for a shipped deliverable."""
        # TODO(bundle-g-integration): cross-reference deliverable status
        # (rejected vs shipped) + downstream verification regressions
        # + presence of superseding deliverable.
        logger.debug(
            "compensation_evaluate_stub",
            deliverable_id=str(deliverable_id),
        )
        raise NotImplementedError("CompensationHandler.evaluate pending Bundle G integration")


__all__ = ["CompensationHandler"]
