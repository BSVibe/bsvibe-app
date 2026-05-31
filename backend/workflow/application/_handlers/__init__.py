"""v8 §7.3 transition handlers — thin scaffolding (Lift H2c).

Each handler class implements the :class:`TransitionHandler` protocol —
an ``async def handle(*, run, current_state, event) -> WorkflowState``
coroutine — and is named after the ``handler_name`` slot in
:data:`backend.workflow.domain.transitions.TRANSITION_MATRIX`.

H2c lands the *wiring*. Most handlers are **thin scaffolding** — they
delegate to the existing application-layer service for the stage's side
effect (FrameStage / RunOrchestrator / VerificationService / settle
drain / delivery worker), and they always return the matrix's
``to_state`` so the driver advances the state machine.

Lift H3d closes out the four stubs left by H2c
(``ResolveDecisionHandler``, ``RetryFailedHandler``,
``SettleCompleteHandler``, ``DeliverCompleteHandler``). Their
delegation targets now live in the Workflow / Knowledge contexts —
``decision_resolution`` under ``workflow/application/intake/`` (H3a),
``DeliveryDispatcher`` under ``workflow/application/delivery/`` (H3b),
and the Knowledge facade Protocol's ``settle`` (Lift A — concrete impl
remains :class:`SettleWorker` in the Knowledge context until Lift I
wires the facade). Each filled handler stays thin scaffolding: it logs
the delegation target + returns the matrix's ``to_state``. No caller
is migrated in H3d; the architecture is just complete.

The driver in :mod:`backend.workflow.application.state_machine_driver`
is the new single entry point. H2c does NOT migrate any caller through
it — existing callers (``AgentRunner``, ``AgentWorker``, REST endpoints,
``SettleWorker``, ``DeliveryWorker``) continue to invoke the underlying
services directly. H3+ will gradually route callers through the driver.
"""

from __future__ import annotations

from backend.workflow.application._handlers.deliver import (
    DeliverCompleteHandler,
    ExpireHandler,
)
from backend.workflow.application._handlers.frame import (
    FrameCompleteHandler,
    RouteCompleteHandler,
)
from backend.workflow.application._handlers.protocol import TransitionHandler
from backend.workflow.application._handlers.run import (
    AbandonHandler,
    DispatchHandler,
    FailHandler,
    RequireDecisionHandler,
    ResolveDecisionHandler,
    RetryFailedHandler,
)
from backend.workflow.application._handlers.settle import (
    SettleCompleteHandler,
    ShipHandler,
)
from backend.workflow.application._handlers.verify import (
    StartVerifyHandler,
    VerifyFailHandler,
    VerifyPassHandler,
)

__all__ = [
    "AbandonHandler",
    "DeliverCompleteHandler",
    "DispatchHandler",
    "ExpireHandler",
    "FailHandler",
    "FrameCompleteHandler",
    "RequireDecisionHandler",
    "ResolveDecisionHandler",
    "RetryFailedHandler",
    "RouteCompleteHandler",
    "SettleCompleteHandler",
    "ShipHandler",
    "StartVerifyHandler",
    "TransitionHandler",
    "VerifyFailHandler",
    "VerifyPassHandler",
]
