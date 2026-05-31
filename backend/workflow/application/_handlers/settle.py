"""Settle-stage transition handlers — ``ship`` + ``settle_complete``.

Settle covers ship (fast-forward main onto the verified run branch) and
the post-ship knowledge settle drain (BSage compile_batch over the
written paths).

The ship side effect lives in :meth:`AgentRunner._auto_ship_product_run`
today; the settle drain lives in
:mod:`backend.knowledge.settle` and is wired via the
:class:`backend.workers.settle_worker.SettleWorker`. H2c keeps both
intact and wires the handlers as scaffolding — the driver returns the
next coarse state; the existing services continue to do the work.

``SettleCompleteHandler`` stays a ``NotImplementedError`` stub. Its
delegation target is the settle drain that today is invoked by
:class:`SettleWorker` directly off the audit/settle activity stream; H3
will promote the drain into a Workflow-context service that this handler
can delegate to.
"""

from __future__ import annotations

from typing import Any

import structlog

from backend.workflow.domain.state import WorkflowEvent, WorkflowState

logger = structlog.get_logger(__name__)


class ShipHandler:
    """``verified → shipped`` via ``ship``.

    Today :meth:`AgentRunner.transition` invokes
    :meth:`AgentRunner._auto_ship_product_run` inline (W2) when a
    product-bound run reaches REVIEW_READY. The H3+ driver routes the
    transition through this handler.
    """

    async def handle(
        self,
        *,
        run: Any,
        current_state: WorkflowState,
        event: WorkflowEvent,
    ) -> WorkflowState:
        logger.debug(
            "ship_handler",
            run_id=str(getattr(run, "id", None)),
            from_state=current_state.value,
            workflow_event=event.value,
        )
        return WorkflowState.shipped


class SettleCompleteHandler:
    """``shipped → settled`` via ``settle_complete``.

    The settle drain (knowledge ingestion of the run's written paths +
    canon pattern fold) runs inside
    :class:`backend.workers.settle_worker.SettleWorker`. H3 will lift the
    drain into a Workflow-context service this handler can delegate to.
    """

    async def handle(
        self,
        *,
        run: Any,
        current_state: WorkflowState,
        event: WorkflowEvent,
    ) -> WorkflowState:
        # TODO(H3): delegate to a Workflow-owned settle service. Today
        # the drain is implemented inside SettleWorker + the knowledge
        # subsystem; this handler will become the single entry point
        # once that service exists.
        raise NotImplementedError(
            "SettleCompleteHandler — H3 will lift the settle drain into a Workflow-owned service."
        )


__all__ = ["SettleCompleteHandler", "ShipHandler"]
