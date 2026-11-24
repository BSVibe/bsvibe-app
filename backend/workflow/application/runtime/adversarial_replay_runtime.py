"""Production wiring of the Stage A replay worker.

The judge LLM and the sandbox come from the SAME helpers a real verification uses
(``resolve_judge_llm`` / ``resolve_sandbox_manager``), so a replayed round runs under the same
conditions as a production one — and the tokens are the workspace owner's.
"""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.config import Settings
from backend.data.rls import workspace_session_scope
from backend.dispatch.adapter import ExecutorAdapter
from backend.workers.emit import get_dispatch_redis_client
from backend.workflow.application.adversarial_replay import (
    CaseUnavailable,
    ReplayInput,
    ReplayRefused,
    load_replay_input,
)
from backend.workflow.application.adversarial_replay_jobs import execute_job
from backend.workflow.application.runtime.account_resolution import resolve_judge_llm
from backend.workflow.application.runtime.sandbox_selection import resolve_sandbox_manager
from backend.workflow.infrastructure.db import AdversarialReplayJob
from backend.workflow.infrastructure.sandbox.protocol import SandboxSession
from backend.workflow.infrastructure.workers.adversarial_replay_worker import (
    AdversarialReplayWorker,
)


async def judge_llm_for(
    session_factory: async_sessionmaker[AsyncSession], settings: Settings, workspace_id: uuid.UUID
) -> Any:
    """The workspace's ``workflow.judge`` route — the one a real verification grades on.

    ``resolve_judge_llm`` wants a run, but only reads ``workspace_id`` from it (and logs ``id`` on
    a miss): it passes NO run id on to the adapter, so the judge turn is attached to no run and
    books no run meter, no executor-task run link and no run event. The stand-in's id is a
    throwaway, never stored (pinned by ``test_adversarial_replay``)."""
    async with session_factory() as session:
        async with workspace_session_scope(session, workspace_id):
            # the dispatch client (any ``worker_mode``, just ``redis_url``) — the same one the
            # production verification round gets; the emit client is None under db_polling
            llm = await resolve_judge_llm(
                session,
                SimpleNamespace(id=uuid.uuid4(), workspace_id=workspace_id),  # type: ignore[arg-type]
                settings,
                redis=get_dispatch_redis_client(settings),
                session_factory=session_factory,
            )
    adapter = getattr(llm, "_adapter", None)
    if isinstance(adapter, ExecutorAdapter) and adapter.redis is None:
        # decided once, at job start: every Stage A would end `unavailable` for this reason
        raise ReplayRefused("judge 경로가 executor 인데 Redis 가 없다 (redis_url 미설정)")
    return llm


def box_factory(settings: Settings) -> Any:
    manager = resolve_sandbox_manager(None, settings)

    @contextlib.asynccontextmanager
    async def _box(path: Path) -> AsyncIterator[SandboxSession]:
        project = uuid.uuid4()
        box = await manager.acquire(project, str(path))
        try:
            yield box
        finally:
            await manager.release(project)

    return _box


def build_adversarial_replay_worker(
    *, session_factory: async_sessionmaker[AsyncSession], settings: Settings
) -> AdversarialReplayWorker:
    root = Path(settings.run_workspace_root)
    root = root if root.is_absolute() else Path.cwd() / root

    def load_for(workspace_id: uuid.UUID) -> Any:
        async def load(deliverable_id: uuid.UUID) -> ReplayInput:
            async with session_factory() as session:
                async with workspace_session_scope(session, workspace_id):
                    return await load_replay_input(session, workspace_id, deliverable_id)

        return load

    async def execute(job: AdversarialReplayJob) -> None:
        await execute_job(
            session_factory,
            job,
            llm_for=lambda ws: judge_llm_for(session_factory, settings, ws),
            load_for=load_for,
            box_for=box_factory(settings),
            work_root=root / "adversarial-replay",
        )

    return AdversarialReplayWorker(session_factory=session_factory, execute=execute)


__all__ = ["CaseUnavailable", "build_adversarial_replay_worker", "judge_llm_for"]
