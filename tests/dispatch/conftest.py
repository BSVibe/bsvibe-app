"""Shared fixtures — in-memory sqlite + ModelAccount + WorkspaceRow seed.

Mirrors ``tests/router/conftest.py`` but also registers the workspace +
run-routing rule tables that the dispatch resolver reads.
"""

from __future__ import annotations

import base64
import os
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

import backend.embedding.db  # noqa: F401 — register tables on Base.metadata.

# Imported for table-registration side effects on the shared Base.metadata.
import backend.identity.workspaces_db  # noqa: F401
import backend.router.accounts.models  # noqa: F401
import backend.router.budget.models  # noqa: F401
import backend.router.routing.db  # noqa: F401
import backend.router.routing.run_routing.db  # noqa: F401
from backend.identity.workspaces_db import WorkspaceRow
from backend.router.accounts.crypto import CredentialCipher
from backend.router.accounts.models import ModelAccount
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


@pytest_asyncio.fixture
async def workspace(session: AsyncSession) -> WorkspaceRow:
    """A seeded :class:`WorkspaceRow` so resolver lookups have a real row."""
    row = WorkspaceRow(
        id=uuid.uuid4(),
        name="test-workspace",
        region="us-1",
        safe_mode=True,
        legal_basis="contract",
    )
    session.add(row)
    await session.flush()
    return row


@pytest_asyncio.fixture
async def model_account(
    session: AsyncSession, workspace: WorkspaceRow, cipher: CredentialCipher
) -> ModelAccount:
    """One active :class:`ModelAccount` for the seeded workspace."""
    account = ModelAccount(
        workspace_id=workspace.id,
        account_id=uuid.uuid4(),
        provider="ollama",
        label="default",
        litellm_model="ollama_chat/qwen3",
        api_base="http://localhost:11434",
        api_key_encrypted=None,
        data_jurisdiction="us",
        is_active=True,
        extra_params={},
    )
    session.add(account)
    await session.flush()
    return account


@pytest_asyncio.fixture
async def cloud_account(
    session: AsyncSession, workspace: WorkspaceRow, cipher: CredentialCipher
) -> ModelAccount:
    """A second active :class:`ModelAccount` with a different litellm_model."""
    account = ModelAccount(
        workspace_id=workspace.id,
        account_id=uuid.uuid4(),
        provider="anthropic",
        label="cloud",
        litellm_model="anthropic/claude-3-5-sonnet-20241022",
        api_base=None,
        api_key_encrypted=cipher.encrypt("sk-test"),
        data_jurisdiction="us",
        is_active=True,
        extra_params={},
    )
    session.add(account)
    await session.flush()
    return account
