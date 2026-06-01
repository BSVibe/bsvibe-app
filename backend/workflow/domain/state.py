# bsvibe:stable-internal — modifications require a design doc update.
# Owners: workflow/domain
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

H2b (v8 §13) absorbs the legacy 4-stage state machine from
``backend.orchestrator.{workflow_sm,schema}`` into this module as
``LegacyStage`` / ``LegacyWorkflowState`` / ``LegacyWorkflowStateMachine``
+ :func:`to_legacy_stage`. The v8 enum is canonical; the legacy 4-stage
projection stays available for callers (workers, orchestrator
agent_runner) that haven't migrated to the 13-state surface yet —
deferred to H2c/H3.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Literal, cast

import structlog

from backend.workflow.domain._domain import (
    ProofState,
    RequestStatus,
    WorkStepStatus,
)

logger = structlog.get_logger(__name__)


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


# ────────── Legacy 4-stage state machine (H2b absorbed) ─────────────────
#
# The legacy 4-stage state machine — receive → frame → agent_loop → epsilon
# (Workflow §1) — was the pre-v8 vocabulary the orchestrator/workers used to
# drive a Request. H2b absorbs it here so its single home is the Workflow
# context. The 4-stage shape and the v8 13-state enum are *different
# conceptual surfaces* (the legacy carries per-Request runtime ids; the v8
# enum is a coarse projection over persistent rows). Per the H2b design
# decision (option a) both live side-by-side — :func:`to_legacy_stage`
# bridges the v8 enum to the 4-stage Literal for callers that haven't
# migrated.


LegacyStage = Literal["receive", "frame", "agent_loop", "epsilon"]


@dataclass
class LegacyWorkflowState:
    """Pre-v8 per-Request runtime container — Workflow §1 4-stage SM.

    The 4 stages — ``receive`` / ``frame`` / ``agent_loop`` / ``epsilon`` —
    are the legacy stage Literal. This dataclass carries the per-Request
    runtime ids the legacy SM needed (request_id + optional run_id).
    Renamed from the pre-H2b ``WorkflowState`` dataclass to avoid the
    name collision with the v8 :class:`WorkflowState` StrEnum.
    """

    stage: LegacyStage
    request_id: uuid.UUID
    run_id: uuid.UUID | None = None


class InvalidLegacyTransitionError(RuntimeError):
    """No legal transition for ``(legacy_stage, event)``."""


# Stage transition table — keyed by ``(current_legacy_stage, event)``. The
# new legacy stage is the value. Anything not in this map raises
# :class:`InvalidLegacyTransitionError`.
_LEGACY_TRANSITIONS: Final[dict[tuple[str, str], str]] = {
    ("receive", "framed"): "frame",
    ("frame", "agent_started"): "agent_loop",
    ("agent_loop", "settled"): "epsilon",
    ("epsilon", "cleaned"): "epsilon",
    # Recovery: a Request can re-enter agent_loop from epsilon if a
    # follow-up RunAttempt is required (e.g. canon decision deferred).
    ("epsilon", "agent_restarted"): "agent_loop",
}


class LegacyWorkflowStateMachine:
    """Legacy 4-stage state machine — receive → frame → agent_loop → epsilon.

    Pre-H2b lived at ``backend.orchestrator.workflow_sm``. Stateless: pass
    the current :class:`LegacyWorkflowState` plus a string ``event`` and
    receive the next state. The transition table is total (every event
    has either a legal new stage or raises
    :class:`InvalidLegacyTransitionError`).
    """

    async def transition(
        self,
        *,
        state: LegacyWorkflowState,
        event: str,
    ) -> LegacyWorkflowState:
        """Apply ``event`` to ``state`` and return the next legacy state."""
        key = (state.stage, event)
        if key not in _LEGACY_TRANSITIONS:
            raise InvalidLegacyTransitionError(
                f"No transition for stage={state.stage!r} event={event!r}"
            )
        new_stage = _LEGACY_TRANSITIONS[key]
        logger.info(
            "legacy_workflow_sm_transition",
            from_stage=state.stage,
            to_stage=new_stage,
            trigger_event=event,
            request_id=str(state.request_id),
        )
        return LegacyWorkflowState(
            stage=cast(LegacyStage, new_stage),
            request_id=state.request_id,
            run_id=state.run_id,
        )


# ────────── Bridge: v8 ``WorkflowState`` → ``LegacyStage`` ─────────────
#
# Callers still using the 4-stage Literal can read the v8 enum through
# this projection. The mapping collapses the 13 coarse stages into the 4
# legacy stages:
#
# * ``received`` → ``receive``
# * ``framed`` → ``frame``
# * any in-flight run state (routed / dispatched / needs_decision /
#   verifying / verified / shipped) → ``agent_loop``
# * any terminal state (settled / delivered / failed / abandoned /
#   expired) → ``epsilon``


_LEGACY_STAGE_PROJECTION: dict[WorkflowState, LegacyStage] = {
    WorkflowState.received: "receive",
    WorkflowState.framed: "frame",
    WorkflowState.routed: "agent_loop",
    WorkflowState.dispatched: "agent_loop",
    WorkflowState.needs_decision: "agent_loop",
    WorkflowState.verifying: "agent_loop",
    WorkflowState.verified: "agent_loop",
    WorkflowState.shipped: "agent_loop",
    WorkflowState.settled: "epsilon",
    WorkflowState.delivered: "epsilon",
    WorkflowState.failed: "epsilon",
    WorkflowState.abandoned: "epsilon",
    WorkflowState.expired: "epsilon",
}


def to_legacy_stage(state: WorkflowState) -> LegacyStage:
    """Project the v8 :class:`WorkflowState` onto the legacy 4-stage Literal.

    Total: every v8 state maps to exactly one legacy stage. The reverse
    direction is many-to-one and not provided — the v8 enum is the
    canonical surface; the legacy stage is a coarse summary kept around
    for migration ergonomics, not reasoning.
    """
    return _LEGACY_STAGE_PROJECTION[state]


__all__ = [
    "InvalidLegacyTransitionError",
    "LegacyStage",
    "LegacyWorkflowState",
    "LegacyWorkflowStateMachine",
    "WorkflowEvent",
    "WorkflowState",
    "project_proof_state",
    "project_request_status",
    "project_work_step_status",
    "to_legacy_stage",
]
