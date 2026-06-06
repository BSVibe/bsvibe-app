"""LiteLLMAdapter + ExecutorAdapter — wire-shape + delegation tests (Lift E2)."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest

from backend.dispatch.adapter import (
    ChatResponse,
    ExecutorAdapter,
    LiteLLMAdapter,
    ModelAccountAdapter,
    _from_llm_response,
)
from backend.router.accounts.models import ModelAccount
from backend.router.llm_client import LlmClient, LlmResponse


def _stub_account(provider: str = "ollama") -> ModelAccount:
    return ModelAccount(
        id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        account_id=uuid.uuid4(),
        provider=provider,
        label="test",
        litellm_model="ollama_chat/qwen3" if provider == "ollama" else "anthropic/claude-x",
        api_base=None,
        api_key_encrypted=None,
        data_jurisdiction="us",
        is_active=True,
        extra_params={},
    )


class TestLiteLLMAdapter:
    async def test_chat_calls_llm_client(self) -> None:
        account = _stub_account()
        mock_completion = AsyncMock(
            return_value={
                "choices": [{"message": {"content": "hello", "tool_calls": []}}],
                "usage": {"prompt_tokens": 4, "completion_tokens": 1},
            }
        )
        llm = LlmClient(completion_fn=mock_completion)
        adapter = LiteLLMAdapter(
            account=account,
            api_key="",
            llm=llm,
            workspace_id=account.workspace_id,
            account_id=account.account_id,
            model_account_id=account.id,
        )
        response = await adapter.chat(
            system="be terse",
            messages=[{"role": "user", "content": "hi"}],
        )
        assert isinstance(response, ChatResponse)
        assert response.content == "hello"
        assert response.tool_calls == ()
        assert response.usage_prompt_tokens == 4
        assert response.usage_completion_tokens == 1
        # System prompt prepended; user message preserved.
        kwargs = mock_completion.call_args.kwargs
        assert kwargs["messages"][0] == {"role": "system", "content": "be terse"}
        assert kwargs["messages"][1] == {"role": "user", "content": "hi"}

    async def test_supported_methods_chat_only(self) -> None:
        adapter = LiteLLMAdapter(
            account=_stub_account(),
            api_key="",
            llm=LlmClient(completion_fn=AsyncMock()),
            workspace_id=uuid.uuid4(),
            account_id=uuid.uuid4(),
            model_account_id=uuid.uuid4(),
        )
        assert adapter.supported_methods == frozenset({"chat"})


class TestExecutorAdapter:
    async def test_chat_raises_until_e3(self) -> None:
        adapter = ExecutorAdapter(
            account=_stub_account("executor"),
            workspace_id=uuid.uuid4(),
            account_id=uuid.uuid4(),
            model_account_id=uuid.uuid4(),
        )
        with pytest.raises(NotImplementedError, match="ExecutorAdapter.chat"):
            await adapter.chat(system="x", messages=[{"role": "user", "content": "y"}])

    def test_supported_methods_chat_only(self) -> None:
        adapter = ExecutorAdapter(
            account=_stub_account("executor"),
            workspace_id=uuid.uuid4(),
            account_id=uuid.uuid4(),
            model_account_id=uuid.uuid4(),
        )
        assert adapter.supported_methods == frozenset({"chat"})


class TestProtocolConformance:
    """Both adapters satisfy the ``ModelAccountAdapter`` Protocol."""

    def test_litellm_adapter_is_model_account_adapter(self) -> None:
        adapter = LiteLLMAdapter(
            account=_stub_account(),
            api_key="",
            llm=LlmClient(completion_fn=AsyncMock()),
            workspace_id=uuid.uuid4(),
            account_id=uuid.uuid4(),
            model_account_id=uuid.uuid4(),
        )
        assert isinstance(adapter, ModelAccountAdapter)

    def test_executor_adapter_is_model_account_adapter(self) -> None:
        adapter = ExecutorAdapter(
            account=_stub_account("executor"),
            workspace_id=uuid.uuid4(),
            account_id=uuid.uuid4(),
            model_account_id=uuid.uuid4(),
        )
        assert isinstance(adapter, ModelAccountAdapter)


def test_from_llm_response_normalizes_tool_calls() -> None:
    response = LlmResponse(
        content="ok",
        usage_prompt_tokens=2,
        usage_completion_tokens=3,
        tool_calls=({"id": "abc", "function": {"name": "write_file", "arguments": "{}"}},),
    )
    chat = _from_llm_response(response)
    assert chat.content == "ok"
    assert len(chat.tool_calls) == 1
    assert chat.tool_calls[0].id == "abc"
    assert chat.tool_calls[0].name == "write_file"
    assert chat.tool_calls[0].arguments_json == "{}"
