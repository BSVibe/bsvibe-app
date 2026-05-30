"""ScheduleTrigger — cron-fired TriggerEvent producer.

Workflow §12.5 #8 (Bundle G — Intake / Triggers). The cron evaluator itself
lives in the scheduler (a separate worker, deferred); this adapter turns a
*fire time* into a :class:`TriggerEvent` + persists it idempotently.

The idempotency_key is ``<plugin_name>:<cron_fire_iso>`` so re-firing the
same cron tick (e.g. on worker restart) is a no-op at the intake table.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import structlog
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.intake.db import TriggerEventRow, TriggerKind
from backend.intake.idempotency import is_duplicate, record
from backend.intake.schema import TriggerEvent
from backend.intake.webhook import WebhookOutcome

logger = structlog.get_logger(__name__)


class ScheduleTrigger:
    """Turn a scheduler firing into a :class:`TriggerEvent`."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def fire(
        self,
        *,
        workspace_id: uuid.UUID,
        plugin_name: str,
        cron_expr: str,
        fired_at: datetime | None = None,
        product_id: uuid.UUID | None = None,
    ) -> WebhookOutcome:
        """Produce + persist a TriggerEvent for a fired cron tick."""
        now = fired_at or datetime.now(tz=UTC)
        idem = f"{plugin_name}:{now.isoformat()}"
        event = TriggerEvent(
            workspace_id=workspace_id,
            source=plugin_name,
            trigger_kind="schedule",
            idempotency_key=idem,
            # ``trigger=schedule`` is the M1 glass-box marker — IntakeWorker
            # copies the trigger payload onto ``RequestRow.payload`` via
            # :func:`backend.intake.receive.receive`, so the Brief / Run views
            # can tell the run came from a schedule (not a Direct ask, a
            # connector inbound, or a decision resolution). Stamped here at
            # the emitter site so EVERY caller (the M1 ``ScheduleWorker`` or a
            # future direct-CLI ``schedule fire`` invocation) gets it for free.
            payload={
                "plugin": plugin_name,
                "cron_expr": cron_expr,
                "fired_at": now.isoformat(),
                "trigger": "schedule",
            },
            product_id=product_id,
            received_at=now,
        )
        if await is_duplicate(
            self._session,
            workspace_id=workspace_id,
            source=plugin_name,
            idempotency_key=idem,
        ):
            logger.info(
                "schedule_duplicate",
                workspace_id=str(workspace_id),
                plugin_name=plugin_name,
                fired_at=now.isoformat(),
            )
            return WebhookOutcome(event=event, duplicate=True)

        row = TriggerEventRow(
            id=uuid.uuid4(),
            workspace_id=workspace_id,
            product_id=product_id,
            source=plugin_name,
            trigger_kind=TriggerKind.SCHEDULE,
            idempotency_key=idem,
            payload=event.payload,
            received_at=now,
        )
        try:
            await record(self._session, row=row)
        except IntegrityError:
            await self._session.rollback()
            return WebhookOutcome(event=event, duplicate=True)
        logger.info(
            "schedule_fired",
            workspace_id=str(workspace_id),
            plugin_name=plugin_name,
            fired_at=now.isoformat(),
            trigger_event_id=str(row.id),
        )
        return WebhookOutcome(event=event, duplicate=False)


__all__ = ["ScheduleTrigger"]
