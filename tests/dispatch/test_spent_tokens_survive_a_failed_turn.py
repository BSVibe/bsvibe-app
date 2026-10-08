"""#928 — the tokens an executor turn burned are counted even when the turn fails.

The run's meter accrued an executor turn's usage only when ``await_completion``
handed back a ``done`` task. Every other ending dropped what the worker had
already spent:

* a task that came back ``failed`` (the worker's terminal chunk carries usage on
  failure too) raised ``ExecutorAdapterUnavailable`` — and the retry loop then
  re-dispatched, so up to two failed attempts' tokens vanished before a success;
* a turn the run was cancelled during (#1106) returned ``None`` from the turn;
* a report that arrived after the awaiter had timed the task out was refused by
  ``record_result``'s terminal-row guard, usage and all.

Measured nothing is not zero (the issue's own warning), so these pin that a
number which DID arrive is never thrown away.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

import backend.executors.db  # noqa: F401
from backend.config import get_settings
from backend.dispatch.adapter import (
    ChatResponse,
    ExecutorAdapter,
    ExecutorAdapterUnavailable,
    RunCancelledDuringTurn,
)
from backend.executors import dispatch
from backend.executors.db import ExecutorTaskRow

from .._support import memory_session, shared_file_sessionmaker
from .test_adapter import (
    _executor_account,
    _make_redis,
    _seed_worker,
    _stub_account,
    _stub_await_completion,
    _StubCompletedTask,
)

pytestmark = pytest.mark.asyncio


def _adapter() -> ExecutorAdapter:
    account = _stub_account(provider="executor", extra_params={"executor_type": "claude_code"})
    return ExecutorAdapter(
        account=account,
        workspace_id=account.workspace_id,
        account_id=account.account_id,
        model_account_id=account.id,
        session=None,
        settings=get_settings(),
        redis=object(),
    )


async def test_a_failed_task_raises_with_the_usage_it_reported(monkeypatch) -> None:
    """The real done/failed branch: the failed row's usage rides the exception."""
    from backend.dispatch import adapter as adapter_mod

    monkeypatch.setattr(adapter_mod, "_EXECUTOR_CHAT_RETRY_BACKOFF_S", 0.0)
    monkeypatch.setattr(adapter_mod, "_EXECUTOR_CHAT_ATTEMPTS", 1)
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
        monkeypatch.setattr(
            dispatch,
            "await_completion",
            _stub_await_completion(
                _StubCompletedTask(
                    status="failed",
                    error_message="exit 1",
                    usage_prompt_tokens=7000,
                    usage_completion_tokens=300,
                )
            ),
        )
        async with sf() as session:
            adapter = ExecutorAdapter(
                account=account,
                workspace_id=workspace_id,
                account_id=account.account_id,
                model_account_id=account.id,
                session=session,
                settings=get_settings(),
                redis=redis,
            )
            with pytest.raises(ExecutorAdapterUnavailable) as caught:
                await adapter.chat(system="s", messages=[{"role": "user", "content": "x"}])
    assert (caught.value.usage_prompt_tokens, caught.value.usage_completion_tokens) == (7000, 300)


async def test_a_retried_failure_adds_its_tokens_to_the_success(monkeypatch) -> None:
    monkeypatch.setattr("backend.dispatch.adapter._EXECUTOR_CHAT_RETRY_BACKOFF_S", 0.0)
    calls: list[int] = []

    async def _fake(_self: Any, **_kw: Any) -> ChatResponse:
        calls.append(1)
        if len(calls) == 1:
            raise ExecutorAdapterUnavailable(
                "failed", retryable=True, usage_prompt_tokens=1000, usage_completion_tokens=50
            )
        return ChatResponse(content="ok", usage_prompt_tokens=200, usage_completion_tokens=10)

    monkeypatch.setattr(ExecutorAdapter, "_chat_with_session", _fake)
    response = await _adapter().chat(system="x", messages=[{"role": "user", "content": "hi"}])
    assert response.content == "ok"
    assert (response.usage_prompt_tokens, response.usage_completion_tokens) == (1200, 60)


async def test_an_exhausted_retry_raises_with_every_attempts_tokens(monkeypatch) -> None:
    monkeypatch.setattr("backend.dispatch.adapter._EXECUTOR_CHAT_RETRY_BACKOFF_S", 0.0)

    async def _always(_self: Any, **_kw: Any) -> ChatResponse:
        raise ExecutorAdapterUnavailable(
            "failed", retryable=True, usage_prompt_tokens=100, usage_completion_tokens=1
        )

    monkeypatch.setattr(ExecutorAdapter, "_chat_with_session", _always)
    with pytest.raises(ExecutorAdapterUnavailable) as caught:
        await _adapter().chat(system="x", messages=[{"role": "user", "content": "hi"}])
    assert (caught.value.usage_prompt_tokens, caught.value.usage_completion_tokens) == (300, 3)


# ── The run's meter takes what a failed turn spent ───────────────────────────


