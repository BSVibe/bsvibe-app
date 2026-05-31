"""Lift H1 — `WorkflowState` projection over today's per-domain enums.

v3 Q11 decision (carried into v8 §7.2): top-level coarse `WorkflowState`
is a **projection** over today's `RequestStatus` / `WorkStepStatus` /
`ProofState`. The per-domain enums stay in `_domain.py` (live db.py
mirrors keep working untouched); `WorkflowState` is the externally
visible coarse stage and is *derived* from the underlying enums.

The projection function `project_request_status` is the documented
mapping. H1 establishes it as data; H2/H3 wire it into the new state
machine handlers.
"""

from __future__ import annotations

from backend.workflow.domain._domain import (
    ProofState,
    RequestStatus,
    WorkStepStatus,
)
from backend.workflow.domain.state import (
    WorkflowEvent,
    WorkflowState,
    project_proof_state,
    project_request_status,
    project_work_step_status,
)

# ────────── v8 §7.2 — WorkflowState enum surface ──────────


def test_workflow_state_has_v8_coarse_values() -> None:
    """v8 §7.2 — 13 coarse top-level states."""
    expected = {
        "received",
        "framed",
        "routed",
        "dispatched",
        "needs_decision",
        "verifying",
        "verified",
        "shipped",
        "settled",
        "delivered",
        "failed",
        "abandoned",
        "expired",
    }
    assert {s.value for s in WorkflowState} == expected


def test_workflow_event_has_v8_event_set() -> None:
    """v8 §7.2 — event vocabulary for transitions."""
    expected = {
        "receive",
        "frame_complete",
        "route_complete",
        "dispatch",
        "decision_required",
        "decision_resolved",
        "verify_start",
        "verify_pass",
        "verify_fail",
        "ship",
        "settle_complete",
        "deliver_complete",
        "fail",
        "abandon",
        "expire",
    }
    assert {e.value for e in WorkflowEvent} == expected


# ────────── Projection: RequestStatus → WorkflowState ──────────


def test_project_request_status_covers_every_value() -> None:
    """Projection MUST be total over RequestStatus — never raise KeyError."""
    for status in RequestStatus:
        result = project_request_status(status)
        assert isinstance(result, WorkflowState)


def test_project_request_status_known_mapping() -> None:
    """Documented projection — RequestStatus.open → received, etc."""
    assert project_request_status(RequestStatus.open) == WorkflowState.received
    assert project_request_status(RequestStatus.running) == WorkflowState.dispatched
    assert project_request_status(RequestStatus.needs_decision) == WorkflowState.needs_decision
    assert project_request_status(RequestStatus.review_ready) == WorkflowState.verified
    assert project_request_status(RequestStatus.shipped) == WorkflowState.shipped
    assert project_request_status(RequestStatus.abandoned) == WorkflowState.abandoned


# ────────── Projection: WorkStepStatus → WorkflowState ──────────


def test_project_work_step_status_covers_every_value() -> None:
    for status in WorkStepStatus:
        result = project_work_step_status(status)
        assert isinstance(result, WorkflowState)


def test_project_work_step_status_known_mapping() -> None:
    assert project_work_step_status(WorkStepStatus.running) == WorkflowState.dispatched
    assert project_work_step_status(WorkStepStatus.verifying) == WorkflowState.verifying
    assert project_work_step_status(WorkStepStatus.review_ready) == WorkflowState.verified
    assert project_work_step_status(WorkStepStatus.failed) == WorkflowState.failed


# ────────── Projection: ProofState → WorkflowState ──────────


def test_project_proof_state_covers_every_value() -> None:
    for state in ProofState:
        result = project_proof_state(state)
        assert isinstance(result, WorkflowState)


def test_project_proof_state_known_mapping() -> None:
    assert project_proof_state(ProofState.verifying) == WorkflowState.verifying
    assert project_proof_state(ProofState.verified) == WorkflowState.verified
    assert project_proof_state(ProofState.verification_failed) == WorkflowState.failed
    assert project_proof_state(ProofState.human_review_required) == WorkflowState.needs_decision


# ────────── Per-domain enums still live in domain/_domain.py ──────────


def test_request_status_values_unchanged() -> None:
    """H1 must not alter underlying per-domain enum values — only relocate."""
    assert {s.value for s in RequestStatus} == {
        "open",
        "running",
        "needs_decision",
        "review_ready",
        "shipped",
        "abandoned",
    }
