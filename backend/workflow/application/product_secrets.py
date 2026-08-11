"""Sealing a product's declared verification secrets at the write seam.

Both write seams — the REST product update and the MCP ``set_metadata`` tool —
go through here, because a secret sealed on one path and stored in the clear on
the other is a secret in the clear.

Here rather than in :mod:`backend.workflow.domain.verify_secrets` because
building the cipher needs the KMS key, and the domain module is deliberately
pure: it can be reasoned about, and tested, without a key existing at all.
"""

from __future__ import annotations

from typing import Any

from backend.router.accounts.crypto import CredentialCipher, _key_from_settings
from backend.workflow.domain.verify_secrets import seal_secrets


def sealed_product_metadata(incoming: dict[str, Any], prior: Any) -> dict[str, Any]:
    """``incoming`` with every declared secret stored encrypted.

    Sealed at the WRITE, because this is the last moment the plaintext is
    supposed to exist: ``products.product_metadata`` is a plaintext JSON column
    that lands in dumps, in backups and in API responses.

    ``prior`` is what the product already holds, which is what lets a masked
    value mean "keep it". The settings round-trip reads the product, is shown a
    mask, and PUTs the whole object back — taking that mask literally would wipe
    the secret on every unrelated edit.

    The cipher is built ON FIRST USE, so the KMS key is required only when a
    plaintext secret is actually being stored. Renaming a product must not start
    failing because of a key that product never needed — and every deployment
    and test that stores no secrets keeps working exactly as before.
    """
    cipher: CredentialCipher | None = None

    def _encrypt(plaintext: str) -> str:
        nonlocal cipher
        if cipher is None:
            cipher = CredentialCipher(_key_from_settings())
        return cipher.encrypt(plaintext)

    return seal_secrets(incoming, encrypt=_encrypt, prior=prior or {})


__all__ = ["sealed_product_metadata"]
