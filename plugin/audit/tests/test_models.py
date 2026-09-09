"""Schema sanity tests for AuditOutboxRecord — the audit subsystem's one table.

The ``audit_events`` ORM used to be tested alongside it. That table was producer-less
(0 rows in prod for its whole life) and was dropped in
``20260909_drop_producerless_audit_events``; a schema test over a table
nothing writes only proved the ORM could round-trip a row the product never
made.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from plugin.audit.models import (
    AuditOutboxBase,
    AuditOutboxRecord,
    SupervisorBase,
)


@pytest_asyncio.fixture
async def fresh_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(SupervisorBase.metadata.create_all)
        await conn.run_sync(AuditOutboxBase.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


class TestAuditOutboxRecordModel:
    async def test_insert_and_select(self, fresh_session: AsyncSession):
        row = AuditOutboxRecord(
            event_id=str(uuid.uuid4()),
            event_type="x.y.z",
            occurred_at=datetime.now(UTC),
            payload={"a": 1},
        )
        fresh_session.add(row)
        await fresh_session.commit()

        rows = (await fresh_session.execute(select(AuditOutboxRecord))).scalars().all()
        assert len(rows) == 1
        r = rows[0]
        assert r.event_type == "x.y.z"
        assert r.delivered_at is None
        assert r.retry_count == 0
        assert r.dead_letter is False
        assert r.payload == {"a": 1}

    async def test_event_id_unique(self, fresh_session: AsyncSession):
        eid = str(uuid.uuid4())
        fresh_session.add(
            AuditOutboxRecord(
                event_id=eid,
                event_type="x",
                occurred_at=datetime.now(UTC),
                payload={},
            )
        )
        await fresh_session.commit()
        fresh_session.add(
            AuditOutboxRecord(
                event_id=eid,
                event_type="y",
                occurred_at=datetime.now(UTC),
                payload={},
            )
        )
        import pytest
        from sqlalchemy.exc import IntegrityError

        with pytest.raises(IntegrityError):
            await fresh_session.commit()
