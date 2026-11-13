"""머지 충돌 재디스패치는 런 단위 상한을 가진다 (run 5b8cb753: 2,001,290 토큰 소진).

``conflict_attempts`` 는 새 head 마다 1 로 초기화되므로, 매번 새로 푸시하는 에이전트는
그 상한에 닿지 않는다. 누적 카운터/토큰 상한은 run.payload 에 산다.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.data.rls import workspace_session_scope
from backend.data.scoping import workspace_scope
from backend.workflow.application.runtime.merge_watch_redispatch import (
    build_merge_watch_conflict_redispatch,
)
from backend.workflow.application.token_budget import account_and_enforce_token_cap
from backend.workflow.domain.conflict_budget import (
    CONFLICT_BUDGET_KEY,
    CONFLICT_REVIEW_DECISION_KIND,
)
from backend.workflow.infrastructure.db import Decision, ExecutionRun, RunStatus

from .._support import db_engine


@pytest_asyncio.fixture
async def sf():
    async with db_engine() as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


async def _run(sf, *, tokens: int = 0) -> tuple[uuid.UUID, uuid.UUID]:
    workspace_id = uuid.uuid4()
    async with sf() as s, workspace_session_scope(s, workspace_id):
        run = ExecutionRun(
            id=uuid.uuid4(),
            workspace_id=workspace_id,
            status=RunStatus.RUNNING,
            payload={"intent_text": "x"},
            usage_prompt_tokens=tokens,
            usage_completion_tokens=0,
            created_at=datetime.now(tz=UTC),
            updated_at=datetime.now(tz=UTC),
        )
        s.add(run)
        await s.commit()
        return workspace_id, run.id


async def _reopen_as_running(sf, workspace_id, run_id, *, add_tokens: int = 0) -> None:
    """The agent re-drove the run (consumed tokens) and pushed; the watch sees it RUNNING."""
    async with sf() as s, workspace_session_scope(s, workspace_id):
        run = await s.get(ExecutionRun, run_id)
        run.status = RunStatus.RUNNING
        run.usage_prompt_tokens += add_tokens
        await s.commit()


async def _fetch(sf, workspace_id, run_id) -> ExecutionRun:
    async with sf() as s, workspace_session_scope(s, workspace_id):
        run = await s.get(ExecutionRun, run_id)
        assert run is not None
        return run


async def _review_decisions(sf, workspace_id, run_id) -> list[Decision]:
    async with sf() as s, workspace_session_scope(s, workspace_id):
        rows = await s.scalars(
            select(Decision).where(
                Decision.run_id == run_id, Decision.decision == CONFLICT_REVIEW_DECISION_KIND
            )
        )
        return list(rows)


async def _conflict(redispatch, ws, run_id, head: int) -> bool | None:
    # 매번 다른 head 의 충돌 — 행의 conflict_attempts 는 매번 1 로 초기화되는 상황.
    with workspace_scope(ws):
        return await redispatch(
            run_id, conflict_paths=[f"f{head}.txt"], base_branch="main", pr_number=7
        )


@pytest.mark.asyncio
async def test_total_redispatch_capped_across_changing_heads(sf) -> None:
    ws, run_id = await _run(sf)
    redispatch = build_merge_watch_conflict_redispatch(
        session_factory=sf, max_total_redispatch=3, token_budget=0
    )
    results = []
    for head in range(10):
        results.append(await _conflict(redispatch, ws, run_id, head))
        if results[-1] is False:
            break
        await _reopen_as_running(sf, ws, run_id)

    assert results == [None, None, None, False]  # 4번째 시도에서 멈춘다
    run = await _fetch(sf, ws, run_id)
    assert run.payload[CONFLICT_BUDGET_KEY]["redispatches"] == 3
    assert run.status is RunStatus.RUNNING  # 재오픈하지 않음 (창업자 결정 대기)
    assert "merge_conflict" not in run.payload
    decisions = await _review_decisions(sf, ws, run_id)
    assert len(decisions) == 1
    assert len(decisions[0].payload["attempts"]) == 3  # 지금까지 시도한 내용 요약


@pytest.mark.asyncio
async def test_history_records_tokens_per_redispatch(sf) -> None:
    ws, run_id = await _run(sf, tokens=100)
    redispatch = build_merge_watch_conflict_redispatch(
        session_factory=sf, max_total_redispatch=5, token_budget=0
    )
    await _conflict(redispatch, ws, run_id, 1)
    await _reopen_as_running(sf, ws, run_id, add_tokens=250)
    await _conflict(redispatch, ws, run_id, 2)

    history = (await _fetch(sf, ws, run_id)).payload[CONFLICT_BUDGET_KEY]["history"]
    assert [h["tokens_total"] for h in history] == [100, 350]
    assert [h["tokens_since_start"] for h in history] == [0, 250]


@pytest.mark.asyncio
async def test_redispatch_refused_once_token_budget_is_spent(sf) -> None:
    ws, run_id = await _run(sf, tokens=1_000)
    redispatch = build_merge_watch_conflict_redispatch(
        session_factory=sf, max_total_redispatch=10, token_budget=500
    )
    assert await _conflict(redispatch, ws, run_id, 1) is None
    await _reopen_as_running(sf, ws, run_id, add_tokens=600)
    assert await _conflict(redispatch, ws, run_id, 2) is False
    decisions = await _review_decisions(sf, ws, run_id)
    assert decisions[0].payload["budget_reason"] == "token_limit"


@pytest.mark.asyncio
async def test_token_ceiling_makes_merge_conflict_review() -> None:
    """충돌 토큰 상한 도달 → run_token_cap_reached 가 아니라 merge_conflict_review."""
    orch = SimpleNamespace(
        _settings=SimpleNamespace(agent_max_run_tokens=2_000_000),
        _audit=AsyncMock(),
        _create_decision=AsyncMock(),
        _session=SimpleNamespace(commit=AsyncMock()),
        _decision_result=lambda *a, **k: "STOPPED",
    )
    run = SimpleNamespace(
        usage_prompt_tokens=700_000,
        usage_completion_tokens=0,
        payload={
            "merge_conflict_resolving": True,
            CONFLICT_BUDGET_KEY: {
                "redispatches": 1,
                "tokens_at_start": 100_000,
                "token_ceiling": 600_000,
                "history": [{"attempt": 1}],
            },
        },
    )
    turn = SimpleNamespace(usage_prompt_tokens=1_000, usage_completion_tokens=0)

    result = await account_and_enforce_token_cap(
        orch,  # type: ignore[arg-type]
        run=run,  # type: ignore[arg-type]
        work_step=None,  # type: ignore[arg-type]
        attempt=None,  # type: ignore[arg-type]
        turn=turn,  # type: ignore[arg-type]
        written_paths=[],
        final_text="",
    )

    assert result == "STOPPED"
    kwargs = orch._create_decision.await_args.kwargs
    assert kwargs["kind"] == CONFLICT_REVIEW_DECISION_KIND
    assert kwargs["payload"]["attempts"] == [{"attempt": 1}]
    assert "token_ceiling" not in run.payload[CONFLICT_BUDGET_KEY]


def _orch() -> SimpleNamespace:
    return SimpleNamespace(
        _settings=SimpleNamespace(agent_max_run_tokens=2_000_000),
        _audit=AsyncMock(),
        _create_decision=AsyncMock(),
        _session=SimpleNamespace(commit=AsyncMock()),
        _decision_result=lambda *a, **k: "STOPPED",
    )


@pytest.mark.asyncio
async def test_ceiling_not_applied_outside_conflict_resolution() -> None:
    """해결 후(merge_conflict_resolving 없음) CI-fix 턴이 예전 상한을 넘어도 review 를 만들지 않는다."""
    orch = _orch()
    run = SimpleNamespace(
        usage_prompt_tokens=700_000,
        usage_completion_tokens=0,
        payload={CONFLICT_BUDGET_KEY: {"redispatches": 1, "token_ceiling": 600_000}},
    )
    turn = SimpleNamespace(usage_prompt_tokens=1_000, usage_completion_tokens=0)
    result = await account_and_enforce_token_cap(
        orch,  # type: ignore[arg-type]
        run=run,  # type: ignore[arg-type]
        work_step=None,  # type: ignore[arg-type]
        attempt=None,  # type: ignore[arg-type]
        turn=turn,  # type: ignore[arg-type]
        written_paths=[],
        final_text="",
    )
    assert result is None
    orch._create_decision.assert_not_awaited()


@pytest.mark.asyncio
async def test_budget_cleared_after_resolution(sf) -> None:
    """워커가 clean 을 보면 예산이 지워진다 (지우는 코드를 빼면 빨갛다)."""
    from backend.workflow.infrastructure.workers.merge_watch_worker import MergeWatchWorker

    ws, run_id = await _run(sf, tokens=100)
    redispatch = build_merge_watch_conflict_redispatch(
        session_factory=sf, max_total_redispatch=3, token_budget=500
    )
    await _conflict(redispatch, ws, run_id, 1)
    assert CONFLICT_BUDGET_KEY in (await _fetch(sf, ws, run_id)).payload

    async with sf() as s, workspace_session_scope(s, ws):
        await MergeWatchWorker._clear_conflict_budget(None, s, run_id)  # type: ignore[arg-type]
        await s.commit()
    assert CONFLICT_BUDGET_KEY not in (await _fetch(sf, ws, run_id)).payload


@pytest.mark.asyncio
async def test_founder_retry_resets_budget(sf) -> None:
    """merge_conflict_review 에 retry 하면 다음 충돌은 다시 자동 재디스패치된다."""
    from backend.workflow.application._checkpoint_shared import ACTION_RETRY
    from backend.workflow.application.checkpoint_resolution import _prepare_the_resume

    ws, run_id = await _run(sf)
    redispatch = build_merge_watch_conflict_redispatch(
        session_factory=sf, max_total_redispatch=1, token_budget=0
    )
    assert await _conflict(redispatch, ws, run_id, 1) is None
    await _reopen_as_running(sf, ws, run_id)
    assert await _conflict(redispatch, ws, run_id, 2) is False  # 소진

    await _reopen_as_running(sf, ws, run_id)
    run = await _fetch(sf, ws, run_id)
    payload = dict(run.payload)
    await _prepare_the_resume(
        None,  # type: ignore[arg-type]
        payload,
        run=run,
        decision=SimpleNamespace(decision=CONFLICT_REVIEW_DECISION_KIND, payload={}),  # type: ignore[arg-type]
        action_key=ACTION_RETRY,
        answer="",
    )
    assert CONFLICT_BUDGET_KEY not in payload
    async with sf() as s, workspace_session_scope(s, ws):
        row = await s.get(ExecutionRun, run_id)
        row.payload = payload
        await s.commit()
    assert await _conflict(redispatch, ws, run_id, 3) is None
