"""Workflow-context channel declarations (INV-1).

Declares the channels whose rows live in the Workflow context, next to those
rows. Imports only Workflow-context rows + the generic :class:`Channel` core —
no cross-context imports, so a producer/consumer that depends on this module
does not transitively pull in another context.

``trigger_events`` is the inbound trigger queue: the Schedule context's
``ScheduleTrigger`` and the Workflow intake receivers (webhook / direct) are
its producers; the :class:`~backend.workflow.infrastructure.workers.intake_worker.IntakeWorker`
is its sole consumer (it drains each un-paired ``TriggerEventRow`` into a
``RequestRow``). The rows are machine-produced (cron tick + inbound
webhook/direct submission), so ``human_origin=False`` and there is no
authoring surface.
"""

from __future__ import annotations

from backend.channels import Channel
from backend.workflow.infrastructure.intake.db import TriggerEventRow

TRIGGER_EVENTS: Channel[TriggerEventRow] = Channel(
    name="trigger_events",
    row=TriggerEventRow,
    producers=(
        "schedule:schedule_trigger",
        "workflow:webhook_receiver",
        "workflow:direct_trigger",
    ),
    consumers=("worker:intake_worker",),
    human_origin=False,
)

__all__ = ["TRIGGER_EVENTS"]
