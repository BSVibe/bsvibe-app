"""Run-stage transition handlers.

The Run stage covers the plan→act→verify loop minus the verify call
itself (verify lives in its own stage). Today the loop is owned by
:class:`backend.workflow.application.agent_loop.RunOrchestrator`;
:class:`backend.workflow.application.agent_runner.AgentRunner` is the
transactional wrapper that opens the ExecutionRun row + maps the loop's
terminal outcome onto the run status.

The handlers here are thin scaffolding — H2c does NOT migrate any caller
through them, the existing call site (``AgentWorker._drive_run`` →
``AgentRunner.drive`` → ``RunOrchestrator.run``) keeps working. H3+ will
move the worker through the state machine driver, at which point each
event in the matrix below routes to one of these handlers.

:class:`ResolveDecisionHandler` and :class:`RetryFailedHandler` stay as
``NotImplementedError`` stubs — their side effect (re-entering the loop
after a Decision row is resolved) is owned by the resolve REST endpoint
(:mod:`backend.api.v1.checkpoints`), which today re-invokes
``AgentRunner.drive`` directly. Until that endpoint routes through the
driver, the handler stubs are placeholders; the driver still returns the
matrix's next state so the smoke tests verify wiring shape.

Cross-stage handlers (``fail`` / ``abandon``) live here too — they apply
from any state and trigger the same DB-side ``transition`` that
:meth:`AgentRunner.transition` already implements.
"""

from __future__ import annotations

from typing import Any

import structlog

from backend.workflow.domain.state import WorkflowEvent, WorkflowState

logger = structlog.get_logger(__name__)


class DispatchHandler:
    """``routed → dispatched`` via ``dispatch``.

    Today's :meth:`AgentRunner.drive` is what flips the underlying
    ExecutionRun status to RUNNING and kicks the loop. The H3+ driver
    will route through this handler; for now it delegates back via the
    existing call site and just returns the next state.
    """

    async def handle(
        self,
        *,
        run: Any,
        current_state: WorkflowState,
        event: WorkflowEvent,
    ) -> WorkflowState:
        logger.debug(
            "dispatch_handler",
            run_id=str(getattr(run, "id", None)),
            from_state=current_state.value,
            workflow_event=event.value,
        )
        return WorkflowState.dispatched


class RequireDecisionHandler:
    """``dispatched → needs_decision`` via ``decision_required``.

    The loop emits a Decision row from inside
    :meth:`RunOrchestrator._drive_loop` (B13). The DB-side status flip
    happens via :func:`backend.workflow.application.run_persistence.decision_result`.
    """

    async def handle(
        self,
        *,
        run: Any,
        current_state: WorkflowState,
        event: WorkflowEvent,
    ) -> WorkflowState:
        logger.debug(
            "require_decision_handler",
            run_id=str(getattr(run, "id", None)),
            from_state=current_state.value,
            workflow_event=event.value,
        )
        return WorkflowState.needs_decision


class ResolveDecisionHandler:
    """``needs_decision → dispatched`` via ``decision_resolved``.

    The resolve REST endpoint (:mod:`backend.api.v1.checkpoints`) today
    re-invokes :meth:`AgentRunner.drive` directly. Promoting that
    re-entry into a Workflow-context service (so this handler can
    delegate) is H3's work.
    """

    async def handle(
        self,
        *,
        run: Any,
        current_state: WorkflowState,
        event: WorkflowEvent,
    ) -> WorkflowState:
        # TODO(H3): delegate to a Workflow-owned resume service. The
        # REST endpoint at backend.api.v1.checkpoints currently inlines
        # the resume logic — it will move into this handler when the
        # endpoint routes through the driver.
        raise NotImplementedError(
            "ResolveDecisionHandler — H3 will lift backend.api.v1.checkpoints "
            "resume logic into a Workflow-owned service."
        )


class RetryFailedHandler:
    """``failed → dispatched`` via ``decision_resolved`` (founder retry).

    The retry path (founder-backed Decision flips a failed run back to
    dispatched) shares its DB plumbing with
    :class:`ResolveDecisionHandler`. H3 promotes both into a single
    resume service.
    """

    async def handle(
        self,
        *,
        run: Any,
        current_state: WorkflowState,
        event: WorkflowEvent,
    ) -> WorkflowState:
        # TODO(H3): delegate to the same Workflow-owned resume service
        # as ResolveDecisionHandler.
        raise NotImplementedError(
            "RetryFailedHandler — H3 will lift the failed-retry path "
            "into a Workflow-owned resume service."
        )


class FailHandler:
    """Cross-stage ``fail`` event → ``failed``.

    Today :meth:`AgentRunner.transition` does the actual DB flip to
    :class:`~backend.execution.db.RunStatus.FAILED`. H3+ routes the
    caller through this handler.
    """

    async def handle(
        self,
        *,
        run: Any,
        current_state: WorkflowState,
        event: WorkflowEvent,
    ) -> WorkflowState:
        logger.debug(
            "fail_handler",
            run_id=str(getattr(run, "id", None)),
            from_state=current_state.value,
            workflow_event=event.value,
        )
        return WorkflowState.failed


class AbandonHandler:
    """Cross-stage ``abandon`` event → ``abandoned``.

    Triggered when a Request is abandoned by the founder (or by a
    higher-level supervisor decision). The DB flip happens through the
    same :meth:`AgentRunner.transition` plumbing as ``fail``.
    """

    async def handle(
        self,
        *,
        run: Any,
        current_state: WorkflowState,
        event: WorkflowEvent,
    ) -> WorkflowState:
        logger.debug(
            "abandon_handler",
            run_id=str(getattr(run, "id", None)),
            from_state=current_state.value,
            workflow_event=event.value,
        )
        return WorkflowState.abandoned


__all__ = [
    "AbandonHandler",
    "DispatchHandler",
    "FailHandler",
    "RequireDecisionHandler",
    "ResolveDecisionHandler",
    "RetryFailedHandler",
]
