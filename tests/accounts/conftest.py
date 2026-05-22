"""Shared accounts fixtures — in-memory sqlite + deterministic crypto key."""

from __future__ import annotations

import base64
import os
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

# Imported for table-registration side effects on the shared Base.metadata.
import backend.accounts.models  # noqa: F401
from backend.accounts.crypto import CredentialCipher
from tests._support import memory_session


@pytest.fixture
def cipher_key() -> bytes:
    return b"a" * 32


@pytest.fixture
def cipher(cipher_key: bytes) -> CredentialCipher:
    return CredentialCipher(cipher_key)


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    async with memory_session() as s:
        yield s


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
