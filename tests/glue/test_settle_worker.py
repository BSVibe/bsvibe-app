"""SettleWorker — drain ``settle``-class ExecutionRunActivity rows into BSage.

The agent loop (RunOrchestrator, PR #8) records a ``settle`` activity for
every verified work step — the "writes knowledge" half of the §5 trust
ratchet. This worker drains those rows into the workspace's BSage graph,
once each (idempotent), with each write structurally scoped to its own
workspace vault.

Real KnowledgeSettleSink + a tmp vault_root is used on purpose: the write
path is a plain markdown filesystem write (no LLM, no network), so the
tests prove the actual settle→BSage deposit rather than a mock of it.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy import create_engine, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.execution.db import ExecutionBase, ExecutionRun, ExecutionRunActivity, RunStatus
from backend.workers.db import SettleDrainRow, WorkersBase
from backend.workers.settle_worker import (
    KnowledgeSettleSink,
    Settlement,
    SettleWorker,
    SettleWorkerConfig,
)
from backend.workspaces.db import WorkspaceRow, WorkspacesBase

PG_URL = os.environ.get(
    "BSVIBE_DATABASE_URL", "postgresql+asyncpg://bsvibe:bsvibe@localhost:5442/bsvibe"
)

pytestmark = pytest.mark.asyncio

_BASES = (ExecutionBase, WorkersBase, WorkspacesBase)


def _can_reach_pg() -> bool:
    sync_url = PG_URL.replace("+asyncpg", "+psycopg") if "+asyncpg" in PG_URL else PG_URL
    try:
        engine = create_engine(sync_url, pool_pre_ping=True)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        engine.dispose()
        return True
    except Exception:
        return False


@pytest_asyncio.fixture
async def sf():
    use_pg = bool(os.environ.get("BSVIBE_DATABASE_URL")) and _can_reach_pg()
    url = PG_URL if use_pg else "sqlite+aiosqlite:///:memory:"
    engine = create_async_engine(url, future=True)
    async with engine.begin() as conn:
        for base in _BASES:
            await conn.run_sync(base.metadata.create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    yield sm
    if use_pg:
        async with engine.begin() as conn:
            for base in reversed(_BASES):
                await conn.run_sync(base.metadata.drop_all)
    await engine.dispose()


async def _seed_settle_activity(
    sf,
    *,
    workspace_id: uuid.UUID,
    summary: str = "added pagination to the orders list",
    artifact_refs: list[str] | None = None,
    activity_type: str = "settle",
) -> uuid.UUID:
    """Insert a run + one activity, return the activity id."""
    run_id = uuid.uuid4()
    activity_id = uuid.uuid4()
    async with sf() as s:
        s.add(
            ExecutionRun(
                id=run_id,
                workspace_id=workspace_id,
                status=RunStatus.REVIEW_READY,
                payload={},
                created_at=datetime.now(tz=UTC),
                updated_at=datetime.now(tz=UTC),
            )
        )
        await s.flush()
        s.add(
            ExecutionRunActivity(
                id=activity_id,
                run_id=run_id,
                workspace_id=workspace_id,
                activity_type=activity_type,
                payload={
                    "verified": True,
                    "artifact_refs": artifact_refs or ["backend/orders/list.py"],
                    "summary": summary,
                },
                created_at=datetime.now(tz=UTC),
            )
        )
        await s.commit()
    return activity_id


def _ws_dir(vault_root, region: str, workspace_id: uuid.UUID):
    return vault_root / region / str(workspace_id)


def _written_notes(vault_root, region: str, workspace_id: uuid.UUID) -> list:
    ws_dir = _ws_dir(vault_root, region, workspace_id)
    return list(ws_dir.rglob("*.md")) if ws_dir.exists() else []


async def test_settle_worker_writes_observation_to_bsage(sf, tmp_path) -> None:
    ws = uuid.uuid4()
    activity_id = await _seed_settle_activity(sf, workspace_id=ws, summary="wired the cache")

    worker = SettleWorker(
        session_factory=sf,
        sink=KnowledgeSettleSink(vault_root=tmp_path),
        config=SettleWorkerConfig(batch_size=10, poll_interval_s=0.01, default_region="us-1"),
    )
    processed = await worker.drain_once()

    assert processed == 1
    notes = _written_notes(tmp_path, "us-1", ws)
    assert len(notes) == 1, f"expected one BSage note under the workspace vault, got {notes}"
    body = notes[0].read_text(encoding="utf-8")
    assert "wired the cache" in body

    # The activity is marked drained exactly once.
    async with sf() as s:
        drains = (await s.execute(select(SettleDrainRow))).scalars().all()
        assert len(drains) == 1
        assert drains[0].activity_id == activity_id
        assert drains[0].workspace_id == ws


async def test_settle_worker_idempotent_redrain(sf, tmp_path) -> None:
    ws = uuid.uuid4()
    await _seed_settle_activity(sf, workspace_id=ws)
    worker = SettleWorker(
        session_factory=sf,
        sink=KnowledgeSettleSink(vault_root=tmp_path),
        config=SettleWorkerConfig(default_region="us-1"),
    )

    first = await worker.drain_once()
    second = await worker.drain_once()

    assert first == 1
    assert second == 0, "re-drain must not re-process an already-drained activity"
    # No duplicate node written, no duplicate drain marker.
    assert len(_written_notes(tmp_path, "us-1", ws)) == 1
    async with sf() as s:
        drains = (await s.execute(select(SettleDrainRow))).scalars().all()
        assert len(drains) == 1


async def test_settle_worker_workspace_isolation(sf, tmp_path) -> None:
    ws_a = uuid.uuid4()
    ws_b = uuid.uuid4()
    # ws_b lives in a different region — exercises per-workspace region resolution.
    async with sf() as s:
        s.add(WorkspaceRow(id=ws_b, name="ws-b", region="eu-1"))
        await s.commit()

    await _seed_settle_activity(sf, workspace_id=ws_a, summary="alpha learning")
    await _seed_settle_activity(sf, workspace_id=ws_b, summary="beta learning")

    worker = SettleWorker(
        session_factory=sf,
        sink=KnowledgeSettleSink(vault_root=tmp_path),
        config=SettleWorkerConfig(default_region="us-1"),
    )
    processed = await worker.drain_once()
    assert processed == 2

    a_notes = _written_notes(tmp_path, "us-1", ws_a)
    b_notes = _written_notes(tmp_path, "eu-1", ws_b)
    assert len(a_notes) == 1
    assert len(b_notes) == 1
    assert "alpha learning" in a_notes[0].read_text(encoding="utf-8")
    assert "beta learning" in b_notes[0].read_text(encoding="utf-8")

    # ws_a's learning never leaks into ws_b's vault and vice versa.
    a_body = a_notes[0].read_text(encoding="utf-8")
    b_body = b_notes[0].read_text(encoding="utf-8")
    assert "beta learning" not in a_body
    assert "alpha learning" not in b_body
    # ws_a's vault dir must contain only ws_a content.
    assert not _ws_dir(tmp_path, "eu-1", ws_a).exists()
    assert not _ws_dir(tmp_path, "us-1", ws_b).exists()


async def test_settle_worker_ignores_non_settle_activities(sf, tmp_path) -> None:
    ws = uuid.uuid4()
    for kind in ("llm_turn", "tool_call", "verify", "error"):
        await _seed_settle_activity(sf, workspace_id=ws, activity_type=kind)

    worker = SettleWorker(
        session_factory=sf,
        sink=KnowledgeSettleSink(vault_root=tmp_path),
        config=SettleWorkerConfig(default_region="us-1"),
    )
    processed = await worker.drain_once()

    assert processed == 0
    assert _written_notes(tmp_path, "us-1", ws) == []
    async with sf() as s:
        assert (await s.execute(select(SettleDrainRow))).scalars().all() == []


async def test_settle_worker_empty_queue(sf, tmp_path) -> None:
    worker = SettleWorker(session_factory=sf, sink=KnowledgeSettleSink(vault_root=tmp_path))
    assert await worker.drain_once() == 0


async def test_settle_worker_tick_drains_one_batch(sf, tmp_path) -> None:
    """The BaseWorker Template-Method hook (_tick) drives one drain batch."""
    ws = uuid.uuid4()
    await _seed_settle_activity(sf, workspace_id=ws)
    worker = SettleWorker(
        session_factory=sf,
        sink=KnowledgeSettleSink(vault_root=tmp_path),
        config=SettleWorkerConfig(default_region="us-1"),
    )
    assert await worker._tick() == 1
    assert len(_written_notes(tmp_path, "us-1", ws)) == 1


async def test_settle_worker_sink_failure_is_retryable(sf, tmp_path) -> None:
    """A sink that raises leaves the activity un-drained so the next tick retries."""
    ws = uuid.uuid4()
    await _seed_settle_activity(sf, workspace_id=ws)

    class _BoomSink:
        async def absorb(self, settlement: Settlement) -> str | None:
            raise RuntimeError("vault disk full")

    worker = SettleWorker(
        session_factory=sf,
        sink=_BoomSink(),
        config=SettleWorkerConfig(default_region="us-1"),
    )
    processed = await worker.drain_once()

    assert processed == 0, "a failed write must not count as processed"
    async with sf() as s:
        # Not marked drained — eligible for retry on the next tick.
        assert (await s.execute(select(SettleDrainRow))).scalars().all() == []
