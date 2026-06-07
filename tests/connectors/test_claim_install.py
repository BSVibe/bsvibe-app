"""claim_install — bind an unclaimed install to a workspace (Sentry claim-later)."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from backend.connectors.auth import service, store
from backend.connectors.auth.db import ConnectorOAuthTokenRow
from backend.connectors.auth.tokenset import TokenSet
from backend.connectors.db import ConnectorAccountRow
from backend.router.accounts.crypto import CredentialCipher
from tests._support import memory_session

pytestmark = pytest.mark.asyncio

KEY = b"0123456789abcdef0123456789abcdef"


def _cipher() -> CredentialCipher:
    return CredentialCipher(KEY)


async def test_claim_binds_install_to_workspace_with_external_ref() -> None:
    cipher = _cipher()
    ws = uuid.uuid4()
    async with memory_session() as s:
        row = await store.create_unclaimed(
            s,
            provider="sentry",
            installation_ref="inst-77",
            account_label="Acme",
            token=TokenSet(access_token="tok", refresh_token="ref", expires_at=None),
            cipher=cipher,
        )
        await s.commit()
        connector = await service.claim_install(
            s, unclaimed_id=row.id, workspace_id=ws, cipher=cipher
        )
        assert connector == "sentry"

        acct = (
            await s.execute(
                select(ConnectorAccountRow).where(ConnectorAccountRow.workspace_id == ws)
            )
        ).scalar_one()
        assert acct.connector == "sentry"
        assert acct.external_ref == "inst-77"  # installationId stored for refresh

        tok = (
            await s.execute(
                select(ConnectorOAuthTokenRow).where(
                    ConnectorOAuthTokenRow.connector_account_id == acct.id
                )
            )
        ).scalar_one()
        assert cipher.decrypt(tok.access_token_ciphertext) == "tok"
        # unclaimed row consumed
        assert await store.list_unclaimed(s) == []


async def test_claim_missing_raises() -> None:
    async with memory_session() as s:
        with pytest.raises(ValueError, match="not found"):
            await service.claim_install(
                s, unclaimed_id=uuid.uuid4(), workspace_id=uuid.uuid4(), cipher=_cipher()
            )


async def test_list_unclaimed_installs_has_no_secrets() -> None:
    cipher = _cipher()
    async with memory_session() as s:
        await store.create_unclaimed(
            s,
            provider="sentry",
            installation_ref="inst-1",
            account_label="Acme",
            token=TokenSet(access_token="secret-tok", refresh_token=None, expires_at=None),
            cipher=cipher,
        )
        await s.commit()
        listed = await service.list_unclaimed_installs(s)
    assert len(listed) == 1
    assert listed[0]["installation_ref"] == "inst-1"
    assert "secret-tok" not in str(listed)
