"""GatewayLoopLlm adapter — maps DispatchResult → LoopTurn and threads
the tool schema into the DispatchRequest."""

from __future__ import annotations

import uuid

from backend.execution.loop_llm import GatewayLoopLlm
from backend.gateway.classifier.base import ClassificationResult
from backend.gateway.dispatch import DispatchRequest, DispatchResult
from backend.gateway.llm_client import LlmResponse


class _FakeDispatcher:
    """Records the DispatchRequest and returns a canned DispatchResult."""

    def __init__(self, response: LlmResponse) -> None:
        self._response = response
        self.seen: DispatchRequest | None = None

    async def dispatch(self, req: DispatchRequest) -> DispatchResult:
        self.seen = req
        return DispatchResult(
            classification=ClassificationResult(tier="cloud", score=90, strategy="substantial"),
            response=self._response,
            actual_cost_cents=1,
        )


async def test_complete_threads_tools_and_maps_tool_calls() -> None:
    response = LlmResponse(
        content="planning",
        usage_prompt_tokens=1,
        usage_completion_tokens=1,
        tool_calls=(
            {
                "id": "c1",
                "type": "function",
                "function": {"name": "file_write", "arguments": '{"path": "x", "content": "y"}'},
            },
        ),
    )
    dispatcher = _FakeDispatcher(response)
    llm = GatewayLoopLlm(
        dispatcher=dispatcher,  # type: ignore[arg-type]
        workspace_id=uuid.uuid4(),
        account_id=uuid.uuid4(),
        model_account_id=uuid.uuid4(),
    )
    tools = [{"type": "function", "function": {"name": "file_write"}}]
    turn = await llm.complete(messages=[{"role": "user", "content": "go"}], tools=tools)

    assert turn.content == "planning"
    assert len(turn.tool_calls) == 1
    call = turn.tool_calls[0]
    assert call.name == "file_write"
    assert call.arguments == {"path": "x", "content": "y"}
    # The tool schema was threaded into the dispatch request.
    assert dispatcher.seen is not None
    assert dispatcher.seen.tools == tools


async def test_complete_handles_bad_arguments_json() -> None:
    response = LlmResponse(
        content="",
        usage_prompt_tokens=0,
        usage_completion_tokens=0,
        tool_calls=(
            {
                "id": "c2",
                "type": "function",
                "function": {"name": "shell_exec", "arguments": "not json"},
            },
        ),
    )
    llm = GatewayLoopLlm(
        dispatcher=_FakeDispatcher(response),  # type: ignore[arg-type]
        workspace_id=uuid.uuid4(),
        account_id=uuid.uuid4(),
        model_account_id=uuid.uuid4(),
    )
    turn = await llm.complete(messages=[], tools=None)
    assert turn.tool_calls[0].arguments == {}
