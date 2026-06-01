"""Lift I-Repo-Identity — SqlAlchemyUserRepository tests."""

from __future__ import annotations

import uuid

import pytest

from backend.identity.db import UserRow
from backend.identity.infrastructure.repositories import SqlAlchemyUserRepository
from tests._support import memory_session


@pytest.mark.asyncio
async def test_add_get_roundtrip() -> None:
    async with memory_session() as session:
        repo = SqlAlchemyUserRepository(session)
        user = UserRow(id=uuid.uuid4(), supabase_user_id="sub-1", email="u@example.com")
        await repo.add(user)
        await session.flush()
        loaded = await repo.get(user.id)
        assert loaded is not None
        assert loaded.supabase_user_id == "sub-1"
        assert loaded.email == "u@example.com"


@pytest.mark.asyncio
async def test_get_missing_returns_none() -> None:
    async with memory_session() as session:
        repo = SqlAlchemyUserRepository(session)
        assert await repo.get(uuid.uuid4()) is None


@pytest.mark.asyncio
async def test_get_by_supabase_id_roundtrip() -> None:
    async with memory_session() as session:
        repo = SqlAlchemyUserRepository(session)
        user = UserRow(id=uuid.uuid4(), supabase_user_id="sub-abc", email=None)
        await repo.add(user)
        await session.flush()

        got = await repo.get_by_supabase_id("sub-abc")
        assert got is not None
        assert got.id == user.id

        missing = await repo.get_by_supabase_id("nope")
        assert missing is None


@pytest.mark.asyncio
async def test_lock_for_update_returns_row() -> None:
    """``lock_for_update`` returns the row; the FOR UPDATE clause is a no-op on SQLite."""
    async with memory_session() as session:
        repo = SqlAlchemyUserRepository(session)
        user = UserRow(id=uuid.uuid4(), supabase_user_id="sub-lock", email=None)
        await repo.add(user)
        await session.flush()

        locked = await repo.lock_for_update(user.id)
        assert locked is not None
        assert locked.id == user.id


@pytest.mark.asyncio
async def test_lock_for_update_missing_returns_none() -> None:
    async with memory_session() as session:
        repo = SqlAlchemyUserRepository(session)
        assert await repo.lock_for_update(uuid.uuid4()) is None
