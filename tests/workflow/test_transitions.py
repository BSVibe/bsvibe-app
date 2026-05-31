"""Lift H1 — `(state, event) → handler_name` transition matrix.

v8 §7.3 codifies the matrix; handler implementations land in H2. H1
asserts the matrix is well-formed (every entry has a documented
handler_name, no orphan entries, total over the documented set).
"""

from __future__ import annotations

from backend.workflow.domain.state import WorkflowEvent, WorkflowState
from backend.workflow.domain.transitions import (
    TRANSITION_MATRIX,
    TransitionEntry,
    lookup_transition,
)


def test_matrix_keys_are_state_event_pairs() -> None:
    """Every key must be (WorkflowState, WorkflowEvent)."""
    for key in TRANSITION_MATRIX:
        from_state, event = key
        assert isinstance(from_state, WorkflowState)
        assert isinstance(event, WorkflowEvent)


def test_matrix_values_carry_to_state_and_handler_name() -> None:
    for entry in TRANSITION_MATRIX.values():
        assert isinstance(entry, TransitionEntry)
        assert isinstance(entry.to_state, WorkflowState)
        assert entry.handler_name  # non-empty
        assert entry.stage in {"Receive", "Frame", "Run", "Verify", "Settle", "Deliver"}


def test_v8_canonical_transitions_present() -> None:
    """Spot-check the v8 §7.3 canonical happy path."""
    # received → framed via frame_complete
    e = TRANSITION_MATRIX[(WorkflowState.received, WorkflowEvent.frame_complete)]
    assert e.to_state == WorkflowState.framed
    assert e.handler_name == "FrameCompleteHandler"

    # framed → routed via route_complete
    e = TRANSITION_MATRIX[(WorkflowState.framed, WorkflowEvent.route_complete)]
    assert e.to_state == WorkflowState.routed

    # routed → dispatched via dispatch
    e = TRANSITION_MATRIX[(WorkflowState.routed, WorkflowEvent.dispatch)]
    assert e.to_state == WorkflowState.dispatched

    # dispatched → verifying via verify_start
    e = TRANSITION_MATRIX[(WorkflowState.dispatched, WorkflowEvent.verify_start)]
    assert e.to_state == WorkflowState.verifying

    # verifying → verified via verify_pass
    e = TRANSITION_MATRIX[(WorkflowState.verifying, WorkflowEvent.verify_pass)]
    assert e.to_state == WorkflowState.verified

    # verified → shipped via ship
    e = TRANSITION_MATRIX[(WorkflowState.verified, WorkflowEvent.ship)]
    assert e.to_state == WorkflowState.shipped

    # shipped → settled via settle_complete
    e = TRANSITION_MATRIX[(WorkflowState.shipped, WorkflowEvent.settle_complete)]
    assert e.to_state == WorkflowState.settled

    # settled → delivered via deliver_complete
    e = TRANSITION_MATRIX[(WorkflowState.settled, WorkflowEvent.deliver_complete)]
    assert e.to_state == WorkflowState.delivered


def test_decision_loop_transitions() -> None:
    """dispatched ⇄ needs_decision via require/resolve, and verify_fail → failed → dispatched retry."""
    e = TRANSITION_MATRIX[(WorkflowState.dispatched, WorkflowEvent.decision_required)]
    assert e.to_state == WorkflowState.needs_decision

    e = TRANSITION_MATRIX[(WorkflowState.needs_decision, WorkflowEvent.decision_resolved)]
    assert e.to_state == WorkflowState.dispatched

    e = TRANSITION_MATRIX[(WorkflowState.verifying, WorkflowEvent.verify_fail)]
    assert e.to_state == WorkflowState.failed

    e = TRANSITION_MATRIX[(WorkflowState.failed, WorkflowEvent.decision_resolved)]
    assert e.to_state == WorkflowState.dispatched


def test_lookup_transition_returns_entry_for_valid_pair() -> None:
    entry = lookup_transition(WorkflowState.received, WorkflowEvent.frame_complete)
    assert entry is not None
    assert entry.to_state == WorkflowState.framed


def test_lookup_transition_returns_none_for_invalid_pair() -> None:
    """Invalid (state, event) returns None (caller decides whether to raise)."""
    assert lookup_transition(WorkflowState.received, WorkflowEvent.ship) is None


def test_no_handler_name_is_empty_or_whitespace() -> None:
    for entry in TRANSITION_MATRIX.values():
        assert entry.handler_name.strip() == entry.handler_name
        assert len(entry.handler_name) > 0
