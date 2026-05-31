"""Verify-stage transition handlers.

Verify is owned by :class:`backend.workflow.application.verification_service.VerificationService`
today. The Run stage's ``RunOrchestrator._verify(...)`` call invokes the
service inline at the end of each plan→act cycle (Lift B2a).

These handlers are the H3+ entry points the driver will use. In H2c
they're thin scaffolding — the verify call has already happened by the
time they fire (inline inside the loop), so the handlers just advance
the coarse state.
"""

from __future__ import annotations

from typing import Any

import structlog

from backend.workflow.domain.state import WorkflowEvent, WorkflowState

logger = structlog.get_logger(__name__)


class StartVerifyHandler:
    """``dispatched → verifying`` via ``verify_start``.

    The work loop transitions through ``verifying`` inline (via
    :class:`~backend.workflow.infrastructure.db.WorkStepStatus.VERIFYING`) before
    invoking the verifier service. This handler is the H3+ driver entry
    point; today's call site is inside
    :meth:`RunOrchestrator._drive_loop`.
    """

    async def handle(
        self,
        *,
        run: Any,
        current_state: WorkflowState,
        event: WorkflowEvent,
    ) -> WorkflowState:
        logger.debug(
            "start_verify_handler",
            run_id=str(getattr(run, "id", None)),
            from_state=current_state.value,
            workflow_event=event.value,
        )
        return WorkflowState.verifying


class VerifyPassHandler:
    """``verifying → verified`` via ``verify_pass``.

    Verifier pass triggers :func:`backend.workflow.application.run_persistence.finish_verified`
    which writes the verified terminal and (for product-bound runs) the
    auto-ship transition. The handler advances the coarse state — the
    actual DB writes have already happened inline.
    """

    async def handle(
        self,
        *,
        run: Any,
        current_state: WorkflowState,
        event: WorkflowEvent,
    ) -> WorkflowState:
        logger.debug(
            "verify_pass_handler",
            run_id=str(getattr(run, "id", None)),
            from_state=current_state.value,
            workflow_event=event.value,
        )
        return WorkflowState.verified


class VerifyFailHandler:
    """``verifying → failed`` via ``verify_fail``.

    A failed verify writes the failed terminal through the same
    :meth:`AgentRunner.transition` path as a system error. The retry
    handler (``RetryFailedHandler``) covers the founder-decision retry
    out of failed back to dispatched.
    """

    async def handle(
        self,
        *,
        run: Any,
        current_state: WorkflowState,
        event: WorkflowEvent,
    ) -> WorkflowState:
        logger.debug(
            "verify_fail_handler",
            run_id=str(getattr(run, "id", None)),
            from_state=current_state.value,
            workflow_event=event.value,
        )
        return WorkflowState.failed


__all__ = ["StartVerifyHandler", "VerifyFailHandler", "VerifyPassHandler"]