class _Orch:
    def __init__(self, session: Any, error: Exception) -> None:
        self._session = session
        self._error = error

        class _Llm:
            async def complete(_self, **_: Any) -> Any:
                raise error

        self._llm = _Llm()

    async def _run_cancelled(self, _run: Any) -> bool:
        return False


async def _run_row(s: Any) -> Any:
    from backend.workflow.infrastructure.db import ExecutionRun

    run = ExecutionRun(workspace_id=uuid.uuid4(), payload={})
    s.add(run)
    await s.commit()
    return run


async def test_a_failed_turn_accrues_its_tokens_onto_the_run_then_raises() -> None:
    import backend.workflow.infrastructure.db  # noqa: F401
    from backend.workflow.application._loop_turn import take_turn

    async with memory_session() as s:
        run = await _run_row(s)
        error = ExecutorAdapterUnavailable(
            "boom", usage_prompt_tokens=4000, usage_completion_tokens=90
        )
        with pytest.raises(ExecutorAdapterUnavailable):
            await take_turn(_Orch(s, error), run, [], None)  # type: ignore[arg-type]
        assert (run.usage_prompt_tokens, run.usage_completion_tokens) == (4000, 90)


async def test_a_turn_cancelled_mid_flight_still_accrues_what_it_spent() -> None:
    import backend.workflow.infrastructure.db  # noqa: F401
    from backend.workflow.application._loop_turn import take_turn

    async with memory_session() as s:
        run = await _run_row(s)
        error = RunCancelledDuringTurn(
            "cancelled", usage_prompt_tokens=10, usage_completion_tokens=2
        )
        assert await take_turn(_Orch(s, error), run, [], None) is None  # type: ignore[arg-type]
        assert (run.usage_prompt_tokens, run.usage_completion_tokens) == (10, 2)


# ── A report that lands after the timeout keeps its usage ────────────────────


async def test_a_late_report_on_a_timed_out_task_records_usage_only() -> None:
    """The row stays ``failed`` with the timeout's message — #926's overwrite guard and
    H1's foreign-worker guard both stand. Only the accounting takes the late truth."""
    redis = await _make_redis()
    workspace_id, worker_id = uuid.uuid4(), uuid.uuid4()
    async with memory_session() as s:
        task = await dispatch.create_task(
            s, workspace_id=workspace_id, executor_type="claude_code", prompt="p"
        )
        await s.flush()
        await dispatch.dispatch_task(redis, session=s, task=task, worker_id=worker_id)
        task.status = "failed"
        task.error_message = "timed out"
        await s.commit()

        result = await dispatch.record_result(
            s,
            redis,
            task_id=task.id,
            worker_id=worker_id,
            success=True,
            output="late output",
            error_message=None,
            usage_prompt_tokens=5000,
            usage_completion_tokens=40,
            usage_cache_read_tokens=4000,
        )
        await s.commit()
        assert result is None  # still the refusal shape to the caller

        row = await s.get(ExecutorTaskRow, task.id)
        assert row is not None
        assert (row.status, row.output, row.error_message) == ("failed", "", "timed out")
        assert (row.usage_prompt_tokens, row.usage_completion_tokens) == (5000, 40)
        assert row.usage_cache_read_tokens == 4000


async def test_a_late_report_from_a_foreign_worker_records_nothing() -> None:
    redis = await _make_redis()
    workspace_id, worker_id = uuid.uuid4(), uuid.uuid4()
    async with memory_session() as s:
        task = await dispatch.create_task(
            s, workspace_id=workspace_id, executor_type="claude_code", prompt="p"
        )
        await s.flush()
        await dispatch.dispatch_task(redis, session=s, task=task, worker_id=worker_id)
        task.status = "failed"
        await s.commit()

        await dispatch.record_result(
            s,
            redis,
            task_id=task.id,
            worker_id=uuid.uuid4(),
            success=True,
            output="x",
            error_message=None,
            usage_prompt_tokens=5000,
        )
        row = await s.get(ExecutorTaskRow, task.id)
        assert row is not None
        assert row.usage_prompt_tokens == 0


async def test_a_second_report_on_a_closed_task_does_not_overwrite_its_usage() -> None:
    """A done task already holds its turn's usage — a replay must not replace it."""
    redis = await _make_redis()
    workspace_id, worker_id = uuid.uuid4(), uuid.uuid4()
    async with memory_session() as s:
        task = await dispatch.create_task(
            s, workspace_id=workspace_id, executor_type="claude_code", prompt="p"
        )
        await s.flush()
        await dispatch.dispatch_task(redis, session=s, task=task, worker_id=worker_id)
        await s.commit()
        common = dict(task_id=task.id, worker_id=worker_id, success=True, error_message=None)
        await dispatch.record_result(s, redis, output="a", usage_prompt_tokens=10, **common)
        await dispatch.record_result(s, redis, output="b", usage_prompt_tokens=999, **common)
        row = await s.get(ExecutorTaskRow, task.id)
        assert row is not None
        assert (row.output, row.usage_prompt_tokens) == ("a", 10)
