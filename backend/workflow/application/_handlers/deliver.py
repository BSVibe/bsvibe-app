"""Deliver-stage transition handlers — ``deliver_complete`` + ``expire``.

Delivery is owned by :class:`backend.workflow.infrastructure.workers.delivery_worker.DeliveryWorker`
today: it consumes the delivery activity stream, applies the per-Run
Safe Mode gate (binding ``output_mode``), and pushes the Deliverable
through the connector tail. The handler here is the H3+ driver entry
point.

``DeliverCompleteHandler`` is filled in Lift H3d — it references the
:class:`backend.workflow.application.delivery.dispatcher.DeliveryDispatcher`
delegation target (relocated into the workflow context by H3b). The
existing worker continues to drive the dispatch; the handler is the
H3+ driver entry point.

``ExpireHandler`` is the cross-stage TTL handler — when a Request's TTL
elapses without delivery, the watchdog flips it to ``expired``. The
watchdog (:class:`backend.intake.watchdog`) does the DB flip today; H3
routes through this handler.
"""

from __future__ import annotations

from typing import Any

import structlog

from backend.workflow.domain.state import WorkflowEvent, WorkflowState

logger = structlog.get_logger(__name__)


class DeliverCompleteHandler:
    """``settled → delivered`` via ``deliver_complete``.

    H3d wiring — the delegation target is
    :class:`backend.workflow.application.delivery.dispatcher.DeliveryDispatcher`
    (relocated into the workflow context by Lift H3b). The
    :class:`~backend.workflow.infrastructure.workers.delivery_worker.DeliveryWorker`
    drains ``DeliveryEventRow`` rows and calls the dispatcher per-row
    today; this handler is the H3+ driver entry point for that path.
    Existing callers keep working — this scaffold only advances the
    coarse state.
    """

    async def handle(
        self,
        *,
        run: Any,
        current_state: WorkflowState,
        event: WorkflowEvent,
    ) -> WorkflowState:
        # Delegation target — lazy import keeps the handler thin. The
        # actual fan-out side effect (plugins iterate, outbound dispatch,
        # compensation handle persist) happens inside
        # DeliveryDispatcher.dispatch + dispatch_delivery() in the
        # worker. This handler advances the state machine; future
        # driver-routed callers will invoke the dispatcher through it.
        from backend.workflow.application.delivery.dispatcher import (  # noqa: PLC0415
            DeliveryDispatcher,
        )

        logger.debug(
            "deliver_complete_handler",
            run_id=str(getattr(run, "id", None)),
            from_state=current_state.value,
            workflow_event=event.value,
            delegation_target=DeliveryDispatcher.__qualname__,
        )
        return WorkflowState.delivered


class ExpireHandler:
    """Cross-stage ``expire`` event → ``expired``.

    The intake watchdog flips Requests whose TTL has elapsed.
    """

    async def handle(
        self,
        *,
        run: Any,
        current_state: WorkflowState,
        event: WorkflowEvent,
    ) -> WorkflowState:
        logger.debug(
            "expire_handler",
            run_id=str(getattr(run, "id", None)),
            from_state=current_state.value,
            workflow_event=event.value,
        )
        return WorkflowState.expired


__all__ = ["DeliverCompleteHandler", "ExpireHandler"]
