"""결정 경로의 discard 도 그 런의 Safe Mode 대기 항목을 정리해야 한다.

폐기는 두 문으로 들어온다. 하나는 정리하고 하나는 안 한다:

======================================  ===================  ===================
                                        ``runs_discard``     결정 경로 discard
                                        (``cancel_run``)     (``_discard_decision_run``)
======================================  ===================  ===================
런 종료                                 ✅                   ✅
워크트리 정리                           ✅                   ✅
**pending Safe Mode 항목 정리**         ✅                   ❌
======================================  ===================  ===================

``cancel_run`` 의 독스트링이 이 결함의 이름까지 적어놨다 — *"cancelling the run
alone leaves its Summary 확인 필요 card up forever (**orphaned-half**)"*. 그 교훈이
결정 경로로는 옮겨가지 않았다.

**prod 실측 (2026-09-09, `e8775e7`)**

결정 경로로 폐기된 런 **3건** 중 Safe Mode 항목을 가진 것은 **1건**(`0093fce6`)이고,
**그 1건이 그대로 고아가 됐다.** 형님이 **2분 27초 뒤 손으로** 지웠고(item
``91081d3d``), 그 사유에 결함을 직접 적어놨다:

    "소속 런 0093fce6 이 폐기(cancelled)됐다 … ⚠️참고: checkpoints_resolve(discard)
     경로는 runs_discard 와 달리 partial 의 Safe Mode 항목을 정리하지 않는다
     — 그래서 손으로 지운다."

나머지 2건은 애초에 정리할 항목이 0이라 반증이 아니다 — **기회 1회 중 1회 발생**이다.

**가르치지 않는다.** 재사용하는 ``_resolve_pending_safe_mode_items`` 는 행의
status/decided_at 만 직접 쓰고 ``SafeModeQueue.deny`` 를 타지 않는다. 자동 정리는
형님의 판단이 아니므로 negative knowledge 가 되면 안 된다 — PR #902 (``DenyKind``)
가 세운 규율과 같은 문장이다.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.workflow.application._checkpoint_shared import ACTION_DISCARD
from backend.workflow.application.checkpoint_resolution import resolve_checkpoint
from backend.workflow.infrastructure.db import (
    Decision,
    DecisionStatus,
    ExecutionRun,
    ExecutionRunActivity,
    RunStatus,
)
from backend.workflow.infrastructure.delivery.db import SafeModeQueueItemRow, SafeModeStatus

from .._support import db_engine

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def sf():
    async with db_engine() as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
def workspace_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def founder_id() -> uuid.UUID:
    return uuid.uuid4()


async def _seed(sf, workspace_id: uuid.UUID, *, items: int = 1):
    """REVIEW_READY 런 + pending Decision + ``items`` 개의 pending Safe Mode 항목."""
    run_id, decision_id = uuid.uuid4(), uuid.uuid4()
    item_ids: list[uuid.UUID] = []
    async with sf() as s:
        s.add(
            ExecutionRun(
                id=run_id,
                workspace_id=workspace_id,
                status=RunStatus.REVIEW_READY,
                payload={"intent_text": "예산 소진 분기를 고쳐라"},
                created_at=datetime.now(tz=UTC),
                updated_at=datetime.now(tz=UTC),
            )
        )
        await s.flush()
        s.add(
            Decision(
                id=decision_id,
                run_id=run_id,
                workspace_id=workspace_id,
                decision="verification_failed",
                payload={"reason": "round_budget_exhausted"},
                status=DecisionStatus.PENDING,
            )
        )
        for _ in range(items):
            item_id = uuid.uuid4()
            item_ids.append(item_id)
            s.add(
                SafeModeQueueItemRow(
                    id=item_id,
                    workspace_id=workspace_id,
                    deliverable_id=uuid.uuid4(),
                    run_id=run_id,
                    status=SafeModeStatus.PENDING,
                    expires_at=datetime.now(tz=UTC) + timedelta(days=30),
                )
            )
        await s.commit()
    return run_id, decision_id, item_ids


async def _items(sf, run_id: uuid.UUID) -> list[SafeModeQueueItemRow]:
    async with sf() as s:
        rows = (
            (
                await s.execute(
                    select(SafeModeQueueItemRow).where(SafeModeQueueItemRow.run_id == run_id)
                )
            )
            .scalars()
            .all()
        )
    return list(rows)


async def test_decision_discard_resolves_the_runs_pending_safe_mode_item(
    sf, workspace_id, founder_id
) -> None:
    """prod `0093fce6` 이 겪은 그 고아. 형님이 손으로 지워야 했던 것."""
    run_id, decision_id, _ = await _seed(sf, workspace_id)

    async with sf() as s:
        await resolve_checkpoint(
            s,
            workspace_id=workspace_id,
            checkpoint_id=decision_id,
            answer="",
            action_key=ACTION_DISCARD,
            reason="예산 소진으로 두 번 실패했고 재시도가 같은 예산에 갇혀 무의미했다",
            actor_id=founder_id,
        )
        await s.commit()

    rows = await _items(sf, run_id)
    assert len(rows) == 1
    assert rows[0].status is SafeModeStatus.DENIED, (
        "폐기된 런의 승인 카드가 Decisions 큐에 영원히 남는다 (orphaned-half)"
    )
    assert rows[0].decided_at is not None


async def test_decision_discard_resolves_every_pending_item_of_the_run(
    sf, workspace_id, founder_id
) -> None:
    """멀티 아티팩트 런은 항목을 여러 개 남긴다 — 하나만 치우면 나머지가 고아다."""
    run_id, decision_id, _ = await _seed(sf, workspace_id, items=3)

    async with sf() as s:
        await resolve_checkpoint(
            s,
            workspace_id=workspace_id,
            checkpoint_id=decision_id,
            answer="",
            action_key=ACTION_DISCARD,
            reason="접근이 틀렸다",
            actor_id=founder_id,
        )
        await s.commit()

    rows = await _items(sf, run_id)
    assert len(rows) == 3
    assert [r.status for r in rows] == [SafeModeStatus.DENIED] * 3


async def test_the_automatic_cleanup_teaches_nothing(sf, workspace_id, founder_id) -> None:
    """자동 정리는 형님의 판단이 아니다 — negative knowledge 가 되면 안 된다.

    형님이 쓴 사유(``reason``)는 **discard 자체**의 지식 경로가 이미 처리한다.
    이 테스트가 막는 것은 *정리된 항목 개수만큼* negative pattern 이 더 생기는 것이다
    (PR #902 가 Safe Mode deny 에서 세운 것과 같은 규율).
    """
    run_id, decision_id, _ = await _seed(sf, workspace_id, items=3)

    async with sf() as s:
        await resolve_checkpoint(
            s,
            workspace_id=workspace_id,
            checkpoint_id=decision_id,
            answer="",
            action_key=ACTION_DISCARD,
            reason="접근이 틀렸다",
            actor_id=founder_id,
        )
        await s.commit()

    async with sf() as s:
        rows = (
            (
                await s.execute(
                    select(ExecutionRunActivity).where(ExecutionRunActivity.run_id == run_id)
                )
            )
            .scalars()
            .all()
        )
    negatives = [r for r in rows if (r.payload or {}).get("kind") == "negative_pattern"]
    assert len(negatives) <= 1, (
        f"정리한 항목 수만큼 지식이 늘었다 ({len(negatives)}건) — 자동 정리가 가르치고 있다"
    )


async def test_a_run_without_pending_items_discards_cleanly(sf, workspace_id, founder_id) -> None:
    """정리할 것이 없어도 폐기는 성공해야 한다 — prod 3건 중 2건이 이 경우였다."""
    run_id, decision_id, _ = await _seed(sf, workspace_id, items=0)

    async with sf() as s:
        outcome = await resolve_checkpoint(
            s,
            workspace_id=workspace_id,
            checkpoint_id=decision_id,
            answer="",
            action_key=ACTION_DISCARD,
            reason="필요 없어졌다",
            actor_id=founder_id,
        )
        await s.commit()

    assert outcome is not None
    assert await _items(sf, run_id) == []
    async with sf() as s:
        run = await s.get(ExecutionRun, run_id)
        assert run is not None
        assert run.status is RunStatus.CANCELLED
