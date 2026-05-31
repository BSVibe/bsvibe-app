"""State machine driver — v8 §7.3 (matrix) + Lift H2c (handlers).

The driver is the single entry point for applying a
:class:`~backend.workflow.domain.state.WorkflowEvent` to a
:class:`~backend.workflow.domain.state.WorkflowState`. It looks the
``(state, event)`` pair up in
:data:`~backend.workflow.domain.transitions.TRANSITION_MATRIX`,
resolves the handler by ``handler_name`` against the
:mod:`backend.workflow.application._handlers` package, invokes the
handler, and returns the next state.

H2c lands the driver. NO caller is migrated through it yet — existing
callers (``AgentRunner``, ``AgentWorker``, REST endpoints,
``SettleWorker``, ``DeliveryWorker``) continue to invoke the underlying
services directly. H3+ migrates each caller behind this driver one at a
time.

The driver enforces two invariants:

* The looked-up handler MUST exist in the ``_handlers`` package — a
  matrix entry naming a missing handler is a wiring bug, not a
  caller-fixable error.
* The handler's returned :class:`WorkflowState` MUST equal the matrix
  entry's ``to_state`` — a handler that returns a different state is
  also a wiring bug (the matrix is the source of truth).
"""

from __future__ import annotations

from typing import Any

import structlog

from backend.workflow.application import _handlers as _handlers_pkg
from backend.workflow.application._handlers.protocol import TransitionHandler
from backend.workflow.domain.state import WorkflowEvent, WorkflowState
from backend.workflow.domain.transitions import (
    TransitionEntry,
    lookup_transition,
)

logger = structlog.get_logger(__name__)


class InvalidTransitionError(RuntimeError):
    """``(current_state, event)`` has no entry in the v8 §7.3 matrix."""


class HandlerWiringError(RuntimeError):
    """The matrix names a handler that the ``_handlers`` package doesn't expose.

    Raised when ``handler_name`` isn't a class on the ``_handlers``
    package, or when the resolved handler doesn't satisfy the
    :class:`TransitionHandler` protocol. Both are wiring bugs the
    caller can't recover from — the matrix is the source of truth.
    """


async def drive_transition(
    *,
    run: Any,
    current_state: WorkflowState,
    event: WorkflowEvent,
) -> WorkflowState:
    """Apply ``event`` to ``(run, current_state)`` and return the next state.

    Looks up the matrix entry, resolves + invokes the handler, asserts
    the handler returned the matrix's ``to_state``, and returns it.
    Raises :class:`InvalidTransitionError` for unknown
    ``(state, event)`` pairs; :class:`HandlerWiringError` for
    matrix-vs-handler mismatch.
    """
    entry = lookup_transition(current_state, event)
    if entry is None:
        raise InvalidTransitionError(
            f"no transition: state={current_state.value!r} event={event.value!r}"
        )

    handler = _resolve_handler(entry)
    new_state = await handler.handle(run=run, current_state=current_state, event=event)
    if new_state != entry.to_state:
        raise HandlerWiringError(
            f"handler {entry.handler_name!r} returned {new_state.value!r}, "
            f"matrix expects {entry.to_state.value!r}"
        )
    logger.info(
        "workflow_driver_transition",
        from_state=current_state.value,
        to_state=new_state.value,
        workflow_event=event.value,
        handler=entry.handler_name,
        stage=entry.stage,
    )
    return new_state


def _resolve_handler(entry: TransitionEntry) -> TransitionHandler:
    """Resolve ``entry.handler_name`` to an instance of the named handler class."""
    cls = getattr(_handlers_pkg, entry.handler_name, None)
    if cls is None:
        raise HandlerWiringError(f"_handlers pkg has no class named {entry.handler_name!r}")
    handler = cls()
    if not isinstance(handler, TransitionHandler):
        raise HandlerWiringError(f"{entry.handler_name!r} does not satisfy TransitionHandler")
    return handler


__all__ = [
    "HandlerWiringError",
    "InvalidTransitionError",
    "drive_transition",
]
