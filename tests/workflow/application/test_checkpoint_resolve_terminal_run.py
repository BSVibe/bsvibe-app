"""끝난 런에 달린 Decision 을 접어도 그 런이 되살아나면 안 된다.

``merge_watch_stalled`` 은 **이미 shipped 된 런**에 달린다 — 딜리버러블은 착지했고,
남은 것은 머지되지 않은 PR 뿐이다. 그런데 resolve 의 기본 경로는 "RUNNING → OPEN
재개"라서, 그대로 두면 형님이 알림을 접는 순간 끝난 런이 다시 드라이브 루프에
올라간다(승인·전달을 다시 태운다).

``checkpoint_resolution`` 의 주석은 이미 "run 이 RUNNING 이 아니면 no-op" 이라고
**선언**하고 있었지만 ``AgentRunner.transition`` 은 그 검사를 하지 않는다. 여기서
그 선언을 참으로 만든다.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.workflow.application._checkpoint_shared import ACTION_ACKNOWLEDGE
from backend.workflow.application.checkpoint_resolution import resolve_checkpoint
from backend.workflow.infrastructure.db import (
    Decision,
    DecisionStatus,
    ExecutionRun,
    RunStatus,
)

from ..._support import db_engine

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


async def _seed_stalled(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    *,
    run_status: RunStatus = RunStatus.SHIPPED,
) -> tuple[uuid.UUID, uuid.UUID]:
    """SHIPPED 런 + 그 위의 PENDING ``merge_watch_stalled`` Decision."""
    run = ExecutionRun(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        status=run_status,
        payload={"intent_text": "리포트에 기간 표시 추가"},
        created_at=datetime.now(tz=UTC),
        updated_at=datetime.now(tz=UTC),
    )
    session.add(run)
    await session.flush()
    decision = Decision(
        id=uuid.uuid4(),
        run_id=run.id,
        workspace_id=workspace_id,
        decision="merge_watch_stalled",
        payload={"reason": "ci_deadline_exceeded", "repo": "acme/x", "pr_number": 23},
        status=DecisionStatus.PENDING,
    )
    session.add(decision)
    await session.flush()
    return run.id, decision.id


async def test_acknowledge_resolves_without_reviving_the_shipped_run(
    sf, workspace_id: uuid.UUID, founder_id: uuid.UUID
) -> None:
    async with sf() as s:
        run_id, decision_id = await _seed_stalled(s, workspace_id)
        await s.commit()

    async with sf() as s:
        outcome = await resolve_checkpoint(
            s,
            workspace_id=workspace_id,
            checkpoint_id=decision_id,
            answer="",
            action_key=ACTION_ACKNOWLEDGE,
            actor_id=founder_id,
        )
        await s.commit()

    assert outcome.status is DecisionStatus.RESOLVED
    # 접혔을 뿐, 런은 그대로 shipped 다 — 되살아나지 않는다.
    assert outcome.run_status is RunStatus.SHIPPED
    async with sf() as s:
        run = await s.get(ExecutionRun, run_id)
        decision = await s.get(Decision, decision_id)
    assert run is not None
    assert run.status is RunStatus.SHIPPED
    assert decision is not None
    assert decision.status is DecisionStatus.RESOLVED
    assert decision.resolved_by == founder_id


@pytest.mark.parametrize("terminal", [RunStatus.SHIPPED, RunStatus.CANCELLED, RunStatus.FAILED])
async def test_free_text_reply_does_not_reopen_a_terminal_run(
    sf, workspace_id: uuid.UUID, founder_id: uuid.UUID, terminal: RunStatus
) -> None:
    """PWA 는 one-click 버튼 옆에 자유 입력창도 항상 띄운다(``need-card__free-toggle``).
    그 경로로도 끝난 런이 다시 열리면 안 된다 — 버튼만 막는 것은 절반이다."""
    async with sf() as s:
        run_id, decision_id = await _seed_stalled(s, workspace_id, run_status=terminal)
        await s.commit()

    async with sf() as s:
        outcome = await resolve_checkpoint(
            s,
            workspace_id=workspace_id,
            checkpoint_id=decision_id,
            answer="GitHub 에서 직접 머지했어요",
            actor_id=founder_id,
        )
        await s.commit()

    assert outcome.status is DecisionStatus.RESOLVED
    assert outcome.run_status is terminal
    async with sf() as s:
        run = await s.get(ExecutionRun, run_id)
    assert run is not None
    assert run.status is terminal


async def test_free_text_reply_still_resumes_a_paused_running_run(
    sf, workspace_id: uuid.UUID, founder_id: uuid.UUID
) -> None:
    """가드는 terminal 런에만 붙는다 — Decision 에 파킹된 RUNNING 런의 재개
    (이 서비스의 본래 계약)는 그대로다."""
    async with sf() as s:
        run_id, decision_id = await _seed_stalled(s, workspace_id, run_status=RunStatus.RUNNING)
        await s.commit()

    async with sf() as s:
        outcome = await resolve_checkpoint(
            s,
            workspace_id=workspace_id,
            checkpoint_id=decision_id,
            answer="이렇게 해줘",
            actor_id=founder_id,
        )
        await s.commit()

    assert outcome.run_status is RunStatus.OPEN
    async with sf() as s:
        run = await s.get(ExecutionRun, run_id)
    assert run is not None
    assert run.status is RunStatus.OPEN
