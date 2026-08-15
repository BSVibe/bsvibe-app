"""One pull request, one watch row.

``enqueue_merge_watch`` inserts unconditionally. Nothing stops the same PR being
registered twice, and live it was: run ``e53e9b5c`` landed two CODE deliverables
(07:56 and 08:14), each was delivered to github, each found the SAME pull
request #754, and each added a watch row six seconds apart.

Two rows for one PR means the poller does everything twice — twice the GitHub
API calls, two racers on the same per-repo merge lock, and, once #746 taught the
watch to speak up when it gives up, **the founder is told the same thing twice**
(two identical ``merge_watch_stalled`` Decisions, 02:21:39 and 02:21:45).

``(repo, pr_number)`` is unique forever — GitHub never reuses a PR number within
a repo — so this is a structural guarantee, not a race to check for.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select

from backend.workflow.infrastructure.github.db import (
    GithubMergeWatchRow,
    MergeWatchStatus,
)

from .._support import memory_session

pytestmark = pytest.mark.asyncio


def _row(*, repo: str = "acme/app", pr_number: int = 754) -> GithubMergeWatchRow:
    now = datetime.now(tz=UTC)
    return GithubMergeWatchRow(
        id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        deliverable_id=uuid.uuid4(),
        repo=repo,
        pr_number=pr_number,
        branch="run/abc",
        base_branch="main",
        status=MergeWatchStatus.PENDING_CI,
        attempts=0,
        next_poll_at=now,
        deadline_at=now + timedelta(hours=1),
        conflict_dispatched=False,
        created_at=now,
    )


async def test_the_same_pr_is_only_watched_once() -> None:
    """The second delivery of the same run must not double the watch."""
    from backend.workflow.infrastructure.github.repository import (
        GithubMergeWatchRepository,
    )

    async with memory_session() as session:
        repo = GithubMergeWatchRepository(session)
        first = await repo.add(_row())
        await session.commit()
        second = await repo.add(_row())
        await session.commit()

        rows = (await session.execute(select(GithubMergeWatchRow))).scalars().all()

    assert len(rows) == 1, "one PR, one watch row"
    assert first is not None
    assert second is None, "the duplicate reports itself rather than inserting"


async def test_a_different_pr_in_the_same_repo_is_watched() -> None:
    """The guard is per PR, not per repo — a repo has many PRs in flight."""
    from backend.workflow.infrastructure.github.repository import (
        GithubMergeWatchRepository,
    )

    async with memory_session() as session:
        repo = GithubMergeWatchRepository(session)
        await repo.add(_row(pr_number=754))
        await repo.add(_row(pr_number=755))
        await session.commit()
        rows = (await session.execute(select(GithubMergeWatchRow))).scalars().all()

    assert len(rows) == 2


async def test_the_same_number_in_another_repo_is_watched() -> None:
    """PR numbers are per-repo — ``acme/app#754`` and ``acme/site#754`` are
    different pull requests."""
    from backend.workflow.infrastructure.github.repository import (
        GithubMergeWatchRepository,
    )

    async with memory_session() as session:
        repo = GithubMergeWatchRepository(session)
        await repo.add(_row(repo="acme/app"))
        await repo.add(_row(repo="acme/site"))
        await session.commit()
        rows = (await session.execute(select(GithubMergeWatchRow))).scalars().all()

    assert len(rows) == 2


async def test_a_duplicate_enqueue_is_not_an_error_for_the_caller() -> None:
    """Delivery must not break because the PR was already under watch — the PR
    is open either way, and the first row is doing the job."""
    from backend.workflow.application.delivery.connector_dispatch._merge_watch import (
        enqueue_merge_watch,
    )

    async with memory_session() as session:
        repo_obj = _row()
        session.add(repo_obj)
        await session.commit()

        deps = _deps(session)
        # Must not raise, and must not add a second row.
        await enqueue_merge_watch(
            deps,
            binding=_binding(),
            workspace_id=uuid.uuid4(),
            run_id=uuid.uuid4(),
            deliverable_id=uuid.uuid4(),
            branch="run/abc",
            pr_number=754,
        )
        rows = (await session.execute(select(GithubMergeWatchRow))).scalars().all()

    assert len(rows) == 1


def _binding() -> Any:
    from types import SimpleNamespace

    return SimpleNamespace(repo="acme/app", base_branch="main", account=None)


def _deps(session: Any) -> Any:
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    from backend.config import get_settings

    @asynccontextmanager
    async def _factory() -> Any:
        yield session

    return SimpleNamespace(
        settings=get_settings().model_copy(update={"github_auto_merge_enabled": True}),
        session_factory=_factory,
    )
