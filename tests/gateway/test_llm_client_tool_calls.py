"""LlmClient tool-call surfacing — the agent loop needs tool_calls back,
not just content."""

from __future__ import annotations

from typing import Any

from backend.gateway.llm_client import LlmClient


async def test_chat_forwards_tools_and_surfaces_tool_calls() -> None:
    seen: dict[str, Any] = {}

    async def fake_completion(**kwargs: Any) -> dict[str, Any]:
        seen.update(kwargs)
        return {
            "choices": [
                {
                    "message": {
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call-1",
                                "type": "function",
                                "function": {
                                    "name": "file_write",
                                    "arguments": '{"path": "a.txt", "content": "hi"}',
                                },
                            }
                        ],
                    }
                }
            ],
            "usage": {"prompt_tokens": 3, "completion_tokens": 4},
        }

    client = LlmClient(completion_fn=fake_completion)
    tools = [{"type": "function", "function": {"name": "file_write"}}]
    resp = await client.chat(model="m", messages=[{"role": "user", "content": "x"}], tools=tools)

    assert seen["tools"] == tools  # tools were forwarded to the provider
    assert len(resp.tool_calls) == 1
    assert resp.tool_calls[0]["function"]["name"] == "file_write"
    assert resp.tool_calls[0]["id"] == "call-1"


async def test_chat_without_tools_has_empty_tool_calls() -> None:
    async def fake_completion(**kwargs: Any) -> dict[str, Any]:
        assert "tools" not in kwargs  # not forwarded when None
        return {
            "choices": [{"message": {"content": "plain answer"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }

    client = LlmClient(completion_fn=fake_completion)
    resp = await client.chat(model="m", messages=[])
    assert resp.content == "plain answer"
    assert resp.tool_calls == ()


async def test_chat_normalizes_object_shaped_tool_calls() -> None:
    """litellm returns objects, not dicts — they must still normalize."""

    class _Fn:
        name = "shell_exec"
        arguments = '{"command": "ls"}'

    class _Call:
        id = "c9"
        type = "function"
        function = _Fn()

    class _Msg:
        content = ""
        tool_calls = [_Call()]

    class _Choice:
        message = _Msg()

    class _Raw:
        choices = [_Choice()]
        usage = {"prompt_tokens": 0, "completion_tokens": 0}

    async def fake_completion(**kwargs: Any) -> _Raw:
        return _Raw()

    client = LlmClient(completion_fn=fake_completion)
    resp = await client.chat(model="m", messages=[], tools=[{"type": "function"}])
    assert resp.tool_calls[0]["function"]["name"] == "shell_exec"
    assert resp.tool_calls[0]["function"]["arguments"] == '{"command": "ls"}'
