"""SafeModeQueue — enqueue / approve / deny / extend / expire against real PG."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.workflow.application.safe_mode_queue import (
    INITIAL_TTL_DAYS,
    MAX_EXTENSIONS,
    SafeModeQueue,
)
from backend.workflow.infrastructure.delivery.db import (
    DeliveryBase,
    SafeModeQueueItemRow,
    SafeModeStatus,
)
from backend.workflow.infrastructure.repositories.safe_mode_queue_repository_sql import (
    SqlAlchemySafeModeQueueRepository,
)

from .._support import db_engine

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def session() -> AsyncSession:
    # In-memory SQLite by default; opt into PG by setting ``BSVIBE_DATABASE_URL``
    # to a reachable Postgres DSN (gate + teardown live in tests/_support).
    async with db_engine(DeliveryBase) as (engine, _is_pg):
        sm = async_sessionmaker(engine, expire_on_commit=False)
        async with sm() as s:
            yield s


def _as_aware(dt: datetime) -> datetime:
    """SQLite drops tz info on ``DateTime(timezone=True)`` round-trips; treat
    naive values as UTC so the comparison against ``now(tz=UTC)`` works on
    both PG and SQLite."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


async def test_enqueue_creates_pending(session: AsyncSession) -> None:
    q = SafeModeQueue(session)
    ws = uuid.uuid4()
    deliv = uuid.uuid4()
    item_id = await q.enqueue(workspace_id=ws, deliverable_id=deliv)
    await session.commit()
    row = await session.get(SafeModeQueueItemRow, item_id)
    assert row is not None
    assert row.status is SafeModeStatus.PENDING
    # ~90 days from now, allow 1s tolerance
    delta = _as_aware(row.expires_at) - datetime.now(tz=UTC)
    assert timedelta(days=INITIAL_TTL_DAYS) - delta < timedelta(seconds=5)


async def test_list_pending_isolated_by_workspace(session: AsyncSession) -> None:
    q = SafeModeQueue(session)
    ws_a = uuid.uuid4()
    ws_b = uuid.uuid4()
    await q.enqueue(workspace_id=ws_a, deliverable_id=uuid.uuid4())
    await q.enqueue(workspace_id=ws_a, deliverable_id=uuid.uuid4())
    await q.enqueue(workspace_id=ws_b, deliverable_id=uuid.uuid4())
    await session.commit()
    a = await q.list_pending(workspace_id=ws_a)
    b = await q.list_pending(workspace_id=ws_b)
    assert len(a) == 2
    assert len(b) == 1


async def test_approve_flips_to_approved(session: AsyncSession) -> None:
    q = SafeModeQueue(session)
    ws = uuid.uuid4()
    item_id = await q.enqueue(workspace_id=ws, deliverable_id=uuid.uuid4())
    ok = await q.approve(workspace_id=ws, item_id=item_id, actor_id=uuid.uuid4())
    assert ok is True
    await session.commit()
    row = await session.get(SafeModeQueueItemRow, item_id)
    assert row.status is SafeModeStatus.APPROVED
    assert row.decided_at is not None


async def test_deny_flips_to_denied(session: AsyncSession) -> None:
    q = SafeModeQueue(session)
    ws = uuid.uuid4()
    item_id = await q.enqueue(workspace_id=ws, deliverable_id=uuid.uuid4())
    ok = await q.deny(
        workspace_id=ws, item_id=item_id, actor_id=uuid.uuid4(), reason="not relevant"
    )
    assert ok is True
    await session.commit()
    row = await session.get(SafeModeQueueItemRow, item_id)
    assert row.status is SafeModeStatus.DENIED


async def test_double_approve_returns_false(session: AsyncSession) -> None:
    q = SafeModeQueue(session)
    ws = uuid.uuid4()
    item_id = await q.enqueue(workspace_id=ws, deliverable_id=uuid.uuid4())
    assert await q.approve(workspace_id=ws, item_id=item_id, actor_id=uuid.uuid4())
    # Already approved; second call is a no-op.
    assert not await q.approve(workspace_id=ws, item_id=item_id, actor_id=uuid.uuid4())


