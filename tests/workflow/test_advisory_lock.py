"""Lift H1 — advisory_lock at new path `backend.workflow.infrastructure`.

The primitive itself is unchanged from `backend.workflow.infrastructure.advisory_lock`
(verified at the old path by `tests/storage/...` indirectly). This
test asserts the relocation: imports resolve at the new path and the
SQLite fallback works in-process.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from backend.workflow.infrastructure.advisory_lock import (
    advisory_key_for_run,
    release_run_dispatch_lock,
    try_run_dispatch_lock,
)


def test_advisory_key_is_signed_int64() -> None:
    """v3 D15 — key must fit Postgres bigint."""
    rid = uuid.uuid4()
    key = advisory_key_for_run(rid)
    assert isinstance(key, int)
    assert -(2**63) <= key < 2**63


def test_advisory_key_is_deterministic() -> None:
    rid = uuid.UUID("00000000-0000-0000-0000-000000000001")
    assert advisory_key_for_run(rid) == advisory_key_for_run(rid)


@pytest.mark.asyncio
async def test_sqlite_fallback_acquire_and_release() -> None:
    """SQLite path uses in-process asyncio.Lock; happy-path acquire+release."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.connect() as conn:
            from sqlalchemy.ext.asyncio import AsyncSession

            session = AsyncSession(bind=conn)
            rid = uuid.uuid4()

            acquired = await try_run_dispatch_lock(session, rid)
            assert acquired is True

            await release_run_dispatch_lock(session, rid)

            # Re-acquire after release must succeed.
            acquired_again = await try_run_dispatch_lock(session, rid)
            assert acquired_again is True

            await release_run_dispatch_lock(session, rid)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_sqlite_fallback_second_caller_observes_busy() -> None:
    """Two tasks racing on the same run_id — second sees False."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.connect() as conn:
            from sqlalchemy.ext.asyncio import AsyncSession

            session = AsyncSession(bind=conn)
            rid = uuid.uuid4()

            first = await try_run_dispatch_lock(session, rid)
            assert first is True

            # Run the second acquire in a separate task so its current_task()
            # differs from the holder. The fallback uses asyncio.Lock which is
            # task-aware via the registry-lock; a second task sees locked()==True.
            async def second_call() -> bool:
                return await try_run_dispatch_lock(session, rid)

            assert await asyncio.create_task(second_call()) is False

            await release_run_dispatch_lock(session, rid)
    finally:
        await engine.dispose()
