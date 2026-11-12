"""#928 step 1 — a workspace is ON a plan, and the plan sets its limits.

Founder decision (2026-10-16): capacity subscription. Tokens stay BYO; BSVibe sells platform
capacity. The levers already exist as per-workspace columns — ``max_concurrent_runs`` ("the
free plan's price lever"), ``verify_stack_slots`` ("a PLAN TIER") — but nothing said which plan
a workspace was on, and coming off the free plan was operator SQL.

Now ``workspaces.plan`` names the plan and :func:`apply_plan` is the ONE place that moves a
workspace between plans, writing every limit the plan sets in the same transaction. The
payment provider's webhook (step 2) will call exactly this.

* Free: 3 runs · 1 verification slot.
* Pro: 10 runs · 2 slots.
* Operator: uncapped runs — the grandfathered operator workspace, named.
* The monthly token budget is NOT a plan limit (tokens are BYO): it is the user's own ceiling.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from backend.data.rls import workspace_session_scope
from backend.identity.plans import PLAN_LIMITS, Plan, apply_plan
from backend.identity.workspaces_db import (
    DEFAULT_MAX_CONCURRENT_RUNS,
    WorkspaceRow,
)
from backend.workflow.infrastructure.db import RunStatus

from .test_run_cap import _seed, client, founder_id, sf, workspace_id  # noqa: F401 — fixtures

pytestmark = pytest.mark.asyncio


async def _plan_row(sf, ws: uuid.UUID) -> WorkspaceRow:  # noqa: F811
    async with sf() as s, workspace_session_scope(s, ws):
        row = (await s.execute(select(WorkspaceRow).where(WorkspaceRow.id == ws))).scalar_one()
        return row


async def _apply(sf, ws: uuid.UUID, plan: Plan) -> None:  # noqa: F811
    async with sf() as s, workspace_session_scope(s, ws):
        await apply_plan(s, workspace_id=ws, plan=plan)
        await s.commit()


async def test_a_pro_workspace_submits_past_the_free_cap(sf, client, workspace_id) -> None:  # noqa: F811
    """End to end: the plan change is what the submission door reads."""
    await _seed(
        sf,
        workspace_id=workspace_id,
        cap=DEFAULT_MAX_CONCURRENT_RUNS,
        statuses=(RunStatus.REVIEW_READY,) * DEFAULT_MAX_CONCURRENT_RUNS,
    )
    refused = await client.post("/api/v1/messages", json={"text": "one more"})
    assert refused.status_code == 429, refused.text  # control: free is full

    await _apply(sf, workspace_id, Plan.PRO)

    accepted = await client.post("/api/v1/messages", json={"text": "one more"})
    assert accepted.status_code == 202, accepted.text


async def test_applying_a_plan_writes_every_limit_it_sets(sf, workspace_id) -> None:  # noqa: F811
    await _seed(sf, workspace_id=workspace_id, cap=DEFAULT_MAX_CONCURRENT_RUNS)

    await _apply(sf, workspace_id, Plan.PRO)

    row = await _plan_row(sf, workspace_id)
    assert row.plan == "pro"
    assert (row.max_concurrent_runs, row.verify_stack_slots) == (10, 2)
    assert row.monthly_token_budget is None  # no plan sets the budget


async def test_a_downgrade_restores_the_free_limits(sf, workspace_id) -> None:  # noqa: F811
    """Cancellation must put the levers BACK — a plan that only ever raises limits sells the
    paid tier forever after one month."""
    await _seed(sf, workspace_id=workspace_id, cap=DEFAULT_MAX_CONCURRENT_RUNS)
    await _apply(sf, workspace_id, Plan.PRO)

    await _apply(sf, workspace_id, Plan.FREE)

    row = await _plan_row(sf, workspace_id)
    assert row.plan == "free"
    assert (row.max_concurrent_runs, row.verify_stack_slots) == (3, 1)


async def test_the_operator_plan_is_uncapped(sf, workspace_id) -> None:  # noqa: F811
    await _seed(sf, workspace_id=workspace_id, cap=DEFAULT_MAX_CONCURRENT_RUNS)

    await _apply(sf, workspace_id, Plan.OPERATOR)

    row = await _plan_row(sf, workspace_id)
    assert row.max_concurrent_runs is None


async def test_a_new_workspace_is_born_free_with_the_free_limits(sf, workspace_id) -> None:  # noqa: F811
    """The free plan's limits and the columns' DDL defaults are one source — a new row must
    read as exactly what applying FREE would write."""
    await _seed(sf, workspace_id=workspace_id, cap=DEFAULT_MAX_CONCURRENT_RUNS)

    row = await _plan_row(sf, workspace_id)
    free = PLAN_LIMITS[Plan.FREE]
    assert row.plan == "free"
    assert (row.max_concurrent_runs, row.verify_stack_slots) == (
        free.max_concurrent_runs,
        free.verify_stack_slots,
    )
    assert row.monthly_token_budget is None


async def test_a_plan_change_does_not_touch_the_budget_the_user_chose(sf, workspace_id) -> None:  # noqa: F811
    """The monthly budget is the workspace's own spend ceiling — no plan may overwrite it."""
    await _seed(sf, workspace_id=workspace_id, cap=DEFAULT_MAX_CONCURRENT_RUNS)
    async with sf() as s, workspace_session_scope(s, workspace_id):
        row = await s.get(WorkspaceRow, workspace_id)
        assert row is not None
        row.monthly_token_budget = 1_234_567
        await s.commit()

    for plan in (Plan.PRO, Plan.FREE, Plan.OPERATOR):
        await _apply(sf, workspace_id, plan)
        assert (await _plan_row(sf, workspace_id)).monthly_token_budget == 1_234_567, plan


async def test_an_unknown_workspace_is_an_error_not_a_silent_no_op(sf) -> None:  # noqa: F811
    """A webhook for a workspace that does not exist must not report success."""
    ghost = uuid.uuid4()
    async with sf() as s, workspace_session_scope(s, ghost):
        with pytest.raises(LookupError):
            await apply_plan(s, workspace_id=ghost, plan=Plan.PRO)
