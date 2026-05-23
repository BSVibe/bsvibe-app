"""Unit tests for the account-id + user-row dependencies in api.deps."""

from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException

from backend.api.deps import (
    get_account_id,
    get_current_user_row,
    require_account_id,
)
from backend.identity.db import UserRow
from backend.shared.authz.types import User

from .._support import memory_session

pytestmark = pytest.mark.asyncio


async def test_get_account_id_none_when_header_absent() -> None:
    assert await get_account_id(None) is None


async def test_get_account_id_parses_valid_uuid() -> None:
    val = uuid.uuid4()
    assert await get_account_id(str(val)) == val


async def test_get_account_id_rejects_garbage() -> None:
    with pytest.raises(HTTPException) as exc:
        await get_account_id("not-a-uuid")
    assert exc.value.status_code == 400


async def test_require_account_id_resolves_personal_account_when_missing() -> None:
    """No header → resolve (create-on-read) the workspace's personal account
    instead of 400ing, so a logged-in founder is never blocked."""
    import backend.accounts.account_models  # noqa: F401, PLC0415 — table registration
    from backend.accounts.account_models import Account  # noqa: PLC0415

    ws = uuid.uuid4()
    async with memory_session() as s:
        resolved = await require_account_id(None, ws, s)
        # A real Account row now exists for the workspace, returned by id.
        from sqlalchemy import select  # noqa: PLC0415

        rows = (await s.execute(select(Account).where(Account.workspace_id == ws))).scalars().all()
        assert len(rows) == 1
        assert rows[0].id == resolved
        # Idempotent: a second call returns the same id.
        again = await require_account_id(None, ws, s)
        assert again == resolved


async def test_require_account_id_passes_through_explicit_header() -> None:
    """A valid header wins verbatim — never replaced by the personal account."""
    val = uuid.uuid4()
    ws = uuid.uuid4()
    async with memory_session() as s:
        assert await require_account_id(val, ws, s) == val


async def test_get_current_user_row_403_when_no_row() -> None:
    async with memory_session() as s:
        with pytest.raises(HTTPException) as exc:
            await get_current_user_row(User(id="ghost"), s)
        assert exc.value.status_code == 403


async def test_get_current_user_row_resolves_existing() -> None:
    async with memory_session() as s:
        row = UserRow(id=uuid.uuid4(), supabase_user_id="sb-1", email="a@x.io")
        s.add(row)
        await s.commit()
        resolved = await get_current_user_row(User(id="sb-1"), s)
        assert resolved.id == row.id
