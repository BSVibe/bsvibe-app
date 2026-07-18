"""DirectTrigger — founder-direct text submission to TriggerEvent.

Workflow §12.5 #8 (Bundle G — Intake / Triggers). The ``source="direct"``
path — founder pastes/types a request and we land it on the workflow the
same way an inbound webhook would.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime

import structlog
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.workflow.application.intake._factories import _new_trigger_row
from backend.workflow.application.intake.webhook import WebhookOutcome
from backend.workflow.domain.incoming import TriggerEvent
from backend.workflow.domain.repositories import IdempotencyRepository
from backend.workflow.infrastructure.intake.db import TriggerKind
from backend.workflow.infrastructure.repositories import SqlAlchemyIdempotencyRepository

logger = structlog.get_logger(__name__)


class DirectTrigger:
    """Convert a founder's typed input into a :class:`TriggerEvent`.

    The idempotency_key is a SHA-256 over ``(founder_id, text)`` so an
    accidental double-submit (same founder, same text) collapses; two
    different founders typing the same text produce distinct events.
    """

    def __init__(
        self,
        session: AsyncSession,
        *,
        idempotency_repository: IdempotencyRepository | None = None,
    ) -> None:
        self._session = session
        # Lift I-Repo-Workflow-3 — idempotency persistence routed through the
        # Repository Protocol; tests may inject a fake.
        self._idempotency: IdempotencyRepository = (
            idempotency_repository or SqlAlchemyIdempotencyRepository(session)
        )

    async def submit(
        self,
        *,
        workspace_id: uuid.UUID,
        founder_id: uuid.UUID,
        text: str,
        product_id: uuid.UUID | None = None,
        trace_id: str | None = None,
    ) -> WebhookOutcome:
        """Adapt one direct submission into a TriggerEvent + persist."""
        key_seed = f"{founder_id}:{text}".encode()
        idem = hashlib.sha256(key_seed).hexdigest()
        now = datetime.now(tz=UTC)
        event = TriggerEvent(
            workspace_id=workspace_id,
            source="direct",
            trigger_kind="direct",
            idempotency_key=idem,
            payload={"founder_id": str(founder_id), "text": text},
            product_id=product_id,
            trace_id=trace_id,
            received_at=now,
        )
        if await self._idempotency.is_duplicate(
            workspace_id=workspace_id,
            source="direct",
            idempotency_key=idem,
        ):
            logger.info(
                "direct_duplicate",
                workspace_id=str(workspace_id),
                founder_id=str(founder_id),
            )
            return WebhookOutcome(event=event, duplicate=True)

        row = _new_trigger_row(
            workspace_id=workspace_id,
            product_id=product_id,
            source="direct",
            kind=TriggerKind.DIRECT,
            idem=idem,
            payload=event.payload,
            trace_id=trace_id,
            received_at=now,
        )
        try:
            await self._idempotency.record(row, producer_id="workflow:direct_trigger")
        except IntegrityError:
            await self._session.rollback()
            return WebhookOutcome(event=event, duplicate=True)
        logger.info(
            "direct_submitted",
            workspace_id=str(workspace_id),
            founder_id=str(founder_id),
            trigger_event_id=str(row.id),
        )
        return WebhookOutcome(event=event, duplicate=False)


__all__ = ["DirectTrigger"]
