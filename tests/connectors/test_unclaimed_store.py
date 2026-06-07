"""connector_oauth_unclaimed — installs awaiting a workspace claim (Sentry, Lift 8).

Sentry's install→grant redirect carries no `state`, so the callback can't bind
the token to a workspace. It stores the exchanged token as an UNCLAIMED install;
the founder later claims it for a workspace (claim-later, design §11). This is
the encrypt-on-write / decrypt-on-claim store over that table.
"""

from __future__ import annotations

import uuid

import pytest

from backend.connectors.auth import store
from backend.connectors.auth.tokenset import TokenSet
from backend.router.accounts.crypto import CredentialCipher
from tests._support import memory_session

pytestmark = pytest.mark.asyncio

KEY = b"0123456789abcdef0123456789abcdef"


def _cipher() -> CredentialCipher:
    return CredentialCipher(KEY)


async def test_create_then_list_unclaimed_encrypts() -> None:
    cipher = _cipher()
    async with memory_session() as s:
        await store.create_unclaimed(
            s,
            provider="sentry",
            installation_ref="inst-1",
            account_label="Acme",
            token=TokenSet(access_token="tok", refresh_token="ref", expires_at=None),
            cipher=cipher,
        )
        await s.commit()
        rows = await store.list_unclaimed(s, provider="sentry")
    assert len(rows) == 1
    assert rows[0].installation_ref == "inst-1"
    assert rows[0].account_label == "Acme"
    assert rows[0].access_token_ciphertext != "tok"  # encrypted at rest


async def test_claim_returns_decrypted_token_and_deletes_row() -> None:
    cipher = _cipher()
    async with memory_session() as s:
        row = await store.create_unclaimed(
            s,
            provider="sentry",
            installation_ref="inst-9",
            account_label="Globex",
            token=TokenSet(access_token="tok", refresh_token="ref", expires_at=None),
            cipher=cipher,
        )
        await s.commit()
        uid = row.id
        claimed = await store.claim_unclaimed(s, unclaimed_id=uid, cipher=cipher)
        await s.commit()
        remaining = await store.list_unclaimed(s)
    assert claimed is not None
    provider, install_ref, token = claimed
    assert provider == "sentry"
    assert install_ref == "inst-9"
    assert token.access_token == "tok"
    assert token.refresh_token == "ref"
    assert remaining == []  # single-use: claimed row is gone


async def test_claim_missing_returns_none() -> None:
    async with memory_session() as s:
        assert await store.claim_unclaimed(s, unclaimed_id=uuid.uuid4(), cipher=_cipher()) is None
