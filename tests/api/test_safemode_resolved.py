"""/api/v1/safemode/resolved — the founder's resolved Safe-Mode deliveries.

The Decisions "Resolved" tab folds resolved Safe-Mode deliveries in alongside
the canon decision log + resolved checkpoints. This endpoint is the
delivery-side source: every Safe-Mode queue item that is no longer pending
(``approved`` / ``denied`` / ``expired``), newest-decided first, scoped to the
caller's workspace.

SQLite by default; real Postgres when the env selects it (mirrors
``tests/api/test_run_detail.py``).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.api.deps import get_current_user, get_db_session, get_workspace_id
from backend.api.main import create_app
from backend.workflow.infrastructure.delivery.db import (
    DeliveryBase,
    SafeModeQueueItemRow,
    SafeModeStatus,
)

from .._support import db_engine, fake_current_user

pytestmark = pytest.mark.asyncio

_NOW = datetime(2026, 5, 25, 12, 0, 0, tzinfo=UTC)


@pytest_asyncio.fixture
async def db():
    async with db_engine(DeliveryBase) as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
def workspace_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest_asyncio.fixture
async def client(db, workspace_id: uuid.UUID):
    app = create_app()

    def _ws() -> uuid.UUID:
        return workspace_id

    async def _session():
        async with db() as s:
            yield s

    app.dependency_overrides[get_current_user] = fake_current_user()
    app.dependency_overrides[get_workspace_id] = _ws
    app.dependency_overrides[get_db_session] = _session

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _seed_item(
    db,
    *,
    ws: uuid.UUID,
    status: SafeModeStatus,
    decided_at: datetime | None,
    created_at: datetime,
) -> uuid.UUID:
    item_id = uuid.uuid4()
    async with db() as s:
        s.add(
            SafeModeQueueItemRow(
                id=item_id,
                workspace_id=ws,
                deliverable_id=uuid.uuid4(),
                status=status,
                expires_at=created_at + timedelta(days=1),
                created_at=created_at,
                decided_at=decided_at,
            )
        )
        await s.commit()
    return item_id


async def test_resolved_lists_decided_items_newest_first(client, db, workspace_id) -> None:
    """approved + denied + expired items come back, most-recently-decided first;
    a still-pending item is excluded (it belongs on the Pending tab)."""
    older = await _seed_item(
        db,
        ws=workspace_id,
        status=SafeModeStatus.APPROVED,
        decided_at=_NOW - timedelta(hours=2),
        created_at=_NOW - timedelta(hours=3),
    )
    newer = await _seed_item(
        db,
        ws=workspace_id,
        status=SafeModeStatus.DENIED,
        decided_at=_NOW - timedelta(minutes=10),
        created_at=_NOW - timedelta(hours=1),
    )
    await _seed_item(
        db,
        ws=workspace_id,
        status=SafeModeStatus.PENDING,
        decided_at=None,
        created_at=_NOW,
    )

    r = await client.get("/api/v1/safemode/resolved")
    assert r.status_code == 200, r.text
    rows = r.json()
    assert [row["id"] for row in rows] == [str(newer), str(older)]
    assert rows[0]["status"] == "denied"
    assert rows[1]["status"] == "approved"
    assert rows[0]["decided_at"]


async def test_resolved_empty_when_only_pending(client, db, workspace_id) -> None:
    await _seed_item(
        db,
        ws=workspace_id,
        status=SafeModeStatus.PENDING,
        decided_at=None,
        created_at=_NOW,
    )
    r = await client.get("/api/v1/safemode/resolved")
    assert r.status_code == 200, r.text
    assert r.json() == []


async def test_resolved_workspace_isolation(client, db, workspace_id) -> None:
    """A resolved item in another workspace is never enumerated for the caller."""
    other = uuid.uuid4()
    await _seed_item(
        db,
        ws=other,
        status=SafeModeStatus.APPROVED,
        decided_at=_NOW,
        created_at=_NOW,
    )
    r = await client.get("/api/v1/safemode/resolved")
    assert r.status_code == 200, r.text
    assert r.json() == []
