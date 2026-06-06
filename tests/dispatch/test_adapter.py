"""LiteLLMAdapter + ExecutorAdapter — wire-shape + delegation tests (Lift E3)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

import pytest

# Importing the module dbs registers them on the shared Base.metadata so
# memory_session create_all materialises them for the executor task /
# worker rows the ExecutorAdapter writes.
import backend.executors.db  # noqa: F401
from backend.config import get_settings
from backend.dispatch.adapter import (
    ChatResponse,
    ExecutorAdapter,
    ExecutorAdapterUnavailable,
    LiteLLMAdapter,
    ModelAccountAdapter,
    _from_llm_response,
    _render_prompt,
)
from backend.executors import dispatch
from backend.executors.db import WorkerRow
from backend.router.accounts.models import ModelAccount
from backend.router.llm_client import LlmClient, LlmResponse

from .._support import memory_session, shared_file_sessionmaker


def _stub_account(
    provider: str = "ollama", extra_params: dict[str, Any] | None = None
) -> ModelAccount:
    return ModelAccount(
        id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        account_id=uuid.uuid4(),
        provider=provider,
        label="test",
        litellm_model="ollama_chat/qwen3" if provider == "ollama" else "executor/claude_code",
        api_base=None,
        api_key_encrypted=None,
        data_jurisdiction="us",
        is_active=True,
        extra_params=extra_params or {},
    )


async def _make_redis() -> Any:
    try:
        import fakeredis
        import fakeredis.aioredis as fakeredis_aio
    except ImportError:  # pragma: no cover
        pytest.skip("fakeredis not installed")
    client = fakeredis_aio.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True)
    await client.flushdb()
    return client


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


# --------------------------------------------------------------------------
# ExecutorAdapter — Lift E3 wires the subprocess dispatch path.
# --------------------------------------------------------------------------


async def _seed_worker(
    s: Any,
    *,
    workspace_id: uuid.UUID,
    capabilities: list[str],
) -> WorkerRow:
    """Insert a fresh online worker with the requested capabilities."""
    worker = WorkerRow(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        name="mac-mini",
        labels=[],
        capabilities=list(capabilities),
        status="online",
        last_heartbeat=datetime.now(UTC) - timedelta(seconds=1),
        token_hash="0" * 64,
        is_active=True,
    )
    s.add(worker)
    await s.flush()
    return worker


def _executor_account(workspace_id: uuid.UUID, worker_id: uuid.UUID) -> ModelAccount:
    return ModelAccount(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        account_id=uuid.uuid4(),
        provider="executor",
        label="mac-mini",
        litellm_model="executor/claude_code",
        api_base=None,
        api_key_encrypted=None,
        data_jurisdiction="unknown",
        is_active=True,
        extra_params={"worker_id": str(worker_id), "executor_type": "claude_code"},
    )


class TestExecutorAdapterChat:
    async def test_supported_methods_chat_only(self) -> None:
        async with memory_session() as s:
            adapter = ExecutorAdapter(
                account=_stub_account("executor"),
                workspace_id=uuid.uuid4(),
                account_id=uuid.uuid4(),
                model_account_id=uuid.uuid4(),
                session=s,
                settings=get_settings(),
            )
            assert adapter.supported_methods == frozenset({"chat"})

    async def test_chat_rejects_tools(self) -> None:
        async with memory_session() as s:
            adapter = ExecutorAdapter(
                account=_stub_account("executor"),
                workspace_id=uuid.uuid4(),
                account_id=uuid.uuid4(),
                model_account_id=uuid.uuid4(),
                session=s,
                settings=get_settings(),
            )
            with pytest.raises(NotImplementedError, match="does not support tool calls"):
                await adapter.chat(
                    system="x",
                    messages=[{"role": "user", "content": "y"}],
                    tools=[{"type": "function", "function": {"name": "write_file"}}],
                )

    async def test_chat_without_redis_raises(self) -> None:
        async with memory_session() as s:
            adapter = ExecutorAdapter(
                account=_stub_account(
                    "executor",
                    extra_params={"executor_type": "claude_code", "worker_id": str(uuid.uuid4())},
                ),
                workspace_id=uuid.uuid4(),
                account_id=uuid.uuid4(),
                model_account_id=uuid.uuid4(),
                session=s,
                settings=get_settings(),
                redis=None,
            )
            with pytest.raises(ExecutorAdapterUnavailable, match="requires a Redis client"):
                await adapter.chat(system="x", messages=[{"role": "user", "content": "y"}])

    async def test_chat_without_executor_type_raises(self) -> None:
        redis = await _make_redis()
        async with memory_session() as s:
            adapter = ExecutorAdapter(
                account=_stub_account("executor", extra_params={}),
                workspace_id=uuid.uuid4(),
                account_id=uuid.uuid4(),
                model_account_id=uuid.uuid4(),
                session=s,
                settings=get_settings(),
                redis=redis,
            )
            with pytest.raises(ExecutorAdapterUnavailable, match="executor_type"):
                await adapter.chat(system="x", messages=[{"role": "user", "content": "y"}])

    async def test_chat_no_worker_raises(self) -> None:
        redis = await _make_redis()
        workspace_id = uuid.uuid4()
        async with memory_session() as s:
            adapter = ExecutorAdapter(
                account=_stub_account(
                    "executor",
                    extra_params={"executor_type": "claude_code"},
                ),
                workspace_id=workspace_id,
                account_id=uuid.uuid4(),
                model_account_id=uuid.uuid4(),
                session=s,
                settings=get_settings(),
                redis=redis,
            )
            with pytest.raises(ExecutorAdapterUnavailable, match="no online worker"):
                await adapter.chat(system="x", messages=[{"role": "user", "content": "y"}])

    async def test_chat_happy_path_dispatches_and_returns_output(self) -> None:
        """Adapter dispatches a chat task and surfaces the worker's output."""
        import asyncio

        redis = await _make_redis()
        workspace_id = uuid.uuid4()
        settings = get_settings().model_copy(update={"executor_task_timeout_s": 30.0})

        async with shared_file_sessionmaker() as sf:
            async with sf() as setup:
                worker = await _seed_worker(
                    setup, workspace_id=workspace_id, capabilities=["claude_code"]
                )
                account = _executor_account(workspace_id, worker.id)
                setup.add(account)
                await setup.commit()

            async with sf() as adapter_session:
                adapter = ExecutorAdapter(
                    account=account,
                    workspace_id=workspace_id,
                    account_id=account.account_id,
                    model_account_id=account.id,
                    session=adapter_session,
                    settings=settings,
                    redis=redis,
                )

                # Simulate the worker reporting its result on a SEPARATE session
                # — the same pattern test_executor_run_e2e.py uses for the
                # legacy full-run path.
                async def _simulate_worker() -> None:
                    stream = dispatch.worker_stream(worker.id)
                    last_id = "0"
                    for _ in range(500):
                        entries = await redis.xread({stream: last_id}, count=1, block=20)
                        if not entries:
                            continue
                        _name, messages = entries[0]
                        for msg_id, fields in messages:
                            last_id = msg_id
                            task_id = uuid.UUID(fields["task_id"])
                            async with sf() as ws_session:
                                await dispatch.record_result(
                                    ws_session,
                                    redis,
                                    task_id=task_id,
                                    success=True,
                                    output="42",
                                    error_message=None,
                                )
                                await ws_session.commit()
                            return
                    raise AssertionError("worker stream never saw the XADD")

                worker_task = asyncio.create_task(_simulate_worker())
                try:
                    response = await adapter.chat(
                        system="be terse",
                        messages=[{"role": "user", "content": "what is 6 * 7?"}],
                    )
                finally:
                    await worker_task

                assert response.content == "42"
                assert response.tool_calls == ()

    async def test_chat_worker_failure_raises(self) -> None:
        """Worker reports ``success=False`` → adapter raises with the error."""
        import asyncio

        redis = await _make_redis()
        workspace_id = uuid.uuid4()
        settings = get_settings().model_copy(update={"executor_task_timeout_s": 30.0})

        async with shared_file_sessionmaker() as sf:
            async with sf() as setup:
                worker = await _seed_worker(
                    setup, workspace_id=workspace_id, capabilities=["claude_code"]
                )
                account = _executor_account(workspace_id, worker.id)
                setup.add(account)
                await setup.commit()

            async with sf() as adapter_session:
                adapter = ExecutorAdapter(
                    account=account,
                    workspace_id=workspace_id,
                    account_id=account.account_id,
                    model_account_id=account.id,
                    session=adapter_session,
                    settings=settings,
                    redis=redis,
                )

                async def _simulate_worker_failure() -> None:
                    stream = dispatch.worker_stream(worker.id)
                    last_id = "0"
                    for _ in range(500):
                        entries = await redis.xread({stream: last_id}, count=1, block=20)
                        if not entries:
                            continue
                        _name, messages = entries[0]
                        for msg_id, fields in messages:
                            last_id = msg_id
                            task_id = uuid.UUID(fields["task_id"])
                            async with sf() as ws_session:
                                await dispatch.record_result(
                                    ws_session,
                                    redis,
                                    task_id=task_id,
                                    success=False,
                                    output="",
                                    error_message="rate limit exceeded",
                                )
                                await ws_session.commit()
                            return
                    raise AssertionError("worker stream never saw the XADD")

                worker_task = asyncio.create_task(_simulate_worker_failure())
                try:
                    with pytest.raises(ExecutorAdapterUnavailable, match="rate limit exceeded"):
                        await adapter.chat(
                            system="be terse",
                            messages=[{"role": "user", "content": "x"}],
                        )
                finally:
                    await worker_task


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

    async def test_executor_adapter_is_model_account_adapter(self) -> None:
        async with memory_session() as s:
            adapter = ExecutorAdapter(
                account=_stub_account("executor"),
                workspace_id=uuid.uuid4(),
                account_id=uuid.uuid4(),
                model_account_id=uuid.uuid4(),
                session=s,
                settings=get_settings(),
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


class TestRenderPrompt:
    def test_simple_user_message(self) -> None:
        assert _render_prompt([{"role": "user", "content": "hello"}]) == "user: hello"

    def test_drops_system_message(self) -> None:
        # System slot ships separately via --append-system-prompt; we
        # must not double-include it as a transcript line.
        rendered = _render_prompt(
            [
                {"role": "system", "content": "be terse"},
                {"role": "user", "content": "hi"},
            ]
        )
        assert "be terse" not in rendered
        assert rendered == "user: hi"

    def test_concatenates_content_parts(self) -> None:
        rendered = _render_prompt(
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "hello"},
                        {"type": "text", "text": " world"},
                    ],
                }
            ]
        )
        assert rendered == "user: hello world"

    def test_renders_tool_message(self) -> None:
        rendered = _render_prompt(
            [
                {"role": "tool", "name": "write_file", "content": "ok"},
            ]
        )
        assert rendered == "[tool:write_file] ok"

    def test_multi_turn_transcript(self) -> None:
        rendered = _render_prompt(
            [
                {"role": "user", "content": "q1"},
                {"role": "assistant", "content": "a1"},
                {"role": "user", "content": "q2"},
            ]
        )
        assert rendered == "user: q1\n\nassistant: a1\n\nuser: q2"
