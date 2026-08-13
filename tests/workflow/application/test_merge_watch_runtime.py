"""PR6 — merge_watch_runtime application-layer callbacks.

Covers the two application seams the infrastructure ``MergeWatchWorker`` gets
INJECTED (so the worker never crosses into the application layer itself):

* :func:`build_merge_watch_conflict_redispatch` — writes
  ``run.payload["merge_conflict"]`` and transitions the run RUNNING → OPEN (the
  same resume seam ``checkpoint_resolution`` uses), so ``AgentWorker`` re-picks
  it and the agent resolves the conflict (PR7's side).

Real Postgres (``BSVIBE_DATABASE_URL``) for the ExecutionRun row.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.data import Base
from backend.workflow.application.runtime.merge_watch_runtime import (
    build_merge_watch_conflict_escalate,
    build_merge_watch_conflict_redispatch,
    build_merge_watch_stall_escalate,
    build_merge_watch_workers,
)
from backend.workflow.infrastructure.db import Decision, DecisionStatus, ExecutionRun, RunStatus
from tests._support import db_engine

pytestmark = pytest.mark.asyncio


async def _seed_run(
    sf: async_sessionmaker,  # noqa: ANN001
    *,
    status: RunStatus,
    payload: dict | None = None,
) -> uuid.UUID:
    run_id = uuid.uuid4()
    async with sf() as session:
        session.add(
            ExecutionRun(
                id=run_id,
                workspace_id=uuid.uuid4(),
                status=status,
                payload=payload or {"intent_text": "x"},
            )
        )
        await session.commit()
    return run_id


async def test_conflict_redispatch_writes_payload_and_reopens_running_run() -> None:
    async with db_engine(Base) as (engine, _pg):
        sf = async_sessionmaker(engine, expire_on_commit=False)
        run_id = await _seed_run(sf, status=RunStatus.RUNNING)

        redispatch = build_merge_watch_conflict_redispatch(session_factory=sf)
        await redispatch(
            run_id, conflict_paths=["shared.txt", "a/b.py"], base_branch="main", pr_number=42
        )

        async with sf() as session:
            run = await session.get(ExecutionRun, run_id)
            assert run is not None
            assert run.status is RunStatus.OPEN  # RUNNING → OPEN resume seam
            assert run.payload["merge_conflict"] == {
                "conflict_paths": ["shared.txt", "a/b.py"],
                "base_branch": "main",
                "pr_number": 42,
            }
            # The originating intent is preserved (payload re-assigned, not clobbered).
            assert run.payload["intent_text"] == "x"


async def test_conflict_redispatch_missing_run_is_a_noop() -> None:
    async with db_engine(Base) as (engine, _pg):
        sf = async_sessionmaker(engine, expire_on_commit=False)
        redispatch = build_merge_watch_conflict_redispatch(session_factory=sf)
        # No such run — must not raise (idempotent / at-least-once contract).
        await redispatch(uuid.uuid4(), conflict_paths=["x"], base_branch="main", pr_number=1)


async def test_conflict_redispatch_clears_stale_resolving_marker() -> None:
    """Conflict-robustness — a re-dispatch after a FAILED prior turn must deliver
    the conflict context afresh: it re-writes ``merge_conflict`` AND clears the
    stale ``merge_conflict_resolving`` marker the failed turn left behind, so the
    drive loop re-injects the directive and re-sets the marker cleanly."""
    async with db_engine(Base) as (engine, _pg):
        sf = async_sessionmaker(engine, expire_on_commit=False)
        run_id = await _seed_run(
            sf,
            status=RunStatus.RUNNING,
            # A prior turn consumed the one-shot directive (merge_conflict gone)
            # but then FAILED — leaving the resolving marker set + no directive.
            payload={"intent_text": "x", "merge_conflict_resolving": True},
        )

        redispatch = build_merge_watch_conflict_redispatch(session_factory=sf)
        await redispatch(run_id, conflict_paths=["shared.txt"], base_branch="main", pr_number=9)

        async with sf() as session:
            run = await session.get(ExecutionRun, run_id)
            assert run is not None
            # The conflict directive is present again ...
            assert run.payload["merge_conflict"]["conflict_paths"] == ["shared.txt"]
            # ... and the stale resolving marker is cleared (retry starts clean).
            assert "merge_conflict_resolving" not in run.payload
            assert run.status is RunStatus.OPEN


async def test_conflict_escalate_raises_review_decision_and_pauses_run() -> None:
    """Conflict-robustness — escalation raises a founder-actionable
    ``merge_conflict_review`` Decision, pauses the run (RUNNING → not re-picked),
    and clears the stale one-shot conflict markers so a guided retry is clean."""
    async with db_engine(Base) as (engine, _pg):
        sf = async_sessionmaker(engine, expire_on_commit=False)
        run_id = await _seed_run(
            sf,
            status=RunStatus.OPEN,  # a wedged re-drive left it OPEN
            payload={"intent_text": "x", "merge_conflict_resolving": True},
        )

        escalate = build_merge_watch_conflict_escalate(session_factory=sf)
        await escalate(run_id, conflict_paths=["shared.txt"], base_branch="develop", pr_number=42)

        async with sf() as session:
            run = await session.get(ExecutionRun, run_id)
            assert run is not None
            # Paused ON the Decision (RUNNING convention — not re-picked by drive_once).
            assert run.status is RunStatus.RUNNING
            # Stale one-shot markers cleared for a clean founder-guided retry.
            assert "merge_conflict_resolving" not in run.payload
            assert "merge_conflict" not in run.payload

            decision = (
                await session.execute(select(Decision).where(Decision.run_id == run_id))
            ).scalar_one()
            assert decision.decision == "merge_conflict_review"
            assert decision.status is DecisionStatus.PENDING
            assert decision.payload["reason"] == "conflict_unresolved_escalated"
            assert decision.payload["pr_number"] == 42
            assert decision.payload["base_branch"] == "develop"


async def test_conflict_escalate_missing_run_is_a_noop() -> None:
    async with db_engine(Base) as (engine, _pg):
        sf = async_sessionmaker(engine, expire_on_commit=False)
        escalate = build_merge_watch_conflict_escalate(session_factory=sf)
        # No such run — must not raise (idempotent / at-least-once contract).
        await escalate(uuid.uuid4(), conflict_paths=["x"], base_branch="main", pr_number=1)


async def test_stall_escalate_raises_stalled_decision_without_reviving_the_run() -> None:
    """머지워치가 포기했을 때의 application 시임.

    conflict escalation 과 다른 점: 이 런은 **이미 끝났다**(딜리버러블 착지 →
    승인 → shipped). 그래서 런을 RUNNING 으로 파킹하지 않는다 — 남은 것은 머지되지
    않은 PR 하나이고, 그 사실을 형님이 알아야 할 뿐이다."""
    async with db_engine(Base) as (engine, _pg):
        sf = async_sessionmaker(engine, expire_on_commit=False)
        run_id = await _seed_run(sf, status=RunStatus.SHIPPED)

        escalate = build_merge_watch_stall_escalate(session_factory=sf)
        await escalate(run_id, reason="ci_deadline_exceeded", repo="acme/x", pr_number=23)

        async with sf() as session:
            run = await session.get(ExecutionRun, run_id)
            assert run is not None
            assert run.status is RunStatus.SHIPPED  # 되살리지 않는다

            decision = (
                await session.execute(select(Decision).where(Decision.run_id == run_id))
            ).scalar_one()
            assert decision.decision == "merge_watch_stalled"
            assert decision.status is DecisionStatus.PENDING
            assert decision.payload["reason"] == "ci_deadline_exceeded"
            assert decision.payload["repo"] == "acme/x"
            assert decision.payload["pr_number"] == 23


async def test_stall_escalate_missing_run_is_a_noop() -> None:
    async with db_engine(Base) as (engine, _pg):
        sf = async_sessionmaker(engine, expire_on_commit=False)
        escalate = build_merge_watch_stall_escalate(session_factory=sf)
        # 없는 런 — at-least-once 계약상 raise 하면 안 된다(폴 전체를 죽인다).
        await escalate(uuid.uuid4(), reason="github_binding_unavailable", repo="a/b", pr_number=1)


async def test_stall_escalation_is_wired_into_the_worker_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """빌더가 있어도 워커에 안 꽂히면 프로덕션에서는 여전히 조용하다
    (half-wired-subsystem: 보이는 절반만 만들어지는 그 결함)."""
    from backend.config import get_settings
    from backend.workflow.application.runtime import merge_watch_runtime

    # 크리덴셜 키는 이 테스트의 대상이 아니다 — 워커 구성만 보면 된다.
    monkeypatch.setattr(merge_watch_runtime, "_key_from_settings", lambda: b"\x00" * 32)

    async with db_engine(Base) as (engine, _pg):
        sf = async_sessionmaker(engine, expire_on_commit=False)
        settings = get_settings().model_copy(update={"github_auto_merge_enabled": True})
        workers = build_merge_watch_workers(sf, settings)
        assert len(workers) == 1
        assert workers[0]._escalate_stall is not None
        # 함께 온 나머지 시임도 그대로 꽂혀 있다(회귀 방지).
        assert workers[0]._escalate_conflict is not None
        assert workers[0]._redispatch_conflict is not None
