"""Shared gateway fixtures — in-memory sqlite + deterministic crypto key."""

from __future__ import annotations

import base64
import os
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

import backend.embedding.db  # noqa: F401

# Imported for table-registration side effects on the shared Base.metadata.
import backend.router.accounts.models  # noqa: F401
import backend.router.budget.models  # noqa: F401
import backend.router.routing.db  # noqa: F401
import backend.router.rules.db  # noqa: F401
from backend.router.accounts.crypto import CredentialCipher
from tests._support import memory_session


@pytest.fixture
def cipher_key() -> bytes:
    return b"a" * 32  # deterministic; tests do not need real entropy


@pytest.fixture
def cipher(cipher_key: bytes) -> CredentialCipher:
    return CredentialCipher(cipher_key)


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    async with memory_session() as s:
        yield s


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
