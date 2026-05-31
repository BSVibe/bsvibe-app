"""WebhookReceiver — adapt inbound webhook payloads into a TriggerEvent.

Workflow §12.5 #8 (Bundle G — Intake / Triggers). The receiver:

1. Computes a stable ``idempotency_key`` from a caller-supplied header
   (falls back to a SHA-256 hash of the body when absent).
2. Checks the ``(workspace_id, source, idempotency_key)`` uniqueness against
   ``backend.workflow.infrastructure.intake.db.TriggerEventRow``.
3. Inserts a row + emits a
   :class:`backend.workflow.domain.incoming.TriggerEvent`.

Per-plugin signature verification (HMAC, etc.) is left to the plugin
adapter (Workflow §6 #2) — this module assumes the headers have already
been authenticated by an upstream FastAPI dependency.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.workflow.domain.incoming import TriggerEvent
from backend.workflow.infrastructure.idempotency import is_duplicate, record
from backend.workflow.infrastructure.intake.db import (
    RequestStatus,
    TriggerEventRow,
    TriggerKind,
)

logger = structlog.get_logger(__name__)

_IDEMPOTENCY_HEADERS = (
    "X-Idempotency-Key",
    "x-idempotency-key",
    "X-GitHub-Delivery",
    "x-github-delivery",
    "Linear-Delivery",
)


@dataclass(slots=True)
class WebhookOutcome:
    """Result of a webhook attempt.

    ``duplicate=True`` means the receiver short-circuited because the
    composite key already existed; ``event`` is then the original
    ``TriggerEvent`` envelope (still useful for telemetry).
    """

    event: TriggerEvent
    duplicate: bool


def _derive_idempotency_key(headers: dict[str, str], body: dict[str, Any]) -> str:
    for h in _IDEMPOTENCY_HEADERS:
        if h in headers and headers[h]:
            return headers[h]
    payload = json.dumps(body, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class WebhookReceiver:
    """Adapt one inbound plugin webhook into a :class:`TriggerEvent`."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def handle(
        self,
        *,
        workspace_id: uuid.UUID,
        source: str,
        headers: dict[str, str],
        body: dict[str, Any],
        product_id: uuid.UUID | None = None,
        trace_id: str | None = None,
    ) -> WebhookOutcome:
        """Produce + persist a TriggerEvent for this delivery."""
        idem = _derive_idempotency_key(headers, body)
        now = datetime.now(tz=UTC)
        event = TriggerEvent(
            workspace_id=workspace_id,
            source=source,
            trigger_kind="webhook",
            idempotency_key=idem,
            payload=body,
            product_id=product_id,
            trace_id=trace_id,
            received_at=now,
        )

        if await is_duplicate(
            self._session,
            workspace_id=workspace_id,
            source=source,
            idempotency_key=idem,
        ):
            logger.info(
                "webhook_duplicate",
                workspace_id=str(workspace_id),
                source=source,
                idempotency_key=idem,
            )
            return WebhookOutcome(event=event, duplicate=True)

        row = TriggerEventRow(
            id=uuid.uuid4(),
            workspace_id=workspace_id,
            product_id=product_id,
            source=source,
            trigger_kind=TriggerKind.WEBHOOK,
            idempotency_key=idem,
            payload=body,
            trace_id=trace_id,
            received_at=now,
        )
        try:
            await record(self._session, row=row)
        except IntegrityError:
            # Race: another writer landed the same key between the
            # is_duplicate check and the INSERT. Surface as duplicate.
            await self._session.rollback()
            logger.info(
                "webhook_race_duplicate",
                workspace_id=str(workspace_id),
                source=source,
                idempotency_key=idem,
            )
            return WebhookOutcome(event=event, duplicate=True)

        logger.info(
            "webhook_received",
            workspace_id=str(workspace_id),
            source=source,
            idempotency_key=idem,
            trigger_event_id=str(row.id),
        )
        return WebhookOutcome(event=event, duplicate=False)


# Re-export so callers don't have to chase the db module separately.
__all__ = ["RequestStatus", "WebhookOutcome", "WebhookReceiver"]
