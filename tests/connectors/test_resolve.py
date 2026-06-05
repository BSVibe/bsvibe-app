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
