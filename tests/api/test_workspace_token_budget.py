"""#930 part 1 — the per-workspace cumulative token budget.

The per-run ceiling (``agent_max_run_tokens``, enforced once in ``_drive_loop``)
bounds ONE run. Nothing bounded a workspace: three concurrent runs, forever, is
inside ``max_concurrent_runs`` and unbounded in tokens. #953 sealed the meter
leaks, so ``execution_runs.usage_*`` is now a total worth budgeting against.

Four propositions this suite exists to pin:

* **The window is not a lifetime total.** A lifetime total can only ever be
  exhausted — it is a fuse, not a budget. ``test_spend_before_the_window_does_
  not_count`` is the test that would fail if the implementation quietly summed
  every run a workspace ever made, and it carries a companion assertion that the
  same rows summed WITHOUT the window DO exceed the limit (otherwise it passes
  for free).
* **``None`` is still unlimited.** Same semantics as ``max_concurrent_runs`` —
  it is how a workspace comes OFF the plan.
* **Spend is spend.** Unlike the concurrent-run cap, a terminal run still counts
  its tokens: shipping a run does not refund the provider bill.
* **The two ceilings are independent.** Turning the per-run ceiling off
  (``agent_max_run_tokens=0``) must not turn the workspace budget off with it.

The refusal mirrors ``run_caps`` exactly rather than inventing a third shape:
REST answers 429 with a structured ``detail``, MCP raises a ``ToolError``
(``tests/mcp/test_direct_workspace_token_budget.py``).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import backend.identity.db  # noqa: F401 — register mappers
import backend.identity.workspaces_db  # noqa: F401
import backend.workflow.infrastructure.db  # noqa: F401
import backend.workflow.infrastructure.intake.db  # noqa: F401
from backend.api.deps import (
    get_current_user,
    get_current_user_row,
    get_db_session,
    get_workspace_id,
)
from backend.api.main import create_app
from backend.identity.workspaces_db import ProductRow, WorkspaceRow
from backend.workflow.application.workspace_token_budget import (
    DEFAULT_MONTHLY_TOKEN_BUDGET,
    budget_window_start,
    sum_workspace_tokens_since,
)
from backend.workflow.infrastructure.db import ExecutionRun, RunStatus

from .._support import db_engine, fake_current_user

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest_asyncio.fixture
async def sf() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with db_engine() as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
def workspace_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def founder_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest_asyncio.fixture
async def client(
    sf: async_sessionmaker[AsyncSession], workspace_id: uuid.UUID, founder_id: uuid.UUID
) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app()

    def _ws() -> uuid.UUID:
        return workspace_id

    def _user_row() -> SimpleNamespace:
        return SimpleNamespace(id=founder_id)

    async def _session() -> AsyncIterator[AsyncSession]:
        async with sf() as s:
            yield s

    app.dependency_overrides[get_current_user] = fake_current_user()
    app.dependency_overrides[get_workspace_id] = _ws
    app.dependency_overrides[get_current_user_row] = _user_row
    app.dependency_overrides[get_db_session] = _session

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


#: A run seeded at this instant is unambiguously INSIDE the current window on
#: both test tiers. ``datetime.now(UTC)`` would be ambiguous on the 1st of the
#: month (the window opens partway through that day); the window's own start is
#: not — it is the boundary itself.
def _inside() -> datetime:
    return budget_window_start() + timedelta(seconds=1)


def _outside() -> datetime:
    """One day before the window opened — the previous period, on both tiers.

    Seeded tz-AWARE on purpose. ``execution_runs.created_at``'s ORM default is a
    naive ``datetime.now()``, and a naive value is stored verbatim by SQLite but
    localized (here: −9h) by asyncpg — measured on the probe PG. A boundary test
    seeded naive would therefore mean two different instants on the two tiers.
    """
    return budget_window_start() - timedelta(days=1)


async def _seed(
    sf: async_sessionmaker[AsyncSession],
    *,
    workspace_id: uuid.UUID,
    budget: int | None,
    spend: tuple[tuple[int, RunStatus, datetime], ...] = (),
) -> uuid.UUID:
    """Seed a workspace at ``budget`` with one run per ``(tokens, status, at)``.

    Returns the product id — every direct submit needs one (L-P1).
    """
    product_id = uuid.uuid4()
    now = datetime.now(tz=UTC)
    async with sf() as s:
        s.add(WorkspaceRow(id=workspace_id, name="budgeted", created_at=now, updated_at=now))
        await s.flush()
        # An INSERT cannot say "unlimited": the default is DDL-side and
        # SQLAlchemy drops a ``None`` column carrying a ``server_default``.
        # Coming off the plan is an UPDATE — here and in the migration alike.
        await s.execute(
            update(WorkspaceRow)
            .where(WorkspaceRow.id == workspace_id)
            .values(monthly_token_budget=budget)
        )
        s.add(
            ProductRow(
                id=product_id,
                workspace_id=workspace_id,
                name="test-product",
                slug="test-product",
                created_at=now,
                updated_at=now,
            )
        )
        for tokens, status, at in spend:
            s.add(
                ExecutionRun(
                    id=uuid.uuid4(),
                    workspace_id=workspace_id,
                    product_id=product_id,
                    status=status,
                    payload={},
                    # Split across the two meter columns so a rule that summed
                    # only one of them fails here rather than passing at half.
                    usage_prompt_tokens=tokens - tokens // 4,
                    usage_completion_tokens=tokens // 4,
                    created_at=at,
                    updated_at=at,
                )
            )
        await s.commit()
    return product_id


# ---------------------------------------------------------------------------
# The budget
# ---------------------------------------------------------------------------
async def test_a_workspace_under_its_budget_can_submit(sf, client, workspace_id) -> None:
    """The control. Without it every refusal below is also satisfied by an
    endpoint that refuses everything."""
    await _seed(
        sf,
        workspace_id=workspace_id,
        budget=10_000,
        spend=((4_000, RunStatus.REVIEW_READY, _inside()),),
    )

    response = await client.post("/api/v1/messages", json={"text": "plenty left"})

    assert response.status_code == 202, response.text


async def test_a_workspace_over_its_budget_is_refused(sf, client, workspace_id) -> None:
    await _seed(
        sf,
        workspace_id=workspace_id,
        budget=10_000,
        spend=(
            (6_000, RunStatus.SHIPPED, _inside()),
            (6_000, RunStatus.REVIEW_READY, _inside()),
        ),
    )

    response = await client.post("/api/v1/messages", json={"text": "over the line"})

    assert response.status_code == 429, response.text


async def test_spend_before_the_window_does_not_count(sf, client, workspace_id) -> None:
    """⭐ The proposition that separates a BUDGET from a lifetime fuse.

    A lifetime total can only ever be exhausted: once a workspace has spent its
    number it is dead forever, with no way back short of an operator UPDATE.
    The budget is therefore scoped to the current calendar month (UTC), and this
    is the test that fails if someone quietly drops the predicate.
    """
    await _seed(
        sf,
        workspace_id=workspace_id,
        budget=10_000,
        spend=(
            (900_000, RunStatus.SHIPPED, _outside()),  # last period — not ours
            (1_000, RunStatus.SHIPPED, _inside()),
        ),
    )

    response = await client.post("/api/v1/messages", json={"text": "new period, new budget"})

    assert response.status_code == 202, response.text

    # Companion: the same rows summed WITHOUT the window DO blow the budget, so
    # the 202 above is the window doing work — not a workspace that was under
    # the limit either way.
    async with sf() as s:
        lifetime = await sum_workspace_tokens_since(
            s, workspace_id, datetime(1970, 1, 1, tzinfo=UTC)
        )
        in_window = await sum_workspace_tokens_since(s, workspace_id, budget_window_start())
    assert lifetime > 10_000, "the out-of-window spend was not seeded — test is vacuous"
    assert in_window < 10_000
    assert lifetime > in_window


async def test_a_null_budget_is_unlimited(sf, client, workspace_id) -> None:
    """``NULL`` is how a workspace comes off the plan — the operator's own does,
    in this feature's migration exactly as it does in the run-cap one."""
    await _seed(
        sf,
        workspace_id=workspace_id,
        budget=None,
        spend=((500_000_000, RunStatus.SHIPPED, _inside()),),
    )

    response = await client.post("/api/v1/messages", json={"text": "no ceiling here"})

    assert response.status_code == 202, response.text


