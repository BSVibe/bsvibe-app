"""PR3 — ``github_merge_watch`` durable table + repository.

Mirrors the delivery-table pattern (``DeliveryEventRow`` +
``build_delivery_claim_stmt`` + the multi-server ``FOR UPDATE SKIP LOCKED``
race test). The table is the durable state a later CI-green auto-merge poller
worker will drain; there is no worker yet, so these tests exercise the model,
the standalone claim statement, and the repository directly.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.data import Base
from backend.workflow.infrastructure.github.db import (
    GithubMergeWatchRow,
    MergeWatchStatus,
)
from backend.workflow.infrastructure.github.repository import (
    GithubMergeWatchRepository,
    build_merge_watch_claim_stmt,
)
from tests._support import db_engine, use_real_pg


def _rendered_sql(stmt: Any) -> str:
    """Render a statement against the PG dialect (``SKIP LOCKED`` is PG-only)."""
    from sqlalchemy.dialects import postgresql

    return str(
        stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": False})
    ).upper()


def _watch_row(
    *,
    status: MergeWatchStatus = MergeWatchStatus.PENDING_CI,
    next_poll_at: datetime | None = None,
    repo: str = "octocat/hello-world",
    pr_number: int = 7,
) -> GithubMergeWatchRow:
    now = datetime.now(tz=UTC)
    run_id = uuid.uuid4()
    return GithubMergeWatchRow(
        id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        run_id=run_id,
        deliverable_id=uuid.uuid4(),
        repo=repo,
        pr_number=pr_number,
        branch=f"bsvibe/run-{run_id}",
        base_branch="main",
        status=status,
        attempts=0,
        next_poll_at=next_poll_at or (now - timedelta(seconds=1)),
        deadline_at=now + timedelta(hours=1),
        conflict_dispatched=False,
        created_at=now,
    )


# ----------------------------------------------------------------------
# Enum
# ----------------------------------------------------------------------


def test_merge_watch_status_enum_values() -> None:
    assert [s.value for s in MergeWatchStatus] == [
        "pending_ci",
        "merging",
        "merged",
        "failed",
        "needs_resolution",
        "abandoned",
    ]


# ----------------------------------------------------------------------
# Compile-time claim guard — the load-bearing production check.
# ----------------------------------------------------------------------


def test_build_merge_watch_claim_stmt_carries_skip_locked() -> None:
    sql = _rendered_sql(build_merge_watch_claim_stmt(now=datetime.now(tz=UTC), batch_size=10))
    assert "FOR UPDATE" in sql, "merge-watch claim must FOR UPDATE"
    assert "SKIP LOCKED" in sql, "merge-watch claim must SKIP LOCKED"


# ----------------------------------------------------------------------
# Repository round-trip + claim semantics.
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_then_claim_due_returns_due_claimable_row() -> None:
    async with db_engine(Base) as (engine, _is_pg):
        sf = async_sessionmaker(engine, expire_on_commit=False)
        row = _watch_row()
        async with sf() as session:
            repo = GithubMergeWatchRepository(session)
            await repo.add(row)
            await session.commit()
        async with sf() as session:
            repo = GithubMergeWatchRepository(session)
            claimed = await repo.claim_due(now=datetime.now(tz=UTC), batch_size=10)
            assert [r.id for r in claimed] == [row.id]


@pytest.mark.asyncio
async def test_claim_due_skips_future_next_poll() -> None:
    async with db_engine(Base) as (engine, _is_pg):
        sf = async_sessionmaker(engine, expire_on_commit=False)
        future = datetime.now(tz=UTC) + timedelta(minutes=30)
        row = _watch_row(next_poll_at=future)
        async with sf() as session:
            repo = GithubMergeWatchRepository(session)
            await repo.add(row)
            await session.commit()
        async with sf() as session:
            repo = GithubMergeWatchRepository(session)
            claimed = await repo.claim_due(now=datetime.now(tz=UTC), batch_size=10)
            assert claimed == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    [MergeWatchStatus.MERGED, MergeWatchStatus.FAILED, MergeWatchStatus.ABANDONED],
)
async def test_claim_due_skips_terminal_status(status: MergeWatchStatus) -> None:
    async with db_engine(Base) as (engine, _is_pg):
        sf = async_sessionmaker(engine, expire_on_commit=False)
        row = _watch_row(status=status)
        async with sf() as session:
            repo = GithubMergeWatchRepository(session)
            await repo.add(row)
            await session.commit()
        async with sf() as session:
            repo = GithubMergeWatchRepository(session)
            claimed = await repo.claim_due(now=datetime.now(tz=UTC), batch_size=10)
            assert claimed == []


@pytest.mark.asyncio
async def test_claim_due_includes_needs_resolution() -> None:
    async with db_engine(Base) as (engine, _is_pg):
        sf = async_sessionmaker(engine, expire_on_commit=False)
        row = _watch_row(status=MergeWatchStatus.NEEDS_RESOLUTION)
        async with sf() as session:
            repo = GithubMergeWatchRepository(session)
            await repo.add(row)
            await session.commit()
        async with sf() as session:
            repo = GithubMergeWatchRepository(session)
            claimed = await repo.claim_due(now=datetime.now(tz=UTC), batch_size=10)
            assert [r.id for r in claimed] == [row.id]


@pytest.mark.asyncio
async def test_mark_status_transitions_row() -> None:
    async with db_engine(Base) as (engine, _is_pg):
        sf = async_sessionmaker(engine, expire_on_commit=False)
        row = _watch_row()
        next_poll = datetime.now(tz=UTC) + timedelta(minutes=5)
        async with sf() as session:
            repo = GithubMergeWatchRepository(session)
            await repo.add(row)
            await session.commit()
        async with sf() as session:
            repo = GithubMergeWatchRepository(session)
            await repo.mark_status(
                row.id,
                MergeWatchStatus.NEEDS_RESOLUTION,
                next_poll_at=next_poll,
                last_error="ci timeout",
                increment_attempt=True,
                conflict_dispatched=True,
            )
            await session.commit()
        async with sf() as session:
            fetched = await session.get(GithubMergeWatchRow, row.id)
            assert fetched is not None
            assert fetched.status == MergeWatchStatus.NEEDS_RESOLUTION
            assert fetched.attempts == 1
            assert fetched.conflict_dispatched is True
            assert fetched.last_error == "ci timeout"


# ----------------------------------------------------------------------
# PG-only behavioural race — two claimers never both get the same row.
# ----------------------------------------------------------------------


@pytest.mark.skipif(
    not use_real_pg(),
    reason="SKIP LOCKED is a PG-only primitive; SQLite ignores the hint",
)
@pytest.mark.asyncio
async def test_two_concurrent_claims_no_double_claim_pg() -> None:
    async with db_engine(Base) as (engine, _is_pg):
        sf = async_sessionmaker(engine, expire_on_commit=False)
        rows = [_watch_row(pr_number=i) for i in range(6)]
        async with sf() as session:
            repo = GithubMergeWatchRepository(session)
            for r in rows:
                await repo.add(r)
            await session.commit()

        now = datetime.now(tz=UTC)

        async def _claim_and_hold() -> list[uuid.UUID]:
            async with sf() as session:
                repo = GithubMergeWatchRepository(session)
                claimed = await repo.claim_due(now=now, batch_size=10)
                ids = [r.id for r in claimed]
                # Hold the FOR UPDATE lock so the sibling coroutine's claim
                # overlaps and must SKIP the rows we locked.
                await asyncio.sleep(0.05)
                await session.commit()
                return ids

        first, second = await asyncio.gather(_claim_and_hold(), _claim_and_hold())
        assert not (set(first) & set(second)), f"double claim: {first!r} & {second!r}"
        assert sorted(str(i) for i in [*first, *second]) == sorted(str(r.id) for r in rows)


@pytest.mark.asyncio
async def test_github_merge_watch_row_persists_all_columns() -> None:
    async with db_engine(Base) as (engine, _is_pg):
        sf = async_sessionmaker(engine, expire_on_commit=False)
        row = _watch_row(repo="acme/widget", pr_number=42)
        async with sf() as session:
            repo = GithubMergeWatchRepository(session)
            await repo.add(row)
            await session.commit()
        async with sf() as session:
            fetched = (
                await session.execute(
                    select(GithubMergeWatchRow).where(GithubMergeWatchRow.id == row.id)
                )
            ).scalar_one()
            assert fetched.repo == "acme/widget"
            assert fetched.pr_number == 42
            assert fetched.base_branch == "main"
            assert fetched.branch == row.branch
            assert fetched.status == MergeWatchStatus.PENDING_CI
            assert fetched.attempts == 0
            assert fetched.conflict_dispatched is False
            assert fetched.last_error is None
