"""#1109 — a GitHub-delivered run is ``shipped`` when its PR MERGES, not when it opens.

The run used to be marked ``shipped`` the moment its PR opened, and a merge
conflict then dragged it back ``shipped → open`` so the agent could re-resolve.
"Terminal" was not terminal, against every reaper, cap and dashboard keyed on
``TERMINAL_RUN_STATUSES``. 형님 ruled (2026-10-04): shipped only after merge.

The rules this pins:

* PR opens AND a merge watch is on it → the run waits at ``review_ready``, marked
  ``awaiting_merge``. With no watch row (auto-merge off, or the enqueue failed)
  nothing would ever observe the merge, so the run ships on open as before.
* the watch sees the merge → ``shipped``; the PR closed unmerged → ``cancelled``.
* a run awaiting merge does not hold one of the workspace's concurrent-run slots.
* the "watch gave up" Decision can now discard the run, and a free-text reply on
  it never re-drives finished work.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.data.rls import workspace_session_scope
from backend.workflow.application.run_delivery_resolution import (
    AWAITING_MERGE_KEY,
    auto_resolve_run_on_delivery,
)
from backend.workflow.infrastructure.db import (
    Decision,
    DecisionStatus,
    Deliverable,
    DeliverableType,
    ExecutionRun,
    RunStatus,
)
from backend.workflow.infrastructure.github.db import GithubMergeWatchRow, MergeWatchStatus

from .._support import db_engine

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def sf():
    async with db_engine() as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


async def _seed(
    sf_: Any,
    *,
    status: RunStatus = RunStatus.REVIEW_READY,
    watch: MergeWatchStatus | None = MergeWatchStatus.PENDING_CI,
    decision_kind: str | None = None,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    workspace_id = uuid.uuid4()
    run_id = uuid.uuid4()
    deliverable_id = uuid.uuid4()
    now = datetime.now(tz=UTC)
    async with sf_() as s, workspace_session_scope(s, workspace_id):
        s.add(
            ExecutionRun(
                id=run_id,
                workspace_id=workspace_id,
                product_id=uuid.uuid4(),
                status=status,
                # What AgentRunner.transition records for a github-bound run.
                payload={"delivers_via_local_product_repo": False},
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
                payload={"summary": "ship it"},
                created_at=now,
            )
        )
        if decision_kind is not None:
            s.add(
                Decision(
                    id=uuid.uuid4(),
                    run_id=run_id,
                    workspace_id=workspace_id,
                    decision=decision_kind,
                    payload={"reason": "no_verification_declared"},
                    status=DecisionStatus.PENDING,
                    created_at=now,
                )
            )
        if watch is not None:
            s.add(
                GithubMergeWatchRow(
                    id=uuid.uuid4(),
                    workspace_id=workspace_id,
                    run_id=run_id,
                    deliverable_id=deliverable_id,
                    repo="acme/site",
                    pr_number=9,
                    branch="bsvibe/run-x",
                    base_branch="main",
                    status=watch,
                    next_poll_at=now,
                    deadline_at=now + timedelta(hours=1),
                )
            )
        await s.commit()
    return workspace_id, run_id, deliverable_id


async def _run(sf_: Any, workspace_id: uuid.UUID, run_id: uuid.UUID) -> ExecutionRun:
    async with sf_() as s, workspace_session_scope(s, workspace_id):
        run = await s.get(ExecutionRun, run_id)
        assert run is not None
        return run


# ── B. delivery: wait for the merge when something is watching for it ────────


async def test_a_watched_pr_leaves_the_run_awaiting_merge(sf) -> None:
    workspace_id, run_id, deliverable_id = await _seed(sf)

    async with sf() as s, workspace_session_scope(s, workspace_id):
        await auto_resolve_run_on_delivery(s, deliverable_id=deliverable_id)
        await s.commit()

    run = await _run(sf, workspace_id, run_id)
    assert run.status is RunStatus.REVIEW_READY
    assert run.payload[AWAITING_MERGE_KEY]["pr_number"] == 9


async def test_an_unwatched_pr_still_ships_on_open(sf) -> None:
    """No watch row → nothing would ever see the merge; waiting would strand it."""
    workspace_id, run_id, deliverable_id = await _seed(sf, watch=None)

    async with sf() as s, workspace_session_scope(s, workspace_id):
        await auto_resolve_run_on_delivery(s, deliverable_id=deliverable_id)
        await s.commit()

    assert (await _run(sf, workspace_id, run_id)).status is RunStatus.SHIPPED


async def test_a_run_paused_on_review_also_waits_for_the_merge(sf) -> None:
    """The review-Decision path resolved the Decision and shipped in one go."""
    workspace_id, run_id, deliverable_id = await _seed(
        sf, status=RunStatus.RUNNING, decision_kind="human_review_required"
    )

    async with sf() as s, workspace_session_scope(s, workspace_id):
        await auto_resolve_run_on_delivery(s, deliverable_id=deliverable_id)
        await s.commit()

    run = await _run(sf, workspace_id, run_id)
    assert run.status is RunStatus.REVIEW_READY
    assert AWAITING_MERGE_KEY in run.payload


# ── C. the watch's conclusion moves the run ───────────────────────────────────


async def _await_merge(sf_: Any) -> tuple[uuid.UUID, uuid.UUID]:
    workspace_id, run_id, deliverable_id = await _seed(sf_)
    async with sf_() as s, workspace_session_scope(s, workspace_id):
        await auto_resolve_run_on_delivery(s, deliverable_id=deliverable_id)
        await s.commit()
    return workspace_id, run_id


async def test_the_merge_ships_the_run(sf) -> None:
    from backend.workflow.application.runtime.merge_watch_runtime import (
        build_merge_watch_pr_concluded,
    )

    workspace_id, run_id = await _await_merge(sf)

    await build_merge_watch_pr_concluded(session_factory=sf)(run_id, merged=True, pr_number=9)

    run = await _run(sf, workspace_id, run_id)
    assert run.status is RunStatus.SHIPPED
    assert AWAITING_MERGE_KEY not in run.payload


async def test_a_pr_closed_unmerged_cancels_the_run(sf) -> None:
    from backend.workflow.application.runtime.merge_watch_runtime import (
        build_merge_watch_pr_concluded,
    )

    workspace_id, run_id = await _await_merge(sf)

    await build_merge_watch_pr_concluded(session_factory=sf)(run_id, merged=False, pr_number=9)

    run = await _run(sf, workspace_id, run_id)
    assert run.status is RunStatus.CANCELLED
    assert AWAITING_MERGE_KEY not in run.payload


async def test_a_run_shipped_before_this_change_stays_shipped(sf) -> None:
    """Runs delivered under the old rule were shipped on open; the merge is a no-op."""
    from backend.workflow.application.runtime.merge_watch_runtime import (
        build_merge_watch_pr_concluded,
    )

    workspace_id, run_id, _ = await _seed(sf, status=RunStatus.SHIPPED)

    await build_merge_watch_pr_concluded(session_factory=sf)(run_id, merged=False, pr_number=9)

    assert (await _run(sf, workspace_id, run_id)).status is RunStatus.SHIPPED


# ── D. a run waiting on a merge holds no concurrent-run slot ──────────────────


async def test_a_run_awaiting_merge_is_not_counted_against_the_cap(sf) -> None:
    from backend.workflow.application.run_caps import count_held_runs

    workspace_id, _ = await _await_merge(sf)

    async with sf() as s, workspace_session_scope(s, workspace_id):
        assert await count_held_runs(s, workspace_id) == 0


async def test_a_run_awaiting_review_still_counts(sf) -> None:
    """Control — the cap's rule for review_ready is unchanged (run_caps docstring)."""
    from backend.workflow.application.run_caps import count_held_runs

    workspace_id, _, _ = await _seed(sf, watch=None)  # review_ready, not delivered

    async with sf() as s, workspace_session_scope(s, workspace_id):
        assert await count_held_runs(s, workspace_id) == 1


