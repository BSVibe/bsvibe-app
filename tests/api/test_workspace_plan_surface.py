"""#928 step 4 — a founder can see which plan they are on and how much of it they use.

``GET /api/v1/workspace/plan`` and MCP ``bsvibe_workspace_plan`` answer the same question from
the same function (:func:`backend.workflow.application.plan_summary.load_plan_summary`): the
plan, each limit, and how much of each limit is in use — counted by the SAME functions the
gates refuse with (``run_caps.count_held_runs``, ``workspace_token_budget``), so the number on
the screen is the number that will say 429.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from backend.data.rls import workspace_session_scope
from backend.identity.plans import Plan, apply_plan
from backend.mcp.api import McpPrincipal, ToolContext, ToolRegistry
from backend.mcp.tools import register_all_tools
from backend.workflow.application.workspace_token_budget import budget_window_start
from backend.workflow.infrastructure.db import ExecutionRun, RunStatus, UnattributedLlmUsage

from .test_run_cap import _seed, client, founder_id, sf, workspace_id  # noqa: F401 — fixtures

pytestmark = pytest.mark.asyncio


async def _spend(sf, ws: uuid.UUID, product_id: uuid.UUID) -> None:  # noqa: F811
    """One held run (review_ready, 1_000 tokens), one shipped run (2_000 — spent, not held),
    one run-less ledger row (500), and last month's run (9_000 — outside the window)."""
    inside = budget_window_start() + timedelta(seconds=1)
    before = budget_window_start() - timedelta(days=1)
    async with sf() as s, workspace_session_scope(s, ws):
        for status, tokens, at in (
            (RunStatus.REVIEW_READY, 1_000, inside),
            (RunStatus.SHIPPED, 2_000, inside),
            (RunStatus.SHIPPED, 9_000, before),
        ):
            s.add(
                ExecutionRun(
                    id=uuid.uuid4(),
                    workspace_id=ws,
                    product_id=product_id,
                    status=status,
                    payload={},
                    usage_prompt_tokens=tokens,
                    usage_completion_tokens=0,
                    created_at=at,
                    updated_at=at,
                )
            )
        s.add(
            UnattributedLlmUsage(
                workspace_id=ws,
                site="knowledge_ingest",
                identity={},
                usage_prompt_tokens=400,
                usage_completion_tokens=100,
                created_at=inside,
            )
        )
        await s.commit()


async def test_the_plan_endpoint_reports_limits_and_use(sf, client, workspace_id) -> None:  # noqa: F811
    product_id = await _seed(sf, workspace_id=workspace_id, cap=3)
    await _spend(sf, workspace_id, product_id)

    r = await client.get("/api/v1/workspace/plan")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["plan"] == "free"
    assert body["runs"] == {"held": 1, "limit": 3}
    assert body["tokens"]["used"] == 3_500
    assert body["tokens"]["limit"] == 50_000_000
    assert body["verify_stack_slots"] == 1
    assert body["billing_status"] is None
    assert datetime.fromisoformat(body["tokens"]["period_start"]) == budget_window_start()


async def test_the_endpoint_follows_a_plan_change(sf, client, workspace_id) -> None:  # noqa: F811
    await _seed(sf, workspace_id=workspace_id, cap=3)
    async with sf() as s, workspace_session_scope(s, workspace_id):
        await apply_plan(s, workspace_id=workspace_id, plan=Plan.PRO)
        await s.commit()

    body = (await client.get("/api/v1/workspace/plan")).json()

    assert (body["plan"], body["runs"]["limit"], body["verify_stack_slots"]) == ("pro", 10, 2)


async def test_unlimited_reads_as_null_not_zero(sf, client, workspace_id) -> None:  # noqa: F811
    """``None`` is "no limit"; a 0 here would tell an uncapped founder they have nothing."""
    await _seed(sf, workspace_id=workspace_id, cap=3)
    async with sf() as s, workspace_session_scope(s, workspace_id):
        await apply_plan(s, workspace_id=workspace_id, plan=Plan.OPERATOR)
        await s.commit()

    body = (await client.get("/api/v1/workspace/plan")).json()

    assert (body["runs"]["limit"], body["tokens"]["limit"]) == (None, None)


async def test_mcp_reads_the_same_numbers_as_rest(sf, client, workspace_id) -> None:  # noqa: F811
    product_id = await _seed(sf, workspace_id=workspace_id, cap=3)
    await _spend(sf, workspace_id, product_id)
    rest = (await client.get("/api/v1/workspace/plan")).json()

    registry = ToolRegistry()
    register_all_tools(registry)
    async with sf() as s, workspace_session_scope(s, workspace_id):
        ctx = ToolContext(
            principal=McpPrincipal(
                user_id=uuid.uuid4(),
                workspace_id=workspace_id,
                client_id="t",
                scopes=frozenset({"mcp:read"}),
                jti=uuid.uuid4(),
            ),
            session=s,
        )
        mcp = await registry.call_tool("bsvibe_workspace_plan", {}, ctx)

    assert mcp == rest


def test_the_window_is_the_budget_window() -> None:
    """Sanity: the period this reports is the one the budget enforces (calendar month UTC)."""
    start = budget_window_start(datetime(2026, 10, 16, 3, tzinfo=UTC))
    assert start == datetime(2026, 10, 1, tzinfo=UTC)
