"""Shared accounts fixtures — in-memory sqlite + deterministic crypto key."""

from __future__ import annotations

import base64
import os
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.accounts.crypto import CredentialCipher
from backend.accounts.models import AccountsBase


@pytest.fixture
def cipher_key() -> bytes:
    return b"a" * 32


@pytest.fixture
def cipher(cipher_key: bytes) -> CredentialCipher:
    return CredentialCipher(cipher_key)


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(AccountsBase.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


@pytest.fixture(autouse=True)
def _gateway_kms_key_env(monkeypatch: pytest.MonkeyPatch, cipher_key: bytes) -> None:
    monkeypatch.setenv(
        "BSVIBE_GATEWAY_KMS_KEY_B64",
        base64.urlsafe_b64encode(cipher_key).decode("ascii"),
    )
    from backend.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
    os.environ.pop("BSVIBE_GATEWAY_KMS_KEY_B64", None)
