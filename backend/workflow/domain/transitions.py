# bsvibe:stable-internal — modifications require a design doc update.
# Owners: workflow/domain
"""v8 §7.3 — ``(WorkflowState, WorkflowEvent) → TransitionEntry`` matrix.

The matrix is the *data*. The handler classes referenced by
``handler_name`` are implemented in :mod:`backend.workflow.application.
_handlers` by Lift H2 (which decomposes ``execution/orchestrator.py``).

H1 establishes the matrix shape only — no handler instances are looked
up here. ``lookup_transition`` returns the entry or ``None``; callers
in H2 will resolve the named handler from a registry.

Cross-stage events (``fail`` / ``abandon`` / ``expire``) apply from any
state and are listed once with ``from_state = None`` semantics encoded
via :data:`CROSS_STAGE_TRANSITIONS`. H2 collapses them into the matrix
via expansion.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from backend.workflow.domain.state import WorkflowEvent, WorkflowState

Stage = Literal["Receive", "Frame", "Run", "Verify", "Settle", "Deliver"]


@dataclass(frozen=True)
class TransitionEntry:
    """One row of the v8 §7.3 transition matrix.

    ``handler_name`` is the application-layer class name H2 will
    register; H1 keeps it as data so the matrix is verifiable
    without H2 yet existing.
    """

    to_state: WorkflowState
    handler_name: str
    stage: Stage


# v8 §7.3 — happy-path + decision-loop + verify-fail-retry transitions.
TRANSITION_MATRIX: dict[tuple[WorkflowState, WorkflowEvent], TransitionEntry] = {
    # ────────── Frame stage ──────────
    (WorkflowState.received, WorkflowEvent.frame_complete): TransitionEntry(
        to_state=WorkflowState.framed,
        handler_name="FrameCompleteHandler",
        stage="Frame",
    ),
    (WorkflowState.framed, WorkflowEvent.route_complete): TransitionEntry(
        to_state=WorkflowState.routed,
        handler_name="RouteCompleteHandler",
        stage="Frame",
    ),
    # ────────── Run stage ──────────
    (WorkflowState.routed, WorkflowEvent.dispatch): TransitionEntry(
        to_state=WorkflowState.dispatched,
        handler_name="DispatchHandler",
        stage="Run",
    ),
    (WorkflowState.dispatched, WorkflowEvent.decision_required): TransitionEntry(
        to_state=WorkflowState.needs_decision,
        handler_name="RequireDecisionHandler",
        stage="Run",
    ),
    (WorkflowState.needs_decision, WorkflowEvent.decision_resolved): TransitionEntry(
        to_state=WorkflowState.dispatched,
        handler_name="ResolveDecisionHandler",
        stage="Run",
    ),
    # ────────── Verify stage ──────────
    (WorkflowState.dispatched, WorkflowEvent.verify_start): TransitionEntry(
        to_state=WorkflowState.verifying,
        handler_name="StartVerifyHandler",
        stage="Verify",
    ),
    (WorkflowState.verifying, WorkflowEvent.verify_pass): TransitionEntry(
        to_state=WorkflowState.verified,
        handler_name="VerifyPassHandler",
        stage="Verify",
    ),
    (WorkflowState.verifying, WorkflowEvent.verify_fail): TransitionEntry(
        to_state=WorkflowState.failed,
        handler_name="VerifyFailHandler",
        stage="Verify",
    ),
    # Failed → dispatched retry (founder Decision-backed retry — Run stage).
    (WorkflowState.failed, WorkflowEvent.decision_resolved): TransitionEntry(
        to_state=WorkflowState.dispatched,
        handler_name="RetryFailedHandler",
        stage="Run",
    ),
    # ────────── Settle stage ──────────
    (WorkflowState.verified, WorkflowEvent.ship): TransitionEntry(
        to_state=WorkflowState.shipped,
        handler_name="ShipHandler",
        stage="Settle",
    ),
    (WorkflowState.shipped, WorkflowEvent.settle_complete): TransitionEntry(
        to_state=WorkflowState.settled,
        handler_name="SettleCompleteHandler",
        stage="Settle",
    ),
    # ────────── Deliver stage ──────────
    (WorkflowState.settled, WorkflowEvent.deliver_complete): TransitionEntry(
        to_state=WorkflowState.delivered,
        handler_name="DeliverCompleteHandler",
        stage="Deliver",
    ),
}


# Cross-stage events — applicable from any state. H2 expands these into
# the matrix or handles via a wildcard pattern at lookup time.
CROSS_STAGE_TRANSITIONS: dict[WorkflowEvent, TransitionEntry] = {
    WorkflowEvent.fail: TransitionEntry(
        to_state=WorkflowState.failed,
        handler_name="FailHandler",
        stage="Run",
    ),
    WorkflowEvent.abandon: TransitionEntry(
        to_state=WorkflowState.abandoned,
        handler_name="AbandonHandler",
        stage="Run",
    ),
    WorkflowEvent.expire: TransitionEntry(
        to_state=WorkflowState.expired,
        handler_name="ExpireHandler",
        stage="Deliver",
    ),
}


def lookup_transition(from_state: WorkflowState, event: WorkflowEvent) -> TransitionEntry | None:
    """Return the entry for ``(from_state, event)`` or ``None``.

    H1 returns ``None`` for invalid pairs; H2's
    ``WorkflowStateMachine`` decides whether to raise. Cross-stage
    events (``fail`` / ``abandon`` / ``expire``) are looked up via
    :data:`CROSS_STAGE_TRANSITIONS` if not in the per-state matrix.
    """
    entry = TRANSITION_MATRIX.get((from_state, event))
    if entry is not None:
        return entry
    return CROSS_STAGE_TRANSITIONS.get(event)


__all__ = [
    "CROSS_STAGE_TRANSITIONS",
    "Stage",
    "TRANSITION_MATRIX",
    "TransitionEntry",
    "lookup_transition",
]
