"""거절이 다음 런을 가르친다 (트랙 A-1b).

A-1a 가 사유를 **보존**했다. 이 lift 는 그것을 **지식으로** 만든다.

prod 실측 (2026-08-16): Safe Mode 거절 **91건**, 서로 다른 런 **67개**, 전부
유효한 ``run_id`` 를 갖는다. 그런데 그중 지식이 된 것은 0건 —
``NEGATIVE_PATTERN_SETTLE_KIND`` 행이 prod 에 **0행**이다. 기존 negative-pattern
경로는 ``checkpoint_resolution`` 의 discard 분기에만 있고, 실제 거절은 Safe Mode
로 일어나기 때문이다.

**새 파이프라인을 만들지 않는다.** ``checkpoint_resolution`` 이 이미 쓰는
seam(``settle`` 활동 + ``settle_run_context`` + SettleWorker + NegativePatternRetriever)
을 그대로 재사용한다 — 끊긴 링크 하나를 잇는 일이다.

폰의 거절 버튼은 사유를 받지 않는다. 예전에는 커넥터가 ``declined via telegram``
을 사유인 척 넘겼는데, 그것이 승격되면 모든 신호에 "avoid: declined via telegram"
이 딸려온다. 큐가 문자열을 감별하는 대신 **커넥터가 지어내지 않도록** 고쳤다 —
그 단언은 ``tests/connectors/test_approval_callback.py`` 에 있다.

**매칭 근거는 형님이 직접 쓴 텍스트뿐이다.** NegativePatternRetriever 는
``reason`` / ``question`` / ``intent_text`` 로만 겹침을 판정한다 —
*"never an LLM-generated body"*. 그래서 딜리버러블 요약(LLM 생성물)은 쓰지 않는다.
이 제약이 `dd2bd3a3`(무관한 기준이 정상 작업을 죽인 사건) 류의 재발을 구조적으로 막는다.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from backend.workflow.application.checkpoint_resolution import NEGATIVE_PATTERN_SETTLE_KIND
from backend.workflow.application.safe_mode_queue import SafeModeQueue
from backend.workflow.infrastructure.db import ExecutionRun, ExecutionRunActivity, RunStatus
from backend.workflow.infrastructure.delivery.db import SafeModeQueueItemRow, SafeModeStatus
from tests._support import memory_session

pytestmark = pytest.mark.asyncio


async def _run_and_item(session, *, intent: str = "브라우저 검증을 붙여라"):
    ws = uuid.uuid4()
    run = ExecutionRun(
        id=uuid.uuid4(),
        workspace_id=ws,
        status=RunStatus.RUNNING,
        payload={"intent_text": intent},
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


async def test_a_denial_with_a_reason_becomes_negative_knowledge() -> None:
    async with memory_session() as session:
        ws, run, item = await _run_and_item(session)

        await SafeModeQueue(session).deny(
            workspace_id=ws,
            item_id=item.id,
            actor_id=uuid.uuid4(),
            reason="백엔드를 가로채면 실제 스택 위에서 깨지는 것을 정의상 못 잡는다",
        )

        rows = await _negatives(session)
        assert len(rows) == 1
        payload = rows[0].payload
        assert rows[0].run_id == run.id
        assert rows[0].workspace_id == ws
        assert payload["reason"] == (
            "백엔드를 가로채면 실제 스택 위에서 깨지는 것을 정의상 못 잡는다"
        )
        # 클러스터링 컨텍스트 — 형님의 Direction 이 함께 실려야 검색이 잡는다.
        assert payload["intent_text"] == "브라우저 검증을 붙여라"
        # 거절은 정직한 형님 신호이지 검증된 코드가 아니다.
        assert payload["verified"] is False


async def test_a_reasonless_denial_teaches_nothing() -> None:
    """``checkpoint_resolution`` 의 기존 규율 그대로 — 사유 없는 거절은 행을 안 쓴다."""
    async with memory_session() as session:
        ws, _run, item = await _run_and_item(session)

        await SafeModeQueue(session).deny(
            workspace_id=ws, item_id=item.id, actor_id=uuid.uuid4(), reason="   "
        )

        assert await _negatives(session) == []


async def test_an_approval_teaches_nothing() -> None:
    """승인은 negative pattern 이 아니다. (그리고 승인을 지식으로 학습하면
    '이 정도면 통과'를 배워 기준이 무뎌진다 — ratchet 은 실수를 줄이는
    일방향이지 기준을 무르게 하는 장치가 아니다.)"""
    async with memory_session() as session:
        ws, _run, item = await _run_and_item(session)

        await SafeModeQueue(session).approve(
            workspace_id=ws, item_id=item.id, actor_id=uuid.uuid4()
        )

        assert await _negatives(session) == []


async def test_a_refused_transition_teaches_nothing() -> None:
    """이미 승인된 항목에 대한 뒤늦은 거절은 상태도 안 바뀌고 지식도 안 남는다."""
    async with memory_session() as session:
        ws, _run, item = await _run_and_item(session)
        queue = SafeModeQueue(session)
        await queue.approve(workspace_id=ws, item_id=item.id, actor_id=uuid.uuid4())

        ok = await queue.deny(
            workspace_id=ws, item_id=item.id, actor_id=uuid.uuid4(), reason="뒤늦은 거절"
        )

        assert ok is False
        assert await _negatives(session) == []


async def test_an_item_without_a_run_still_denies_cleanly() -> None:
    """``run_id`` 는 nullable 이다. 지식화는 못 하지만 거절 자체는 성공해야 한다 —
    가시성 장치가 형님의 판단을 막으면 본말전도다."""
    async with memory_session() as session:
        ws = uuid.uuid4()
        item = SafeModeQueueItemRow(
            id=uuid.uuid4(),
            workspace_id=ws,
            deliverable_id=uuid.uuid4(),
            run_id=None,
            status=SafeModeStatus.PENDING,
            expires_at=datetime.now(tz=UTC) + timedelta(days=30),
        )
        session.add(item)
        await session.flush()

        ok = await SafeModeQueue(session).deny(
            workspace_id=ws, item_id=item.id, actor_id=uuid.uuid4(), reason="사유 있음"
        )

        assert ok is True
        assert await _negatives(session) == []


# ---------------------------------------------------------------------------
# 거절이 **그 런에** 도달한다 (트랙 A-1c)
#
# A-1b 는 거절을 MIRAE 런을 위한 지식으로 만들었다. 정작 거절당한 런 자신은
# 아무것도 못 듣는다 — Safe Mode 의 ``deny`` 는 런을 건드리지 않기 때문이다.
# Decision 경로에는 그 링크가 이미 있다(``checkpoint_resolution``: RUNNING →
# OPEN + 답변을 맥락에 접어 넣음). 실제 거절은 전부 Safe Mode 로 일어난다.
#
# 새 서브시스템이 아니라 링크 하나다: 기계는 이미 다 있다 —
# ``payload["resolved_decisions"]`` 는 ``_resumption_messages`` 가 읽어 user
# 메시지로 만들고, 상태 hop 은 ``ExecutionRunHistory`` 직접 쓰기 패턴이 있다.
# ---------------------------------------------------------------------------


async def _deny(session, ws, item, reason: str) -> bool:
    return await SafeModeQueue(session).deny(
        workspace_id=ws, item_id=item.id, actor_id=uuid.uuid4(), reason=reason
    )


async def test_a_denial_with_a_reason_reopens_the_run() -> None:
    """RUNNING → OPEN so ``AgentWorker.drive_once`` (scans OPEN) re-picks it."""
    async with memory_session() as session:
        ws, run, item = await _run_and_item(session)
        await _deny(session, ws, item, "이건 표면에서 막을 게 아니라 브리핑에서 막아야 한다")
        await session.refresh(run)
        assert run.status is RunStatus.OPEN


async def test_the_reopened_run_actually_hears_the_reason() -> None:
    """The load-bearing half. A resume that does not CARRY the rejection is a
    run that wakes up and repeats the same work — so assert what the agent will
    literally receive, not merely that a key was written."""
    from backend.workflow.application._loop_context import _resumption_messages

    async with memory_session() as session:
        ws, run, item = await _run_and_item(session)
        reason = "이건 표면에서 막을 게 아니라 브리핑에서 막아야 한다"
        await _deny(session, ws, item, reason)
        await session.refresh(run)

        messages = _resumption_messages(run)
        assert messages, "the reopened run seeds no founder feedback at all"
        assert any(reason in str(m["content"]) for m in messages), messages


async def test_a_reason_the_founder_did_not_write_is_never_invented() -> None:
    """A blank reason teaches nothing (A-1a stores NULL rather than ""), so it
    must not wake the run either — it would resume with no new information and
    redo the identical work."""
    async with memory_session() as session:
        ws, run, item = await _run_and_item(session)
        await _deny(session, ws, item, "   ")
        await session.refresh(run)
        assert run.status is RunStatus.RUNNING


async def test_a_terminal_run_is_left_alone() -> None:
    """A run that already ENDED has nowhere to resume to — reopening it would
    re-run finished work, including its approval + delivery."""
    async with memory_session() as session:
        ws, run, item = await _run_and_item(session)
        run.status = RunStatus.SHIPPED
        await session.flush()
        await _deny(session, ws, item, "늦었지만 이건 아니다")
        await session.refresh(run)
        assert run.status is RunStatus.SHIPPED


async def test_the_hop_is_recorded_in_run_history() -> None:
    """B4 trust integrity — every status hop leaves a row saying why."""
    from backend.workflow.infrastructure.db import ExecutionRunHistory

    async with memory_session() as session:
        ws, run, item = await _run_and_item(session)
        await _deny(session, ws, item, "이건 아니다")
        rows = (await session.execute(select(ExecutionRunHistory))).scalars().all()
        hops = [r for r in rows if r.run_id == run.id and r.to_status is RunStatus.OPEN]
        assert len(hops) == 1
        assert str(item.id) in (hops[0].reason or "")
