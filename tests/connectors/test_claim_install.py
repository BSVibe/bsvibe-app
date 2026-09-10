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
        await store.create_unclaimed(
            s,
            provider="sentry",
            installation_ref="inst-77",
            account_label="Acme",
            token=TokenSet(access_token="tok", refresh_token="ref", expires_at=None),
            cipher=cipher,
        )
        await s.commit()
        connector = await service.claim_install(
            s, provider="sentry", installation_ref="inst-77", workspace_id=ws, cipher=cipher
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
        # unclaimed row consumed — a second claim of the same ref finds nothing
        assert (
            await store.claim_by_installation(
                s, provider="sentry", installation_ref="inst-77", cipher=cipher
            )
            is None
        )


async def test_claim_missing_raises() -> None:
    async with memory_session() as s:
        with pytest.raises(ValueError, match="not found"):
            await service.claim_install(
                s,
                provider="sentry",
                installation_ref="nope",
                workspace_id=uuid.uuid4(),
                cipher=_cipher(),
            )


async def test_claim_with_wrong_ref_does_not_bind_and_leaves_the_row() -> None:
    """H2 — a tenant who does not possess the installation ref cannot claim
    another tenant's pending install, and the row survives for its true owner.

    The vulnerability: ``claim`` matched a caller-supplied row id from a global
    ``list_unclaimed``, so any workspace could claim any pending install and bind
    the third party's OAuth token into itself. Now the ref (a Sentry-side UUID
    seen only by whoever installed) is the proof; a wrong ref binds nothing.
    """
    cipher = _cipher()
    victim_ws = uuid.uuid4()
    attacker_ws = uuid.uuid4()
    async with memory_session() as s:
        await store.create_unclaimed(
            s,
            provider="sentry",
            installation_ref="inst-victim",
            account_label="Victim Corp",
            token=TokenSet(access_token="victim-tok", refresh_token=None, expires_at=None),
            cipher=cipher,
        )
        await s.commit()

        with pytest.raises(ValueError, match="not found"):
            await service.claim_install(
                s,
                provider="sentry",
                installation_ref="inst-guessed",
                workspace_id=attacker_ws,
                cipher=cipher,
            )
        # No account was created for the attacker.
        attacker_accts = (
            (
                await s.execute(
                    select(ConnectorAccountRow).where(
                        ConnectorAccountRow.workspace_id == attacker_ws
                    )
                )
            )
            .scalars()
            .all()
        )
        assert attacker_accts == []

        # The true owner, who possesses the ref, still claims it.
        connector = await service.claim_install(
            s,
            provider="sentry",
            installation_ref="inst-victim",
            workspace_id=victim_ws,
            cipher=cipher,
        )
        assert connector == "sentry"
