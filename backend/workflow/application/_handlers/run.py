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

:class:`ResolveDecisionHandler` and :class:`RetryFailedHandler` are
filled in Lift H3d — they reference the
:class:`backend.workflow.application.intake.decision_resolution.DecisionResolutionTrigger`
delegation target (relocated by H3a) and return the matrix's
``to_state``. The actual side effect (re-entering the loop after a
Decision row is resolved) is still owned by the resolve REST endpoint
(:mod:`backend.api.v1.checkpoints`), which calls
:meth:`AgentRunner.transition` directly. Future driver-routed callers
will invoke the trigger through these handlers.

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

    H3d wiring — the delegation target is
    :class:`backend.workflow.application.intake.decision_resolution.DecisionResolutionTrigger`
    (relocated from BSNexus into the workflow context by Lift H3a). The
    REST endpoint at :mod:`backend.api.v1.checkpoints` still owns the
    side effect today (records the answer + flips ExecutionRun to OPEN);
    this handler is the H3+ driver entry point that future callers will
    route through. Behaviorally identical to the 11 other H2c handlers:
    it just advances the coarse state — the actual DB writes already
    happened on the existing caller path.
    """

    async def handle(
        self,
        *,
        run: Any,
        current_state: WorkflowState,
        event: WorkflowEvent,
    ) -> WorkflowState:
        # Delegation target — lazy import keeps the handler thin and
        # avoids a hard dependency cycle. The actual resume side effect
        # is owned by backend.api.v1.checkpoints.resolve_checkpoint
        # today; this handler advances the state machine while the
        # caller continues to execute the side effect inline.
        from backend.workflow.application.intake import decision_resolution  # noqa: PLC0415

        logger.debug(
            "resolve_decision_handler",
            run_id=str(getattr(run, "id", None)),
            from_state=current_state.value,
            workflow_event=event.value,
            delegation_target=decision_resolution.DecisionResolutionTrigger.__qualname__,
        )
        return WorkflowState.dispatched


class RetryFailedHandler:
    """``failed → dispatched`` via ``decision_resolved`` (founder retry).

    H3d wiring — shares the delegation target with
    :class:`ResolveDecisionHandler`: the failed→dispatched flip is the
    same resume path keyed on a founder-resolved Decision. Today the
    REST endpoint at :mod:`backend.api.v1.checkpoints` calls
    :meth:`AgentRunner.transition` directly (RUNNING → OPEN on the
    failed run); future callers route through this handler.
    """

    async def handle(
        self,
        *,
        run: Any,
        current_state: WorkflowState,
        event: WorkflowEvent,
    ) -> WorkflowState:
        # Delegation target identical to ResolveDecisionHandler; both
        # share the single Workflow-owned resume path. The event payload
        # differentiates "resume from needs_decision" vs "retry from
        # failed" — both end at dispatched per the matrix.
        from backend.workflow.application.intake import decision_resolution  # noqa: PLC0415

        logger.debug(
            "retry_failed_handler",
            run_id=str(getattr(run, "id", None)),
            from_state=current_state.value,
            workflow_event=event.value,
            delegation_target=decision_resolution.DecisionResolutionTrigger.__qualname__,
        )
        return WorkflowState.dispatched


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
