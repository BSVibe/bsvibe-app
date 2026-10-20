"""#929 (B), stage 1 PR 1 — a run being driven names the MACHINE driving it.

``claimed_by`` is a random id per worker PROCESS, so nothing said which machine holds a run's
worktree and sandbox. With more than one machine the MCP work tools (which execute in whichever
API replica received the call) must find that machine, and a run arriving on a new machine
must know it moved. ``home_machine`` is ``BSVIBE_MACHINE_ID`` stamped on claim and cleared on
every release — exactly alongside ``claimed_by``. An empty machine id (single-box) records NULL.

See docs/design/stateless-workers.md §2.1.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.config import get_settings
from backend.data.rls import workspace_session_scope
from backend.data.scoping import workspace_scope
from backend.workflow.infrastructure.db import ExecutionBase, ExecutionRun, RunStatus
from backend.workflow.infrastructure.workers.agent_worker import AgentWorker

from .._support import db_engine

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def sf():
    async with db_engine(ExecutionBase) as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


async def _seed(sf_, *, status: RunStatus = RunStatus.OPEN, **over) -> tuple[uuid.UUID, uuid.UUID]:
    ws, rid = uuid.uuid4(), uuid.uuid4()
    now = datetime.now(tz=UTC)
    async with sf_() as s, workspace_session_scope(s, ws):
        s.add(
            ExecutionRun(
                id=rid,
                workspace_id=ws,
                status=status,
                payload={},
                created_at=now,
                updated_at=now,
                **over,
            )
        )
        await s.commit()
    return ws, rid


async def _home(sf_, ws: uuid.UUID, rid: uuid.UUID) -> str | None:
    async with sf_() as s, workspace_session_scope(s, ws):
        run = await s.get(ExecutionRun, rid)
        assert run is not None
        return run.home_machine


async def test_a_claim_stamps_this_machine(sf, monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "machine_id", "box-a")
    ws, rid = await _seed(sf)

    claimed = await AgentWorker(session_factory=sf)._claim_runs_for_drive()

    assert (rid, ws) in claimed
    assert await _home(sf, ws, rid) == "box-a"


async def test_a_single_box_records_no_home(sf, monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "machine_id", "")
    ws, rid = await _seed(sf)

    await AgentWorker(session_factory=sf)._claim_runs_for_drive()

    assert await _home(sf, ws, rid) is None


async def test_every_release_clears_the_home(sf, monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "machine_id", "box-a")
    worker = AgentWorker(session_factory=sf)

    ws, rid = await _seed(sf)
    await worker._claim_runs_for_drive()
    # The worker publishes the run's workspace around everything after the claim.
    with workspace_scope(ws):
        await worker._clear_claim(rid)
    assert await _home(sf, ws, rid) is None, "a paused run is held by no machine"

    ws2, rid2 = await _seed(sf)
    await worker._claim_runs_for_drive()
    with workspace_scope(ws2):
        await worker._release_claim_to_open(rid2, reason="test")
    assert await _home(sf, ws2, rid2) is None, "a run back in the queue is held by no machine"


async def test_a_reaped_stale_claim_clears_the_home(sf) -> None:
    ws, rid = await _seed(
        sf,
        status=RunStatus.RUNNING,
        claimed_at=datetime.now(tz=UTC) - timedelta(days=1),
        claimed_by=uuid.uuid4(),
        home_machine="dead-box",
    )

    assert await AgentWorker(session_factory=sf)._reap_stale_claims() >= 1

    assert await _home(sf, ws, rid) is None


def test_every_claim_write_also_writes_the_home() -> None:
    """``claimed_by`` is set or cleared in five places (claim, clear, release, reap, the
    drive-failure escalation). A sixth that forgot ``home_machine`` would leave a run pointing
    at a machine that no longer holds it — tool calls would be forwarded to the wrong box."""
    src = (
        Path(__file__).resolve().parents[2]
        / "backend/workflow/infrastructure/workers/agent_worker.py"
    ).read_text()
    claim_writes = len(re.findall(r"claimed_by\s*=\s*(None|self\._worker_id)", src))
    home_writes = len(re.findall(r"home_machine\s*=", src))
    assert claim_writes >= 5, "control: the claim writes must be found"
    assert home_writes == claim_writes, (claim_writes, home_writes)