async def test_cross_workspace_approve_rejected(session: AsyncSession) -> None:
    q = SafeModeQueue(session)
    ws_a = uuid.uuid4()
    ws_b = uuid.uuid4()
    item_id = await q.enqueue(workspace_id=ws_a, deliverable_id=uuid.uuid4())
    ok = await q.approve(workspace_id=ws_b, item_id=item_id, actor_id=uuid.uuid4())
    assert ok is False


async def test_extend_caps_at_max(session: AsyncSession) -> None:
    q = SafeModeQueue(session)
    ws = uuid.uuid4()
    item_id = await q.enqueue(workspace_id=ws, deliverable_id=uuid.uuid4())
    for i in range(MAX_EXTENSIONS):
        ok = await q.extend(workspace_id=ws, item_id=item_id)
        assert ok, f"extension #{i + 1} failed"
    # MAX_EXTENSIONS exceeded
    assert not await q.extend(workspace_id=ws, item_id=item_id)
    row = await session.get(SafeModeQueueItemRow, item_id)
    assert row.extension_count == MAX_EXTENSIONS


async def test_overdue_rows_are_found_and_flipped_per_row(session: AsyncSession) -> None:
    """살아 있는 만료 경로 그대로 — ``list_due_expired`` 로 찾고 per-row 로 뒤집는다.

    앞 세대는 ``SafeModeQueue.expire`` (벌크)를 불렀는데 그건 프로덕션 호출자가
    0인 추월당한 변형이었다. 커버리지("기한 지난 행이 EXPIRED 가 된다")는 그대로
    두고 축만 ``SafeModeExpirySweepRunner`` 가 실제로 도는 경로로 옮겼다.
    """
    q = SafeModeQueue(session)
    ws = uuid.uuid4()
    # Hand-craft an overdue row to skip the 90d wait.
    row = SafeModeQueueItemRow(
        id=uuid.uuid4(),
        workspace_id=ws,
        deliverable_id=uuid.uuid4(),
        status=SafeModeStatus.PENDING,
        expires_at=datetime.now(tz=UTC) - timedelta(days=1),
        extension_count=0,
        created_at=datetime.now(tz=UTC) - timedelta(days=91),
    )
    session.add(row)
    await session.commit()

    due = await SqlAlchemySafeModeQueueRepository(session).list_due_expired()
    assert [r.id for r in due] == [row.id]
    assert await q.mark_expired(workspace_id=ws, item_id=row.id) is True
    await session.commit()
    fresh = await session.get(SafeModeQueueItemRow, row.id)
    assert fresh.status is SafeModeStatus.EXPIRED


async def test_mark_expired_is_workspace_scoped(session: AsyncSession) -> None:
    """다른 워크스페이스의 행은 만료시킬 수 없다 — 테넌트 격리.

    이 커버리지는 원래 ``repo.mark_expired_bulk`` 의 워크스페이스 스코핑을
    검증하던 것이다. 그 벌크 변형은 프로덕션 호출자가 0이라 지웠지만,
    **격리 성질은 지울 수 없다** — 살아 있는 per-row 경로가 같은 것을 강제하므로
    (``row.workspace_id != workspace_id`` → ``False``) 그쪽으로 옮겼다.
    """
    q = SafeModeQueue(session)
    mine, theirs = uuid.uuid4(), uuid.uuid4()
    row = SafeModeQueueItemRow(
        id=uuid.uuid4(),
        workspace_id=theirs,
        deliverable_id=uuid.uuid4(),
        status=SafeModeStatus.PENDING,
        expires_at=datetime.now(tz=UTC) - timedelta(days=1),
        extension_count=0,
        created_at=datetime.now(tz=UTC) - timedelta(days=91),
    )
    session.add(row)
    await session.commit()

    # 남의 워크스페이스 id 로는 못 뒤집는다.
    assert await q.mark_expired(workspace_id=mine, item_id=row.id) is False
    await session.commit()
    fresh = await session.get(SafeModeQueueItemRow, row.id)
    assert fresh.status is SafeModeStatus.PENDING

    # 양성 대조군 — 주인은 뒤집을 수 있다(위 False 가 "언제나 False" 가 아님).
    assert await q.mark_expired(workspace_id=theirs, item_id=row.id) is True
    await session.commit()
    fresh = await session.get(SafeModeQueueItemRow, row.id)
    assert fresh.status is SafeModeStatus.EXPIRED
