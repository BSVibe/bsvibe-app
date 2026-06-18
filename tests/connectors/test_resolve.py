"""Slice 0.4 — resolve_connector_credentials (oauth → secret fallback).

The single place that turns a connector binding into the ``{"token": …}`` an
API call needs. Precedence:

1. An OAuth token row exists → use its (decrypted) access token, refreshing
   first if it's expiring and refresh material + a refreshable provider exist.
2. No token row → fall back to the legacy signing secret (today's behavior, so
   every existing connector keeps working unchanged until OAuth is wired).

Exercised against StubProvider + a real DB session.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.connectors.auth.db import ConnectorOAuthTokenRow  # noqa: F401 — register
from backend.connectors.auth.providers import StubProvider, register_provider
from backend.connectors.auth.resolve import resolve_connector_credentials
from backend.connectors.auth.store import get_or_create_account, upsert_token
from backend.connectors.auth.tokenset import TokenSet
from backend.connectors.db import ConnectorAccountRow  # noqa: F401 — register
from backend.router.accounts.crypto import CredentialCipher

from .._support import db_engine

pytestmark = pytest.mark.asyncio

TEST_KEY = b"0123456789abcdef0123456789abcdef"


@pytest_asyncio.fixture
async def sf():
    async with db_engine() as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
def cipher() -> CredentialCipher:
    return CredentialCipher(TEST_KEY)


async def _account(sf, cipher, connector: str) -> ConnectorAccountRow:
    async with sf() as s:
        acct = await get_or_create_account(
            s, workspace_id=uuid.uuid4(), connector=connector, cipher=cipher
        )
        await s.commit()
        return acct


async def test_falls_back_to_signing_secret_when_no_token(sf, cipher) -> None:
    # get_or_create_account stores the placeholder secret; with no OAuth token
    # row, resolution returns that decrypted secret under "token".
    acct = await _account(sf, cipher, "telegram")
    async with sf() as s:
        acct = await s.get(ConnectorAccountRow, acct.id)
        creds = await resolve_connector_credentials(s, account=acct, cipher=cipher)
    assert creds["token"] == "no-webhook-secret"


async def test_uses_oauth_access_token_when_present(sf, cipher) -> None:
    register_provider(StubProvider(name="resolve-stub"))
    acct = await _account(sf, cipher, "resolve-stub")
    async with sf() as s:
        await upsert_token(
            s,
            connector_account_id=acct.id,
            provider="resolve-stub",
            token=TokenSet(access_token="live-access", refresh_token=None),
            cipher=cipher,
        )
        await s.commit()
    async with sf() as s:
        acct = await s.get(ConnectorAccountRow, acct.id)
        creds = await resolve_connector_credentials(s, account=acct, cipher=cipher)
    assert creds["token"] == "live-access"  # OAuth token, not the signing secret


async def test_refreshes_when_expiring_and_refreshable(sf, cipher) -> None:
    register_provider(StubProvider(name="refresh-stub", refreshable=True))
    acct = await _account(sf, cipher, "refresh-stub")
    expired = datetime.now(tz=UTC) - timedelta(minutes=1)
    async with sf() as s:
        await upsert_token(
            s,
            connector_account_id=acct.id,
            provider="refresh-stub",
            token=TokenSet(
                access_token="stale-access",
                refresh_token="r-1",
                expires_at=expired,
            ),
            cipher=cipher,
        )
        await s.commit()
    async with sf() as s:
        acct = await s.get(ConnectorAccountRow, acct.id)
        creds = await resolve_connector_credentials(s, account=acct, cipher=cipher)
        await s.commit()
    # Stub.refresh mints "stub-access-refreshed".
    assert creds["token"] == "stub-access-refreshed"


async def test_refresh_failure_raises_connector_reauth_required(sf, cipher) -> None:
    """Lift E45 — when the provider's ``refresh`` raises (the GitHub OAuth
    refresh-token endpoint returns ``bad_refresh_token`` when the refresh
    token has been consumed or expired), ``resolve_connector_credentials``
    surfaces a typed :class:`ConnectorReauthRequired` instead of leaking the
    raw provider exception. This lets the caller (``deliver_github`` /
    safe_mode_approve / etc.) format a clear "founder must re-OAuth" signal
    instead of "Authentication failed" + a stack trace.
    """
    from backend.connectors.auth.resolve import ConnectorReauthRequired

    class _BadRefreshStub(StubProvider):
        async def refresh(self, *, refresh_token: str):  # noqa: ARG002
            raise ValueError(
                "github token exchange failed: bad_refresh_token "
                "(The refresh token passed is incorrect or expired.)"
            )

    register_provider(_BadRefreshStub(name="badrefresh-stub", refreshable=True))
    acct = await _account(sf, cipher, "badrefresh-stub")
    expired = datetime.now(tz=UTC) - timedelta(minutes=1)
    async with sf() as s:
        await upsert_token(
            s,
            connector_account_id=acct.id,
            provider="badrefresh-stub",
            token=TokenSet(
                access_token="stale-access",
                refresh_token="dead-refresh",
                expires_at=expired,
            ),
            cipher=cipher,
        )
        await s.commit()
    async with sf() as s:
        acct = await s.get(ConnectorAccountRow, acct.id)
        with pytest.raises(ConnectorReauthRequired) as exc_info:
            await resolve_connector_credentials(s, account=acct, cipher=cipher)
    assert "bad_refresh_token" in str(exc_info.value)
    # The exception carries the account + provider so the caller can surface
    # them to the founder ("reconnect github").
    assert exc_info.value.account_id == acct.id
    assert exc_info.value.provider == "badrefresh-stub"

    # Lift E46 — the token row's ``status`` is persisted to
    # ``needs_reauth`` so the connectors API + PWA card can render a
    # Reconnect CTA on the next read.
    async with sf() as s:
        row = (
            await s.execute(
                select(ConnectorOAuthTokenRow).where(
                    ConnectorOAuthTokenRow.connector_account_id == acct.id
                )
            )
        ).scalar_one()
        assert row.status == "needs_reauth"


async def test_successful_refresh_resets_status_to_active(sf, cipher) -> None:
    """Lift E46 — after a previous refresh failure persisted
    ``status='needs_reauth'``, a fresh OAuth flow (re-OAuth → upsert_token)
    and the FIRST successful refresh that follows return the row to
    ``status='active'`` so the PWA card flips back to "connected".
    """
    register_provider(StubProvider(name="resetstatus-stub", refreshable=True))
    acct = await _account(sf, cipher, "resetstatus-stub")
    expired = datetime.now(tz=UTC) - timedelta(minutes=1)
    async with sf() as s:
        await upsert_token(
            s,
            connector_account_id=acct.id,
            provider="resetstatus-stub",
            token=TokenSet(
                access_token="stale-access",
                refresh_token="r-1",
                expires_at=expired,
            ),
            cipher=cipher,
        )
        # Manually flip to needs_reauth simulating a prior failure.
        from backend.connectors.auth.db import ConnectorOAuthTokenRow

        row = (
            await s.execute(
                select(ConnectorOAuthTokenRow).where(
                    ConnectorOAuthTokenRow.connector_account_id == acct.id
                )
            )
        ).scalar_one()
        row.status = "needs_reauth"
        await s.commit()
    async with sf() as s:
        acct = await s.get(ConnectorAccountRow, acct.id)
        creds = await resolve_connector_credentials(s, account=acct, cipher=cipher)
        await s.commit()
    assert creds["token"] == "stub-access-refreshed"
    async with sf() as s:
        from backend.connectors.auth.db import ConnectorOAuthTokenRow

        row = (
            await s.execute(
                select(ConnectorOAuthTokenRow).where(
                    ConnectorOAuthTokenRow.connector_account_id == acct.id
                )
            )
        ).scalar_one()
        assert row.status == "active"


async def test_no_refresh_without_refresh_material(sf, cipher) -> None:
    register_provider(StubProvider(name="norefresh-stub"))
    acct = await _account(sf, cipher, "norefresh-stub")
    expired = datetime.now(tz=UTC) - timedelta(minutes=1)
    async with sf() as s:
        await upsert_token(
            s,
            connector_account_id=acct.id,
            provider="norefresh-stub",
            token=TokenSet(
                access_token="stale-access",
                refresh_token=None,  # nothing to refresh with
                expires_at=expired,
            ),
            cipher=cipher,
        )
        await s.commit()
    async with sf() as s:
        acct = await s.get(ConnectorAccountRow, acct.id)
        creds = await resolve_connector_credentials(s, account=acct, cipher=cipher)
    # No refresh material → return the existing (decrypted) access token as-is.
    assert creds["token"] == "stale-access"