async def test_terminal_runs_still_spend_the_budget(sf, client, workspace_id) -> None:
    """Deliberately the OPPOSITE of ``max_concurrent_runs``.

    The run cap counts what a workspace HOLDS, so shipping a run frees a slot.
    The token budget counts what a workspace SPENT, and shipping a run does not
    refund the provider bill. A rule that reused the run cap's ``not_in(
    TERMINAL_RUN_STATUSES)`` predicate would answer 202 here.
    """
    await _seed(
        sf,
        workspace_id=workspace_id,
        budget=10_000,
        spend=(
            (5_000, RunStatus.SHIPPED, _inside()),
            (5_000, RunStatus.FAILED, _inside()),
            (5_000, RunStatus.CANCELLED, _inside()),
        ),
    )

    response = await client.post("/api/v1/messages", json={"text": "all finished, all spent"})

    assert response.status_code == 429, response.text


async def test_the_budget_counts_only_this_workspace(sf, client, workspace_id) -> None:
    """Another tenant's spend must not consume this workspace's budget."""
    await _seed(
        sf,
        workspace_id=workspace_id,
        budget=10_000,
        spend=((1_000, RunStatus.SHIPPED, _inside()),),
    )
    await _seed(
        sf,
        workspace_id=uuid.uuid4(),
        budget=10_000,
        spend=((90_000, RunStatus.SHIPPED, _inside()),),
    )

    response = await client.post("/api/v1/messages", json={"text": "my own budget is free"})

    assert response.status_code == 202, response.text


