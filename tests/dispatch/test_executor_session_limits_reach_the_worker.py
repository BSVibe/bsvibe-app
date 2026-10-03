"""The run's remaining token budget and the turn limit travel with an agentic task (#1104, #1114).

The worker can only stop a Claude Code session mid-flight if it is told where to stop. The
run's ceiling (``agent_max_run_tokens``) lived only in the backend and was checked only after a
turn came back — so a session that crossed it ran to completion first (run ``92b76fba``:
3,055,575 input tokens on a 2M ceiling).

Each hop is asserted where the production path crosses it: the adapter computes the numbers,
the dispatch payload carries them (dispatch and the #965 re-delivery alike), and the worker
hands them to the executor. A capability nothing passes is a capability that does not exist.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest

from backend.config import get_settings
from backend.dispatch.adapter import ExecutorAdapter
from backend.executors import dispatch
from backend.executors.worker.executors import ExecutionChunk
from backend.workflow.infrastructure.db import ExecutionRun, RunStatus

from .._support import shared_file_sessionmaker
from .test_adapter import _executor_account, _make_redis, _seed_worker

_TOOLS = [{"type": "function", "function": {"name": "bsvibe_work_file_read"}}]


class _CapturingRedis:
    def __init__(self) -> None:
        self.adds: list[dict[str, Any]] = []

    async def xadd(self, _name: str, fields: dict[str, Any], **_kw: Any) -> str:
        self.adds.append(dict(fields))
        return "1-0"


async def _adapter_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    *,
    settings: Any,
    tools: list[dict[str, Any]] | None,
    used: tuple[int, int] = (0, 0),
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Drive one adapter turn to the dispatch and return what ``dispatch_task`` was given."""
    redis = await _make_redis()
    workspace_id = uuid.uuid4()
    seen: dict[str, Any] = {}

    async with shared_file_sessionmaker() as sf:
        async with sf() as setup:
            worker = await _seed_worker(
                setup, workspace_id=workspace_id, capabilities=["claude_code"]
            )
            account = _executor_account(workspace_id, worker.id)
            setup.add(account)
            run = ExecutionRun(
                id=uuid.uuid4(),
                workspace_id=workspace_id,
                status=RunStatus.RUNNING,
                usage_prompt_tokens=used[0],
                usage_completion_tokens=used[1],
                payload=payload or {},
            )
            setup.add(run)
            await setup.commit()

        real_dispatch = dispatch.dispatch_task

        async def _spy_dispatch(*a: Any, **kw: Any) -> str:
            seen.update(kw)
            return await real_dispatch(*a, **kw)

        async def _spy_redispatch(*_a: Any, **kw: Any) -> str:
            seen["redelivered"] = kw
            return "1-1"

        async def _stop(*_a: Any, redeliver: Any = None, **_kw: Any) -> Any:
            # The #965 re-delivery is a closure over what was dispatched; drive it once.
            await redeliver(10.0)
            raise dispatch.TaskTimeout("stop here — the dispatch is captured")

        async def _noop_cancel(*_a: Any, **_kw: Any) -> None:
            return None

        async def _surface(_self: Any, _session: Any, *, agentic: bool) -> Any:
            return {"mcp_config": "{}", "allowed_tools": []} if agentic else None

        monkeypatch.setattr(dispatch, "dispatch_task", _spy_dispatch)
        monkeypatch.setattr(dispatch, "redispatch_task", _spy_redispatch)
        monkeypatch.setattr(dispatch, "await_completion", _stop)
        monkeypatch.setattr(dispatch, "cancel_task", _noop_cancel)
        monkeypatch.setattr(ExecutorAdapter, "_work_tool_surface", _surface)

        async with sf() as adapter_session:
            adapter = ExecutorAdapter(
                account=account,
                workspace_id=workspace_id,
                account_id=account.account_id,
                model_account_id=account.id,
                session=adapter_session,
                session_factory=sf,
                settings=settings,
                redis=redis,
                run_id=run.id,
            )
            with pytest.raises(Exception):  # noqa: B017 — the timeout path is not under test
                await adapter.chat(
                    system="x", messages=[{"role": "user", "content": "y"}], tools=tools
                )
    return seen


# ── Adapter: computes what is left of the run ────────────────────────────────


