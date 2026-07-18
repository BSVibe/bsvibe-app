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

``requests`` is the next hop of the same pipeline: the IntakeWorker is its
sole producer (each drained TriggerEvent mints one ``RequestRow`` with status
``OPEN``) and the
:class:`~backend.workflow.infrastructure.workers.agent_worker.AgentWorker` is
its sole consumer (it claims OPEN requests off the head of the queue to drive
each run). A Request is machine-minted, so ``human_origin=False`` and there is
no authoring surface.

``safe_mode_queue_items`` is the founder approval gate for outbound deliveries
(Workflow §10.5). The
:class:`~backend.workflow.infrastructure.workers.delivery_worker.DeliveryWorker`
is its sole producer — when the output-mode gate says HOLD it enqueues a
``pending`` item (via the :class:`~backend.workflow.application.safe_mode_queue.SafeModeQueue`
service) instead of dispatching. Its sole worker-claim consumer is the
:class:`~backend.workflow.application.safe_mode_expiry.SafeModeExpirySweepRunner`,
which claims every ``PENDING``/``EXTENDED`` row past ``expires_at`` (across all
workspaces) to sweep it to ``EXPIRED``. The founder-facing REST/MCP reads
(``get`` / ``list_pending_*`` / ``list_resolved_*``) are API reads, not
worker-claims, so they stay off the channel. A held item is machine-enqueued,
so ``human_origin=False`` and there is no authoring surface.

``delivery_events`` is the outbound-dispatch queue (Workflow §12.5 #8 — Bundle
G). It has three machine producers, all in
:mod:`~backend.workflow.domain.verified_deliverable` — the single source of
truth for the artifact-write contract:
:func:`~backend.workflow.domain.verified_deliverable.write_verified_deliverable`
(the verified terminal, shared by the native agent loop and the external CLI
executor), :func:`~backend.workflow.domain.verified_deliverable.write_partial_deliverable`
(each mid-loop ``emit_deliverable`` tool call), and
:func:`~backend.workflow.domain.verified_deliverable.write_answer_deliverable`
(a knowledge-only answer). Its sole consumer is the
:class:`~backend.workflow.infrastructure.workers.delivery_worker.DeliveryWorker`,
which claims a batch under ``FOR UPDATE SKIP LOCKED`` and dispatches (or holds
via Safe Mode) each row before deleting it. There are no API reads of this row
— it is a pure internal queue. Deliver events are machine-emitted, so
``human_origin=False`` and there is no authoring surface.
"""

from __future__ import annotations

from backend.channels import Channel
from backend.workflow.infrastructure.delivery.db import DeliveryEventRow, SafeModeQueueItemRow
from backend.workflow.infrastructure.intake.db import RequestRow, TriggerEventRow

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

REQUESTS: Channel[RequestRow] = Channel(
    name="requests",
    row=RequestRow,
    producers=("worker:intake_worker",),
    consumers=("worker:agent_worker",),
    human_origin=False,
)

SAFE_MODE_QUEUE_ITEMS: Channel[SafeModeQueueItemRow] = Channel(
    name="safe_mode_queue_items",
    row=SafeModeQueueItemRow,
    producers=("worker:delivery_worker",),
    consumers=("worker:safe_mode_expiry_sweep",),
    human_origin=False,
)

DELIVERY_EVENTS: Channel[DeliveryEventRow] = Channel(
    name="delivery_events",
    row=DeliveryEventRow,
    producers=(
        "workflow:verified_deliverable",
        "workflow:partial_deliverable",
        "workflow:answer_deliverable",
    ),
    consumers=("worker:delivery_worker",),
    human_origin=False,
)

__all__ = ["DELIVERY_EVENTS", "REQUESTS", "SAFE_MODE_QUEUE_ITEMS", "TRIGGER_EVENTS"]
