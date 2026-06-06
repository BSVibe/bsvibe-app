"""LiteLLMAdapter + ExecutorAdapter — wire-shape + delegation tests."""

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
    async def test_direct_path_calls_llm_client(self) -> None:
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
            dispatcher=None,  # direct path
            legacy_features=None,
        )
        response = await adapter.chat(
            system="be terse",
            messages=[{"role": "user", "content": "hi"}],
        )
        assert isinstance(response, ChatResponse)
        assert response.content == "hello"
        assert response.usage_prompt_tokens == 4
        mock_completion.assert_awaited_once()
        # System prompt prepended.
        kwargs = mock_completion.await_args.kwargs
        assert kwargs["messages"][0] == {"role": "system", "content": "be terse"}
        assert kwargs["messages"][1] == {"role": "user", "content": "hi"}

    async def test_dispatcher_path_routes_through_gateway(self) -> None:
        account = _stub_account()
        # Hand-mock the dispatcher so we don't touch the real classifier.
        from backend.router.classifier.base import (
            ClassificationFeatures,
            ClassificationResult,
        )
        from backend.router.dispatch import DispatchResult

        mock_dispatcher = AsyncMock()
        mock_dispatcher.dispatch.return_value = DispatchResult(
            classification=ClassificationResult(
                tier="local", score=10, strategy="static", reason="t"
            ),
            response=LlmResponse(
                content="from-gateway",
                usage_prompt_tokens=2,
                usage_completion_tokens=1,
                tool_calls=(),
            ),
            actual_cost_cents=1,
        )
        adapter = LiteLLMAdapter(
            account=account,
            api_key="",
            llm=LlmClient(completion_fn=AsyncMock()),  # untouched
            workspace_id=account.workspace_id,
            account_id=account.account_id,
            model_account_id=account.id,
            dispatcher=mock_dispatcher,
            legacy_features=ClassificationFeatures(
                token_count=10,
                system_prompt_chars=0,
                conversation_turns=1,
                code_block_count=0,
                tool_count=0,
            ),
        )
        response = await adapter.chat(
            system="s",
            messages=[{"role": "user", "content": "u"}],
        )
        assert response.content == "from-gateway"
        mock_dispatcher.dispatch.assert_awaited_once()

    def test_supported_methods_includes_chat(self) -> None:
        adapter = LiteLLMAdapter(
            account=_stub_account(),
            api_key="",
            llm=LlmClient(completion_fn=AsyncMock()),
            workspace_id=uuid.uuid4(),
            account_id=uuid.uuid4(),
            model_account_id=uuid.uuid4(),
        )
        assert "chat" in adapter.supported_methods

    def test_satisfies_protocol(self) -> None:
        adapter = LiteLLMAdapter(
            account=_stub_account(),
            api_key="",
            llm=LlmClient(completion_fn=AsyncMock()),
            workspace_id=uuid.uuid4(),
            account_id=uuid.uuid4(),
            model_account_id=uuid.uuid4(),
        )
        assert isinstance(adapter, ModelAccountAdapter)


class TestExecutorAdapter:
    async def test_chat_without_dispatcher_raises_not_implemented(self) -> None:
        adapter = ExecutorAdapter(
            account=_stub_account(provider="executor"),
            workspace_id=uuid.uuid4(),
            account_id=uuid.uuid4(),
            model_account_id=uuid.uuid4(),
            dispatcher=None,
            legacy_features=None,
        )
        with pytest.raises(NotImplementedError):
            await adapter.chat(
                system="x",
                messages=[{"role": "user", "content": "y"}],
            )

    async def test_chat_delegates_to_dispatcher_when_wired(self) -> None:
        from backend.router.classifier.base import (
            ClassificationFeatures,
            ClassificationResult,
        )
        from backend.router.dispatch import DispatchResult

        mock_dispatcher = AsyncMock()
        mock_dispatcher.dispatch.return_value = DispatchResult(
            classification=ClassificationResult(
                tier="cloud", score=80, strategy="static", reason="t"
            ),
            response=LlmResponse(
                content="exec-output",
                usage_prompt_tokens=10,
                usage_completion_tokens=5,
                tool_calls=(),
            ),
            actual_cost_cents=1,
        )
        adapter = ExecutorAdapter(
            account=_stub_account(provider="executor"),
            workspace_id=uuid.uuid4(),
            account_id=uuid.uuid4(),
            model_account_id=uuid.uuid4(),
            dispatcher=mock_dispatcher,
            legacy_features=ClassificationFeatures(
                token_count=10,
                system_prompt_chars=0,
                conversation_turns=1,
                code_block_count=0,
                tool_count=0,
            ),
        )
        response = await adapter.chat(
            system="s",
            messages=[{"role": "user", "content": "u"}],
        )
        assert response.content == "exec-output"

    def test_supported_methods_includes_chat(self) -> None:
        adapter = ExecutorAdapter(
            account=_stub_account(provider="executor"),
            workspace_id=uuid.uuid4(),
            account_id=uuid.uuid4(),
            model_account_id=uuid.uuid4(),
        )
        assert "chat" in adapter.supported_methods


class TestFromLlmResponse:
    def test_normalizes_tool_calls(self) -> None:
        raw = LlmResponse(
            content="call X",
            usage_prompt_tokens=1,
            usage_completion_tokens=2,
            tool_calls=(
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "do_x", "arguments": '{"a": 1}'},
                },
            ),
        )
        chat = _from_llm_response(raw)
        assert chat.content == "call X"
        assert chat.tool_calls[0].name == "do_x"
        assert chat.tool_calls[0].arguments_json == '{"a": 1}'
