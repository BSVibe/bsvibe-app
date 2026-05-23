"""Unit tests for the personal-Account provisioning service.

The personal ``Account`` is invisible billing-axis infra: one per workspace,
auto-seeded at login bootstrap so the model-accounts surface
(``X-BSVibe-Account-Id``) has a real id to partition on. These tests cover the
provisioning primitive — idempotency, backfill, and earliest-created
resolution — independent of the HTTP/bootstrap wiring.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

# Imported for table registration on the shared Base.metadata.
import backend.accounts.account_models  # noqa: F401
from backend.accounts.account_models import Account
from backend.accounts.account_service import ensure_personal_account
from backend.accounts.service import DEFAULT_ACCOUNT_LABEL

from .._support import memory_session

pytestmark = pytest.mark.asyncio


async def test_ensure_personal_account_creates_one() -> None:
    ws = uuid.uuid4()
    async with memory_session() as s:
        acct = await ensure_personal_account(s, workspace_id=ws)
        await s.commit()
        assert acct.workspace_id == ws
        assert acct.label == DEFAULT_ACCOUNT_LABEL
        rows = (await s.execute(select(Account))).scalars().all()
        assert len(rows) == 1
        assert rows[0].id == acct.id


async def test_ensure_personal_account_idempotent_same_id() -> None:
    ws = uuid.uuid4()
    async with memory_session() as s:
        first = await ensure_personal_account(s, workspace_id=ws)
        await s.commit()
        second = await ensure_personal_account(s, workspace_id=ws)
        await s.commit()
        assert first.id == second.id
        rows = (await s.execute(select(Account))).scalars().all()
        assert len(rows) == 1


async def test_ensure_personal_account_returns_earliest_when_multiple() -> None:
    """If multiple accounts somehow exist, resolution picks the earliest-created."""
    ws = uuid.uuid4()
    async with memory_session() as s:
        first = await ensure_personal_account(s, workspace_id=ws)
        await s.commit()
        # Simulate a second account created later (future multi-account room).
        later = Account(id=uuid.uuid4(), workspace_id=ws, label="secondary")
        s.add(later)
        await s.commit()
        resolved = await ensure_personal_account(s, workspace_id=ws)
        assert resolved.id == first.id


async def test_ensure_personal_account_scoped_per_workspace() -> None:
    ws_a = uuid.uuid4()
    ws_b = uuid.uuid4()
    async with memory_session() as s:
        a = await ensure_personal_account(s, workspace_id=ws_a)
        b = await ensure_personal_account(s, workspace_id=ws_b)
        await s.commit()
        assert a.id != b.id
        assert a.workspace_id == ws_a
        assert b.workspace_id == ws_b
