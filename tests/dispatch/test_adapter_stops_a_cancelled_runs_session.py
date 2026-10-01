"""Cancelling a run reaches the executor session that is working for it (#1106).

Measured 2026-09-30: run ``58a7426d`` was cancelled at 10:41:01; its Claude Code session ran on
for 11 minutes and 6.4M prompt tokens. The cancel only wrote the run row. The adapter waiting on
that session's task had a kill path to the worker (``cancel_task``, built for timeouts) but no
reason to use it before the deadline.

An adapter that works FOR a run (``run_id`` set) now hands the awaiter a probe that reads the
run's status; on cancel it signals the worker to kill the session and raises
:class:`RunCancelledDuringTurn` — not retryable, because re-dispatching a cancelled run's turn
is exactly the waste being fixed.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from backend.config import get_settings
from backend.dispatch.adapter import ExecutorAdapter
from backend.executors import dispatch
from backend.workflow.infrastructure.db import ExecutionRun, RunStatus

from .._support import shared_file_sessionmaker
from .test_adapter import _executor_account, _make_redis, _seed_worker

pytestmark = pytest.mark.asyncio


async def _set_status(sf: Any, run_id: uuid.UUID, status: RunStatus) -> None:
    async with sf() as s:
        run = await s.get(ExecutionRun, run_id)
        run.status = status
        await s.commit()


async def test_a_cancelled_run_kills_its_session_and_is_not_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.dispatch.adapter import RunCancelledDuringTurn

    redis = await _make_redis()
    workspace_id = uuid.uuid4()
    settings = get_settings()
    awaits: list[bool] = []
    cancels: list[dict[str, Any]] = []

    async with shared_file_sessionmaker() as sf:
        async with sf() as setup:
            worker = await _seed_worker(
                setup, workspace_id=workspace_id, capabilities=["claude_code"]
            )
            account = _executor_account(workspace_id, worker.id)
            setup.add(account)
            run = ExecutionRun(id=uuid.uuid4(), workspace_id=workspace_id, status=RunStatus.RUNNING)
            setup.add(run)
            await setup.commit()

        async def _await(*_a: Any, abandon_if: Any = None, **_kw: Any) -> Any:
            assert abandon_if is not None, "a run's adapter must give the awaiter a probe"
            awaits.append(True)
            assert await abandon_if() is False, "a running run must not be abandoned"
            await _set_status(sf, run.id, RunStatus.CANCELLED)
            assert await abandon_if() is True, "the probe must see the cancel"
            raise dispatch.TaskAbandoned("run cancelled")

        async def _spy_cancel(*_a: Any, **kw: Any) -> None:
            cancels.append(dict(kw))

        monkeypatch.setattr(dispatch, "await_completion", _await)
        monkeypatch.setattr(dispatch, "cancel_task", _spy_cancel)

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
            with pytest.raises(RunCancelledDuringTurn) as exc_info:
                await adapter.chat(system="x", messages=[{"role": "user", "content": "y"}])

    assert exc_info.value.retryable is False
    assert len(awaits) == 1, "a cancelled run's turn must not be re-dispatched"
    assert len(cancels) == 1 and cancels[0]["worker_id"] == worker.id


async def test_a_runless_adapter_has_no_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Control: frame / judge / ingest calls belong to no run — nothing to abandon for."""
    redis = await _make_redis()
    workspace_id = uuid.uuid4()
    seen: list[Any] = []

    async with shared_file_sessionmaker() as sf:
        async with sf() as setup:
            worker = await _seed_worker(
                setup, workspace_id=workspace_id, capabilities=["claude_code"]
            )
            account = _executor_account(workspace_id, worker.id)
            setup.add(account)
            await setup.commit()

        async def _await(*_a: Any, abandon_if: Any = None, **_kw: Any) -> Any:
            seen.append(abandon_if)
            raise dispatch.TaskTimeout("stop here")

        async def _noop_cancel(*_a: Any, **_kw: Any) -> None:
            return None

        monkeypatch.setattr(dispatch, "await_completion", _await)
        monkeypatch.setattr(dispatch, "cancel_task", _noop_cancel)

        async with sf() as adapter_session:
            adapter = ExecutorAdapter(
                account=account,
                workspace_id=workspace_id,
                account_id=account.account_id,
                model_account_id=account.id,
                session=adapter_session,
                session_factory=sf,
                settings=get_settings(),
                redis=redis,
            )
            with pytest.raises(Exception):  # noqa: B017 — the timeout path is not under test
                await adapter.chat(system="x", messages=[{"role": "user", "content": "y"}])

    assert seen == [None]
