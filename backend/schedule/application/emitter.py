"""ScheduleTrigger — cron-fired TriggerEvent producer.

Workflow §12.5 #8 (Bundle G — Intake / Triggers). The cron evaluator
itself lives in the runner (:mod:`backend.schedule.infrastructure.db_poll_runner`);
this adapter turns a *fire time* into a :class:`TriggerEvent` + persists
it idempotently.

The idempotency_key is ``<plugin_name>:<cron_fire_iso>`` so re-firing the
same cron tick (e.g. on worker restart) is a no-op at the intake table.

Cross-context note
------------------

Schedule is a *producer* of inbound triggers; the Workflow context owns
the inbound queue. This module therefore writes into the Workflow
context's persistence — :class:`TriggerEvent` (domain envelope),
:class:`TriggerEventRow` (SQLAlchemy row), and the idempotency helpers
all live in :mod:`backend.workflow`. The Schedule context's
contribution is the ``ScheduleTrigger`` adapter + the
``workspace_schedules`` table the runner polls.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.identity.workspaces_db import load_workspace_language
from backend.schedule.domain.product_tick import product_tick_instruction
from backend.schedule.infrastructure.schedule_db import SCHEDULE_KIND_PRODUCT_TICK
from backend.workflow.application.intake.webhook import WebhookOutcome
from backend.workflow.domain.incoming import TriggerEvent
from backend.workflow.infrastructure.idempotency import is_duplicate, record
from backend.workflow.infrastructure.intake.db import TriggerEventRow, TriggerKind

logger = structlog.get_logger(__name__)

# The constant ``source`` for every schedule-fired TriggerEvent. The
# idempotency window is keyed by the schedule row's surrogate ``id`` (moved
# OFF ``plugin_name``, which is NULL for the S1 ``instruction`` kind), so a
# fixed source keeps the ``(workspace_id, source, idempotency_key)`` unique
# constraint one-per-(schedule, window).
_SCHEDULE_SOURCE = "schedule"


class ScheduleTrigger:
    """Turn a scheduler firing into a :class:`TriggerEvent`."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def fire(
        self,
        *,
        workspace_id: uuid.UUID,
        schedule_id: uuid.UUID,
        kind: str,
        schedule_payload: dict[str, Any] | None,
        cron_expr: str,
        plugin_name: str | None = None,
        fired_at: datetime | None = None,
        product_id: uuid.UUID | None = None,
    ) -> WebhookOutcome:
        """Produce + persist a TriggerEvent for a fired schedule window.

        The instruction (``schedule_payload["text"]``) is merged into the
        TriggerEvent payload under ``text`` — the key the run framer reads
        (``_request_intent_text``). Without this the run had no instruction to
        act on and framed as literally "Untitled run" (the invisible half of
        the dead channel). The idempotency key is
        ``<schedule_id>:<window_iso>`` so two ticks in the SAME window collapse
        to one Request at the unique constraint (window = the row's own
        ``next_run_at``, passed as ``fired_at`` by the runner).
        """
        now = fired_at or datetime.now(tz=UTC)
        idem = f"{schedule_id}:{now.isoformat()}"
        if kind == SCHEDULE_KIND_PRODUCT_TICK:
            # The founder set only the cadence; BSVibe decides WHAT to do. Seed a
            # localized meta-instruction (workspaces.language) so the run frames a
            # real "decide + do the next action for THIS product" task instead of
            # the (unused) schedule text — the agent loop reads knowledge/history
            # and acts, or asks via ask_user_question.
            language = await load_workspace_language(self._session, workspace_id)
            instruction = product_tick_instruction(language)
        else:
            instruction = ""
            if isinstance(schedule_payload, dict):
                text_value = schedule_payload.get("text")
                if isinstance(text_value, str):
                    instruction = text_value
        # ``trigger=schedule`` is the glass-box marker (Brief/Run provenance);
        # ``text`` is the instruction the framer acts on. Stamped here at the
        # emitter site so EVERY caller (the ``ScheduleWorker`` or a future
        # direct-CLI ``schedule fire``) gets both for free.
        payload: dict[str, Any] = {
            "text": instruction,
            "trigger": "schedule",
            "kind": kind,
            "schedule_id": str(schedule_id),
            "cron_expr": cron_expr,
            "fired_at": now.isoformat(),
        }
        if plugin_name is not None:
            payload["plugin"] = plugin_name
        event = TriggerEvent(
            workspace_id=workspace_id,
            source=_SCHEDULE_SOURCE,
            trigger_kind="schedule",
            idempotency_key=idem,
            payload=payload,
            product_id=product_id,
            received_at=now,
        )
        if await is_duplicate(
            self._session,
            workspace_id=workspace_id,
            source=_SCHEDULE_SOURCE,
            idempotency_key=idem,
        ):
            logger.info(
                "schedule_duplicate",
                workspace_id=str(workspace_id),
                schedule_id=str(schedule_id),
                fired_at=now.isoformat(),
            )
            return WebhookOutcome(event=event, duplicate=True)

        row = TriggerEventRow(
            id=uuid.uuid4(),
            workspace_id=workspace_id,
            product_id=product_id,
            source=_SCHEDULE_SOURCE,
            trigger_kind=TriggerKind.SCHEDULE,
            idempotency_key=idem,
            payload=event.payload,
            received_at=now,
        )
        try:
            await record(self._session, row=row, producer_id="schedule:schedule_trigger")
        except IntegrityError:
            await self._session.rollback()
            return WebhookOutcome(event=event, duplicate=True)
        logger.info(
            "schedule_fired",
            workspace_id=str(workspace_id),
            schedule_id=str(schedule_id),
            fired_at=now.isoformat(),
            trigger_event_id=str(row.id),
        )
        return WebhookOutcome(event=event, duplicate=False)


__all__ = ["ScheduleTrigger"]
