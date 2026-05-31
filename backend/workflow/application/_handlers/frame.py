"""Frame-stage transition handlers — ``frame_complete`` + ``route_complete``.

Both delegate to today's
:class:`backend.workflow.application.stages.frame.FrameStage` for the
Frame side effect. Route resolution lives in
:mod:`backend.router.routing.run_routing` and is invoked downstream of
``FrameStage.frame(...)``; the ``RouteCompleteHandler`` doesn't re-invoke
it in H2c — it just advances the coarse state once the upstream caller
has framed + routed.

The handlers do NOT replace today's call sites — they're the entry
points the H3+ driver-driven workers will use. Until then,
:class:`backend.workflow.infrastructure.workers.agent_worker.AgentWorker` continues to call
``FrameStage.frame(...)`` directly and these handlers are wired only
through the state machine driver smoke tests.
"""

from __future__ import annotations

from typing import Any

import structlog

from backend.workflow.domain.state import WorkflowEvent, WorkflowState

logger = structlog.get_logger(__name__)


class FrameCompleteHandler:
    """``received → framed`` via ``frame_complete``.

    Today's :class:`backend.workflow.infrastructure.workers.agent_worker.AgentWorker._drive_run`
    already calls ``FrameStage.frame(run, request)`` and writes the
    frame onto ``ExecutionRun.payload`` before any compute. This handler
    is the H3+ entry point — the worker will route through the driver,
    the driver invokes this handler, which delegates to ``FrameStage``.

    In H2c the side effect is deferred (caller has already framed); the
    handler returns the matrix's ``to_state`` so the driver advances.
    """

    async def handle(
        self,
        *,
        run: Any,
        current_state: WorkflowState,
        event: WorkflowEvent,
    ) -> WorkflowState:
        logger.debug(
            "frame_complete_handler",
            run_id=str(getattr(run, "id", None)),
            from_state=current_state.value,
            workflow_event=event.value,
        )
        return WorkflowState.framed


class RouteCompleteHandler:
    """``framed → routed`` via ``route_complete``.

    Route resolution is done by
    :func:`backend.router.routing.run_routing.resolve_run_routing` —
    Frame writes ``routing`` onto the Request payload (P1 routing lift)
    and the Run inherits it. This handler is the H3+ entry point; the
    actual resolve has already happened by the time it fires.
    """

    async def handle(
        self,
        *,
        run: Any,
        current_state: WorkflowState,
        event: WorkflowEvent,
    ) -> WorkflowState:
        logger.debug(
            "route_complete_handler",
            run_id=str(getattr(run, "id", None)),
            from_state=current_state.value,
            workflow_event=event.value,
        )
        return WorkflowState.routed


__all__ = ["FrameCompleteHandler", "RouteCompleteHandler"]
