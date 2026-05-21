"""Shared gateway fixtures — in-memory sqlite + deterministic crypto key."""

from __future__ import annotations

import base64
import os
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.accounts.crypto import CredentialCipher
from backend.accounts.models import AccountsBase
from backend.gateway.budget.models import GatewayBudgetBase
from backend.gateway.embedding.db import GatewayEmbeddingBase
from backend.gateway.routing.db import GatewayRoutingBase
from backend.gateway.rules.db import GatewayRulesBase


@pytest.fixture
def cipher_key() -> bytes:
    return b"a" * 32  # deterministic; tests do not need real entropy


@pytest.fixture
def cipher(cipher_key: bytes) -> CredentialCipher:
    return CredentialCipher(cipher_key)


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(AccountsBase.metadata.create_all)
        await conn.run_sync(GatewayBudgetBase.metadata.create_all)
        await conn.run_sync(GatewayRulesBase.metadata.create_all)
        await conn.run_sync(GatewayEmbeddingBase.metadata.create_all)
        await conn.run_sync(GatewayRoutingBase.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


@pytest.fixture(autouse=True)
def _gateway_kms_key_env(monkeypatch: pytest.MonkeyPatch, cipher_key: bytes) -> None:
    """Set BSVIBE_GATEWAY_KMS_KEY_B64 so module-level encrypt helpers work."""
    monkeypatch.setenv(
        "BSVIBE_GATEWAY_KMS_KEY_B64",
        base64.urlsafe_b64encode(cipher_key).decode("ascii"),
    )
    from backend.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
    # Don't leak the env var into other test modules.
    os.environ.pop("BSVIBE_GATEWAY_KMS_KEY_B64", None)
