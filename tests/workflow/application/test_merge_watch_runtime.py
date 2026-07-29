"""PR6 — merge_watch_runtime application-layer callbacks.

Covers the two application seams the infrastructure ``MergeWatchWorker`` gets
INJECTED (so the worker never crosses into the application layer itself):

* :func:`build_merge_watch_conflict_redispatch` — writes
  ``run.payload["merge_conflict"]`` and transitions the run RUNNING → OPEN (the
  same resume seam ``checkpoint_resolution`` uses), so ``AgentWorker`` re-picks
  it and the agent resolves the conflict (PR7's side).

Real Postgres (``BSVIBE_DATABASE_URL``) for the ExecutionRun row.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.data import Base
from backend.workflow.application.runtime.merge_watch_runtime import (
    build_merge_watch_conflict_redispatch,
)
from backend.workflow.infrastructure.db import ExecutionRun, RunStatus
from tests._support import db_engine

pytestmark = pytest.mark.asyncio


async def _seed_run(sf: async_sessionmaker, *, status: RunStatus) -> uuid.UUID:  # noqa: ANN001
    run_id = uuid.uuid4()
    async with sf() as session:
        session.add(
            ExecutionRun(
                id=run_id,
                workspace_id=uuid.uuid4(),
                status=status,
                payload={"intent_text": "x"},
            )
        )
        await session.commit()
    return run_id


async def test_conflict_redispatch_writes_payload_and_reopens_running_run() -> None:
    async with db_engine(Base) as (engine, _pg):
        sf = async_sessionmaker(engine, expire_on_commit=False)
        run_id = await _seed_run(sf, status=RunStatus.RUNNING)

        redispatch = build_merge_watch_conflict_redispatch(session_factory=sf)
        await redispatch(
            run_id, conflict_paths=["shared.txt", "a/b.py"], base_branch="main", pr_number=42
        )

        async with sf() as session:
            run = await session.get(ExecutionRun, run_id)
            assert run is not None
            assert run.status is RunStatus.OPEN  # RUNNING → OPEN resume seam
            assert run.payload["merge_conflict"] == {
                "conflict_paths": ["shared.txt", "a/b.py"],
                "base_branch": "main",
                "pr_number": 42,
            }
            # The originating intent is preserved (payload re-assigned, not clobbered).
            assert run.payload["intent_text"] == "x"


async def test_conflict_redispatch_missing_run_is_a_noop() -> None:
    async with db_engine(Base) as (engine, _pg):
        sf = async_sessionmaker(engine, expire_on_commit=False)
        redispatch = build_merge_watch_conflict_redispatch(session_factory=sf)
        # No such run — must not raise (idempotent / at-least-once contract).
        await redispatch(uuid.uuid4(), conflict_paths=["x"], base_branch="main", pr_number=1)
