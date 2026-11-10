"""An executor turn lives and dies with the job that launched it (2026-11-10, run ``f33f959b``).

The worker's ``stop()`` cancels the drive task awaiting the executor, but the turn itself runs on
the executor's side. Two ties keep it from outliving the job:

* the awaiting adapter kills the worker's session when it is cancelled;
* the run-scoped token it minted carries the run's claim, so the work tools refuse the turn once
  the claim is gone (the safety net for an executor that cannot be killed).
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest

from backend.config import get_settings
from backend.dispatch import adapter as adapter_module
from backend.dispatch.adapter import ExecutorAdapter
from backend.executors import dispatch
from backend.identity import oauth_service
from backend.workflow.infrastructure.db import ExecutionRun, RunStatus

from .._support import shared_file_sessionmaker
from .test_adapter import _executor_account, _make_redis, _seed_worker

pytestmark = pytest.mark.asyncio


async def _adapter_for(sf: Any, setup: Any, workspace_id: uuid.UUID, run_id: uuid.UUID) -> Any:
    worker = await _seed_worker(setup, workspace_id=workspace_id, capabilities=["claude_code"])
    account = _executor_account(workspace_id, worker.id)
    setup.add(account)
    await setup.commit()
    adapter_session = sf()
    adapter = ExecutorAdapter(
        account=account,
        workspace_id=workspace_id,
        account_id=account.account_id,
        model_account_id=account.id,
        session=adapter_session,
        session_factory=sf,
        settings=get_settings(),
        redis=await _make_redis(),
        run_id=run_id,
    )
    return worker, adapter, adapter_session


async def test_a_cancelled_job_kills_its_executor_session(monkeypatch: pytest.MonkeyPatch) -> None:
    workspace_id = uuid.uuid4()
    cancels: list[dict[str, Any]] = []

    async with shared_file_sessionmaker() as sf:
        async with sf() as setup:
            run = ExecutionRun(
                id=uuid.uuid4(),
                workspace_id=workspace_id,
                status=RunStatus.RUNNING,
                claimed_by=uuid.uuid4(),
            )
            setup.add(run)
            await setup.commit()
            worker, adapter, adapter_session = await _adapter_for(sf, setup, workspace_id, run.id)

        async def _hang(*_a: Any, **_kw: Any) -> Any:
            await asyncio.sleep(3600)

        async def _spy_cancel(*_a: Any, **kw: Any) -> None:
            cancels.append(dict(kw))

        monkeypatch.setattr(dispatch, "await_completion", _hang)
        monkeypatch.setattr(dispatch, "cancel_task", _spy_cancel)

        job = asyncio.create_task(
            adapter.chat(system="x", messages=[{"role": "user", "content": "y"}])
        )
        await asyncio.sleep(0.2)
        job.cancel()
        with pytest.raises(asyncio.CancelledError):
            await job
        await adapter_session.close()

    assert len(cancels) == 1 and cancels[0]["worker_id"] == worker.id


async def test_the_task_token_carries_the_runs_claim(monkeypatch: pytest.MonkeyPatch) -> None:
    workspace_id = uuid.uuid4()
    job = uuid.uuid4()
    minted: list[uuid.UUID | None] = []

    real = oauth_service.issue_run_task_token

    async def _spy(*a: Any, **kw: Any) -> str:
        minted.append(kw.get("claim"))
        return await real(*a, **kw)

    async def _founder(*_a: Any, **_kw: Any) -> uuid.UUID:
        return uuid.uuid4()

    monkeypatch.setattr(oauth_service, "issue_run_task_token", _spy)
    monkeypatch.setattr(adapter_module, "_founder_of", _founder)

    async def _boom(*_a: Any, **_kw: Any) -> Any:
        raise dispatch.TaskTimeout("stop here")

    async def _noop(*_a: Any, **_kw: Any) -> None:
        return None

    monkeypatch.setattr(dispatch, "await_completion", _boom)
    monkeypatch.setattr(dispatch, "cancel_task", _noop)

    async with shared_file_sessionmaker() as sf:
        async with sf() as setup:
            run = ExecutionRun(
                id=uuid.uuid4(),
                workspace_id=workspace_id,
                status=RunStatus.RUNNING,
                claimed_by=job,
            )
            setup.add(run)
            await setup.commit()
            _worker, adapter, adapter_session = await _adapter_for(sf, setup, workspace_id, run.id)
        with pytest.raises(Exception):  # noqa: B017 -- the timeout path is not under test
            await adapter.chat(
                system="x",
                messages=[{"role": "user", "content": "y"}],
                tools=[{"type": "function", "function": {"name": "file_read"}}],
            )
        await adapter_session.close()

    assert minted and minted[0] == job
