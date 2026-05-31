"""Deliver-stage transition handlers — ``deliver_complete`` + ``expire``.

Delivery is owned by :class:`backend.workflow.infrastructure.workers.delivery_worker.DeliveryWorker`
today: it consumes the delivery activity stream, applies the per-Run
Safe Mode gate (binding ``output_mode``), and pushes the Deliverable
through the connector tail. The handler here is the H3+ driver entry
point.

``DeliverCompleteHandler`` stays a ``NotImplementedError`` stub — its
delegation target is the worker's per-Deliverable dispatch path. H3
will lift that path into a Workflow-context service.

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

    Today the DeliveryWorker dispatches per-Deliverable to the
    connector tail; H3 will lift the dispatch into a Workflow-context
    service this handler can delegate to.
    """

    async def handle(
        self,
        *,
        run: Any,
        current_state: WorkflowState,
        event: WorkflowEvent,
    ) -> WorkflowState:
        # TODO(H3): delegate to a Workflow-owned delivery service.
        # Currently inside backend.workflow.infrastructure.workers.delivery_worker.
        raise NotImplementedError(
            "DeliverCompleteHandler — H3 will lift the delivery dispatch "
            "into a Workflow-owned service."
        )


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
