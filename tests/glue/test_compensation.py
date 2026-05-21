"""CompensationHandler — supersede / revert / notify decision rules."""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.delivery.compensation import CompensationHandler
from backend.execution.db import (
    Deliverable,
    DeliverableType,
    ExecutionBase,
    ExecutionRun,
    RunStatus,
    VerificationOutcome,
    VerificationResult,
)

PG_URL = os.environ.get(
    "BSVIBE_DATABASE_URL", "postgresql+asyncpg://bsvibe:bsvibe@localhost:5442/bsvibe"
)


pytestmark = pytest.mark.asyncio


async def _can_reach_pg() -> bool:
    try:
        engine = create_async_engine(PG_URL, future=True, pool_pre_ping=True)
        async with engine.connect() as conn:
            await conn.execute(__import__("sqlalchemy").text("SELECT 1"))
        await engine.dispose()
        return True
    except Exception:
        return False


@pytest_asyncio.fixture
async def session_factory():
    if not await _can_reach_pg():
        pytest.skip(f"Postgres not reachable at {PG_URL}")
    engine = create_async_engine(PG_URL, future=True)
    async with engine.begin() as conn:
        await conn.run_sync(ExecutionBase.metadata.create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    yield sm
    async with engine.begin() as conn:
        await conn.run_sync(ExecutionBase.metadata.drop_all)
    await engine.dispose()


async def _seed_run_with_deliverable(
    sm,
    *,
    artifact_type: DeliverableType = DeliverableType.PR,
    delivered_at: datetime | None = None,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Return (run_id, deliverable_id)."""
    delivered_at = delivered_at or datetime.now(tz=UTC)
    workspace_id = uuid.uuid4()
    run_id = uuid.uuid4()
    deliv_id = uuid.uuid4()
    async with sm() as s:
        s.add(
            ExecutionRun(
                id=run_id,
                workspace_id=workspace_id,
                status=RunStatus.SHIPPED,
                payload={},
                created_at=delivered_at,
                updated_at=delivered_at,
            )
        )
        await s.flush()
        s.add(
            Deliverable(
                id=deliv_id,
                run_id=run_id,
                workspace_id=workspace_id,
                deliverable_type=artifact_type,
                artifact_uri="https://example/pr/1",
                payload={},
                created_at=delivered_at,
            )
        )
        await s.commit()
    return run_id, deliv_id


async def test_no_compensation_when_clean(session_factory) -> None:
    _, deliv_id = await _seed_run_with_deliverable(session_factory)
    async with session_factory() as s:
        handler = CompensationHandler(s)
        result = await handler.evaluate(deliverable_id=deliv_id)
    assert result is None


async def test_supersede_when_newer_same_type(session_factory) -> None:
    earlier = datetime.now(tz=UTC) - timedelta(hours=1)
    run_id, deliv_id = await _seed_run_with_deliverable(
        session_factory,
        artifact_type=DeliverableType.PR,
        delivered_at=earlier,
    )
    # Drop in a NEWER PR for the same run
    async with session_factory() as s:
        s.add(
            Deliverable(
                id=uuid.uuid4(),
                run_id=run_id,
                workspace_id=uuid.uuid4(),  # workspace_id not checked by compensation
                deliverable_type=DeliverableType.PR,
                artifact_uri="https://example/pr/2",
                payload={},
                created_at=datetime.now(tz=UTC),
            )
        )
        await s.commit()

    async with session_factory() as s:
        handler = CompensationHandler(s)
        result = await handler.evaluate(deliverable_id=deliv_id)
    assert result is not None
    assert result.action == "supersede"


async def test_revert_on_verification_failure(session_factory) -> None:
    earlier = datetime.now(tz=UTC) - timedelta(hours=1)
    run_id, deliv_id = await _seed_run_with_deliverable(
        session_factory, artifact_type=DeliverableType.PR, delivered_at=earlier
    )
    async with session_factory() as s:
        s.add(
            VerificationResult(
                id=uuid.uuid4(),
                run_id=run_id,
                workspace_id=uuid.uuid4(),
                outcome=VerificationOutcome.FAILED,
                contract={},
                result={},
                created_at=datetime.now(tz=UTC),
            )
        )
        await s.commit()
    async with session_factory() as s:
        handler = CompensationHandler(s)
        result = await handler.evaluate(deliverable_id=deliv_id)
    assert result is not None
    assert result.action == "revert"


async def test_notify_for_direct_output_failure(session_factory) -> None:
    earlier = datetime.now(tz=UTC) - timedelta(hours=1)
    run_id, deliv_id = await _seed_run_with_deliverable(
        session_factory,
        artifact_type=DeliverableType.DIRECT_OUTPUT,
        delivered_at=earlier,
    )
    async with session_factory() as s:
        s.add(
            VerificationResult(
                id=uuid.uuid4(),
                run_id=run_id,
                workspace_id=uuid.uuid4(),
                outcome=VerificationOutcome.FAILED,
                contract={},
                result={},
                created_at=datetime.now(tz=UTC),
            )
        )
        await s.commit()
    async with session_factory() as s:
        handler = CompensationHandler(s)
        result = await handler.evaluate(deliverable_id=deliv_id)
    assert result is not None
    assert result.action == "notify"


async def test_missing_deliverable_returns_none(session_factory) -> None:
    async with session_factory() as s:
        handler = CompensationHandler(s)
        result = await handler.evaluate(deliverable_id=uuid.uuid4())
    assert result is None
