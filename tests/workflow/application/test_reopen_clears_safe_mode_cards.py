"""A run reopened by the founder's message loses its waiting Safe Mode cards.

Real runs, real cards, and the real ``route_*_to_run`` entry points.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.data import Base
from backend.data.rls import workspace_session_scope
from backend.workflow.application.intake.issue_edit import (
    route_issue_comment_to_run,
    route_issue_edit_to_run,
)
from backend.workflow.application.intake.pr_comment import route_pr_comment_to_run
from backend.workflow.application.run_detail import _activity_label
from backend.workflow.application.run_inbox import (
    REOPEN_DENY_REASON,
    SAFE_MODE_CLEARED_ACTIVITY,
)
from backend.workflow.infrastructure.db import ExecutionRun, ExecutionRunActivity, RunStatus
from backend.workflow.infrastructure.delivery.db import SafeModeQueueItemRow, SafeModeStatus
from tests._support import db_engine
from tests.workflow.application import test_issue_edit_routing as issue_t
from tests.workflow.application import test_pr_comment_inbox as pr_t

pytestmark = pytest.mark.asyncio

Route = Callable[[AsyncSession, uuid.UUID], Awaitable[Any]]


def _comment_event() -> dict[str, Any]:
    return {
        "github_event": "issue_comment",
        "repo": issue_t.REPO,
        "body": {
            "issue": {"number": 1263, "title": "t"},
            "comment": {
                "body": "- [ ] 하나 더 해 줘",
                "author_association": "OWNER",
                "user": {"login": "blasin"},
            },
        },
    }


async def _route_edit(s: AsyncSession, ws: uuid.UUID) -> Any:
    return await route_issue_edit_to_run(
        s, workspace_id=ws, payload=issue_t._event(issue_t._BODY_V2)
    )


async def _route_comment(s: AsyncSession, ws: uuid.UUID) -> Any:
    return await route_issue_comment_to_run(s, workspace_id=ws, payload=_comment_event())


async def _route_pr(s: AsyncSession, ws: uuid.UUID) -> Any:
    return await route_pr_comment_to_run(s, workspace_id=ws, payload=pr_t._payload())


async def _seed_issue(sf, status: RunStatus) -> tuple[uuid.UUID, uuid.UUID]:  # noqa: ANN001
    return await issue_t._seed(sf, status=status)


async def _seed_pr(sf, status: RunStatus) -> tuple[uuid.UUID, uuid.UUID]:  # noqa: ANN001
    run_id, ws = await pr_t._seed(sf, status)
    return ws, run_id


async def _card(
    sf,  # noqa: ANN001
    ws: uuid.UUID,
    run_id: uuid.UUID,
    status: SafeModeStatus = SafeModeStatus.PENDING,
) -> uuid.UUID:
    item = SafeModeQueueItemRow(
        id=uuid.uuid4(),
        workspace_id=ws,
        run_id=run_id,
        deliverable_id=uuid.uuid4(),
        status=status,
        expires_at=datetime.now(tz=UTC),
        created_at=datetime.now(tz=UTC),
    )
    async with sf() as s, workspace_session_scope(s, ws):
        s.add(item)
        await s.commit()
    return item.id


async def _get(sf, ws: uuid.UUID, item_id: uuid.UUID) -> SafeModeQueueItemRow:  # noqa: ANN001
    async with sf() as s, workspace_session_scope(s, ws):
        item = await s.get(SafeModeQueueItemRow, item_id)
        assert item is not None
        return item


async def _do(sf, ws: uuid.UUID, route: Route) -> Any:  # noqa: ANN001
    async with sf() as s:
        routed = await route(s, ws)
        await s.commit()
    return routed


_PATHS = [
    pytest.param(_seed_issue, _route_edit, id="issue_edit"),
    pytest.param(_seed_issue, _route_comment, id="issue_comment"),
    pytest.param(_seed_pr, _route_pr, id="pr_comment"),
]


@pytest.mark.parametrize("status", [RunStatus.REVIEW_READY, RunStatus.FAILED])
@pytest.mark.parametrize(("seed", "route"), _PATHS)
async def test_reopen_denies_pending_card(seed, route, status) -> None:  # noqa: ANN001
    async with db_engine(Base) as (engine, _pg):
        sf = async_sessionmaker(engine, expire_on_commit=False)
        ws, run_id = await seed(sf, status)
        card = await _card(sf, ws, run_id)
        assert await _do(sf, ws, route) == run_id
        item = await _get(sf, ws, card)
        async with sf() as s, workspace_session_scope(s, ws):
            run = await s.get(ExecutionRun, run_id)
            rows = (await s.execute(ExecutionRunActivity.__table__.select())).mappings().all()

    assert run is not None and run.status is RunStatus.OPEN
    assert item.status is SafeModeStatus.DENIED
    assert item.deny_reason == REOPEN_DENY_REASON
    assert item.decided_at is not None
    assert any(r["activity_type"] == SAFE_MODE_CLEARED_ACTIVITY for r in rows)


@pytest.mark.parametrize(("seed", "route"), _PATHS)
async def test_approved_card_untouched(seed, route) -> None:  # noqa: ANN001
    async with db_engine(Base) as (engine, _pg):
        sf = async_sessionmaker(engine, expire_on_commit=False)
        ws, run_id = await seed(sf, RunStatus.REVIEW_READY)
        card = await _card(sf, ws, run_id, SafeModeStatus.APPROVED)
        await _do(sf, ws, route)
        item = await _get(sf, ws, card)

    assert item.status is SafeModeStatus.APPROVED
    assert item.deny_reason is None


@pytest.mark.parametrize(("seed", "route"), _PATHS)
async def test_other_runs_card_untouched(seed, route) -> None:  # noqa: ANN001
    async with db_engine(Base) as (engine, _pg):
        sf = async_sessionmaker(engine, expire_on_commit=False)
        ws, run_id = await seed(sf, RunStatus.REVIEW_READY)
        other = await _card(sf, ws, uuid.uuid4())
        await _do(sf, ws, route)
        item = await _get(sf, ws, other)

    assert item.status is SafeModeStatus.PENDING


@pytest.mark.parametrize("status", [RunStatus.OPEN, RunStatus.RUNNING])
@pytest.mark.parametrize(("seed", "route"), _PATHS)
async def test_in_flight_run_card_untouched(seed, route, status) -> None:  # noqa: ANN001
    async with db_engine(Base) as (engine, _pg):
        sf = async_sessionmaker(engine, expire_on_commit=False)
        ws, run_id = await seed(sf, status)
        card = await _card(sf, ws, run_id)
        assert await _do(sf, ws, route) == run_id
        item = await _get(sf, ws, card)

    assert item.status is SafeModeStatus.PENDING


async def test_new_card_after_reopen_stays_pending() -> None:
    async with db_engine(Base) as (engine, _pg):
        sf = async_sessionmaker(engine, expire_on_commit=False)
        ws, run_id = await _seed_issue(sf, RunStatus.REVIEW_READY)
        old = await _card(sf, ws, run_id)
        await _do(sf, ws, _route_edit)
        new = await _card(sf, ws, run_id)  # the reopened run's new deliverable
        old_item, new_item = await _get(sf, ws, old), await _get(sf, ws, new)

    assert old_item.status is SafeModeStatus.DENIED
    assert new_item.status is SafeModeStatus.PENDING


def test_timeline_line() -> None:
    assert (
        _activity_label(SAFE_MODE_CLEARED_ACTIVITY, {}, "ko") == "다시 열려 대기 카드를 정리했어요"
    )
    assert _activity_label(SAFE_MODE_CLEARED_ACTIVITY, {}, "en")
