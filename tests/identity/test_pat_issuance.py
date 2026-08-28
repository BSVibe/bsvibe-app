"""Tests for PAT issuance — backend.identity.oauth_service.

A Personal Access Token is not a new credential type: it is an ordinary
``oauth_access_tokens`` row with no expiry and a ``pat:`` label, carried by the
same ES256 JWT every other token uses. These tests pin the three properties
that make it a PAT rather than a session token:

* no ``exp`` on the wire and a NULL ``expires_at`` in the row (unless one was
  explicitly requested),
* no refresh token — a credential that can re-mint itself is a durable foothold,
  not a machine credential (same reasoning as ``issue_run_task_token``),
* discoverable and revocable by label, without touching grant-issued tokens.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

# Imported for table registration on the shared Base.metadata.
import backend.identity.db  # noqa: F401
import backend.identity.oauth_db  # noqa: F401
import backend.identity.workspaces_db  # noqa: F401
import backend.router.accounts.account_models  # noqa: F401
from backend.identity.db import UserRow
from backend.identity.oauth_db import OAuthAccessTokenRow, OAuthRefreshTokenRow, aware_utc
from backend.identity.oauth_jwt import verify_access_token
from backend.identity.oauth_keys import reset_signing_key_for_tests
from backend.identity.oauth_service import (
    issue_pat,
    issue_token_pair,
    list_pats,
    revoke_pat,
)
from backend.identity.workspaces_db import WorkspaceRow

from .._support import memory_session

pytestmark = pytest.mark.asyncio

ISSUER = "http://test/oauth"


@pytest.fixture(autouse=True)
def _reset_keys() -> None:
    reset_signing_key_for_tests()
    yield
    reset_signing_key_for_tests()


async def _seed_user_workspace(session) -> tuple[UserRow, WorkspaceRow]:
    ws = WorkspaceRow(name="t-ws")
    session.add(ws)
    user = UserRow(supabase_user_id=f"sb-{uuid.uuid4()}", email="t@example.com")
    session.add(user)
    await session.flush()
    return user, ws


async def test_issue_pat_mints_never_expiring_row() -> None:
    async with memory_session() as session:
        user, ws = await _seed_user_workspace(session)
        issued = await issue_pat(
            session,
            user_id=user.id,
            workspace_id=ws.id,
            name="mac-mini",
            scope=["mcp:read", "mcp:write"],
            issuer=ISSUER,
        )
        await session.flush()

        row = await session.get(OAuthAccessTokenRow, issued.id)
        assert row is not None
        assert row.expires_at is None
        assert row.revoked_at is None
        assert row.label == "pat:mac-mini"
        assert row.scope == ["mcp:read", "mcp:write"]
        assert row.user_id == user.id
        assert row.workspace_id == ws.id

        claims = verify_access_token(issued.token, issuer=ISSUER)
        assert "exp" not in claims
        assert claims["jti"] == str(issued.id)
        assert claims["scope"] == "mcp:read mcp:write"


async def test_issue_pat_creates_no_refresh_token() -> None:
    """A PAT that could re-mint its own access would be a durable foothold."""
    async with memory_session() as session:
        user, ws = await _seed_user_workspace(session)
        issued = await issue_pat(
            session,
            user_id=user.id,
            workspace_id=ws.id,
            name="ci",
            scope=["mcp:read"],
            issuer=ISSUER,
        )
        await session.flush()

        refresh = (
            (
                await session.execute(
                    select(OAuthRefreshTokenRow).where(
                        OAuthRefreshTokenRow.access_token_id == issued.id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert refresh == []


async def test_issue_pat_with_expiry_stamps_row_and_claim() -> None:
    """A bounded PAT is still allowed — NULL is the default, not the only option."""
    # Anchored to real now: the JWT carries this as ``iat``/``exp``, and PyJWT
    # rejects a token minted in the future or already expired.
    now = datetime.now(UTC).replace(microsecond=0)
    async with memory_session() as session:
        user, ws = await _seed_user_workspace(session)
        issued = await issue_pat(
            session,
            user_id=user.id,
            workspace_id=ws.id,
            name="temp",
            scope=["mcp:read"],
            issuer=ISSUER,
            expires_in_days=30,
            now=now,
        )
        await session.flush()

        row = await session.get(OAuthAccessTokenRow, issued.id)
        assert row is not None
        assert row.expires_at is not None
        # SQLite hands the column back naive; compare the instant, not the tz form.
        assert aware_utc(row.expires_at) == now + timedelta(days=30)
        claims = verify_access_token(issued.token, issuer=ISSUER)
        assert claims["exp"] == int((now + timedelta(days=30)).timestamp())


async def test_list_pats_excludes_grant_issued_tokens() -> None:
    async with memory_session() as session:
        user, ws = await _seed_user_workspace(session)
        await issue_pat(
            session,
            user_id=user.id,
            workspace_id=ws.id,
            name="laptop",
            scope=["mcp:read"],
            issuer=ISSUER,
        )
        # An ordinary authorization_code-issued pair must not show up as a PAT.
        await issue_token_pair(
            session,
            user_id=user.id,
            workspace_id=ws.id,
            client_id="dcr-editor",
            scope=["mcp:read"],
            issuer=ISSUER,
            label="client:editor",
        )
        await session.flush()

        rows = await list_pats(session, workspace_id=ws.id)
        assert [r.label for r in rows] == ["pat:laptop"]


async def test_revoke_pat_marks_revoked_and_drops_from_listing() -> None:
    async with memory_session() as session:
        user, ws = await _seed_user_workspace(session)
        issued = await issue_pat(
            session,
            user_id=user.id,
            workspace_id=ws.id,
            name="laptop",
            scope=["mcp:read"],
            issuer=ISSUER,
        )
        await session.flush()

        revoked = await revoke_pat(session, pat_id=issued.id, workspace_id=ws.id)
        assert revoked is not None
        assert revoked.revoked_at is not None
        assert await list_pats(session, workspace_id=ws.id) == []


async def test_revoke_pat_from_another_workspace_is_not_found() -> None:
    """Workspace isolation — a PAT id alone must not be enough to kill it."""
    async with memory_session() as session:
        user, ws = await _seed_user_workspace(session)
        issued = await issue_pat(
            session,
            user_id=user.id,
            workspace_id=ws.id,
            name="laptop",
            scope=["mcp:read"],
            issuer=ISSUER,
        )
        await session.flush()

        assert await revoke_pat(session, pat_id=issued.id, workspace_id=uuid.uuid4()) is None
        row = await session.get(OAuthAccessTokenRow, issued.id)
        assert row is not None
        assert row.revoked_at is None
