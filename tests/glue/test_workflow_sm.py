"""LegacyWorkflowStateMachine — Workflow §1 3+ε legacy transition table.

Post-H2b the 4-stage state machine lives in
:mod:`backend.workflow.domain.state` as ``LegacyWorkflowStateMachine`` /
``LegacyWorkflowState`` / ``InvalidLegacyTransitionError`` — the old
``backend.orchestrator.workflow_sm`` module is gone. The v8 13-state
coarse enum (:class:`WorkflowState`) is the canonical Workflow surface;
the legacy 4-stage is preserved for migration ergonomics.
"""

from __future__ import annotations

import uuid

import pytest

from backend.workflow.domain.state import (
    InvalidLegacyTransitionError,
    LegacyWorkflowState,
    LegacyWorkflowStateMachine,
)


@pytest.mark.asyncio
async def test_receive_to_frame() -> None:
    sm = LegacyWorkflowStateMachine()
    state = LegacyWorkflowState(stage="receive", request_id=uuid.uuid4(), run_id=None)
    nxt = await sm.transition(state=state, event="framed")
    assert nxt.stage == "frame"


@pytest.mark.asyncio
async def test_frame_to_agent_loop() -> None:
    sm = LegacyWorkflowStateMachine()
    state = LegacyWorkflowState(stage="frame", request_id=uuid.uuid4(), run_id=None)
    nxt = await sm.transition(state=state, event="agent_started")
    assert nxt.stage == "agent_loop"


@pytest.mark.asyncio
async def test_agent_loop_to_epsilon() -> None:
    sm = LegacyWorkflowStateMachine()
    state = LegacyWorkflowState(stage="agent_loop", request_id=uuid.uuid4(), run_id=uuid.uuid4())
    nxt = await sm.transition(state=state, event="settled")
    assert nxt.stage == "epsilon"


@pytest.mark.asyncio
async def test_epsilon_self_loop_on_cleaned() -> None:
    sm = LegacyWorkflowStateMachine()
    state = LegacyWorkflowState(stage="epsilon", request_id=uuid.uuid4(), run_id=None)
    nxt = await sm.transition(state=state, event="cleaned")
    assert nxt.stage == "epsilon"


@pytest.mark.asyncio
async def test_epsilon_can_re_enter_agent_loop() -> None:
    sm = LegacyWorkflowStateMachine()
    state = LegacyWorkflowState(stage="epsilon", request_id=uuid.uuid4(), run_id=None)
    nxt = await sm.transition(state=state, event="agent_restarted")
    assert nxt.stage == "agent_loop"


@pytest.mark.asyncio
async def test_illegal_transition_raises() -> None:
    sm = LegacyWorkflowStateMachine()
    state = LegacyWorkflowState(stage="receive", request_id=uuid.uuid4(), run_id=None)
    with pytest.raises(InvalidLegacyTransitionError, match="stage='receive'"):
        await sm.transition(state=state, event="settled")


@pytest.mark.asyncio
async def test_run_id_preserved() -> None:
    sm = LegacyWorkflowStateMachine()
    rid = uuid.uuid4()
    state = LegacyWorkflowState(stage="agent_loop", request_id=uuid.uuid4(), run_id=rid)
    nxt = await sm.transition(state=state, event="settled")
    assert nxt.run_id == rid
