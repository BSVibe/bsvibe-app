"""ScheduleTrigger — cron-fired TriggerEvent producer.

Workflow §12.5 #8 (Bundle G — Intake / Triggers). Cron evaluation lives
in the scheduler (Bundle X / supervisor); this module is the adapter
that turns a fired schedule into a TriggerEvent.
"""

from __future__ import annotations

import uuid

import structlog

from backend.intake.schema import TriggerEvent

logger = structlog.get_logger(__name__)


class ScheduleTrigger:
    """Turn a scheduler firing into a :class:`TriggerEvent`.

    The idempotency_key here is typically ``<plugin_name>:<cron_fire_iso>``
    so re-firing the same cron tick is a no-op at the intake table.
    """

    async def fire(
        self,
        *,
        workspace_id: uuid.UUID,
        plugin_name: str,
        cron_expr: str,
    ) -> TriggerEvent:
        """Produce a TriggerEvent for a fired cron tick."""
        # TODO(bundle-g-integration): concrete lift from BSNexus
        # backend/scheduler/cron_runner.py — uses croniter to derive
        # the canonical fire-time, then builds the TriggerEvent.
        logger.debug(
            "schedule_trigger_stub",
            workspace_id=str(workspace_id),
            plugin_name=plugin_name,
            cron_expr=cron_expr,
        )
        raise NotImplementedError("ScheduleTrigger.fire pending Bundle G integration")


__all__ = ["ScheduleTrigger"]
