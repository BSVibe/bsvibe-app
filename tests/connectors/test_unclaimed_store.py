"""connector_oauth_unclaimed — installs awaiting a workspace claim (Sentry, Lift 8).

Sentry's install→grant redirect carries no `state`, so the callback can't bind
the token to a workspace. It stores the exchanged token as an UNCLAIMED install;
the founder later claims it by PROVING possession of the installation ref (H2,
2026-09-10 audit) — the id visible only in their own Sentry org. This is the
encrypt-on-write / decrypt-on-claim store over that table.
"""

from __future__ import annotations

import pytest

from backend.connectors.auth import store
from backend.connectors.auth.tokenset import TokenSet
from backend.router.accounts.crypto import CredentialCipher
from tests._support import memory_session

pytestmark = pytest.mark.asyncio

KEY = b"0123456789abcdef0123456789abcdef"


def _cipher() -> CredentialCipher:
    return CredentialCipher(KEY)


async def _seed(s, cipher, *, provider="sentry", ref="inst-1", label="Acme"):
    await store.create_unclaimed(
        s,
        provider=provider,
        installation_ref=ref,
        account_label=label,
        token=TokenSet(access_token="tok", refresh_token="ref", expires_at=None),
        cipher=cipher,
    )
    await s.commit()


async def test_create_encrypts_at_rest() -> None:
    cipher = _cipher()
    async with memory_session() as s:
        row = await store.create_unclaimed(
            s,
            provider="sentry",
            installation_ref="inst-1",
            account_label="Acme",
            token=TokenSet(access_token="tok", refresh_token="ref", expires_at=None),
            cipher=cipher,
        )
        await s.commit()
        assert row.access_token_ciphertext != "tok"  # encrypted at rest


async def test_claim_by_installation_returns_decrypted_token_and_deletes_row() -> None:
    cipher = _cipher()
    async with memory_session() as s:
        await _seed(s, cipher, ref="inst-9", label="Globex")
        claimed = await store.claim_by_installation(
            s, provider="sentry", installation_ref="inst-9", cipher=cipher
        )
        await s.commit()
    assert claimed is not None
    provider, install_ref, token = claimed
    assert provider == "sentry"
    assert install_ref == "inst-9"
    assert token.access_token == "tok"
    assert token.refresh_token == "ref"


async def test_claim_by_installation_is_single_use() -> None:
    cipher = _cipher()
    async with memory_session() as s:
        await _seed(s, cipher, ref="inst-9")
        first = await store.claim_by_installation(
            s, provider="sentry", installation_ref="inst-9", cipher=cipher
        )
        await s.commit()
        second = await store.claim_by_installation(
            s, provider="sentry", installation_ref="inst-9", cipher=cipher
        )
    assert first is not None
    assert second is None  # single-use: the row is gone


async def test_claim_wrong_ref_returns_none_and_leaves_the_row() -> None:
    """H2 — a caller who does not possess the ref cannot claim, and the pending
    row survives for its true owner. This is the cross-tenant defense: another
    tenant's ``installation_ref`` (a Sentry-side UUID) is not knowable."""
    cipher = _cipher()
    async with memory_session() as s:
        await _seed(s, cipher, ref="inst-real")
        miss = await store.claim_by_installation(
            s, provider="sentry", installation_ref="inst-guessed", cipher=cipher
        )
        assert miss is None
        # The real owner can still claim.
        hit = await store.claim_by_installation(
            s, provider="sentry", installation_ref="inst-real", cipher=cipher
        )
        assert hit is not None


async def test_claim_wrong_provider_returns_none() -> None:
    cipher = _cipher()
    async with memory_session() as s:
        await _seed(s, cipher, provider="sentry", ref="inst-1")
        assert (
            await store.claim_by_installation(
                s, provider="github", installation_ref="inst-1", cipher=cipher
            )
            is None
        )
