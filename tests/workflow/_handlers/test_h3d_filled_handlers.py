"""Unit tests for the H3d-filled handler stubs.

Lift H3d removes the ``NotImplementedError`` stubs on:

* :class:`ResolveDecisionHandler` — ``needs_decision`` → ``dispatched``
* :class:`RetryFailedHandler` — ``failed`` → ``dispatched``
* :class:`SettleCompleteHandler` — ``shipped`` → ``settled``
* :class:`DeliverCompleteHandler` — ``settled`` → ``delivered``

Each handler stays thin scaffolding — H3d only confirms the delegation
target is importable + the matrix's ``to_state`` is returned, mirroring
the 11 already-implemented handlers (H2c). No caller is migrated.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from backend.workflow.application._handlers import (
    DeliverCompleteHandler,
    ResolveDecisionHandler,
    RetryFailedHandler,
    SettleCompleteHandler,
)
from backend.workflow.application.state_machine_driver import drive_transition
from backend.workflow.domain.state import WorkflowEvent, WorkflowState


@pytest.mark.asyncio
async def test_resolve_decision_handler_no_longer_raises() -> None:
    """ResolveDecisionHandler must not raise NotImplementedError."""
    handler = ResolveDecisionHandler()
    new_state = await handler.handle(
        run=MagicMock(id="r"),
        current_state=WorkflowState.needs_decision,
        event=WorkflowEvent.decision_resolved,
    )
    assert new_state == WorkflowState.dispatched


@pytest.mark.asyncio
async def test_retry_failed_handler_no_longer_raises() -> None:
    """RetryFailedHandler must not raise NotImplementedError."""
    handler = RetryFailedHandler()
    new_state = await handler.handle(
        run=MagicMock(id="r"),
        current_state=WorkflowState.failed,
        event=WorkflowEvent.decision_resolved,
    )
    assert new_state == WorkflowState.dispatched


@pytest.mark.asyncio
async def test_settle_complete_handler_no_longer_raises() -> None:
    """SettleCompleteHandler must not raise NotImplementedError."""
    handler = SettleCompleteHandler()
    new_state = await handler.handle(
        run=MagicMock(id="r"),
        current_state=WorkflowState.shipped,
        event=WorkflowEvent.settle_complete,
    )
    assert new_state == WorkflowState.settled


@pytest.mark.asyncio
async def test_deliver_complete_handler_no_longer_raises() -> None:
    """DeliverCompleteHandler must not raise NotImplementedError."""
    handler = DeliverCompleteHandler()
    new_state = await handler.handle(
        run=MagicMock(id="r"),
        current_state=WorkflowState.settled,
        event=WorkflowEvent.deliver_complete,
    )
    assert new_state == WorkflowState.delivered


# ────────── Driver-level end-to-end ──────────


@pytest.mark.asyncio
async def test_driver_resolve_decision_advances_state() -> None:
    next_state = await drive_transition(
        run=MagicMock(),
        current_state=WorkflowState.needs_decision,
        event=WorkflowEvent.decision_resolved,
    )
    assert next_state == WorkflowState.dispatched


@pytest.mark.asyncio
async def test_driver_retry_failed_advances_state() -> None:
    next_state = await drive_transition(
        run=MagicMock(),
        current_state=WorkflowState.failed,
        event=WorkflowEvent.decision_resolved,
    )
    assert next_state == WorkflowState.dispatched


@pytest.mark.asyncio
async def test_driver_settle_complete_advances_state() -> None:
    next_state = await drive_transition(
        run=MagicMock(),
        current_state=WorkflowState.shipped,
        event=WorkflowEvent.settle_complete,
    )
    assert next_state == WorkflowState.settled


@pytest.mark.asyncio
async def test_driver_deliver_complete_advances_state() -> None:
    next_state = await drive_transition(
        run=MagicMock(),
        current_state=WorkflowState.settled,
        event=WorkflowEvent.deliver_complete,
    )
    assert next_state == WorkflowState.delivered


# ────────── Delegation-target presence ──────────


def test_resolve_decision_delegation_target_importable() -> None:
    """The decision_resolution module under workflow/intake/ must be present (H3a)."""
    from backend.workflow.application.intake import decision_resolution

    assert hasattr(decision_resolution, "DecisionResolutionTrigger")


def test_delivery_dispatcher_delegation_target_importable() -> None:
    """The DeliveryDispatcher under workflow/delivery/ must be present (H3b)."""
    from backend.workflow.application.delivery.dispatcher import DeliveryDispatcher

    assert DeliveryDispatcher is not None


def test_knowledge_settle_delegation_target_importable() -> None:
    """The SettleWorker / settle drain in the knowledge context must be present."""
    from backend.knowledge.infrastructure.workers.settle_worker import SettleWorker

    assert SettleWorker is not None


def test_knowledge_facade_protocol_importable() -> None:
    """The Knowledge facade Protocol (Lift A) names a settle method."""
    from backend.knowledge.facade import Knowledge

    assert hasattr(Knowledge, "settle")
