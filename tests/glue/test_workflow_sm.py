"""WorkflowStateMachine — Workflow §1 3+ε transition table."""

from __future__ import annotations

import uuid

import pytest

from backend.orchestrator.schema import WorkflowState
from backend.orchestrator.workflow_sm import (
    InvalidTransitionError,
    WorkflowStateMachine,
)


@pytest.mark.asyncio
async def test_receive_to_frame() -> None:
    sm = WorkflowStateMachine()
    state = WorkflowState(stage="receive", request_id=uuid.uuid4(), run_id=None)
    nxt = await sm.transition(state=state, event="framed")
    assert nxt.stage == "frame"


@pytest.mark.asyncio
async def test_frame_to_agent_loop() -> None:
    sm = WorkflowStateMachine()
    state = WorkflowState(stage="frame", request_id=uuid.uuid4(), run_id=None)
    nxt = await sm.transition(state=state, event="agent_started")
    assert nxt.stage == "agent_loop"


@pytest.mark.asyncio
async def test_agent_loop_to_epsilon() -> None:
    sm = WorkflowStateMachine()
    state = WorkflowState(stage="agent_loop", request_id=uuid.uuid4(), run_id=uuid.uuid4())
    nxt = await sm.transition(state=state, event="settled")
    assert nxt.stage == "epsilon"


@pytest.mark.asyncio
async def test_epsilon_self_loop_on_cleaned() -> None:
    sm = WorkflowStateMachine()
    state = WorkflowState(stage="epsilon", request_id=uuid.uuid4(), run_id=None)
    nxt = await sm.transition(state=state, event="cleaned")
    assert nxt.stage == "epsilon"


@pytest.mark.asyncio
async def test_epsilon_can_re_enter_agent_loop() -> None:
    sm = WorkflowStateMachine()
    state = WorkflowState(stage="epsilon", request_id=uuid.uuid4(), run_id=None)
    nxt = await sm.transition(state=state, event="agent_restarted")
    assert nxt.stage == "agent_loop"


@pytest.mark.asyncio
async def test_illegal_transition_raises() -> None:
    sm = WorkflowStateMachine()
    state = WorkflowState(stage="receive", request_id=uuid.uuid4(), run_id=None)
    with pytest.raises(InvalidTransitionError, match="stage='receive'"):
        await sm.transition(state=state, event="settled")


@pytest.mark.asyncio
async def test_run_id_preserved() -> None:
    sm = WorkflowStateMachine()
    rid = uuid.uuid4()
    state = WorkflowState(stage="agent_loop", request_id=uuid.uuid4(), run_id=rid)
    nxt = await sm.transition(state=state, event="settled")
    assert nxt.run_id == rid
