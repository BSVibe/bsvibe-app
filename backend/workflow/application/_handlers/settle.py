"""Settle-stage transition handlers — ``ship`` + ``settle_complete``.

Settle covers ship (fast-forward main onto the verified run branch) and
the post-ship knowledge settle drain (BSage compile_batch over the
written paths).

The ship side effect lives in :meth:`AgentRunner._auto_ship_product_run`
today; the settle drain lives in
:mod:`backend.knowledge.settle` and is wired via the
:class:`backend.knowledge.infrastructure.workers.settle_worker.SettleWorker`. H2c keeps both
intact and wires the handlers as scaffolding — the driver returns the
next coarse state; the existing services continue to do the work.

``SettleCompleteHandler`` is filled in Lift H3d and wired to the
:class:`~backend.knowledge.facade.Knowledge` facade Protocol surface
in Lift I-Repo-Knowledge. The handler now references the facade
Protocol's ``settle`` method as the documented delegation target; the
existing :class:`~backend.knowledge.infrastructure.workers.settle_worker.SettleWorker`
polling loop continues to execute the drain on a polling cadence, and
the facade's concrete (:class:`~backend.knowledge.application.knowledge.SqlAlchemyKnowledge`)
routes the per-call settle through the same drain implementation, so
no behavior changes — only the coupling does.
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

    Lift I-Repo-Knowledge wiring — the delegation target is the
    :class:`~backend.knowledge.facade.Knowledge` facade Protocol's
    ``settle(*, workspace_id, region) -> int`` surface. The concrete
    (:class:`~backend.knowledge.application.knowledge.SqlAlchemyKnowledge`)
    forwards the call through to
    :meth:`backend.knowledge.infrastructure.workers.settle_worker.SettleWorker.drain_once`
    so behavior is unchanged from the H3d direct reference — only the
    coupling. The existing worker polling loop is still the canonical
    drain cadence; this handler advances the coarse state for
    driver-driven callers.
    """

    async def handle(
        self,
        *,
        run: Any,
        current_state: WorkflowState,
        event: WorkflowEvent,
    ) -> WorkflowState:
        # Delegation target — lazy import keeps the handler thin and
        # avoids the knowledge subsystem being imported at workflow
        # module-import time. Importing the facade Protocol (not the
        # concrete) is the v8 §5.2 invariant: workflow code depends on
        # the Knowledge interface, not the SettleWorker class.
        from backend.knowledge.facade import Knowledge  # noqa: PLC0415

        logger.debug(
            "settle_complete_handler",
            run_id=str(getattr(run, "id", None)),
            from_state=current_state.value,
            workflow_event=event.value,
            delegation_target=Knowledge.__qualname__,
        )
        return WorkflowState.settled


__all__ = ["SettleCompleteHandler", "ShipHandler"]