# ── E. the "watch gave up" Decision on a run that now waits at review_ready ───


async def _stalled(sf_: Any) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    workspace_id, run_id = await _await_merge(sf_)
    decision_id = uuid.uuid4()
    async with sf_() as s, workspace_session_scope(s, workspace_id):
        s.add(
            Decision(
                id=decision_id,
                run_id=run_id,
                workspace_id=workspace_id,
                decision="merge_watch_stalled",
                payload={"reason": "ci_failed", "repo": "acme/site", "pr_number": 9},
                status=DecisionStatus.PENDING,
                created_at=datetime.now(tz=UTC),
            )
        )
        await s.commit()
    return workspace_id, run_id, decision_id


def test_the_stalled_decision_can_discard_the_run() -> None:
    from backend.workflow.application._checkpoint_shared import (
        ACTION_ACKNOWLEDGE,
        ACTION_DISCARD,
        _decision_actions,
    )

    decision = Decision(decision="merge_watch_stalled", payload={"reason": "ci_failed"})
    actions = _decision_actions(decision)
    assert actions is not None
    assert {a.key for a in actions} == {ACTION_ACKNOWLEDGE, ACTION_DISCARD}


async def test_discarding_a_stalled_run_cancels_it(sf) -> None:
    from backend.workflow.application._checkpoint_shared import ACTION_DISCARD
    from backend.workflow.application.checkpoint_resolution import resolve_checkpoint

    workspace_id, run_id, decision_id = await _stalled(sf)

    async with sf() as s, workspace_session_scope(s, workspace_id):
        await resolve_checkpoint(
            s,
            workspace_id=workspace_id,
            checkpoint_id=decision_id,
            action_key=ACTION_DISCARD,
            actor_id=uuid.uuid4(),
        )
        await s.commit()

    assert (await _run(sf, workspace_id, run_id)).status is RunStatus.CANCELLED


async def test_a_free_text_reply_on_a_stalled_run_does_not_re_drive_it(sf) -> None:
    """The run's work is done and out as a PR; resuming it would re-run the agent."""
    from backend.workflow.application.checkpoint_resolution import resolve_checkpoint

    workspace_id, run_id, decision_id = await _stalled(sf)

    async with sf() as s, workspace_session_scope(s, workspace_id):
        await resolve_checkpoint(
            s,
            workspace_id=workspace_id,
            checkpoint_id=decision_id,
            answer="I'll merge it myself",
            actor_id=uuid.uuid4(),
        )
        await s.commit()

    assert (await _run(sf, workspace_id, run_id)).status is RunStatus.REVIEW_READY
