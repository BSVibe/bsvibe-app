"""거절은 두 가지 행위다 — 하나만 가르쳐야 한다.

``deny`` 는 오랫동안 동사 하나로 서로 다른 두 행위를 처리했다:

1. **접근 거절** — *"답변이 코드 근거 없는 추론이다"*. 다음 런이 같은 접근을
   반복하지 않도록 가르쳐야 한다. 이것이 A-1b/A-1c 가 만들어진 이유다.
2. **큐 정리** — *"같은 런의 중간 스냅샷 — 최종 산출물에 포함됐다. **내용 문제
   아님**"*. 배송할 것이 없어 큐에서 치우는 것뿐이고, 가르칠 판단이 아니다.

``_record_rejection_knowledge`` 와 ``_reopen_run_with_the_reason`` 은 **사유 텍스트가
비어 있지 않기만 하면** 발화했다. 그래서 큐 정리 문장이 그대로 negative knowledge 가
됐고, ``NegativePatternRetriever`` 를 타고 다음 런의 **판사 기준**이 됐다.

**prod 실측 (2026-09-08, `c852786`)**

* vault 의 ``negative_pattern`` 노트 **25개 중 11개가 큐 정리 문장**
* 판사 계약 **58건**이 ``Avoid (prior rejection)`` 을 싣고, 그중 **10건이 큐 정리
  문장**을 기준으로 실었다 — **2건은 "내용 문제 아님" 이라는 문장 자체**를 실었다.
  형님이 *"내용 문제가 아니다"* 라고 쓴 것이 다음 런을 **내용으로 심판하는 기준**이 됐다
* 가장 최근 런 ``1515f44e`` (09-08 01:45) 이 그중 하나다
* 재개 경로도 같은 결함이다 — ``_reopen_run_with_the_reason`` 은 prod 에서 **6번
  발화했고 6번 다 큐 정리 거절**이었다. 큐 정리 문장이 *"Approve delivering this
  run's result?"* 의 **형님 답변**인 척 에이전트 맥락에 접혀 들어갔다

**왜 축을 지우지 않고 더했나.** 형님 철학은 *더하기 전에 지워라* 다. 그래서 먼저
"Safe Mode 교육을 통째로 지운다" 를 쟀다 — negative pattern **25건 중 22건이 Safe
Mode 출신**이고 진짜 교육(*"답변이 코드 근거 없는 추론이다"*)도 그쪽에 있다. 지우면
래칫이 죽는다. 축은 그래서 정당하다.

**선례.** 커넥터는 이미 같은 이유로 가르치지 않는다 — 폰의 거절 탭이 ``declined via
telegram`` 을 사유인 척 넘기면 그것이 승격된다는 것을 알고 ``reason=""`` 을 넘긴다
(``backend/connectors/approval_callback.py``). **그 교훈이 형님이 직접 타이핑한
사유에는 옮겨가지 않았다.**
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from backend.workflow.application.checkpoint_resolution import NEGATIVE_PATTERN_SETTLE_KIND
from backend.workflow.application.safe_mode_queue import DenyKind, SafeModeQueue
from backend.workflow.infrastructure.db import ExecutionRun, ExecutionRunActivity, RunStatus
from backend.workflow.infrastructure.delivery.db import SafeModeQueueItemRow, SafeModeStatus
from tests._support import memory_session

pytestmark = pytest.mark.asyncio

#: 형님이 prod 에서 실제로 쓴 큐 정리 문장 (item ``76dea860``, 2026-09-08).
#: 지어낸 문자열로는 이 결함을 재지 못한다 — 이 문장이 판사 기준이 됐다.
CLEANUP_REASON = (
    "같은 런의 중간 스냅샷 — 최종 산출물 bb9b9664 에 포함됐다. "
    "둘 다 승인하면 동일 내용으로 PR 이 둘 열린다. 내용 문제 아님."
)

#: 같은 큐의 진짜 접근 거절 (2026-08-19). 이것은 반드시 계속 가르쳐야 한다.
JUDGMENT_REASON = (
    "답변이 코드 근거 없는 추론이다. 지시는 코드로 확인하고 근거 파일:라인을 대라 였다."
)


async def _run_and_item(session, *, status: RunStatus = RunStatus.RUNNING):
    ws = uuid.uuid4()
    run = ExecutionRun(
        id=uuid.uuid4(),
        workspace_id=ws,
        status=status,
        payload={"intent_text": "브라우저 검증을 붙여라"},
    )
    session.add(run)
    await session.flush()
    item = SafeModeQueueItemRow(
        id=uuid.uuid4(),
        workspace_id=ws,
        deliverable_id=uuid.uuid4(),
        run_id=run.id,
        status=SafeModeStatus.PENDING,
        expires_at=datetime.now(tz=UTC) + timedelta(days=30),
    )
    session.add(item)
    await session.flush()
    return ws, run, item


async def _negatives(session) -> list[ExecutionRunActivity]:
    rows = (await session.execute(select(ExecutionRunActivity))).scalars().all()
    return [r for r in rows if (r.payload or {}).get("kind") == NEGATIVE_PATTERN_SETTLE_KIND]


# ---------------------------------------------------------------------------
# 큐 정리 — 상태는 바뀌고, 가르치지도 재개하지도 않는다
# ---------------------------------------------------------------------------


async def test_queue_cleanup_denial_teaches_nothing() -> None:
    """prod 의 그 문장 그대로. 판사 기준이 되면 안 된다."""
    async with memory_session() as session:
        ws, _run, item = await _run_and_item(session)

        ok = await SafeModeQueue(session).deny(
            workspace_id=ws,
            item_id=item.id,
            actor_id=uuid.uuid4(),
            reason=CLEANUP_REASON,
            kind=DenyKind.QUEUE_CLEANUP,
        )

        assert ok is True
        assert await _negatives(session) == []


async def test_queue_cleanup_denial_does_not_reopen_the_run() -> None:
    """prod 재개 6건이 전부 이 경우였다 — 큐 정리 문장이 형님 답변인 척 접혔다."""
    async with memory_session() as session:
        ws, run, item = await _run_and_item(session, status=RunStatus.REVIEW_READY)

        await SafeModeQueue(session).deny(
            workspace_id=ws,
            item_id=item.id,
            actor_id=uuid.uuid4(),
            reason=CLEANUP_REASON,
            kind=DenyKind.QUEUE_CLEANUP,
        )

        await session.refresh(run)
        assert run.status is RunStatus.REVIEW_READY
        assert (run.payload or {}).get("resolved_decisions") in (None, [])


async def test_queue_cleanup_denial_still_records_the_reason_on_the_row() -> None:
    """가르치지 않는 것과 기록하지 않는 것은 다르다 — 감사 흔적은 남아야 한다."""
    async with memory_session() as session:
        ws, _run, item = await _run_and_item(session)

        await SafeModeQueue(session).deny(
            workspace_id=ws,
            item_id=item.id,
            actor_id=uuid.uuid4(),
            reason=CLEANUP_REASON,
            kind=DenyKind.QUEUE_CLEANUP,
        )

        await session.refresh(item)
        assert item.status is SafeModeStatus.DENIED
        assert item.deny_reason == CLEANUP_REASON


# ---------------------------------------------------------------------------
# 접근 거절 — A-1b / A-1c 회귀 가드. 이 축이 교육을 죽이면 안 된다.
# ---------------------------------------------------------------------------


async def test_rejected_approach_still_teaches() -> None:
    async with memory_session() as session:
        ws, run, item = await _run_and_item(session)

        await SafeModeQueue(session).deny(
            workspace_id=ws,
            item_id=item.id,
            actor_id=uuid.uuid4(),
            reason=JUDGMENT_REASON,
            kind=DenyKind.REJECTED_APPROACH,
        )

        rows = await _negatives(session)
        assert len(rows) == 1
        assert rows[0].run_id == run.id
        assert rows[0].payload["reason"] == JUDGMENT_REASON


async def test_rejected_approach_still_reopens_the_run() -> None:
    async with memory_session() as session:
        ws, run, item = await _run_and_item(session, status=RunStatus.REVIEW_READY)

        await SafeModeQueue(session).deny(
            workspace_id=ws,
            item_id=item.id,
            actor_id=uuid.uuid4(),
            reason=JUDGMENT_REASON,
            kind=DenyKind.REJECTED_APPROACH,
        )

        await session.refresh(run)
        assert run.status is RunStatus.OPEN
        folded = (run.payload or {}).get("resolved_decisions") or []
        assert len(folded) == 1
        assert JUDGMENT_REASON in folded[0]["answer"]


async def test_a_reasonless_rejected_approach_still_teaches_nothing() -> None:
    """사유 게이트는 그대로다 — 축이 생겨도 빈 사유는 여전히 아무것도 안 가르친다."""
    async with memory_session() as session:
        ws, _run, item = await _run_and_item(session)

        await SafeModeQueue(session).deny(
            workspace_id=ws,
            item_id=item.id,
            actor_id=uuid.uuid4(),
            reason="   ",
            kind=DenyKind.REJECTED_APPROACH,
        )

        assert await _negatives(session) == []


# ---------------------------------------------------------------------------
# 축은 호출자가 반드시 말해야 한다 — 조용히 틀린 기본값이 없어야 한다
# ---------------------------------------------------------------------------


async def test_deny_requires_the_caller_to_name_the_act() -> None:
    """기본값을 두면 둘 중 하나가 조용히 틀린다.

    ``QUEUE_CLEANUP`` 기본이면 진짜 거절이 조용히 안 가르치고, ``REJECTED_APPROACH``
    기본이면 오늘의 오염이 그대로 계속된다. 둘 다 침묵하므로 **필수**로 만든다.
    """
    async with memory_session() as session:
        ws, _run, item = await _run_and_item(session)

        with pytest.raises(TypeError):
            await SafeModeQueue(session).deny(  # type: ignore[call-arg]
                workspace_id=ws,
                item_id=item.id,
                actor_id=uuid.uuid4(),
                reason=JUDGMENT_REASON,
            )
