"""승인한 코드 산출물이 PR 을 못 열었는데 "배달 성공" 으로 세어지던 결함 (#1259 런 686d3874).

승인 경로는 예외만 없으면 ``dispatched_count`` 에 넣었고, GitHub 배달은
체크아웃이 없거나 커밋할 게 없을 때 PR 없이 끝났다. 이 테스트는 **실제**
``approve_run_and_dispatch`` 와 **실제** 배달 함수를 이어서 부른다 — 어댑터만 얇게
감싼 전달자로 — 그래서 한쪽을 mock 으로 바꿔 놓고 통과하는 일이 없다.
실패한 배달도 보상 핸들(compensation)은 남고 ``delivery_failed`` Decision 이 하나 생긴다.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.data.rls import workspace_session_scope
from backend.data.scoping import workspace_scope
from backend.workflow.application.delivery.connector_dispatch import _github
from backend.workflow.application.delivery.connector_dispatch._github import (
    GithubDeliveryDeps,
    deliver_github,
)
from backend.workflow.application.delivery.connector_dispatch._resolver import GithubBinding
from backend.workflow.application.safe_mode_approval import approve_run_and_dispatch
from backend.workflow.application.safe_mode_queue import SafeModeQueue
from backend.workflow.domain.delivery import ActionResult, DeliveryResult
from backend.workflow.infrastructure.db import (
    Decision,
    DecisionStatus,
    Deliverable,
    DeliverableType,
    ExecutionBase,
    ExecutionRun,
    RunStatus,
)
from backend.workflow.infrastructure.workers.delivery_worker import set_decision_factory

from .._support import db_engine

pytestmark = pytest.mark.asyncio

_HANDLE = {"pr_number": 7, "repo": "owner/name"}


@pytest_asyncio.fixture
async def sf():
    async with db_engine(ExecutionBase) as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


async def _decision_row(session, run, work_step, *, kind: str, **fields: Any) -> Decision:
    decision = Decision(
        id=uuid.uuid4(),
        run_id=run.id,
        workspace_id=run.workspace_id,
        decision=kind,
        payload=fields["payload"],
        rationale=fields["rationale"],
    )
    session.add(decision)
    await session.flush()
    return decision


@pytest.fixture(autouse=True)
def _wired(monkeypatch):
    set_decision_factory(_decision_row)

    async def _creds(*_a: Any, **_k: Any) -> dict[str, str]:
        return {"token": "tok"}

    monkeypatch.setattr(_github, "resolve_connector_credentials", _creds)
    yield
    set_decision_factory(None)


class _GitOps:
    def __init__(self, *, committed: bool) -> None:
        self._committed = committed

    async def commit_all(self, dest: Path, message: str) -> bool:
        return self._committed

    async def is_ahead_of_base(self, dest: Path, base_branch: str) -> bool:
        return False

    async def push(self, dest: Path, branch: str, *, token: str | None) -> None:
        return None


class _Runner:
    async def dispatch_action(self, plugin: Any, **kwargs: Any) -> dict[str, Any]:
        return {"url": "https://github.com/owner/name/pull/7", "compensation_handle": _HANDLE}


class _RealGithubDispatcher:
    """배달 전달 자리에서 실제 GitHub 배달 함수를 돌린다."""

    def __init__(self, sf_, root: Path, *, committed: bool, also_failing: bool = False) -> None:
        self._sf = sf_
        self._also_failing = also_failing
        self._deps = GithubDeliveryDeps(
            cipher=None,  # type: ignore[arg-type]
            plugins_by_name={"github": object()},  # type: ignore[dict-item]
            workspace_root=root,
            git_ops=_GitOps(committed=committed),  # type: ignore[arg-type]
            remote_url_for=lambda repo: f"https://example.invalid/{repo}.git",
            runner=_Runner(),  # type: ignore[arg-type]
            session_factory=sf_,
        )

    async def dispatch(self, **kwargs: Any) -> DeliveryResult:
        async with self._sf() as s:
            deliverable = await s.get(Deliverable, kwargs["deliverable_id"])
        binding = GithubBinding(
            account=SimpleNamespace(delivery_config={}),  # type: ignore[arg-type]
            repo="owner/name",
            base_branch="main",
        )
        actions = await deliver_github(
            deps=self._deps,
            binding=binding,
            workspace_id=kwargs["workspace_id"],
            deliverable_id=kwargs["deliverable_id"],
            run_id=deliverable.run_id,
            content={"summary": "Add the thing"},
        )
        if self._also_failing:
            actions.append(
                ActionResult(action="slack:outbound:pr", succeeded=False, error="channel_not_found")
            )
        return DeliveryResult(
            workspace_id=kwargs["workspace_id"],
            deliverable_id=kwargs["deliverable_id"],
            artifact_type="pr",
            actions=actions,
            delivered_at=datetime.now(tz=UTC),
        )


async def _seed(sf_, *, checkout_root: Path | None) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """REVIEW_READY 런 + PR 산출물 + 대기 중 큐 항목. ``checkout_root`` 가 있으면 체크아웃을 만든다."""
    workspace_id, run_id, deliverable_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    now = datetime.now(tz=UTC)
    if checkout_root is not None:
        (checkout_root / str(run_id)).mkdir()
    async with sf_() as s, workspace_session_scope(s, workspace_id):
        s.add(
            ExecutionRun(
                id=run_id,
                workspace_id=workspace_id,
                product_id=None,
                status=RunStatus.REVIEW_READY,
                payload={},
                created_at=now,
                updated_at=now,
            )
        )
        await s.flush()
        s.add(
            Deliverable(
                id=deliverable_id,
                run_id=run_id,
                workspace_id=workspace_id,
                deliverable_type=DeliverableType.PR,
                payload={"summary": "Add the thing"},
                created_at=now,
            )
        )
        await s.flush()
        await SafeModeQueue(s).enqueue(
            workspace_id=workspace_id, deliverable_id=deliverable_id, run_id=run_id
        )
        await s.commit()
    return workspace_id, run_id, deliverable_id


async def _approve(sf_, dispatcher, workspace_id, run_id) -> tuple[int, int]:
    with workspace_scope(workspace_id):
        async with sf_() as s, workspace_session_scope(s, workspace_id):
            return await approve_run_and_dispatch(
                s,
                workspace_id=workspace_id,
                run_id=run_id,
                actor_id=uuid.uuid4(),
                dispatcher=dispatcher,
            )


async def _failed_decisions(sf_, workspace_id) -> list[Decision]:
    async with sf_() as s, workspace_session_scope(s, workspace_id):
        rows = await s.execute(
            select(Decision).where(
                Decision.decision == "delivery_failed",
                Decision.status == DecisionStatus.PENDING,
            )
        )
        return list(rows.scalars())


async def test_missing_checkout_is_not_dispatched(sf, tmp_path: Path) -> None:
    workspace_id, run_id, _ = await _seed(sf, checkout_root=None)

    counts = await _approve(
        sf, _RealGithubDispatcher(sf, tmp_path, committed=True), workspace_id, run_id
    )

    assert counts == (1, 0)
    assert len(await _failed_decisions(sf, workspace_id)) == 1


async def test_no_changes_raises_delivery_failed(sf, tmp_path: Path) -> None:
    workspace_id, run_id, _ = await _seed(sf, checkout_root=tmp_path)

    counts = await _approve(
        sf, _RealGithubDispatcher(sf, tmp_path, committed=False), workspace_id, run_id
    )

    assert counts == (1, 0)
    decisions = await _failed_decisions(sf, workspace_id)
    assert len(decisions) == 1
    assert "no changes" in decisions[0].rationale


async def test_opened_pr_is_dispatched(sf, tmp_path: Path) -> None:
    workspace_id, run_id, deliverable_id = await _seed(sf, checkout_root=tmp_path)

    counts = await _approve(
        sf, _RealGithubDispatcher(sf, tmp_path, committed=True), workspace_id, run_id
    )

    assert counts == (1, 1)
    assert await _failed_decisions(sf, workspace_id) == []
    async with sf() as s, workspace_session_scope(s, workspace_id):
        row = await s.get(Deliverable, deliverable_id)
        assert [e["handle"] for e in row.compensation_handles] == [_HANDLE]


async def test_pr_opened_but_other_target_failed_keeps_handle(sf, tmp_path: Path) -> None:
    """열린 PR 은 나중에 회수할 수 있어야 한다 — 핸들은 저장하되 배달됨으로 세지 않는다."""
    workspace_id, run_id, deliverable_id = await _seed(sf, checkout_root=tmp_path)

    counts = await _approve(
        sf,
        _RealGithubDispatcher(sf, tmp_path, committed=True, also_failing=True),
        workspace_id,
        run_id,
    )

    assert counts == (1, 0)
    assert len(await _failed_decisions(sf, workspace_id)) == 1
    async with sf() as s, workspace_session_scope(s, workspace_id):
        row = await s.get(Deliverable, deliverable_id)
        assert [e["handle"] for e in row.compensation_handles] == [_HANDLE]
