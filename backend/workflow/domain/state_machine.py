from __future__ import annotations

from backend.workflow.domain._domain import (
    ProofState,
    RequestStatus,
    RunAttemptPhase,
    WorkStepStatus,
)

REQUEST_TRANSITIONS: dict[RequestStatus, set[RequestStatus]] = {
    RequestStatus.open: {RequestStatus.running, RequestStatus.abandoned},
    RequestStatus.running: {
        RequestStatus.needs_decision,
        RequestStatus.review_ready,
        RequestStatus.abandoned,
    },
    RequestStatus.needs_decision: {RequestStatus.running, RequestStatus.abandoned},
    RequestStatus.review_ready: {RequestStatus.running, RequestStatus.shipped},
    RequestStatus.shipped: set(),
    RequestStatus.abandoned: set(),
}

WORK_STEP_TRANSITIONS: dict[WorkStepStatus, set[WorkStepStatus]] = {
    WorkStepStatus.pending: {WorkStepStatus.running, WorkStepStatus.skipped},
    WorkStepStatus.running: {
        WorkStepStatus.needs_decision,
        WorkStepStatus.verifying,
        WorkStepStatus.failed,
    },
    WorkStepStatus.needs_decision: {WorkStepStatus.running, WorkStepStatus.skipped},
    WorkStepStatus.verifying: {WorkStepStatus.review_ready, WorkStepStatus.failed},
    WorkStepStatus.review_ready: set(),
    # ``failed`` is no longer a dead-end: a founder Decision backstops a
    # ``failed`` WorkStep (verification-failed deliverable / executor
    # error) and resolving it with ``retry`` re-engages the step. The
    # re-dispatch transitions ``failed → running``, consistent with the
    # no-dead-end model — every stuck step has a forward edge.
    WorkStepStatus.failed: {WorkStepStatus.running},
    WorkStepStatus.skipped: set(),
}

RUN_ATTEMPT_PHASE_ORDER = (
    RunAttemptPhase.prepare,
    RunAttemptPhase.work,
    RunAttemptPhase.verify,
    RunAttemptPhase.summarize,
    RunAttemptPhase.terminal,
)

PROOF_TRANSITIONS: dict[ProofState, set[ProofState]] = {
    ProofState.verification_missing: {
        ProofState.verifying,
        ProofState.human_review_required,
    },
    ProofState.verifying: {
        ProofState.verified,
        ProofState.verification_failed,
        ProofState.human_review_required,
    },
    ProofState.verification_failed: {ProofState.verifying},
    ProofState.human_review_required: {ProofState.verified},
    ProofState.verified: set(),
}


def can_transition_request(current: RequestStatus, target: RequestStatus) -> bool:
    return target in REQUEST_TRANSITIONS[current]


def can_transition_work_step(current: WorkStepStatus, target: WorkStepStatus) -> bool:
    return target in WORK_STEP_TRANSITIONS[current]


def can_advance_run_attempt_phase(current: RunAttemptPhase, target: RunAttemptPhase) -> bool:
    current_index = RUN_ATTEMPT_PHASE_ORDER.index(current)
    next_index = current_index + 1
    return (
        next_index < len(RUN_ATTEMPT_PHASE_ORDER) and RUN_ATTEMPT_PHASE_ORDER[next_index] == target
    )


def can_transition_proof(current: ProofState, target: ProofState) -> bool:
    return target in PROOF_TRANSITIONS[current]
