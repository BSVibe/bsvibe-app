"""run_cleanup — cancel / discard a run + cascade-cancel a product's runs.

Fixes the orphaned-run bug: deleting a product left its ExecutionRuns behind
(product_id is a loose reference, no FK cascade), and there was no path to clear
a ``review_ready`` run that had no Safe Mode entry. These service functions are
the canonical primitives the MCP tools + product-delete cascade both call.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.workflow.application.run_cleanup import (
    cancel_product_runs,
    cancel_run,
    discard_run,
)
from backend.workflow.infrastructure.db import (
    Deliverable,
    DeliverableType,
    ExecutionRun,
    RunStatus,
)

from ..._support import db_engine

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def sf():
    async with db_engine() as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
def workspace_id() -> uuid.UUID:
    return uuid.uuid4()


async def _seed_run(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    *,
    status: RunStatus,
    product_id: uuid.UUID | None = None,
) -> uuid.UUID:
    run = ExecutionRun(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        product_id=product_id,
        status=status,
        payload={"text": "build the thing"},
        created_at=datetime.now(tz=UTC),
        updated_at=datetime.now(tz=UTC),
    )
    session.add(run)
    await session.flush()
    return run.id


async def _seed_deliverable(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    run_id: uuid.UUID,
    *,
    handles: list[dict] | None = None,
) -> uuid.UUID:
    d = Deliverable(
        id=uuid.uuid4(),
        run_id=run_id,
        workspace_id=workspace_id,
        deliverable_type=DeliverableType.DIRECT_OUTPUT,
        payload={},
        compensation_handles=handles,
        created_at=datetime.now(tz=UTC),
    )
    session.add(d)
    await session.flush()
    return d.id


# --- cancel_run (OPEN/RUNNING only, mirrors REST /cancel) ------------------


@pytest.mark.parametrize("status", [RunStatus.OPEN, RunStatus.RUNNING])
async def test_cancel_run_cancels_inflight(sf, workspace_id, status) -> None:
    async with sf() as s:
        run_id = await _seed_run(s, workspace_id, status=status)
        outcome = await cancel_run(s, run_id=run_id, workspace_id=workspace_id, reason="mcp")
        await s.commit()
    assert outcome.found is True
    assert outcome.cancelled is True
    assert outcome.status == "cancelled"


async def test_cancel_run_review_ready_not_cancellable(sf, workspace_id) -> None:
    async with sf() as s:
        run_id = await _seed_run(s, workspace_id, status=RunStatus.REVIEW_READY)
        outcome = await cancel_run(s, run_id=run_id, workspace_id=workspace_id, reason="mcp")
    assert outcome.found is True
    assert outcome.cancelled is False
    assert outcome.status == "review_ready"


async def test_cancel_run_cross_workspace_not_found(sf, workspace_id) -> None:
    async with sf() as s:
        run_id = await _seed_run(s, uuid.uuid4(), status=RunStatus.RUNNING)
        outcome = await cancel_run(s, run_id=run_id, workspace_id=workspace_id, reason="mcp")
    assert outcome.found is False


# --- discard_run (any non-terminal → cancelled + best-effort tombstone) ----


async def test_discard_cancels_review_ready_and_tombstones_handleless(sf, workspace_id) -> None:
    async with sf() as s:
        run_id = await _seed_run(s, workspace_id, status=RunStatus.REVIEW_READY)
        d_id = await _seed_deliverable(s, workspace_id, run_id, handles=None)
        outcome = await discard_run(s, run_id=run_id, workspace_id=workspace_id, reason="mcp")
        await s.commit()

    assert outcome is not None
    assert outcome.cancelled is True
    assert outcome.status == "cancelled"
    assert str(d_id) in outcome.deliverables_retracted
    async with sf() as s:
        run = await s.get(ExecutionRun, run_id)
        deliv = await s.get(Deliverable, d_id)
        assert run.status is RunStatus.CANCELLED
        assert deliv.retracted_at is not None


async def test_discard_surfaces_deliverables_with_compensation_handles(sf, workspace_id) -> None:
    """A deliverable with captured compensation handles is NOT silently tombstoned
    (that would falsely claim its external artifact was rolled back) — it's
    surfaced for an explicit compensating retract instead."""
    async with sf() as s:
        run_id = await _seed_run(s, workspace_id, status=RunStatus.REVIEW_READY)
        d_id = await _seed_deliverable(
            s,
            workspace_id,
            run_id,
            handles=[{"plugin": "github", "artifact_type": "pr", "handle": {"n": 1}}],
        )
        outcome = await discard_run(s, run_id=run_id, workspace_id=workspace_id, reason="mcp")
        await s.commit()

    assert outcome.cancelled is True
    assert str(d_id) in outcome.deliverables_need_compensation
    assert str(d_id) not in outcome.deliverables_retracted
    async with sf() as s:
        deliv = await s.get(Deliverable, d_id)
        assert deliv.retracted_at is None  # not faked


async def test_discard_unknown_returns_none(sf, workspace_id) -> None:
    async with sf() as s:
        outcome = await discard_run(s, run_id=uuid.uuid4(), workspace_id=workspace_id, reason="mcp")
    assert outcome is None


# --- cascade cancel on product delete --------------------------------------


async def test_cancel_product_runs_cancels_non_terminal_only(sf, workspace_id) -> None:
    product_id = uuid.uuid4()
    async with sf() as s:
        open_id = await _seed_run(s, workspace_id, status=RunStatus.OPEN, product_id=product_id)
        rr_id = await _seed_run(
            s, workspace_id, status=RunStatus.REVIEW_READY, product_id=product_id
        )
        shipped_id = await _seed_run(
            s, workspace_id, status=RunStatus.SHIPPED, product_id=product_id
        )
        # A run for a DIFFERENT product must be untouched.
        other_id = await _seed_run(s, workspace_id, status=RunStatus.OPEN, product_id=uuid.uuid4())
        n = await cancel_product_runs(
            s, product_id=product_id, workspace_id=workspace_id, reason="product deleted"
        )
        await s.commit()

    assert n == 2  # open + review_ready
    async with sf() as s:
        assert (await s.get(ExecutionRun, open_id)).status is RunStatus.CANCELLED
        assert (await s.get(ExecutionRun, rr_id)).status is RunStatus.CANCELLED
        assert (await s.get(ExecutionRun, shipped_id)).status is RunStatus.SHIPPED
        assert (await s.get(ExecutionRun, other_id)).status is RunStatus.OPEN
