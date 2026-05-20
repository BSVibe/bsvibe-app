"""Acceptance #4 — end-to-end PG + Redis round-trip against a live stack.

Skipped automatically when the services are unreachable, so unit-only runs
(``pytest tests/`` outside ``docker compose up``) stay green. CI brings up
PG + Redis as service containers and this test must pass there.
"""

from __future__ import annotations

import os
import uuid

import pytest
import redis.asyncio as redis_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from backend.config import get_settings


async def _pg_reachable(url: str) -> bool:
    engine = create_async_engine(url, future=True)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False
    finally:
        await engine.dispose()


async def _redis_reachable(url: str) -> bool:
    client = redis_asyncio.from_url(url)
    try:
        await client.ping()
        return True
    except Exception:
        return False
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_pg_and_redis_roundtrip() -> None:
    settings = get_settings()
    pg_url = os.environ.get("BSVIBE_DATABASE_URL", settings.database_url)
    redis_url = os.environ.get("BSVIBE_REDIS_URL", settings.redis_url)

    if not await _pg_reachable(pg_url):
        pytest.skip(f"Postgres not reachable at {pg_url}")
    if not await _redis_reachable(redis_url):
        pytest.skip(f"Redis not reachable at {redis_url}")

    # --- PG: create table on demand, insert a workspace row, read it back ---
    engine = create_async_engine(pg_url, future=True)
    workspace_id = uuid.uuid4()
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "CREATE TABLE IF NOT EXISTS workspaces ("
                    "id UUID PRIMARY KEY, "
                    "created_at TIMESTAMPTZ NOT NULL DEFAULT now()"
                    ")"
                )
            )
            await conn.execute(
                text("INSERT INTO workspaces (id) VALUES (:id)"),
                {"id": str(workspace_id)},
            )

        async with engine.connect() as conn:
            result = await conn.execute(
                text("SELECT id FROM workspaces WHERE id = :id"),
                {"id": str(workspace_id)},
            )
            row = result.first()
        assert row is not None
        assert str(row[0]) == str(workspace_id)
    finally:
        async with engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM workspaces WHERE id = :id"),
                {"id": str(workspace_id)},
            )
        await engine.dispose()

    # --- Redis: produce + consume on health:test stream ---
    client = redis_asyncio.from_url(redis_url)
    stream = "health:test"
    payload = {"ping": uuid.uuid4().hex}
    try:
        message_id = await client.xadd(stream, payload)
        entries = await client.xrange(stream, min=message_id, max=message_id)
        assert entries, "stream entry not found"
        _, fields = entries[0]
        decoded = {k.decode(): v.decode() for k, v in fields.items()}
        assert decoded == payload
    finally:
        await client.delete(stream)
        await client.aclose()
