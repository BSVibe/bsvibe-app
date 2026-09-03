"""The free plan's concurrent-run cap — ``workspaces.max_concurrent_runs``.

The founder decision (2026-09-03): a free workspace may hold only N runs at
once, and ``review_ready`` **counts**. That last clause is the whole feature.
Prod measured 103 non-terminal runs across two workspaces and *every one of
them* was ``review_ready`` (73/73 and 30/30 — zero ``open``, zero ``running``),
so a cap that counted only in-flight work would have been a no-op on the exact
state it exists to price.

The rule lives in one place (:mod:`backend.workflow.application.run_caps`) and
each surface puts its OWN error face on it — REST answers 429, MCP raises a
``ToolError`` — mirroring how ``resolve_product_for_workspace`` owns the L-P1
rule while ``_h_direct`` only shapes the message.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
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


async def _seed(
    sf: async_sessionmaker[AsyncSession],
    *,
    workspace_id: uuid.UUID,
    cap: int | None,
    statuses: tuple[RunStatus, ...] = (),
) -> uuid.UUID:
    """Seed a workspace at ``cap`` holding one run per entry in ``statuses``.

    Returns the product id — every direct submit needs one (L-P1).
    """
    product_id = uuid.uuid4()
    now = datetime.now(tz=UTC)
    async with sf() as s:
        s.add(
            WorkspaceRow(
                id=workspace_id,
                name="capped-workspace",
                created_at=now,
                updated_at=now,
            )
        )
        await s.flush()
        # An INSERT cannot say "uncapped": the column's default is DDL-side, and
        # SQLAlchemy drops a ``None`` column from the INSERT when a
        # ``server_default`` exists (measured — passing ``None`` yields 3).
        # Coming off the plan is an UPDATE, here and in the migration alike.
        await s.execute(
            update(WorkspaceRow)
            .where(WorkspaceRow.id == workspace_id)
            .values(max_concurrent_runs=cap)
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
        for status in statuses:
            s.add(
                ExecutionRun(
                    id=uuid.uuid4(),
                    workspace_id=workspace_id,
                    product_id=product_id,
                    status=status,
                    payload={},
                    created_at=now,
                    updated_at=now,
                )
            )
        await s.commit()
    return product_id


# ---------------------------------------------------------------------------
# The cap
# ---------------------------------------------------------------------------
async def test_submit_below_the_cap_is_accepted(sf, client, workspace_id) -> None:
    """Two held runs against a cap of three still leaves room for one more."""
    await _seed(
        sf,
        workspace_id=workspace_id,
        cap=3,
        statuses=(RunStatus.REVIEW_READY, RunStatus.RUNNING),
    )

    response = await client.post("/api/v1/messages", json={"text": "one more please"})

    assert response.status_code == 202, response.text


async def test_submit_at_the_cap_is_rejected(sf, client, workspace_id) -> None:
    """A workspace already holding its whole budget cannot open another run."""
    await _seed(
        sf,
        workspace_id=workspace_id,
        cap=3,
        statuses=(RunStatus.OPEN, RunStatus.RUNNING, RunStatus.REVIEW_READY),
    )

    response = await client.post("/api/v1/messages", json={"text": "one too many"})

    assert response.status_code == 429, response.text


async def test_review_ready_runs_count_against_the_cap(sf, client, workspace_id) -> None:
    """⭐ The load-bearing proposition.

    Nothing here is in flight — no ``open``, no ``running``. This is the exact
    shape prod was measured in, and a cap that counted only in-flight work
    would answer 202 to every one of these.
    """
    await _seed(
        sf,
        workspace_id=workspace_id,
        cap=3,
        statuses=(RunStatus.REVIEW_READY,) * 3,
    )

    response = await client.post("/api/v1/messages", json={"text": "blocked by review backlog"})

    assert response.status_code == 429, response.text


async def test_terminal_runs_do_not_count_against_the_cap(sf, client, workspace_id) -> None:
    """A finished run has released the workspace — shipped/failed/cancelled are free."""
    await _seed(
        sf,
        workspace_id=workspace_id,
        cap=3,
        statuses=(
            RunStatus.SHIPPED,
            RunStatus.SHIPPED,
            RunStatus.FAILED,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
            RunStatus.CANCELLED,
            RunStatus.RUNNING,
        ),
    )

    response = await client.post("/api/v1/messages", json={"text": "six are finished"})

    assert response.status_code == 202, response.text


async def test_a_null_cap_is_unlimited(sf, client, workspace_id) -> None:
    """``NULL`` is how a workspace is taken off the free plan (the operator's own)."""
    await _seed(
        sf,
        workspace_id=workspace_id,
        cap=None,
        statuses=(RunStatus.REVIEW_READY,) * 25,
    )

    response = await client.post("/api/v1/messages", json={"text": "no ceiling here"})

    assert response.status_code == 202, response.text


async def test_the_cap_counts_only_this_workspace(sf, client, workspace_id) -> None:
    """Another tenant's backlog must not spend this workspace's budget."""
    await _seed(sf, workspace_id=workspace_id, cap=3, statuses=(RunStatus.REVIEW_READY,))
    await _seed(
        sf,
        workspace_id=uuid.uuid4(),
        cap=3,
        statuses=(RunStatus.REVIEW_READY,) * 10,
    )

    response = await client.post("/api/v1/messages", json={"text": "my own budget is free"})

    assert response.status_code == 202, response.text


async def test_the_rejection_names_the_limit_and_carries_a_machine_code(
    sf, client, workspace_id
) -> None:
    """The PWA localizes its OWN sentence, so it needs the code and the number —
    not an English string it would have to show a Korean founder verbatim, and
    not a hardcoded ``3`` that would lie for a workspace on a different cap."""
    await _seed(sf, workspace_id=workspace_id, cap=3, statuses=(RunStatus.REVIEW_READY,) * 3)

    response = await client.post("/api/v1/messages", json={"text": "tell me why"})

    assert response.status_code == 429, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "run_cap_reached"
    assert detail["limit"] == 3


async def test_a_new_workspace_starts_on_the_free_cap(sf, workspace_id) -> None:
    """The column's default IS the free plan — a workspace nobody configured
    is capped, not uncapped. A fail-open default would price nothing."""
    from backend.workflow.application.run_caps import DEFAULT_MAX_CONCURRENT_RUNS

    now = datetime.now(tz=UTC)
    async with sf() as s:
        s.add(WorkspaceRow(id=workspace_id, name="fresh", created_at=now, updated_at=now))
        await s.commit()
    async with sf() as s:
        row = await s.get(WorkspaceRow, workspace_id)
        assert row is not None
        assert row.max_concurrent_runs == DEFAULT_MAX_CONCURRENT_RUNS


async def test_a_capped_workspace_can_still_ask_a_question(sf, client, workspace_id) -> None:
    """Being out of run budget must not take away the ability to ASK.

    ``/messages/ask`` answers inline — no run, no executor, nothing held. Capping
    it would charge the founder for something that costs them no budget, and
    would leave a workspace at its ceiling unable to even ask how to get back
    under it. The endpoint answers ``answered=False`` here (no chat model is
    configured in this test) — what matters is that it is not a 429.
    """
    await _seed(sf, workspace_id=workspace_id, cap=3, statuses=(RunStatus.REVIEW_READY,) * 5)

    response = await client.post("/api/v1/messages/ask", json={"text": "how do I free a slot?"})

    assert response.status_code == 200, response.text
