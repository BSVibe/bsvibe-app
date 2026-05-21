"""RelayWorker — drain audit_outbox into a fake Relay."""

from __future__ import annotations

import os
from collections.abc import Sequence
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.supervisor.audit.models import AuditOutboxBase, AuditOutboxRecord
from backend.supervisor.audit.store import OutboxStore
from backend.workers.relay_worker import RelayConfig, RelayWorker

PG_URL = os.environ.get(
    "BSVIBE_DATABASE_URL", "postgresql+asyncpg://bsvibe:bsvibe@localhost:5442/bsvibe"
)


pytestmark = pytest.mark.asyncio


async def _can_reach_pg() -> bool:
    try:
        engine = create_async_engine(PG_URL, future=True, pool_pre_ping=True)
        async with engine.connect() as conn:
            await conn.execute(__import__("sqlalchemy").text("SELECT 1"))
        await engine.dispose()
        return True
    except Exception:
        return False


@pytest_asyncio.fixture
async def session_factory() -> async_sessionmaker[AsyncSession]:
    if not await _can_reach_pg():
        pytest.skip(f"Postgres not reachable at {PG_URL}")
    engine = create_async_engine(PG_URL, future=True)
    async with engine.begin() as conn:
        await conn.run_sync(AuditOutboxBase.metadata.create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    yield sm
    async with engine.begin() as conn:
        await conn.run_sync(AuditOutboxBase.metadata.drop_all)
    await engine.dispose()


async def _seed_outbox(sm: async_sessionmaker[AsyncSession], *, count: int) -> list[int]:
    ids: list[int] = []
    async with sm() as session:
        for i in range(count):
            row = AuditOutboxRecord(
                event_id=f"event-{i}",
                event_type="gateway.completion.dispatched",
                occurred_at=datetime.now(tz=UTC),
                payload={"i": i},
            )
            session.add(row)
        await session.commit()
        # Re-fetch ids after flush.
        store = OutboxStore()
        async with sm() as s2:
            rows = await store.select_undelivered(s2, batch_size=count + 1)
            ids = [r.id for r in rows]
    return ids


class _CaptureRelay:
    def __init__(self) -> None:
        self.batches: list[list[int]] = []
        self.fail_ids: set[int] = set()
        self.raise_on_send = False

    async def send(self, records: Sequence[AuditOutboxRecord]) -> Sequence[int]:
        if self.raise_on_send:
            raise RuntimeError("upstream down")
        ids = [r.id for r in records]
        self.batches.append(ids)
        return [rid for rid in ids if rid not in self.fail_ids]


async def test_drain_once_marks_delivered(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    ids = await _seed_outbox(session_factory, count=3)
    assert len(ids) == 3
    relay = _CaptureRelay()
    worker = RelayWorker(session_factory=session_factory, relay=relay)

    delivered = await worker.drain_once()
    assert delivered == 3
    assert relay.batches[0] == ids

    # The same drain again returns zero — rows are marked delivered.
    again = await worker.drain_once()
    assert again == 0


async def test_partial_failure_records(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    ids = await _seed_outbox(session_factory, count=4)
    relay = _CaptureRelay()
    relay.fail_ids = {ids[1], ids[3]}
    worker = RelayWorker(session_factory=session_factory, relay=relay)

    delivered = await worker.drain_once()
    assert delivered == 2

    # Two rows remain undelivered (record_failure incremented retry_count +
    # pushed next_attempt_at to a backoff; query without time filter).
    from sqlalchemy import select

    async with session_factory() as s:
        result = await s.execute(
            select(AuditOutboxRecord).where(AuditOutboxRecord.delivered_at.is_(None))
        )
        undelivered = list(result.scalars())
        ids_remaining = sorted(r.id for r in undelivered)
        assert ids_remaining == sorted({ids[1], ids[3]})
        for r in undelivered:
            assert r.retry_count == 1
            assert r.last_error == "upstream rejected"


async def test_relay_exception_records_per_row(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_outbox(session_factory, count=2)
    relay = _CaptureRelay()
    relay.raise_on_send = True
    worker = RelayWorker(
        session_factory=session_factory,
        relay=relay,
        config=RelayConfig(batch_size=10, poll_interval_s=0.01, max_retries=2),
    )
    delivered = await worker.drain_once()
    assert delivered == 0
    # All rows still undelivered with retry_count incremented.
    from sqlalchemy import select

    async with session_factory() as s:
        result = await s.execute(
            select(AuditOutboxRecord).where(AuditOutboxRecord.delivered_at.is_(None))
        )
        undelivered = list(result.scalars())
        assert len(undelivered) == 2
        for r in undelivered:
            assert r.retry_count >= 1
            assert r.last_error == "upstream down"


async def test_empty_outbox_returns_zero(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    relay = _CaptureRelay()
    worker = RelayWorker(session_factory=session_factory, relay=relay)
    assert await worker.drain_once() == 0
    assert relay.batches == []
