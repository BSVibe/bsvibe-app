"""Unit tests for the H2c state machine driver."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from backend.workflow.application.state_machine_driver import (
    InvalidTransitionError,
    drive_transition,
)
from backend.workflow.domain.state import WorkflowEvent, WorkflowState
from backend.workflow.domain.transitions import (
    CROSS_STAGE_TRANSITIONS,
    TRANSITION_MATRIX,
)


@pytest.mark.asyncio
async def test_drive_transition_happy_path_received_to_framed() -> None:
    next_state = await drive_transition(
        run=MagicMock(),
        current_state=WorkflowState.received,
        event=WorkflowEvent.frame_complete,
    )
    assert next_state == WorkflowState.framed


@pytest.mark.asyncio
async def test_drive_transition_dispatch_routed_to_dispatched() -> None:
    next_state = await drive_transition(
        run=MagicMock(),
        current_state=WorkflowState.routed,
        event=WorkflowEvent.dispatch,
    )
    assert next_state == WorkflowState.dispatched


@pytest.mark.asyncio
async def test_drive_transition_verify_pass_verifying_to_verified() -> None:
    next_state = await drive_transition(
        run=MagicMock(),
        current_state=WorkflowState.verifying,
        event=WorkflowEvent.verify_pass,
    )
    assert next_state == WorkflowState.verified


@pytest.mark.asyncio
async def test_drive_transition_ship_verified_to_shipped() -> None:
    next_state = await drive_transition(
        run=MagicMock(),
        current_state=WorkflowState.verified,
        event=WorkflowEvent.ship,
    )
    assert next_state == WorkflowState.shipped


@pytest.mark.asyncio
async def test_drive_transition_cross_stage_fail() -> None:
    next_state = await drive_transition(
        run=MagicMock(),
        current_state=WorkflowState.dispatched,
        event=WorkflowEvent.fail,
    )
    assert next_state == WorkflowState.failed


@pytest.mark.asyncio
async def test_drive_transition_cross_stage_expire() -> None:
    next_state = await drive_transition(
        run=MagicMock(),
        current_state=WorkflowState.shipped,
        event=WorkflowEvent.expire,
    )
    assert next_state == WorkflowState.expired


@pytest.mark.asyncio
async def test_drive_transition_invalid_pair_raises() -> None:
    with pytest.raises(InvalidTransitionError):
        await drive_transition(
            run=MagicMock(),
            current_state=WorkflowState.received,
            event=WorkflowEvent.deliver_complete,
        )


@pytest.mark.asyncio
async def test_drive_transition_resolve_decision_advances_state() -> None:
    """H3d — ResolveDecisionHandler filled; returns the matrix's to_state."""
    next_state = await drive_transition(
        run=MagicMock(),
        current_state=WorkflowState.needs_decision,
        event=WorkflowEvent.decision_resolved,
    )
    assert next_state == WorkflowState.dispatched


@pytest.mark.asyncio
async def test_drive_transition_retry_failed_advances_state() -> None:
    """H3d — RetryFailedHandler filled; returns the matrix's to_state."""
    next_state = await drive_transition(
        run=MagicMock(),
        current_state=WorkflowState.failed,
        event=WorkflowEvent.decision_resolved,
    )
    assert next_state == WorkflowState.dispatched


@pytest.mark.asyncio
async def test_drive_transition_settle_complete_advances_state() -> None:
    """H3d — SettleCompleteHandler filled; returns the matrix's to_state."""
    next_state = await drive_transition(
        run=MagicMock(),
        current_state=WorkflowState.shipped,
        event=WorkflowEvent.settle_complete,
    )
    assert next_state == WorkflowState.settled


@pytest.mark.asyncio
async def test_drive_transition_deliver_complete_advances_state() -> None:
    """H3d — DeliverCompleteHandler filled; returns the matrix's to_state."""
    next_state = await drive_transition(
        run=MagicMock(),
        current_state=WorkflowState.settled,
        event=WorkflowEvent.deliver_complete,
    )
    assert next_state == WorkflowState.delivered


def test_drive_transition_handler_wiring_error_propagates() -> None:
    """If matrix names an unknown handler the driver raises ``HandlerWiringError``.

    Imports happen *inside* the test so other tests in the suite that wipe
    ``sys.modules`` (e.g. ``tests/test_bundle1_imports``) can't shadow the
    class identity.
    """
    from backend.workflow.application import state_machine_driver as drv
    from backend.workflow.application.state_machine_driver import (
        HandlerWiringError as _HandlerWiringError,
    )
    from backend.workflow.domain.state import WorkflowState as _WorkflowState
    from backend.workflow.domain.transitions import TransitionEntry

    # Forge a matrix entry with a bogus handler name and look it up directly.
    bogus = TransitionEntry(
        to_state=_WorkflowState.framed,
        handler_name="DoesNotExistHandler",
        stage="Frame",
    )
    with pytest.raises(_HandlerWiringError, match="DoesNotExistHandler"):
        drv._resolve_handler(bogus)


def test_every_matrix_handler_resolves() -> None:
    """Every handler named in the matrix can be resolved + instantiated."""
    from backend.workflow.application import state_machine_driver as drv

    for entry in TRANSITION_MATRIX.values():
        drv._resolve_handler(entry)
    for entry in CROSS_STAGE_TRANSITIONS.values():
        drv._resolve_handler(entry)
