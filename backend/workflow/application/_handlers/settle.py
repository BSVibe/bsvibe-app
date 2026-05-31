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

``SettleCompleteHandler`` is filled in Lift H3d — it references the
:class:`backend.knowledge.infrastructure.workers.settle_worker.SettleWorker`
delegation target (Knowledge context). The
:class:`~backend.knowledge.facade.Knowledge` facade Protocol declares
the ``settle`` surface this handler will route through once Lift I
wires the facade to a concrete; H3d bypasses the Protocol in favor of
the direct SettleWorker reference, matching the prompt's "import the
actual settle function rather than going through the Protocol"
guidance.
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

    H3d wiring — the delegation target is the Knowledge context's
    settle drain, implemented by
    :class:`backend.knowledge.infrastructure.workers.settle_worker.SettleWorker`.
    The Knowledge facade Protocol (Lift A —
    :class:`backend.knowledge.facade.Knowledge`) declares the
    ``settle(*, workspace_id, region) -> int`` surface this handler
    will route through once the facade is wired to a concrete in Lift I.
    Until then we reference the SettleWorker directly — the existing
    caller (the worker's own polling loop) continues to execute the
    drain; this handler just advances the coarse state for future
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
        # module-import time. SettleWorker.drain_once is the concrete
        # impl today; Lift I wires the Knowledge facade Protocol to it
        # so callers can depend on the interface, not the concrete.
        from backend.knowledge.infrastructure.workers.settle_worker import (  # noqa: PLC0415
            SettleWorker,
        )

        logger.debug(
            "settle_complete_handler",
            run_id=str(getattr(run, "id", None)),
            from_state=current_state.value,
            workflow_event=event.value,
            delegation_target=SettleWorker.__qualname__,
        )
        return WorkflowState.settled


__all__ = ["SettleCompleteHandler", "ShipHandler"]
