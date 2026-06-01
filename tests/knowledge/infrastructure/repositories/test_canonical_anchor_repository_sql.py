"""Lift I-Repo-Knowledge — SqlAlchemyCanonicalAnchorRepository tests."""

from __future__ import annotations

import uuid

import pytest

from backend.knowledge.canonicalization.db import CanonicalAnchor
from backend.knowledge.infrastructure.repositories import (
    SqlAlchemyCanonicalAnchorRepository,
)
from tests._support import memory_session


@pytest.mark.asyncio
async def test_add_get_roundtrip() -> None:
    async with memory_session() as session:
        repo = SqlAlchemyCanonicalAnchorRepository(session)
        workspace_id = uuid.uuid4()
        anchor = CanonicalAnchor(
            id=uuid.uuid4(), workspace_id=workspace_id, name="alpha", description="x"
        )
        await repo.add(anchor)
        await session.flush()
        loaded = await repo.get(anchor.id)
        assert loaded is not None
        assert loaded.name == "alpha"


@pytest.mark.asyncio
async def test_get_missing_returns_none() -> None:
    async with memory_session() as session:
        repo = SqlAlchemyCanonicalAnchorRepository(session)
        assert await repo.get(uuid.uuid4()) is None


@pytest.mark.asyncio
async def test_find_by_name_scoped_to_workspace() -> None:
    async with memory_session() as session:
        repo = SqlAlchemyCanonicalAnchorRepository(session)
        ws_a = uuid.uuid4()
        ws_b = uuid.uuid4()
        anchor_a = CanonicalAnchor(id=uuid.uuid4(), workspace_id=ws_a, name="same")
        anchor_b = CanonicalAnchor(id=uuid.uuid4(), workspace_id=ws_b, name="same")
        await repo.add(anchor_a)
        await repo.add(anchor_b)
        await session.flush()

        got = await repo.find_by_name(ws_a, "same")
        assert got is not None
        assert got.id == anchor_a.id

        missing = await repo.find_by_name(ws_a, "nope")
        assert missing is None


@pytest.mark.asyncio
async def test_list_by_workspace_sorted_by_name_and_scoped() -> None:
    async with memory_session() as session:
        repo = SqlAlchemyCanonicalAnchorRepository(session)
        ws_a = uuid.uuid4()
        ws_b = uuid.uuid4()

        await repo.add(CanonicalAnchor(id=uuid.uuid4(), workspace_id=ws_a, name="charlie"))
        await repo.add(CanonicalAnchor(id=uuid.uuid4(), workspace_id=ws_a, name="alpha"))
        await repo.add(CanonicalAnchor(id=uuid.uuid4(), workspace_id=ws_a, name="bravo"))
        await repo.add(CanonicalAnchor(id=uuid.uuid4(), workspace_id=ws_b, name="other"))
        await session.flush()

        rows = await repo.list_by_workspace(ws_a)
        assert [r.name for r in rows] == ["alpha", "bravo", "charlie"]

        limited = await repo.list_by_workspace(ws_a, limit=2)
        assert len(limited) == 2


@pytest.mark.asyncio
async def test_list_by_workspace_empty_returns_empty_list() -> None:
    async with memory_session() as session:
        repo = SqlAlchemyCanonicalAnchorRepository(session)
        assert await repo.list_by_workspace(uuid.uuid4()) == []