async def test_an_agentic_turn_is_dispatched_with_what_is_left_of_the_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings().model_copy(
        update={"agent_max_run_tokens": 2_000_000, "executor_agent_max_turns": 40}
    )

    seen = await _adapter_dispatch(
        monkeypatch, settings=settings, tools=_TOOLS, used=(500_000, 20_000)
    )

    assert seen["token_budget"] == 2_000_000 - 520_000
    assert seen["max_turns"] == 40
    # A re-delivered task is the same session's bounds, not an unbounded one.
    assert seen["redelivered"]["token_budget"] == 2_000_000 - 520_000
    assert seen["redelivered"]["max_turns"] == 40


async def test_a_run_already_at_its_ceiling_still_gets_a_budget_that_stops_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Zero or negative would read as "no budget" on the worker — the opposite of the truth."""
    settings = get_settings().model_copy(update={"agent_max_run_tokens": 1_000})

    seen = await _adapter_dispatch(monkeypatch, settings=settings, tools=_TOOLS, used=(900, 300))

    assert seen["token_budget"] == 1


async def test_a_granted_ceiling_reaches_the_session_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1105 — after the founder grants more budget, the worker must be told the NEW
    remainder; the base ceiling would kill the resumed session at once (budget 1)."""
    settings = get_settings().model_copy(update={"agent_max_run_tokens": 1_000})

    seen = await _adapter_dispatch(
        monkeypatch,
        settings=settings,
        tools=_TOOLS,
        used=(1_100, 0),
        payload={"token_cap_granted": 2_100},
    )

    assert seen["token_budget"] == 1_000


async def test_an_uncapped_deployment_sends_no_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = get_settings().model_copy(
        update={"agent_max_run_tokens": 0, "executor_agent_max_turns": 0}
    )

    seen = await _adapter_dispatch(monkeypatch, settings=settings, tools=_TOOLS)

    assert seen["token_budget"] is None
    assert seen["max_turns"] is None


async def test_a_chat_turn_carries_no_session_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    """Control: frame / judge turns are single completions with their own deadline."""
    seen = await _adapter_dispatch(monkeypatch, settings=get_settings(), tools=None)

    assert seen["token_budget"] is None
    assert seen["max_turns"] is None


# ── Dispatch payload: dispatch and re-delivery alike ─────────────────────────


async def _task_row(session: Any) -> Any:
    from backend.executors.db import ExecutorTaskRow

    task = ExecutorTaskRow(
        id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        executor_type="claude_code",
        prompt="p",
        system="",
        workspace_dir=".",
        status="pending",
    )
    session.add(task)
    await session.flush()
    return task


async def test_the_payload_carries_the_limits_on_dispatch_and_redelivery() -> None:
    async with shared_file_sessionmaker() as sf, sf() as session:
        task = await _task_row(session)
        redis = _CapturingRedis()
        worker_id = uuid.uuid4()
        await dispatch.dispatch_task(
            redis,
            session=session,
            task=task,
            worker_id=worker_id,
            token_budget=1_480_000,
            max_turns=40,
        )
        await dispatch.redispatch_task(
            redis, task=task, worker_id=worker_id, token_budget=1_480_000, max_turns=40
        )

    for payload in redis.adds:
        assert payload["token_budget"] == "1480000"
        assert payload["max_turns"] == "40"


async def test_the_payload_omits_absent_limits() -> None:
    """Redis Streams reject ``None``; a worker reading no key keeps today's behaviour."""
    async with shared_file_sessionmaker() as sf, sf() as session:
        task = await _task_row(session)
        redis = _CapturingRedis()
        await dispatch.dispatch_task(redis, session=session, task=task, worker_id=uuid.uuid4())

    assert "token_budget" not in redis.adds[0]
    assert "max_turns" not in redis.adds[0]


# ── Worker: hands them to the executor ───────────────────────────────────────


class _CapturingExecutor:
    def __init__(self) -> None:
        self.context: dict[str, Any] = {}

    async def execute(self, prompt: str, context: dict[str, Any]) -> AsyncIterator[ExecutionChunk]:
        self.context = dict(context)
        yield ExecutionChunk(done=True)


async def test_the_worker_hands_the_limits_to_the_executor() -> None:
    from backend.executors.worker import main as worker_main
    from tests.executors.worker.test_main import _client, _task

    executor = _CapturingExecutor()
    async with _client({}) as client:
        await worker_main.handle_task(
            _task(token_budget="1480000", max_turns="40"),
            executors={"claude_code": executor},
            client=client,
            headers={"X-Worker-Token": "WORKER-TOKEN"},
            redis=None,
        )

    assert executor.context.get("token_budget") == "1480000"
    assert executor.context.get("max_turns") == "40"
