"""The missing GitHub App ``issues: write`` permission reaches the founder — once.

#1221 left it as a log line nobody reads, remembered in process memory, so a
restart (or a second worker) repeated it. Now it is a ``github_app_permission_missing``
Decision (a report: the PR is already open) whose existence IS the "already told".
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.data.rls import workspace_session_scope
from backend.notifications.db import NotificationEventRow
from backend.workflow.application._checkpoint_shared import (
    ACTION_ACKNOWLEDGE,
    _decision_actions,
    _question_text,
)
from backend.workflow.application.delivery.connector_dispatch import _github_pr
from backend.workflow.infrastructure.db import Decision, RunStatus

from .._support import db_engine
from .test_every_decision_has_a_way_out import _seed

_FORBIDDEN = RuntimeError("Client error '403 Forbidden' Resource not accessible by integration")


@pytest_asyncio.fixture
async def sf():
    async with db_engine() as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


class _Runner:
    """Opens the PR fine; the issue comment fails with ``comment_error``."""

    def __init__(self, comment_error: Exception) -> None:
        self._comment_error = comment_error

    async def dispatch_action(self, plugin: Any, *, action_name: str, **_: Any) -> Any:
        if action_name == "open_pr":
            return {"url": "https://github.com/o/r/pull/9", "pr_number": 9}
        raise self._comment_error


async def _deliver(sf_: Any, workspace_id: uuid.UUID, run_id: uuid.UUID, error: Exception) -> None:
    deps = SimpleNamespace(
        session_factory=sf_,
        plugins_by_name={"github": object()},
        runner=_Runner(error),
        settings=None,
    )
    binding = SimpleNamespace(
        repo="o/r", base_branch="main", account=SimpleNamespace(delivery_config={})
    )
    await _github_pr._open_pr_and_settle(
        deps=deps,  # type: ignore[arg-type]
        binding=binding,  # type: ignore[arg-type]
        workspace_id=workspace_id,
        deliverable_id=uuid.uuid4(),
        run_id=run_id,
        branch="b",
        title="t",
        body="x",
        token="tok",
        source_issue=5,
        action_prefix="github:outbound:code",
        pushed_by="server",
    )


async def _decisions(sf_: Any, workspace_id: uuid.UUID) -> list[Decision]:
    async with sf_() as s, workspace_session_scope(s, workspace_id):
        rows = await s.execute(
            select(Decision).where(Decision.decision == "github_app_permission_missing")
        )
        return list(rows.scalars())


async def _notifications(sf_: Any, workspace_id: uuid.UUID) -> int:
    async with sf_() as s, workspace_session_scope(s, workspace_id):
        rows = await s.execute(
            select(NotificationEventRow).where(NotificationEventRow.event == "needs_you")
        )
        return len(list(rows.scalars()))


async def test_a_403_on_the_issue_comment_tells_the_founder(sf) -> None:
    workspace_id, run_id, _ = await _seed(sf, labels=[])

    await _deliver(sf, workspace_id, run_id, _FORBIDDEN)

    [decision] = await _decisions(sf, workspace_id)
    assert decision.payload["repo"] == "o/r"
    assert decision.payload["hint"] == _github_pr._ISSUES_WRITE_HINT
    assert "github-app-permissions.md" in _question_text(decision, "ko")
    actions = _decision_actions(decision)
    assert actions is not None and {a.key for a in actions} == {ACTION_ACKNOWLEDGE}
    assert await _notifications(sf, workspace_id) == 1
    async with sf() as s, workspace_session_scope(s, workspace_id):
        from backend.workflow.infrastructure.db import ExecutionRun

        run = await s.get(ExecutionRun, run_id)
        assert run is not None and run.status is RunStatus.RUNNING  # the run is not blocked


async def test_a_second_403_adds_nothing_even_in_a_fresh_process(sf) -> None:
    workspace_id, run_id, _ = await _seed(sf, labels=[])

    await _deliver(sf, workspace_id, run_id, _FORBIDDEN)
    await _deliver(sf, workspace_id, run_id, _FORBIDDEN)

    assert len(await _decisions(sf, workspace_id)) == 1
    assert await _notifications(sf, workspace_id) == 1


async def test_an_acknowledged_notice_is_not_raised_again(sf) -> None:
    workspace_id, run_id, _ = await _seed(sf, labels=[])
    await _deliver(sf, workspace_id, run_id, _FORBIDDEN)

    from backend.workflow.application.checkpoint_resolution import resolve_checkpoint

    [decision] = await _decisions(sf, workspace_id)
    async with sf() as s, workspace_session_scope(s, workspace_id):
        await resolve_checkpoint(
            s,
            workspace_id=workspace_id,
            checkpoint_id=decision.id,
            action_key=ACTION_ACKNOWLEDGE,
            actor_id=uuid.uuid4(),
        )
        await s.commit()

    await _deliver(sf, workspace_id, run_id, _FORBIDDEN)

    assert len(await _decisions(sf, workspace_id)) == 1


async def test_another_workspace_is_told_separately(sf) -> None:
    first, first_run, _ = await _seed(sf, labels=[])
    second, second_run, _ = await _seed(sf, labels=[])

    await _deliver(sf, first, first_run, _FORBIDDEN)
    await _deliver(sf, second, second_run, _FORBIDDEN)

    assert len(await _decisions(sf, first)) == 1
    assert len(await _decisions(sf, second)) == 1


async def test_a_failure_that_is_not_a_missing_permission_tells_nobody(sf) -> None:
    workspace_id, run_id, _ = await _seed(sf, labels=[])

    await _deliver(sf, workspace_id, run_id, RuntimeError("500 Internal Server Error"))

    assert await _decisions(sf, workspace_id) == []
    assert await _notifications(sf, workspace_id) == 0
