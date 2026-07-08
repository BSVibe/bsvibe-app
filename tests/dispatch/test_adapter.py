"""LiteLLMAdapter + ExecutorAdapter — wire-shape + delegation tests (Lift E3)."""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Iterator
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


def _poll_deadline(seconds: float = 30.0) -> Iterator[bool]:
    """Time-bounded replacement for ``range(N)`` in the worker-stream polls.

    The worker-simulation loops below poll a fakeredis stream for the
    ExecutorAdapter's XADD. A fixed iteration count gave up after a fixed
    *number* of polls, which a contended CI event loop (a shared-SQLite
    lock resolving, xdist CPU pressure delaying the chat coroutine's XADD)
    could exhaust before the entry landed — the intermittent "worker
    stream never saw the XADD" flake. A wall-clock deadline waits long
    enough regardless of how many polls fit in the window, and still
    returns immediately on the happy path.
    """
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        yield True


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

    async def test_chat_appends_output_language_directive(self) -> None:
        """#6 — when the workspace output language is set (via the contextvar the
        resolver stamps), chat() appends a 'write prose in <lang>' directive to
        the system prompt so generated prose follows the workspace language.
        English (the default) appends nothing."""
        from backend.identity.output_language import set_output_language

        account = _stub_account()
        mock_completion = AsyncMock(
            return_value={
                "choices": [{"message": {"content": "x", "tool_calls": []}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            }
        )
        adapter = LiteLLMAdapter(
            account=account,
            api_key="",
            llm=LlmClient(completion_fn=mock_completion),
            workspace_id=account.workspace_id,
            account_id=account.account_id,
            model_account_id=account.id,
        )
        try:
            set_output_language("ko")
            await adapter.chat(system="be terse", messages=[{"role": "user", "content": "hi"}])
            sys_msg = mock_completion.call_args.kwargs["messages"][0]["content"]
            assert sys_msg.startswith("be terse")
            assert "Korean" in sys_msg

            set_output_language("en")
            await adapter.chat(system="be terse", messages=[{"role": "user", "content": "hi"}])
            assert mock_completion.call_args.kwargs["messages"][0]["content"] == "be terse"
        finally:
            set_output_language("en")

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

    async def test_chat_propagates_per_caller_timeout(self) -> None:
        """Lift E9 — when constructed with ``timeout_s`` the adapter folds
        it into LiteLLM's ``timeout`` kwarg so a chat-shaped caller's 3 min
        cap actually reaches the provider call."""
        account = _stub_account()
        mock_completion = AsyncMock(
            return_value={
                "choices": [{"message": {"content": "ok", "tool_calls": []}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
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
            timeout_s=180.0,
        )
        await adapter.chat(system="x", messages=[{"role": "user", "content": "y"}])
        kwargs = mock_completion.call_args.kwargs
        assert kwargs["timeout"] == 180.0

    async def test_chat_without_timeout_omits_kwarg(self) -> None:
        """``timeout_s=None`` lets LiteLLM use its own default."""
        account = _stub_account()
        mock_completion = AsyncMock(
            return_value={
                "choices": [{"message": {"content": "ok", "tool_calls": []}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
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
        await adapter.chat(system="x", messages=[{"role": "user", "content": "y"}])
        kwargs = mock_completion.call_args.kwargs
        assert "timeout" not in kwargs

    async def test_account_extra_params_timeout_wins_over_caller_default(self) -> None:
        """Operator-set ``extra_params.timeout`` on the model account row
        overrides the per-caller default — lets a deployment tune per
        provider when a global per-caller cap is too aggressive."""
        account = _stub_account(extra_params={"timeout": 600.0})
        mock_completion = AsyncMock(
            return_value={
                "choices": [{"message": {"content": "ok", "tool_calls": []}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
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
            timeout_s=180.0,
        )
        await adapter.chat(system="x", messages=[{"role": "user", "content": "y"}])
        kwargs = mock_completion.call_args.kwargs
        assert kwargs["timeout"] == 600.0


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

    async def test_chat_with_tools_no_longer_rejects(self) -> None:
        """Lift E30 — passing tools to ExecutorAdapter MUST NOT raise.

        Pre-E30 the adapter rejected any chat with ``tools`` so the agent
        loop forced LiteLLM-backed accounts only. That violated the
        ``no-implicit-routing`` principle (BSVibe must work with a coding
        agent attached). Post-E30 the tool list is formatted into the
        system prompt as a verification-contract guide; the dispatch still
        needs Redis to actually fire, which the next assertion checks.
        """
        async with memory_session() as s:
            adapter = ExecutorAdapter(
                account=_stub_account("executor"),
                workspace_id=uuid.uuid4(),
                account_id=uuid.uuid4(),
                model_account_id=uuid.uuid4(),
                session=s,
                settings=get_settings(),
            )
            # No redis → no NotImplementedError; only the redis-missing
            # error path fires, exactly like a tools-less call would.
            with pytest.raises(ExecutorAdapterUnavailable, match="requires a Redis client"):
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

    async def test_chat_no_worker_waits_then_raises_capacity_timeout(self) -> None:
        """Lift E16 — no available worker means *wait*, not immediate raise.

        Pre-E16 the adapter raised ``no online worker`` instantly when
        :func:`find_available_worker` returned None — that made sense
        because pre-E16 "no worker" only meant "no row at all". Post-E16
        the same return value can also mean "every worker temporarily at
        capacity" (the worker's poll loop skips polling at-cap), so the
        adapter waits with bounded retry. After the bounded wait it
        raises a distinct "no worker capacity within" message so the
        caller sees the difference between misconfig and saturation.
        """
        redis = await _make_redis()
        workspace_id = uuid.uuid4()
        # Tiny wait budget so the test is fast — the real default is 30 min.
        settings = get_settings().model_copy(
            update={
                "executor_capacity_wait_max_s": 0.1,
                "executor_capacity_wait_poll_s": 0.02,
            }
        )
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
                settings=settings,
                redis=redis,
            )
            with pytest.raises(ExecutorAdapterUnavailable, match="no worker capacity"):
                await adapter.chat(system="x", messages=[{"role": "user", "content": "y"}])

    async def test_chat_waits_for_capacity_then_dispatches(self) -> None:
        """Lift E16 — adapter retries until capacity frees up, then dispatches.

        Mock :func:`find_available_worker` to return None twice, then a
        real worker. The adapter must NOT raise — it must keep
        re-checking on the configured poll interval until the worker
        becomes available, then dispatch + await as usual.
        """
        import asyncio

        redis = await _make_redis()
        workspace_id = uuid.uuid4()
        settings = get_settings().model_copy(
            update={
                "executor_task_timeout_s": 30.0,
                "executor_capacity_wait_max_s": 5.0,
                "executor_capacity_wait_poll_s": 0.02,
            }
        )

        async with shared_file_sessionmaker() as sf:
            async with sf() as setup:
                worker = await _seed_worker(
                    setup, workspace_id=workspace_id, capabilities=["claude_code"]
                )
                account = _executor_account(workspace_id, worker.id)
                setup.add(account)
                await setup.commit()

            # Monkey-patch the dispatch module's find_available_worker so
            # the first two calls return None (simulating "every worker at
            # capacity"), then the real worker is returned on the third.
            from backend.executors import dispatch as dispatch_mod

            real_find = dispatch_mod.find_available_worker
            call_count = {"n": 0}

            async def _flaky_find(*args: Any, **kwargs: Any) -> WorkerRow | None:
                call_count["n"] += 1
                if call_count["n"] <= 2:
                    return None
                return await real_find(*args, **kwargs)

            dispatch_mod.find_available_worker = _flaky_find  # type: ignore[assignment]
            try:
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

                    async def _simulate_worker() -> None:
                        stream = dispatch.worker_stream(worker.id)
                        last_id = "0"
                        for _ in _poll_deadline():
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
                    # Twice None + once real = at least 3 calls.
                    assert call_count["n"] >= 3
            finally:
                dispatch_mod.find_available_worker = real_find  # type: ignore[assignment]

    async def test_chat_passes_account_litellm_model_into_dispatch(self) -> None:
        """E21 — the adapter pulls ``account.litellm_model`` and forwards it as
        the dispatch ``model`` so it lands on the task row + the worker's
        stream entry. The legacy ``executor/<type>`` placeholder still maps
        to ``None`` for back-compat with pre-E21 accounts."""
        redis = await _make_redis()
        workspace_id = uuid.uuid4()

        async with shared_file_sessionmaker() as sf:
            async with sf() as setup:
                worker = await _seed_worker(
                    setup, workspace_id=workspace_id, capabilities=["opencode"]
                )
                account = ModelAccount(
                    id=uuid.uuid4(),
                    workspace_id=workspace_id,
                    account_id=uuid.uuid4(),
                    provider="executor",
                    label="mac-mini-qwen",
                    litellm_model="opencode-go/qwen3.6-plus",
                    api_base=None,
                    api_key_encrypted=None,
                    data_jurisdiction="unknown",
                    is_active=True,
                    extra_params={
                        "worker_id": str(worker.id),
                        "executor_type": "opencode",
                    },
                )
                setup.add(account)
                await setup.commit()

            async with sf() as adapter_session:
                adapter = ExecutorAdapter(
                    account=account,
                    workspace_id=workspace_id,
                    account_id=account.account_id,
                    model_account_id=account.id,
                    session=adapter_session,
                    settings=get_settings().model_copy(update={"executor_task_timeout_s": 5.0}),
                    redis=redis,
                )

                # Don't wait for a worker — short-circuit by timing out the
                # await_completion. The XADD we want to inspect happens
                # BEFORE await_completion blocks.
                with pytest.raises(ExecutorAdapterUnavailable):
                    await adapter.chat(
                        system="",
                        messages=[{"role": "user", "content": "hi"}],
                    )

                entries = await redis.xrange(dispatch.worker_stream(worker.id))
                # 2 entries — execute + cancel (post-timeout). Pick the first.
                execute_entry = next(
                    fields for _id, fields in entries if fields.get("action") == "execute"
                )
                assert execute_entry["model"] == "opencode-go/qwen3.6-plus"

    async def test_chat_legacy_executor_placeholder_omits_model(self) -> None:
        """E21 back-compat — accounts whose ``litellm_model`` is the legacy
        ``executor/<type>`` placeholder MUST NOT propagate that string as the
        underlying model; absence means "use CLI default"."""
        redis = await _make_redis()
        workspace_id = uuid.uuid4()

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
                    settings=get_settings().model_copy(update={"executor_task_timeout_s": 5.0}),
                    redis=redis,
                )

                with pytest.raises(ExecutorAdapterUnavailable):
                    await adapter.chat(
                        system="",
                        messages=[{"role": "user", "content": "hi"}],
                    )

                entries = await redis.xrange(dispatch.worker_stream(worker.id))
                execute_entry = next(
                    fields for _id, fields in entries if fields.get("action") == "execute"
                )
                # Either absent or empty — never the placeholder string.
                assert execute_entry.get("model", "") == ""

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
                    for _ in _poll_deadline():
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
                # No files shipped → no captured artifact_refs.
                assert response.artifact_refs == ()

    async def test_chat_localizes_the_system_prompt_like_litellm(self) -> None:
        """Abstraction parity — the executor adapter localizes the system prompt
        the SAME way the LiteLLM adapter does (shared helper). A ``ko`` workspace's
        executor-generated prose (verify demonstration, decision questions) then
        follows the workspace language, not English."""
        import asyncio

        from backend.identity.output_language import set_output_language

        redis = await _make_redis()
        workspace_id = uuid.uuid4()
        settings = get_settings().model_copy(update={"executor_task_timeout_s": 30.0})
        captured: dict[str, str] = {}

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

                async def _simulate_worker() -> None:
                    stream = dispatch.worker_stream(worker.id)
                    last_id = "0"
                    for _ in _poll_deadline():
                        entries = await redis.xread({stream: last_id}, count=1, block=20)
                        if not entries:
                            continue
                        _name, msgs = entries[0]
                        for msg_id, fields in msgs:
                            last_id = msg_id
                            # The dispatched task carries the (localized) system.
                            captured["system"] = fields.get("system", "")
                            async with sf() as ws_session:
                                await dispatch.record_result(
                                    ws_session,
                                    redis,
                                    task_id=uuid.UUID(fields["task_id"]),
                                    success=True,
                                    output="ok",
                                    error_message=None,
                                )
                                await ws_session.commit()
                            return
                    raise AssertionError("worker stream never saw the XADD")

                try:
                    set_output_language("ko")
                    worker_task = asyncio.create_task(_simulate_worker())
                    try:
                        await adapter.chat(
                            system="be terse", messages=[{"role": "user", "content": "hi"}]
                        )
                    finally:
                        await worker_task
                finally:
                    set_output_language("en")

        # The dispatched system prompt carries the Korean output-language directive.
        assert "Korean" in captured["system"]
        assert "be terse" in captured["system"]
        # v2 — a chat-shaped (tools=None) call also carries the completion
        # directive so the coding agent answers as a raw LLM (clean output),
        # not agentically — ExecutorAdapter.chat matches LiteLLMAdapter.chat.
        assert "TEXT-COMPLETION endpoint" in captured["system"]

    def _retry_adapter(self) -> ExecutorAdapter:
        """A minimal ExecutorAdapter that passes chat()'s redis + executor_type
        guards — the dispatch itself is stubbed per test via ``_chat_with_session``."""
        account = _stub_account(provider="executor", extra_params={"executor_type": "claude_code"})
        return ExecutorAdapter(
            account=account,
            workspace_id=account.workspace_id,
            account_id=account.account_id,
            model_account_id=account.id,
            session=None,  # unused — _chat_with_session is stubbed
            settings=get_settings(),
            redis=object(),  # non-None so the redis guard passes
        )

    async def test_chat_retries_transient_task_failure_then_succeeds(
        self, monkeypatch: Any
    ) -> None:
        """A transient ``failed`` (worker CLI ``exit 1``, retryable) is
        re-dispatched — the next attempt succeeds and ``chat`` returns its
        output. The J4 fix: a single executor blip no longer kills the run."""
        monkeypatch.setattr("backend.dispatch.adapter._EXECUTOR_CHAT_RETRY_BACKOFF_S", 0.0)
        adapter = self._retry_adapter()
        calls: list[int] = []

        async def _fake(_self: Any, **_kw: Any) -> ChatResponse:
            calls.append(1)
            if len(calls) == 1:
                raise ExecutorAdapterUnavailable("task failed: exit 1", retryable=True)
            return ChatResponse(content="42", tool_calls=(), artifact_refs=())

        monkeypatch.setattr(ExecutorAdapter, "_chat_with_session", _fake)
        response = await adapter.chat(system="x", messages=[{"role": "user", "content": "hi"}])
        assert response.content == "42"
        assert len(calls) == 2  # one failed dispatch + one successful retry

    async def test_chat_persistent_failure_raises_after_bounded_retries(
        self, monkeypatch: Any
    ) -> None:
        """A retryable outcome that never clears exhausts the bounded attempts
        and raises — clean termination, no infinite re-dispatch."""
        monkeypatch.setattr("backend.dispatch.adapter._EXECUTOR_CHAT_RETRY_BACKOFF_S", 0.0)
        adapter = self._retry_adapter()
        calls: list[int] = []

        async def _always_fail(_self: Any, **_kw: Any) -> ChatResponse:
            calls.append(1)
            raise ExecutorAdapterUnavailable("task failed: exit 1", retryable=True)

        monkeypatch.setattr(ExecutorAdapter, "_chat_with_session", _always_fail)
        with pytest.raises(ExecutorAdapterUnavailable, match="failed"):
            await adapter.chat(system="x", messages=[{"role": "user", "content": "hi"}])
        assert len(calls) == 3  # _EXECUTOR_CHAT_ATTEMPTS — bounded, no infinite loop

    async def test_chat_non_retryable_failure_raises_immediately(self, monkeypatch: Any) -> None:
        """A NON-retryable failure (e.g. timeout / config) is raised on the first
        attempt — the retry loop only re-dispatches transient task failures."""
        adapter = self._retry_adapter()
        calls: list[int] = []

        async def _fail_hard(_self: Any, **_kw: Any) -> ChatResponse:
            calls.append(1)
            raise ExecutorAdapterUnavailable("timed out", retryable=False)

        monkeypatch.setattr(ExecutorAdapter, "_chat_with_session", _fail_hard)
        with pytest.raises(ExecutorAdapterUnavailable, match="timed out"):
            await adapter.chat(system="x", messages=[{"role": "user", "content": "hi"}])
        assert len(calls) == 1  # no retry on a non-retryable outcome

    async def test_chat_surfaces_worker_captured_artifact_refs(self, tmp_path: Any) -> None:
        """A run-bound executor chat surfaces the files the worker captured
        (persisted on the task row as artifact_refs) on the ChatResponse, so the
        agent loop can record them as the verified deliverable's artifact_refs.
        Without this the deliverable came out with empty artifact_refs for every
        executor run even though the agent changed files in its clone.
        """
        import asyncio
        import base64

        redis = await _make_redis()
        workspace_id = uuid.uuid4()
        run_id = uuid.uuid4()
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
                    run_id=run_id,
                )

                async def _simulate_worker() -> None:
                    stream = dispatch.worker_stream(worker.id)
                    last_id = "0"
                    for _ in _poll_deadline():
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
                                    output="done",
                                    error_message=None,
                                    files=[
                                        {
                                            "path": "backend/common/bytesize.py",
                                            "content_b64": base64.b64encode(b"x = 1\n").decode(),
                                            "truncated": False,
                                        }
                                    ],
                                    run_workspace_root=str(tmp_path),
                                )
                                await ws_session.commit()
                            return
                    raise AssertionError("worker stream never saw the XADD")

                worker_task = asyncio.create_task(_simulate_worker())
                try:
                    response = await adapter.chat(
                        system="be terse",
                        messages=[{"role": "user", "content": "add bytesize util"}],
                    )
                finally:
                    await worker_task

                assert response.artifact_refs == ("backend/common/bytesize.py",)

    async def test_chat_uses_per_caller_timeout_over_settings_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Lift E9 — when constructed with ``timeout_s`` the executor adapter
        passes that value to ``dispatch.await_completion`` instead of the
        settings default. Spies on the captured timeout_s kwarg."""
        import asyncio

        redis = await _make_redis()
        workspace_id = uuid.uuid4()
        # Settings default 1800; the per-caller override is 180.
        settings = get_settings().model_copy(update={"executor_task_timeout_s": 1800.0})

        captured: dict[str, float] = {}
        real_await = dispatch.await_completion

        async def _spy_await(*args: Any, **kwargs: Any) -> Any:
            captured["timeout_s"] = kwargs["timeout_s"]
            return await real_await(*args, **kwargs)

        monkeypatch.setattr(dispatch, "await_completion", _spy_await)

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
                    timeout_s=180.0,
                )

                async def _simulate_worker() -> None:
                    stream = dispatch.worker_stream(worker.id)
                    last_id = "0"
                    for _ in _poll_deadline():
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
                                    output="ok",
                                    error_message=None,
                                )
                                await ws_session.commit()
                            return
                    raise AssertionError("worker stream never saw the XADD")

                worker_task = asyncio.create_task(_simulate_worker())
                try:
                    await adapter.chat(
                        system="x",
                        messages=[{"role": "user", "content": "y"}],
                    )
                finally:
                    await worker_task

                assert captured["timeout_s"] == 180.0, (
                    "ExecutorAdapter.chat ignored the per-caller timeout — "
                    "still passing the 1800 s settings default."
                )

    async def test_chat_falls_back_to_settings_when_timeout_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``timeout_s=None`` uses ``settings.executor_task_timeout_s`` —
        ``workflow.agent_loop.act`` keeps the legacy 1800 s default."""
        import asyncio

        redis = await _make_redis()
        workspace_id = uuid.uuid4()
        settings = get_settings().model_copy(update={"executor_task_timeout_s": 1800.0})

        captured: dict[str, float] = {}
        real_await = dispatch.await_completion

        async def _spy_await(*args: Any, **kwargs: Any) -> Any:
            captured["timeout_s"] = kwargs["timeout_s"]
            return await real_await(*args, **kwargs)

        monkeypatch.setattr(dispatch, "await_completion", _spy_await)

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
                    # No timeout_s — falls back to settings.
                )

                async def _simulate_worker() -> None:
                    stream = dispatch.worker_stream(worker.id)
                    last_id = "0"
                    for _ in _poll_deadline():
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
                                    output="ok",
                                    error_message=None,
                                )
                                await ws_session.commit()
                            return
                    raise AssertionError("worker stream never saw the XADD")

                worker_task = asyncio.create_task(_simulate_worker())
                try:
                    await adapter.chat(
                        system="x",
                        messages=[{"role": "user", "content": "y"}],
                    )
                finally:
                    await worker_task

                assert captured["timeout_s"] == 1800.0

    async def test_chat_cancels_worker_task_on_timeout(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Lift E14 — when ``await_completion`` raises :class:`TaskTimeout`,
        the adapter MUST signal the worker so it stops running the now-
        abandoned subprocess. Verifies the cancel XADD is issued and the
        exception still propagates as :class:`ExecutorAdapterUnavailable`."""
        redis = await _make_redis()
        workspace_id = uuid.uuid4()
        settings = get_settings().model_copy(update={"executor_task_timeout_s": 0.1})

        async def _timeout(*_a: Any, **_kw: Any) -> Any:
            raise dispatch.TaskTimeout("test forced timeout")

        monkeypatch.setattr(dispatch, "await_completion", _timeout)

        cancel_calls: list[dict[str, Any]] = []
        real_cancel = dispatch.cancel_task

        async def _spy_cancel(*args: Any, **kwargs: Any) -> Any:
            cancel_calls.append(dict(kwargs))
            return await real_cancel(*args, **kwargs)

        monkeypatch.setattr(dispatch, "cancel_task", _spy_cancel)

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
                    timeout_s=0.1,
                )
                with pytest.raises(ExecutorAdapterUnavailable, match="timed out"):
                    await adapter.chat(
                        system="x",
                        messages=[{"role": "user", "content": "y"}],
                    )

        assert len(cancel_calls) == 1, (
            "ExecutorAdapter must call cancel_task() exactly once on TaskTimeout — "
            "otherwise the worker keeps running its abandoned subprocess."
        )
        assert cancel_calls[0]["worker_id"] == worker.id
        assert "task_id" in cancel_calls[0]

    async def test_chat_worker_failure_raises(self, monkeypatch: Any) -> None:
        """Worker reports ``success=False`` on every re-dispatch → after the
        bounded transient retries, the adapter raises with the worker's error."""
        import asyncio

        from backend.dispatch import adapter as _adapter_mod

        # Pin a single attempt: this test exercises the REAL dispatch →
        # record_result → raise path; the transient-retry behaviour (3
        # round-trips) is covered by the fast mocked tests above, and driving 3
        # real worker round-trips through fakeredis+sqlite is timing-fragile.
        monkeypatch.setattr(_adapter_mod, "_EXECUTOR_CHAT_ATTEMPTS", 1)
        monkeypatch.setattr(_adapter_mod, "_EXECUTOR_CHAT_RETRY_BACKOFF_S", 0.0)
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
                    # Fail EVERY re-dispatch (the transient-failure retry loop
                    # creates a fresh task per attempt) until the attempts are
                    # exhausted, so the adapter raises the worker's error rather
                    # than timing out waiting for a result that never comes.
                    stream = dispatch.worker_stream(worker.id)
                    last_id = "0"
                    handled = 0
                    for _ in _poll_deadline():
                        entries = await redis.xread({stream: last_id}, count=1, block=20)
                        if not entries:
                            continue
                        _name, messages = entries[0]
                        for msg_id, fields in messages:
                            last_id = msg_id
                            task_id = uuid.UUID(fields["task_id"])
                            handled += 1
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
                            if handled >= _adapter_mod._EXECUTOR_CHAT_ATTEMPTS:
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


class TestExecutorAdapterE30ToolsAndContract:
    """Lift E30 — when ``tools`` are passed the adapter formats them into
    a verification-contract guide in the system prompt instead of raising;
    when the agent's output contains a ``<verification-contract>{…}</…>``
    block, the adapter synthesizes a ``declare_verification`` tool call so
    the downstream loop registers the contract through the existing path.
    """

    def test_augment_system_appends_contract_guide_when_tools_set(self) -> None:
        from backend.dispatch.adapter import _augment_system_for_executor_tools

        out = _augment_system_for_executor_tools(
            "You are a worker.",
            [
                {
                    "type": "function",
                    "function": {
                        "name": "declare_verification",
                        "description": "Declare how BSVibe verifies your work.",
                    },
                },
                {
                    "type": "function",
                    "function": {
                        "name": "write_file",
                        "description": "Write a file under the run workspace.",
                    },
                },
            ],
        )
        # Original system prompt survives untouched at the head.
        assert out.startswith("You are a worker.")
        # The E30 guide is appended (now E34 strengthens its DO-IT framing),
        # including the contract template AND the named tools the loop
        # registered.
        assert "BSVibe coding-agent contract — MANDATORY (Lift E30 / E34 / E37)" in out
        assert "<verification-contract>" in out
        assert "declare_verification" in out
        assert "write_file" in out

    def test_augment_system_no_tools_returns_original(self) -> None:
        from backend.dispatch.adapter import _augment_system_for_executor_tools

        assert _augment_system_for_executor_tools("be terse", None) == "be terse"
        assert _augment_system_for_executor_tools("be terse", []) == "be terse"

    def test_augment_system_for_chat_adds_completion_directive(self) -> None:
        """A chat-shaped (tools=None) executor call gets a completion directive so
        the coding agent answers as a raw LLM (clean, parseable output) instead of
        agentically — keeping ExecutorAdapter.chat identical to LiteLLMAdapter.chat.
        The original system leads; the directive is appended."""
        from backend.dispatch.adapter import _augment_system_for_executor_chat

        out = _augment_system_for_executor_chat("Output ONLY a JSON object with a title.")
        assert out.startswith("Output ONLY a JSON object with a title.")
        assert "TEXT-COMPLETION endpoint" in out
        assert "no tool use" in out
        assert "single JSON object" in out

    def test_guide_documents_optional_knowledge_declaration(self) -> None:
        """v2 — the guide teaches the agent to record what it LEARNED (a
        retrospective ``knowledge`` block in the contract) with the shared bar,
        so verified-work knowledge is agent-authored in the moment."""
        from backend.dispatch.adapter import _augment_system_for_executor_tools
        from backend.knowledge.extraction.worth_remembering import WORTH_REMEMBERING_PRINCIPLE

        out = _augment_system_for_executor_tools(
            "You are a worker.",
            [
                {
                    "type": "function",
                    "function": {"name": "declare_verification", "description": "d"},
                }
            ],
        )
        assert '"knowledge"' in out
        assert "topic" in out and "insight" in out
        # The shared worth-remembering bar is embedded verbatim.
        assert WORTH_REMEMBERING_PRINCIPLE in out

    def test_synthesize_forwards_declared_knowledge(self) -> None:
        """v2 — a contract carrying a ``knowledge`` block forwards it verbatim on
        the synthesized declare_verification args (the handler latches it)."""
        import json as _json

        from backend.dispatch.adapter import _synthesize_executor_tool_calls

        out = (
            "done.\n<verification-contract>\n"
            '{"checks": [{"kind": "command", "command": "pytest"}], '
            '"knowledge": {"topic": "Idempotent webhooks", "insight": "Dedupe by event id."}}\n'
            "</verification-contract>"
        )
        calls = _synthesize_executor_tool_calls(out)
        assert len(calls) == 1
        args = _json.loads(calls[0].arguments_json)
        assert args["knowledge"] == {
            "topic": "Idempotent webhooks",
            "insight": "Dedupe by event id.",
        }
        assert args["checks"][0]["command"] == "pytest"

    def test_e37_contract_requirement_is_loud_and_first_in_guide(self) -> None:
        """Lift E37 — qwen3.6-plus dogfood (session ses_12f1a577d, 2026-06-16)
        proved the contract requirement buried at step 4 of the guide gets
        IGNORED by the agent: it edits, commits, ends with a summary text,
        and emits NO ``<verification-contract>`` block. The fix is structural,
        not a louder nudge — the requirement must be the FIRST thing the
        agent sees in the guide section, with explicit failure semantics
        ("MANDATORY", "no block → human review → no PR") so a model that
        skims the prompt cannot miss it.
        """
        from backend.dispatch.adapter import _augment_system_for_executor_tools

        out = _augment_system_for_executor_tools(
            "You are a worker.",
            [{"type": "function", "function": {"name": "declare_verification"}}],
        )
        # The original system prompt still survives at the head so chat-shape
        # preservation isn't disturbed.
        assert out.startswith("You are a worker.")
        # The guide block now leads with a MANDATORY / REQUIRED marker on
        # the contract — well BEFORE any reference to tool flow / "Concretely:".
        guide_start = out.index("BSVibe coding-agent contract")
        guide = out[guide_start:]
        loud_idx = next(
            (guide.find(marker) for marker in ("MANDATORY", "REQUIRED") if marker in guide),
            -1,
        )
        assert loud_idx >= 0, "guide must contain MANDATORY/REQUIRED marker"
        # The <verification-contract> template appears before the work flow
        # description so the agent reads the gate first.
        contract_template_idx = guide.find("<verification-contract>")
        concretely_idx = guide.find("Concretely:")
        assert contract_template_idx >= 0
        assert contract_template_idx < concretely_idx or concretely_idx < 0, (
            "contract template must appear before the work-flow steps"
        )
        # E37 must also lift the lift-tag so future audits know which
        # restructuring is in play.
        assert "Lift E30 / E34 / E37" in guide

    def test_e38_contract_template_uses_canonical_kind_and_command_fields(self) -> None:
        """Lift E38 — the E37 dogfood proved the agent OBEYS the prompt
        template verbatim; the template's `kind: "shell"` + `cmd:` choice
        produced contracts the parser rejected (it accepts `kind: "command"`
        + `command:`). Align the template with the parser's canonical
        shape so the wire is end-to-end consistent on the first try.
        """
        from backend.dispatch.adapter import _augment_system_for_executor_tools

        out = _augment_system_for_executor_tools(
            "You are a worker.",
            [{"type": "function", "function": {"name": "declare_verification"}}],
        )
        guide_start = out.index("BSVibe coding-agent contract")
        guide = out[guide_start:]
        # The first contract template the agent reads must use
        # ``kind: "command"`` + ``command:`` so the parser accepts the
        # naïve copy-paste shape.
        first_template_idx = guide.index("<verification-contract>")
        # Look at the FIRST contract template only — later examples may
        # mention alternative aliases for tolerance, but the primary
        # example must be the canonical shape.
        first_template = guide[first_template_idx : first_template_idx + 400]
        assert '"kind": "command"' in first_template, (
            f"first template must show canonical kind=command, got: {first_template!r}"
        )
        assert '"command":' in first_template, (
            f"first template must show canonical command field, got: {first_template!r}"
        )
        # The wrong-shape pair the E37 dogfood reproduced must NOT appear
        # in the first template — it's the source of the rejection loop.
        assert '"cmd"' not in first_template, (
            "first template must not use the cmd alias which the parser originally rejected"
        )

    def test_synthesize_tool_call_from_contract_block(self) -> None:
        from backend.dispatch.adapter import _synthesize_executor_tool_calls

        output = (
            "I edited tests/api/test_inside_trust.py to inject a fixture clock.\n"
            "\n"
            "<verification-contract>\n"
            '{"checks": [{"kind": "shell", "cmd": "pytest tests/api/test_inside_trust.py::test_product_trust_detail_shape -q"}]}\n'
            "</verification-contract>\n"
        )
        calls = _synthesize_executor_tool_calls(output)
        assert len(calls) == 1
        call = calls[0]
        assert call.name == "declare_verification"
        payload = json.loads(call.arguments_json)
        assert payload["checks"][0]["kind"] == "shell"
        assert "pytest" in payload["checks"][0]["cmd"]

    def test_synthesize_returns_empty_when_no_block(self) -> None:
        from backend.dispatch.adapter import _synthesize_executor_tool_calls

        assert _synthesize_executor_tool_calls("just text, no contract") == ()
        assert _synthesize_executor_tool_calls("") == ()

    def test_synthesize_returns_empty_on_malformed_json(self) -> None:
        from backend.dispatch.adapter import _synthesize_executor_tool_calls

        bad = "<verification-contract>{not valid json}</verification-contract>"
        assert _synthesize_executor_tool_calls(bad) == ()

    @pytest.mark.asyncio
    async def test_chat_with_tools_dispatches_with_augmented_system(
        self,
    ) -> None:
        """End-to-end — passing ``tools`` augments the system prompt that
        lands on the worker's stream entry, and a contract block in the
        worker's reply synthesizes the ``declare_verification`` tool call."""
        redis = await _make_redis()
        workspace_id = uuid.uuid4()

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
                    settings=get_settings().model_copy(update={"executor_task_timeout_s": 30.0}),
                    redis=redis,
                )

                async def _simulate_worker() -> None:
                    stream = dispatch.worker_stream(worker.id)
                    last_id = "0"
                    captured_system: list[str] = []
                    for _ in _poll_deadline():
                        entries = await redis.xread({stream: last_id}, count=1, block=20)
                        if not entries:
                            continue
                        _name, messages = entries[0]
                        for msg_id, fields in messages:
                            last_id = msg_id
                            if fields.get("action") != "execute":
                                continue
                            captured_system.append(fields.get("system", ""))
                            task_id = uuid.UUID(fields["task_id"])
                            async with sf() as ws_session:
                                await dispatch.record_result(
                                    ws_session,
                                    redis,
                                    task_id=task_id,
                                    success=True,
                                    output=(
                                        "Edited the file. Verification follows.\n"
                                        "<verification-contract>\n"
                                        '{"checks": [{"kind": "shell", "cmd": "pytest -q tests/foo.py"}]}\n'
                                        "</verification-contract>\n"
                                    ),
                                    error_message=None,
                                )
                                await ws_session.commit()
                            # Stash for the outer assertion.
                            self._captured_system = captured_system[0]  # type: ignore[attr-defined]
                            return
                    raise AssertionError("worker stream never saw the XADD")

                import asyncio as _asyncio

                worker_task = _asyncio.create_task(_simulate_worker())
                try:
                    response = await adapter.chat(
                        system="ORIGINAL SYSTEM",
                        messages=[{"role": "user", "content": "fix the flake"}],
                        tools=[
                            {
                                "type": "function",
                                "function": {
                                    "name": "declare_verification",
                                    "description": "Declare contract.",
                                },
                            }
                        ],
                    )
                finally:
                    await worker_task

                # The system prompt the worker received was the augmented one.
                augmented = getattr(self, "_captured_system", "")
                assert augmented.startswith("ORIGINAL SYSTEM")
                assert "verification-contract" in augmented

                # The contract block in the worker's reply became a synthesized
                # ``declare_verification`` tool call the downstream loop will
                # register through its existing path.
                assert len(response.tool_calls) == 1
                assert response.tool_calls[0].name == "declare_verification"

    @pytest.mark.asyncio
    async def test_chat_with_run_id_persists_files_under_run(self) -> None:
        """Lift E31 — when the resolver wired ``run_id`` (agent_loop callers)
        the dispatched task carries it through to ``ExecutorTaskRow.run_id``
        so files captured by the worker (B1) get persisted as the run's
        ``artifact_refs`` via ``record_result``.

        Pre-E31 the chat path hard-coded ``run_id=None`` so the worker's
        file capture was silently dropped — chat tasks were detached from
        any run. With E31 the agent_loop's ``act`` caller threads its run
        id end-to-end so the coding agent's edits land in the vault.
        """
        redis = await _make_redis()
        workspace_id = uuid.uuid4()
        run_id = uuid.uuid4()

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
                    settings=get_settings().model_copy(update={"executor_task_timeout_s": 30.0}),
                    redis=redis,
                    run_id=run_id,
                )

                from backend.executors.db import ExecutorTaskRow

                captured_run_ids: list[uuid.UUID | None] = []

                async def _simulate_worker() -> None:
                    stream = dispatch.worker_stream(worker.id)
                    last_id = "0"
                    for _ in _poll_deadline():
                        entries = await redis.xread({stream: last_id}, count=1, block=20)
                        if not entries:
                            continue
                        _name, messages = entries[0]
                        for msg_id, fields in messages:
                            last_id = msg_id
                            if fields.get("action") != "execute":
                                continue
                            task_id = uuid.UUID(fields["task_id"])
                            # Read the task row to confirm the run_id was
                            # threaded through ``dispatch.create_task``.
                            async with sf() as ws_session:
                                row = await ws_session.get(ExecutorTaskRow, task_id)
                                captured_run_ids.append(row.run_id if row else None)
                                await dispatch.record_result(
                                    ws_session,
                                    redis,
                                    task_id=task_id,
                                    success=True,
                                    output="ok",
                                    error_message=None,
                                )
                                await ws_session.commit()
                            return
                    raise AssertionError("worker stream never saw the XADD")

                import asyncio as _asyncio

                worker_task = _asyncio.create_task(_simulate_worker())
                try:
                    await adapter.chat(
                        system="x",
                        messages=[{"role": "user", "content": "fix"}],
                    )
                finally:
                    await worker_task

                assert captured_run_ids == [run_id], (
                    "E31 — ExecutorAdapter.run_id MUST land on ExecutorTaskRow.run_id"
                )


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
