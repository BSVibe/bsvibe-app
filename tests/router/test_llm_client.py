"""Tests for backend.router.llm_client — chat surface, mocked litellm."""

from __future__ import annotations

from unittest.mock import AsyncMock

from backend.router.llm_client import LlmClient, LlmResponse


class _Choice:
    def __init__(self, content: str) -> None:
        self.message = type("M", (), {"content": content})()


class _Usage:
    def __init__(self, prompt: int, completion: int) -> None:
        self.prompt_tokens = prompt
        self.completion_tokens = completion


class _Response:
    def __init__(self, content: str) -> None:
        self.choices = [_Choice(content)]
        self.usage = _Usage(10, 5)


class TestChatSurface:
    async def test_extracts_content_and_usage_from_litellm_shape(self):
        fake = AsyncMock(return_value=_Response("hello"))
        client = LlmClient(completion_fn=fake)
        response = await client.chat(model="openai/gpt-4o", messages=[])
        assert isinstance(response, LlmResponse)
        assert response.content == "hello"
        assert response.usage_prompt_tokens == 10
        assert response.usage_completion_tokens == 5

    async def test_works_with_dict_shaped_response(self):
        fake = AsyncMock(
            return_value={
                "choices": [{"message": {"content": "world"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 2},
            }
        )
        client = LlmClient(completion_fn=fake)
        response = await client.chat(model="m", messages=[])
        assert response.content == "world"
        assert response.usage_prompt_tokens == 1
        assert response.usage_completion_tokens == 2

    async def test_passes_through_api_base_and_key(self):
        fake = AsyncMock(return_value=_Response(""))
        client = LlmClient(completion_fn=fake)
        await client.chat(
            model="m",
            messages=[{"role": "user", "content": "hi"}],
            api_base="https://api.example.com",
            api_key="sk-x",
            extra_params={"temperature": 0.1},
        )
        kwargs = fake.await_args.kwargs
        assert kwargs["api_base"] == "https://api.example.com"
        assert kwargs["api_key"] == "sk-x"
        assert kwargs["temperature"] == 0.1
        assert kwargs["model"] == "m"

    async def test_missing_usage_defaults_zero(self):
        fake = AsyncMock(return_value={"choices": [{"message": {"content": "x"}}]})
        client = LlmClient(completion_fn=fake)
        response = await client.chat(model="m", messages=[])
        assert response.usage_prompt_tokens == 0
        assert response.usage_completion_tokens == 0

    async def test_no_choices_yields_empty_content(self):
        fake = AsyncMock(return_value={"choices": []})
        client = LlmClient(completion_fn=fake)
        response = await client.chat(model="m", messages=[])
        assert response.content == ""
