"""Lift I-Repo-Knowledge — SqlAlchemyProposalRepository round-trip tests.

Uses ``tests._support.memory_session`` (in-memory SQLite) for speed; the
schema also runs against real PG via the suite's CI gates.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from backend.knowledge.canonicalization.db import (
    ActionKind,
    CanonicalizationProposal,
    ProposalKind,
    ProposalStatus,
)
from backend.knowledge.infrastructure.repositories import SqlAlchemyProposalRepository
from tests._support import memory_session


def _make_proposal(
    workspace_id: uuid.UUID,
    *,
    status: ProposalStatus = ProposalStatus.PENDING,
    created_at: datetime | None = None,
    action_path: str = "concept:test",
) -> CanonicalizationProposal:
    return CanonicalizationProposal(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        proposal_kind=ProposalKind.CREATE_CONCEPT,
        action_kind=ActionKind.CREATE_CONCEPT,
        action_path=action_path,
        payload={"target": "foo"},
        evidence=None,
        status=status,
        score=None,
        created_at=created_at or datetime.now(tz=UTC),
        expires_at=None,
        resolved_at=None,
    )


@pytest.mark.asyncio
async def test_add_and_get_proposal_roundtrip() -> None:
    async with memory_session() as session:
        repo = SqlAlchemyProposalRepository(session)
        workspace_id = uuid.uuid4()
        proposal = _make_proposal(workspace_id)
        await repo.add(proposal)
        await session.flush()

        loaded = await repo.get(proposal.id)
        assert loaded is not None
        assert loaded.id == proposal.id
        assert loaded.status is ProposalStatus.PENDING


@pytest.mark.asyncio
async def test_get_missing_returns_none() -> None:
    async with memory_session() as session:
        repo = SqlAlchemyProposalRepository(session)
        assert await repo.get(uuid.uuid4()) is None


@pytest.mark.asyncio
async def test_list_by_workspace_newest_first_respects_limit() -> None:
    async with memory_session() as session:
        repo = SqlAlchemyProposalRepository(session)
        workspace_id = uuid.uuid4()
        other_workspace = uuid.uuid4()
        now = datetime.now(tz=UTC)

        ids: list[uuid.UUID] = []
        for i in range(3):
            p = _make_proposal(
                workspace_id,
                created_at=now - timedelta(minutes=2 - i),
                action_path=f"concept:{i}",
            )
            ids.append(p.id)
            await repo.add(p)
        await repo.add(_make_proposal(other_workspace, action_path="concept:other"))
        await session.flush()

        rows = await repo.list_by_workspace(workspace_id)
        assert {r.workspace_id for r in rows} == {workspace_id}
        assert len(rows) == 3
        # Newest first
        assert rows[0].id == ids[2]
        assert rows[2].id == ids[0]

        limited = await repo.list_by_workspace(workspace_id, limit=2)
        assert len(limited) == 2


@pytest.mark.asyncio
async def test_list_pending_by_workspace_filters_status_and_orders_oldest_first() -> None:
    async with memory_session() as session:
        repo = SqlAlchemyProposalRepository(session)
        workspace_id = uuid.uuid4()
        now = datetime.now(tz=UTC)

        # 2 pending (oldest first), 1 approved.
        oldest = _make_proposal(
            workspace_id, created_at=now - timedelta(minutes=10), action_path="a"
        )
        middle = _make_proposal(
            workspace_id, created_at=now - timedelta(minutes=5), action_path="b"
        )
        approved = _make_proposal(
            workspace_id,
            status=ProposalStatus.APPROVED,
            created_at=now,
            action_path="c",
        )
        await repo.add(oldest)
        await repo.add(middle)
        await repo.add(approved)
        await session.flush()

        rows = await repo.list_pending_by_workspace(workspace_id)
        assert [r.id for r in rows] == [oldest.id, middle.id]


@pytest.mark.asyncio
async def test_list_by_status_filters_by_status() -> None:
    async with memory_session() as session:
        repo = SqlAlchemyProposalRepository(session)
        workspace_id = uuid.uuid4()

        pending = _make_proposal(workspace_id, action_path="p")
        approved = _make_proposal(workspace_id, status=ProposalStatus.APPROVED, action_path="a")
        await repo.add(pending)
        await repo.add(approved)
        await session.flush()

        approved_rows = await repo.list_by_status(workspace_id, ProposalStatus.APPROVED)
        assert [r.id for r in approved_rows] == [approved.id]
        pending_rows = await repo.list_by_status(workspace_id, ProposalStatus.PENDING)
        assert [r.id for r in pending_rows] == [pending.id]
