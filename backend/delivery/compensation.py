"""CompensationHandler — post-delivery revert / supersede / notify.

Workflow §12.5 #8 (Bundle G — Delivery) and Workflow §9.x. When a shipped
deliverable is later found to be wrong (verification regression, founder
rejection, replacement deliverable shipped), compensation decides whether
to revert it, mark it superseded, or just notify.

Decision rules (Phase 1):

* If a NEWER deliverable for the same run exists with same artifact_type
  → ``supersede``
* If the verification stream for the deliverable's run flipped to
  ``failed`` after delivery → ``revert``
* If the deliverable is ``direct_output`` (already user-facing, can't
  undo) → ``notify``
* Otherwise → ``None`` (no compensation needed)
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.delivery.schema import CompensationResult
from backend.execution.db import (
    Deliverable,
    DeliverableType,
    VerificationOutcome,
    VerificationResult,
)

logger = structlog.get_logger(__name__)


@dataclass(slots=True)
class CompensationConfig:
    notify_only_for_direct_output: bool = True


class CompensationHandler:
    """Evaluate compensation for one delivered artifact."""

    def __init__(self, session: AsyncSession, config: CompensationConfig | None = None) -> None:
        self._session = session
        self._cfg = config or CompensationConfig()

    async def evaluate(self, *, deliverable_id: uuid.UUID) -> CompensationResult | None:
        """Decide whether to compensate for a shipped deliverable."""
        deliv = await self._session.get(Deliverable, deliverable_id)
        if deliv is None:
            return None

        if await self._has_superseding(deliv):
            return CompensationResult(
                deliverable_id=deliverable_id,
                action="supersede",
                reason=f"newer {deliv.deliverable_type.value} delivered for same run",
            )

        if await self._verification_failed(deliv):
            if (
                self._cfg.notify_only_for_direct_output
                and deliv.deliverable_type is DeliverableType.DIRECT_OUTPUT
            ):
                return CompensationResult(
                    deliverable_id=deliverable_id,
                    action="notify",
                    reason="verification failed; direct_output can't be reverted",
                )
            return CompensationResult(
                deliverable_id=deliverable_id,
                action="revert",
                reason="verification flipped to failed after delivery",
            )

        return None

    async def _has_superseding(self, deliv: Deliverable) -> bool:
        """True iff a newer deliverable of the same type exists on the same run."""
        stmt = (
            select(Deliverable)
            .where(
                Deliverable.run_id == deliv.run_id,
                Deliverable.deliverable_type == deliv.deliverable_type,
                Deliverable.id != deliv.id,
                Deliverable.created_at > deliv.created_at,
            )
            .limit(1)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none() is not None

    async def _verification_failed(self, deliv: Deliverable) -> bool:
        """True iff any verification_results row for this run has FAILED outcome."""
        stmt = select(VerificationResult).where(
            VerificationResult.run_id == deliv.run_id,
            VerificationResult.outcome == VerificationOutcome.FAILED,
            VerificationResult.created_at > deliv.created_at,
        )
        return (await self._session.execute(stmt)).scalar_one_or_none() is not None


__all__ = ["CompensationConfig", "CompensationHandler"]
