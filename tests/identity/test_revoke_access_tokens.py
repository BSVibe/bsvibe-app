"""OAuth signing-key rotation — the command that revokes every live access token.

Swapping the signing key does not stop a client holding an old refresh token (a refresh returns a
token signed by the NEW key); revoking the parent access tokens does (``PARENT_REVOKED``). So:

* a dry run (the default) changes nothing and reports how many tokens are live;
* ``--apply`` revokes every token with ``revoked_at IS NULL`` — PATs and never-expiring ones too;
* an already-revoked token keeps its original revocation time;
* re-running is a no-op.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import backend.identity.db  # noqa: F401
import backend.identity.oauth_db  # noqa: F401
import backend.identity.workspaces_db  # noqa: F401
import backend.router.accounts.account_models  # noqa: F401
from backend.data.rls import workspace_session_scope
from backend.identity.db import UserRow
from backend.identity.oauth_db import OAuthAccessTokenRow, aware_utc
from backend.identity.revoke_access_tokens import _parse_args, revoke_access_tokens
from backend.identity.workspaces_db import WorkspaceRow

from .._support import db_engine

pytestmark = pytest.mark.asyncio

_EARLIER = datetime(2026, 1, 1, tzinfo=UTC)


@pytest_asyncio.fixture
async def sf() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with db_engine() as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


async def _seed(sf: async_sessionmaker[AsyncSession]) -> dict[str, uuid.UUID]:
    """Two live tokens (one never expires), one already revoked at ``_EARLIER``."""
    ws_id = uuid.uuid4()
    async with sf() as s, workspace_session_scope(s, ws_id):
        ws = WorkspaceRow(id=ws_id, name="w")
        user = UserRow(supabase_user_id=f"sb-{uuid.uuid4()}", email="t@example.com")
        s.add_all([ws, user])
        await s.flush()
        future = datetime.now(UTC) + timedelta(hours=1)

        def row(expires_at: datetime | None, revoked_at: datetime | None) -> OAuthAccessTokenRow:
            return OAuthAccessTokenRow(
                workspace_id=ws.id,
                user_id=user.id,
                client_id="c",
                scope=["read"],
                expires_at=expires_at,
                revoked_at=revoked_at,
            )

        live, pat, revoked = row(future, None), row(None, None), row(future, _EARLIER)
        s.add_all([live, pat, revoked])
        await s.commit()
        return {"live": live.id, "pat": pat.id, "revoked": revoked.id}


async def _revoked_at(sf: async_sessionmaker[AsyncSession], token_id: uuid.UUID):
    async with sf() as s:
        value = await s.scalar(
            select(OAuthAccessTokenRow.revoked_at).where(OAuthAccessTokenRow.id == token_id)
        )
    return aware_utc(value) if value is not None else None


async def test_dry_run_reports_and_changes_nothing(sf) -> None:
    ids = await _seed(sf)

    assert await revoke_access_tokens(sf, apply=False) == 2

    assert await _revoked_at(sf, ids["live"]) is None
    assert await _revoked_at(sf, ids["pat"]) is None


async def test_apply_revokes_every_live_token_and_keeps_old_revocations(sf) -> None:
    ids = await _seed(sf)
    now = datetime(2026, 10, 23, tzinfo=UTC)

    assert await revoke_access_tokens(sf, apply=True, now=now) == 2

    assert await _revoked_at(sf, ids["live"]) == now
    assert await _revoked_at(sf, ids["pat"]) == now
    assert await _revoked_at(sf, ids["revoked"]) == _EARLIER


async def test_apply_twice_is_a_no_op(sf) -> None:
    await _seed(sf)
    await revoke_access_tokens(sf, apply=True)

    assert await revoke_access_tokens(sf, apply=True) == 0
    assert await revoke_access_tokens(sf, apply=False) == 0


async def test_apply_flag_defaults_to_dry_run() -> None:
    assert _parse_args([]).apply is False
    assert _parse_args(["--apply"]).apply is True
