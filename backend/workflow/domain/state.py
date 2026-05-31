"""v8 §7.2 — coarse top-level ``WorkflowState`` + ``WorkflowEvent`` enums.

The Workflow context's externally visible stage is a **projection** over
today's per-domain enums (v3 Q11). The per-domain enums in
:mod:`backend.workflow.domain._domain` are the persistent storage shape;
the v8 ``WorkflowState`` is the coarse stage that REST / SSE / UI
surfaces consume.

H1 establishes the enum surface + the documented projection. H2's
transition handlers (``backend.workflow.application._handlers``) will
read/write the per-domain enums and emit the v8 stage transitions via
the matrix in :mod:`backend.workflow.domain.transitions`.

The projection is **total** over each input enum — no value of
``RequestStatus`` / ``WorkStepStatus`` / ``ProofState`` may be left
unmapped, otherwise the coarse stage would be undefined for some row.
"""

from __future__ import annotations

from enum import StrEnum

from backend.workflow.domain._domain import (
    ProofState,
    RequestStatus,
    WorkStepStatus,
)


class WorkflowState(StrEnum):
    """v8 §7.2 — coarse top-level state.

    Six stages: ``Receive → Frame → Run → Verify → Settle → Deliver``.
    Each stage may have one or more coarse states.
    """

    received = "received"
    framed = "framed"
    routed = "routed"
    dispatched = "dispatched"
    needs_decision = "needs_decision"
    verifying = "verifying"
    verified = "verified"
    shipped = "shipped"
    settled = "settled"
    delivered = "delivered"
    failed = "failed"
    abandoned = "abandoned"
    expired = "expired"


class WorkflowEvent(StrEnum):
    """v8 §7.2 — transition vocabulary.

    Events drive a single state → state transition through the matrix
    in :mod:`backend.workflow.domain.transitions`. Cross-stage events
    (``fail`` / ``abandon`` / ``expire``) apply from any state.
    """

    receive = "receive"
    frame_complete = "frame_complete"
    route_complete = "route_complete"
    dispatch = "dispatch"
    decision_required = "decision_required"
    decision_resolved = "decision_resolved"
    verify_start = "verify_start"
    verify_pass = "verify_pass"  # noqa: S105 — workflow event name, not a password
    verify_fail = "verify_fail"
    ship = "ship"
    settle_complete = "settle_complete"
    deliver_complete = "deliver_complete"
    fail = "fail"
    abandon = "abandon"
    expire = "expire"


# ────────── Projection: per-domain enum → coarse WorkflowState ──────────
#
# Per v3 Q11 — projection-style. Today's enums stay where the
# SQLAlchemy mirrors expect them; the coarse stage is *derived*. H2
# transition handlers read/write the per-domain enums and emit the
# coarse stage via these functions.


def project_request_status(status: RequestStatus) -> WorkflowState:
    """Map a persisted ``RequestStatus`` to the coarse v8 stage."""
    return _REQUEST_STATUS_PROJECTION[status]


def project_work_step_status(status: WorkStepStatus) -> WorkflowState:
    """Map a persisted ``WorkStepStatus`` to the coarse v8 stage."""
    return _WORK_STEP_STATUS_PROJECTION[status]


def project_proof_state(state: ProofState) -> WorkflowState:
    """Map a persisted ``ProofState`` to the coarse v8 stage."""
    return _PROOF_STATE_PROJECTION[state]


_REQUEST_STATUS_PROJECTION: dict[RequestStatus, WorkflowState] = {
    RequestStatus.open: WorkflowState.received,
    RequestStatus.running: WorkflowState.dispatched,
    RequestStatus.needs_decision: WorkflowState.needs_decision,
    RequestStatus.review_ready: WorkflowState.verified,
    RequestStatus.shipped: WorkflowState.shipped,
    RequestStatus.abandoned: WorkflowState.abandoned,
}

_WORK_STEP_STATUS_PROJECTION: dict[WorkStepStatus, WorkflowState] = {
    WorkStepStatus.pending: WorkflowState.routed,
    WorkStepStatus.running: WorkflowState.dispatched,
    WorkStepStatus.needs_decision: WorkflowState.needs_decision,
    WorkStepStatus.verifying: WorkflowState.verifying,
    WorkStepStatus.review_ready: WorkflowState.verified,
    WorkStepStatus.failed: WorkflowState.failed,
    WorkStepStatus.skipped: WorkflowState.abandoned,
}

_PROOF_STATE_PROJECTION: dict[ProofState, WorkflowState] = {
    ProofState.verification_missing: WorkflowState.dispatched,
    ProofState.verifying: WorkflowState.verifying,
    ProofState.verified: WorkflowState.verified,
    ProofState.verification_failed: WorkflowState.failed,
    ProofState.human_review_required: WorkflowState.needs_decision,
}


__all__ = [
    "WorkflowEvent",
    "WorkflowState",
    "project_proof_state",
    "project_request_status",
    "project_work_step_status",
]
