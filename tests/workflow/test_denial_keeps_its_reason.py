"""거절은 자기 이유를 간직한다 (트랙 A-1a).

prod 실측 (2026-08-16): 형님이 2개월간 Safe Mode 로 **128번 판단**했다 —
거절 91 · 승인 37 · 대기 20. **그중 지식에 남은 것은 0건**이고, 거절 사유는
저장조차 되지 않았다:

    # safe_mode_queue.py
    del actor_id, reason  # surface for audit hook

그 "audit hook" 은 `audit_events` 테이블이고 **prod 에서 0행**이다 — 한 번도
발화한 적이 없다. 살아 있는 `audit_outbox`(4,967행)에도 safe-mode 흔적은 0건.
테이블에 사유 컬럼도 없다. **91번의 "아니오"가 그 이유째로 사라졌다.**

ratchet(redesign §5)은 형님 교정이 누적되어야 돌고, §6 의 포착 모먼트 표는
**Review (approve / reject) → 품질 기준** 을 명시적으로 꼽는다. 거절이야말로
가장 값어치 있는 교정인데(agent 가 위반한 표준) 그것이 버려지고 있었다.

이 lift 는 **보존만** 한다. 지식화(negative pattern 포착)는 A-1b, 승인 지표는
A-1c. 사유가 남지 않으면 그 둘 다 의미가 없으므로 이것이 먼저다.

⚠️ 과거 91건은 복구 불가다. 오늘 이후의 거절부터 남는다.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from backend.workflow.application.safe_mode_queue import SafeModeQueue
from backend.workflow.infrastructure.delivery.db import (
    SafeModeQueueItemRow,
    SafeModeStatus,
)
from tests._support import memory_session

pytestmark = pytest.mark.asyncio


async def _queued(session, workspace_id: uuid.UUID) -> SafeModeQueueItemRow:
    row = SafeModeQueueItemRow(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        deliverable_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        status=SafeModeStatus.PENDING,
        expires_at=datetime.now(tz=UTC) + timedelta(days=30),
    )
    session.add(row)
    await session.flush()
    return row


async def test_a_denial_stores_its_reason() -> None:
    """형님이 왜 거절했는지가 행에 남는다. 이것이 없으면 A-1b 가 읽을 것이 없다."""
    async with memory_session() as session:
        ws = uuid.uuid4()
        item = await _queued(session, ws)
        actor = uuid.uuid4()

        ok = await SafeModeQueue(session).deny(
            workspace_id=ws,
            item_id=item.id,
            actor_id=actor,
            reason="이 접근은 실제 스택을 안 띄우고 백엔드를 가로채서 검증 가치가 없다",
        )

        assert ok
        row = (await session.execute(select(SafeModeQueueItemRow))).scalar_one()
        assert row.status is SafeModeStatus.DENIED
        assert row.deny_reason == (
            "이 접근은 실제 스택을 안 띄우고 백엔드를 가로채서 검증 가치가 없다"
        )


async def test_a_denial_records_who_decided() -> None:
    """행은 ``decided_at`` 은 갖고 있었지만 **누가** 는 갖고 있지 않았다.
    A-1c(감쇠 지표)가 사람의 판단과 시스템 정리를 갈라야 한다."""
    async with memory_session() as session:
        ws = uuid.uuid4()
        item = await _queued(session, ws)
        actor = uuid.uuid4()

        await SafeModeQueue(session).deny(
            workspace_id=ws, item_id=item.id, actor_id=actor, reason="사유"
        )

        row = (await session.execute(select(SafeModeQueueItemRow))).scalar_one()
        assert row.decided_by == actor
        assert row.decided_at is not None


async def test_a_reasonless_denial_stores_no_reason() -> None:
    """이유 없는 거절은 아무것도 가르치지 않는다 — 빈 문자열을 지식으로 오인하지
    않도록 ``None`` 으로 남긴다 (``checkpoint_resolution`` 의 기존 규율과 동일)."""
    async with memory_session() as session:
        ws = uuid.uuid4()
        item = await _queued(session, ws)

        await SafeModeQueue(session).deny(
            workspace_id=ws, item_id=item.id, actor_id=uuid.uuid4(), reason="   "
        )

        row = (await session.execute(select(SafeModeQueueItemRow))).scalar_one()
        assert row.status is SafeModeStatus.DENIED
        assert row.deny_reason is None


async def test_an_approval_records_who_but_no_reason() -> None:
    """승인도 사람의 판단이므로 행위자는 남는다. 사유는 승인에 없다."""
    async with memory_session() as session:
        ws = uuid.uuid4()
        item = await _queued(session, ws)
        actor = uuid.uuid4()

        await SafeModeQueue(session).approve(workspace_id=ws, item_id=item.id, actor_id=actor)

        row = (await session.execute(select(SafeModeQueueItemRow))).scalar_one()
        assert row.status is SafeModeStatus.APPROVED
        assert row.decided_by == actor
        assert row.deny_reason is None


async def test_denying_a_non_pending_item_changes_nothing() -> None:
    """상태 전이 가드는 그대로 — 사유 보존이 전이 규칙을 무르게 하면 안 된다."""
    async with memory_session() as session:
        ws = uuid.uuid4()
        item = await _queued(session, ws)
        queue = SafeModeQueue(session)
        await queue.approve(workspace_id=ws, item_id=item.id, actor_id=uuid.uuid4())

        ok = await queue.deny(
            workspace_id=ws, item_id=item.id, actor_id=uuid.uuid4(), reason="뒤늦은 거절"
        )

        assert ok is False
        row = (await session.execute(select(SafeModeQueueItemRow))).scalar_one()
        assert row.status is SafeModeStatus.APPROVED
        assert row.deny_reason is None


async def test_another_workspace_cannot_deny_this_item() -> None:
    """워크스페이스 스코프 — 새 필드가 새 구멍이 되면 안 된다."""
    async with memory_session() as session:
        ws = uuid.uuid4()
        item = await _queued(session, ws)

        ok = await SafeModeQueue(session).deny(
            workspace_id=uuid.uuid4(),
            item_id=item.id,
            actor_id=uuid.uuid4(),
            reason="남의 워크스페이스",
        )

        assert ok is False
        row = (await session.execute(select(SafeModeQueueItemRow))).scalar_one()
        assert row.status is SafeModeStatus.PENDING
        assert row.deny_reason is None
