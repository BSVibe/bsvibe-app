"""같은 커넥터 계정의 토큰을 동시에 갱신해도 refresh 는 한 번만 일어난다.

Postgres 에서 두 세션이 동시에 resolve 한다(HTTP 계층인 provider.refresh 만 가짜).
refresh 토큰은 한 번만 쓸 수 있다 — 잠금이 없으면 늦은 쪽이 실패해 needs_reauth 가 된다.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.connectors.auth.db import ConnectorOAuthTokenRow
from backend.connectors.auth.providers import StubProvider, register_provider
from backend.connectors.auth.resolve import (
    ConnectorReauthRequired,
    resolve_connector_credentials,
)
from backend.connectors.auth.store import get_or_create_account, upsert_token
from backend.connectors.auth.tokenset import TokenSet
from backend.connectors.db import ConnectorAccountRow
from backend.router.accounts.crypto import CredentialCipher

from .._support import db_engine

pytestmark = pytest.mark.asyncio

TEST_KEY = b"0123456789abcdef0123456789abcdef"


@pytest_asyncio.fixture
async def sf():
    async with db_engine() as (engine, is_pg):
        if not is_pg:
            pytest.skip("행 잠금 동시성은 실제 Postgres 에서만 검증된다")
        yield async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
def cipher() -> CredentialCipher:
    return CredentialCipher(TEST_KEY)


class _SingleUseProvider(StubProvider):
    """refresh 토큰은 한 번만 유효하다(GitHub 처럼). 호출 횟수를 센다."""

    def __init__(self, *, name: str, fail_always: bool = False) -> None:
        super().__init__(name=name, refreshable=True)
        self.calls = 0
        self._used: set[str] = set()
        self._fail_always = fail_always

    async def refresh(self, *, refresh_token: str):
        self.calls += 1
        await asyncio.sleep(0.3)  # 경합 창을 넓힌다
        if self._fail_always or refresh_token in self._used:
            raise ValueError("bad_refresh_token")
        self._used.add(refresh_token)
        return TokenSet(
            access_token="new-access",
            refresh_token="r-2",
            expires_at=datetime.now(tz=UTC) + timedelta(hours=1),
        )


async def _seed(sf, cipher, name: str) -> uuid.UUID:
    async with sf() as s:
        acct = await get_or_create_account(
            s, workspace_id=uuid.uuid4(), connector=name, cipher=cipher
        )
        await upsert_token(
            s,
            connector_account_id=acct.id,
            provider=name,
            token=TokenSet(
                access_token="stale",
                refresh_token="r-1",
                expires_at=datetime.now(tz=UTC) - timedelta(minutes=1),
            ),
            cipher=cipher,
        )
        await s.commit()
        return acct.id


async def _resolve(sf, cipher, account_id: uuid.UUID) -> dict[str, str]:
    async with sf() as s:
        acct = await s.get(ConnectorAccountRow, account_id)
        return await resolve_connector_credentials(s, account=acct, cipher=cipher)


async def _status(sf, account_id: uuid.UUID) -> str:
    async with sf() as s:
        row = (
            await s.execute(
                select(ConnectorOAuthTokenRow).where(
                    ConnectorOAuthTokenRow.connector_account_id == account_id
                )
            )
        ).scalar_one()
        return row.status


async def test_concurrent_refresh_calls_provider_once(sf, cipher) -> None:
    provider = _SingleUseProvider(name="race-stub")
    register_provider(provider)
    account_id = await _seed(sf, cipher, "race-stub")

    a, b = await asyncio.gather(_resolve(sf, cipher, account_id), _resolve(sf, cipher, account_id))

    assert provider.calls == 1
    assert a["token"] == b["token"] == "new-access"
    assert await _status(sf, account_id) != "needs_reauth"


async def test_truly_expired_refresh_token_flags_needs_reauth(sf, cipher) -> None:
    provider = _SingleUseProvider(name="dead-stub", fail_always=True)
    register_provider(provider)
    account_id = await _seed(sf, cipher, "dead-stub")

    with pytest.raises(ConnectorReauthRequired):
        await _resolve(sf, cipher, account_id)

    assert provider.calls == 1
    assert await _status(sf, account_id) == "needs_reauth"
