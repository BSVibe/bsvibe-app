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

import uuid
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.identity.workspaces_db import WorkspaceRow, WorkspacesBase
from backend.knowledge.infrastructure.workers.settle_worker import (
    KnowledgeSettleSink,
    Settlement,
    SettleWorker,
    SettleWorkerConfig,
)
from backend.workers.db import SettleDrainRow, WorkersBase
from backend.workflow.infrastructure.db import (
    ExecutionBase,
    ExecutionRun,
    ExecutionRunActivity,
    RunStatus,
)

from .._support import db_engine

pytestmark = pytest.mark.asyncio

_BASES = (ExecutionBase, WorkersBase, WorkspacesBase)


@pytest_asyncio.fixture
async def sf():
    async with db_engine(*_BASES) as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


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


async def test_settle_worker_calls_embed_hook_per_absorbed_note(sf, tmp_path) -> None:
    """G5b: when an embed hook is wired, the worker invokes it once per absorbed
    settlement with the written note_ref, so note_embeddings get populated."""
    ws = uuid.uuid4()
    await _seed_settle_activity(sf, workspace_id=ws, summary="wired the cache")

    calls: list[tuple[str, str]] = []

    async def _hook(settlement, node_ref: str) -> None:
        calls.append((settlement.summary, node_ref))

    worker = SettleWorker(
        session_factory=sf,
        sink=KnowledgeSettleSink(vault_root=tmp_path),
        config=SettleWorkerConfig(batch_size=10, poll_interval_s=0.01, default_region="us-1"),
        embed_hook=_hook,
    )
    processed = await worker.drain_once()

    assert processed == 1
    assert len(calls) == 1
    summary, node_ref = calls[0]
    assert summary == "wired the cache"
    assert node_ref  # the written note path was handed to the hook


async def test_settle_worker_embed_hook_failure_is_soft(sf, tmp_path) -> None:
    """A failing embed hook must NOT break the drain — the note is still absorbed
    + drained (the settle SoT is independent of embedding population)."""
    ws = uuid.uuid4()
    await _seed_settle_activity(sf, workspace_id=ws, summary="wired the cache")

    async def _boom(settlement, node_ref: str) -> None:
        raise RuntimeError("embedding provider down")

    worker = SettleWorker(
        session_factory=sf,
        sink=KnowledgeSettleSink(vault_root=tmp_path),
        config=SettleWorkerConfig(batch_size=10, poll_interval_s=0.01, default_region="us-1"),
        embed_hook=_boom,
    )
    assert await worker.drain_once() == 1
    async with sf() as s:
        assert len((await s.execute(select(SettleDrainRow))).scalars().all()) == 1


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


async def test_settle_worker_note_carries_content_tags_not_just_structural(sf, tmp_path) -> None:
    """The written garden note carries deterministically-derived content tags
    (from artifact_refs + summary) alongside the structural markers — so the
    promoter actually has candidates instead of an all-structural note it drops."""
    from backend.knowledge.canonicalization.store import NoteStore  # noqa: PLC0415
    from backend.knowledge.graph.storage import FileSystemStorage  # noqa: PLC0415

    ws = uuid.uuid4()
    await _seed_settle_activity(
        sf,
        workspace_id=ws,
        summary="configured the reverse proxy",
        artifact_refs=["backend/auth/client.py"],
    )
    worker = SettleWorker(
        session_factory=sf,
        sink=KnowledgeSettleSink(vault_root=tmp_path),
        config=SettleWorkerConfig(default_region="us-1"),
    )
    assert await worker.drain_once() == 1

    store = NoteStore(FileSystemStorage(_ws_dir(tmp_path, "us-1", ws)))
    garden_paths = await store.list_garden_paths()
    assert len(garden_paths) == 1
    tags = set(await store.read_garden_tags(garden_paths[0]))

    # Structural tags preserved (other consumers rely on them) ...
    assert {"settle", "verified-run"} <= tags
    # ... and content tags derived from the inputs are present.
    assert {"auth", "client"} <= tags, tags  # artifact stems
    assert {"configured", "reverse", "proxy"} <= tags, tags  # salient summary terms
    # Stopwords / structural markers never leak in as content.
    assert "the" not in tags


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