async def test_the_refusal_names_the_limit_and_carries_a_machine_code(
    sf, client, workspace_id
) -> None:
    """Same shape as ``run_cap_reached``: a structured detail, not a sentence.

    The PWA writes its own localized copy (an English string would land verbatim
    on a KO surface) and must not hardcode a limit that differs per workspace.
    ``window_start`` is in there because "when do I get it back" is the only
    actionable thing a founder can do about this refusal.
    """
    await _seed(
        sf,
        workspace_id=workspace_id,
        budget=10_000,
        spend=((12_000, RunStatus.SHIPPED, _inside()),),
    )

    response = await client.post("/api/v1/messages", json={"text": "tell me why"})

    assert response.status_code == 429, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "workspace_token_budget_reached"
    assert detail["limit"] == 10_000
    assert detail["used"] >= 12_000
    assert detail["window_start"] == budget_window_start().isoformat()


async def test_a_new_workspace_starts_on_the_default_budget(sf, workspace_id) -> None:
    """The column's default IS the plan. A workspace nobody configured is
    budgeted, not unlimited — a fail-open default would price nothing, and under
    public signup (founder decision, 2026-09-14) it would price nothing for
    every stranger who signs up."""
    now = datetime.now(tz=UTC)
    async with sf() as s:
        s.add(WorkspaceRow(id=workspace_id, name="fresh", created_at=now, updated_at=now))
        await s.commit()
    async with sf() as s:
        row = await s.get(WorkspaceRow, workspace_id)
        assert row is not None
        assert row.monthly_token_budget == DEFAULT_MONTHLY_TOKEN_BUDGET


async def test_the_budget_holds_when_the_per_run_ceiling_is_switched_off(
    sf, client, workspace_id, monkeypatch
) -> None:
    """⭐ Independence, direction 1.

    ``agent_max_run_tokens = 0`` is a supported configuration ("uncapped,
    metering still runs" — ``token_budget``'s own contract). If the workspace
    budget were folded into the per-run ceiling's ``cap <= 0`` short-circuit,
    switching that off would silently switch this off too.
    """
    from backend.config import get_settings

    monkeypatch.setenv("BSVIBE_AGENT_MAX_RUN_TOKENS", "0")
    get_settings.cache_clear()
    try:
        assert get_settings().agent_max_run_tokens == 0, "the per-run ceiling is not off"
        await _seed(
            sf,
            workspace_id=workspace_id,
            budget=10_000,
            spend=((50_000, RunStatus.SHIPPED, _inside()),),
        )

        response = await client.post("/api/v1/messages", json={"text": "ceiling off, budget on"})

        assert response.status_code == 429, response.text
    finally:
        get_settings.cache_clear()


async def test_a_workspace_out_of_budget_can_still_ask_a_question(sf, client, workspace_id) -> None:
    """Mirrors the run cap's equivalent. ``/messages/ask`` answers inline — no
    run, no executor, no meter. Gating it would charge the founder for something
    that costs them nothing and leave them unable to ask how to get unblocked.
    (It answers ``answered=False`` here — no chat model is configured; what
    matters is that it is not a 429.)"""
    await _seed(
        sf,
        workspace_id=workspace_id,
        budget=10_000,
        spend=((99_000, RunStatus.SHIPPED, _inside()),),
    )

    response = await client.post("/api/v1/messages/ask", json={"text": "how do I get budget?"})

    assert response.status_code == 200, response.text


async def test_the_run_cap_still_answers_first_when_both_would_refuse(
    sf, client, workspace_id
) -> None:
    """The order is deliberate and load-bearing for the PWA.

    A workspace that is BOTH at its run cap and over its token budget keeps
    getting ``run_cap_reached`` — the code the PWA already knows how to render
    with a "ship or discard a run" hint. Adding a second gate must not silently
    relabel an existing refusal.
    """
    await _seed(
        sf,
        workspace_id=workspace_id,
        budget=10_000,
        spend=(
            (20_000, RunStatus.REVIEW_READY, _inside()),
            (1, RunStatus.REVIEW_READY, _inside()),
            (1, RunStatus.REVIEW_READY, _inside()),
        ),
    )
    async with sf() as s:
        await s.execute(
            update(WorkspaceRow)
            .where(WorkspaceRow.id == workspace_id)
            .values(max_concurrent_runs=3)
        )
        await s.commit()

    response = await client.post("/api/v1/messages", json={"text": "both walls at once"})

    assert response.status_code == 429, response.text
    assert response.json()["detail"]["code"] == "run_cap_reached"
