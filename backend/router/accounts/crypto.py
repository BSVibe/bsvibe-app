"""AES-256-GCM credential encryption — with a key id, so the key can be rotated (#957).

Every stored secret goes through here: connector OAuth tokens and bot tokens, GitHub App
credentials, model-account API keys, sealed product secrets.

Format. New ciphertext is ``kms1.<kid>.<urlsafe-b64(nonce ‖ ct)>``. ``kid`` is the first 8 hex
of SHA-256 over the key — derived, so it needs no registry and cannot disagree with the key
it names. The pre-#957 format (``urlsafe-b64(nonce ‖ ct)``, no prefix) still decrypts; the
urlsafe alphabet has no ``.``, so the two cannot be confused.

Keys. ``BSVIBE_GATEWAY_KMS_KEY_B64`` encrypts (and decrypts); the comma-separated
``BSVIBE_GATEWAY_KMS_PREVIOUS_KEYS_B64`` only decrypts. Rotating is: put the new key in the
first, move the old one to the second, deploy, run the rewrap command
(``python -m backend.tools.rewrap_credentials``), then drop the old key — see
``docs/runbook/secret-rotation.md`` §3.

Failure. Before #957 a missing key surfaced as a bare ``InvalidTag`` inside whatever happened to
decrypt first. A ciphertext whose ``kid`` is not configured now raises
:class:`UnknownCredentialKey` naming it; a FORGED ciphertext is still ``InvalidTag`` — the
lookup never turns tampering into a "missing key" story.
"""

from __future__ import annotations

import base64
import hashlib
import os
from collections.abc import Iterable

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_FORMAT = "kms1"


class UnknownCredentialKey(ValueError):
    """The ciphertext was made by a key this process does not hold. Names the key id."""


def key_id(key: bytes) -> str:
    """The 8-hex id a ciphertext carries for ``key``."""
    return hashlib.sha256(key).hexdigest()[:8]


class CredentialCipher:
    """AES-256-GCM: encrypt with ``key``; decrypt with ``key`` or any of ``previous``."""

    NONCE_BYTES = 12

    def __init__(self, key: bytes, *, previous: Iterable[bytes] = ()) -> None:
        keys = [key, *previous]
        for k in keys:
            if len(k) != 32:
                raise ValueError("CredentialCipher requires 32-byte keys")
        self._kid = key_id(key)
        self._aesgcm = AESGCM(key)
        # Current first: a legacy (kid-less) ciphertext is tried in this order.
        self._by_kid: dict[str, AESGCM] = {}
        for k in keys:
            self._by_kid.setdefault(key_id(k), AESGCM(k))

    def encrypt(self, plaintext: str) -> str:
        nonce = os.urandom(self.NONCE_BYTES)
        ct = self._aesgcm.encrypt(nonce, plaintext.encode("utf-8"), associated_data=None)
        body = base64.urlsafe_b64encode(nonce + ct).decode("ascii")
        return f"{_FORMAT}.{self._kid}.{body}"

    def decrypt(self, token: str) -> str:
        kid, body = _split(token)
        raw = base64.urlsafe_b64decode(body.encode("ascii"))
        nonce, ct = raw[: self.NONCE_BYTES], raw[self.NONCE_BYTES :]
        if kid is not None:
            aesgcm = self._by_kid.get(kid)
            if aesgcm is None:
                raise UnknownCredentialKey(
                    f"credential was encrypted with key {kid}, which is not configured "
                    "(BSVIBE_GATEWAY_KMS_KEY_B64 / BSVIBE_GATEWAY_KMS_PREVIOUS_KEYS_B64)"
                )
            return aesgcm.decrypt(nonce, ct, associated_data=None).decode("utf-8")
        for aesgcm in self._by_kid.values():
            try:
                return aesgcm.decrypt(nonce, ct, associated_data=None).decode("utf-8")
            except InvalidTag:
                continue
        raise UnknownCredentialKey(
            "legacy credential (no key id) opens with no configured key — it was encrypted "
            "with a key that is neither current nor listed as previous"
        )

    def needs_rewrap(self, token: str) -> bool:
        """``True`` unless ``token`` is already in the current format under the current key."""
        kid, _ = _split(token)
        return kid != self._kid


def _split(token: str) -> tuple[str | None, str]:
    """``(kid, body)`` — ``kid`` is ``None`` for the pre-#957 format."""
    if token.startswith(f"{_FORMAT}."):
        _, kid, body = token.split(".", 2)
        return kid, body
    return None, token


def _decode_key(b64: str, *, name: str) -> bytes:
    key = base64.urlsafe_b64decode(b64)
    if len(key) != 32:
        raise RuntimeError(f"{name} must decode to 32 bytes")
    return key


def _key_from_settings() -> bytes:
    from backend.config import get_settings  # noqa: PLC0415 — lazy to avoid circular import

    s = get_settings()
    if not s.gateway_kms_key_b64:
        raise RuntimeError("BSVIBE_GATEWAY_KMS_KEY_B64 is not set; cannot encrypt credentials")
    return _decode_key(s.gateway_kms_key_b64, name="BSVIBE_GATEWAY_KMS_KEY_B64")


def _previous_keys_from_settings() -> list[bytes]:
    from backend.config import get_settings  # noqa: PLC0415

    return [
        _decode_key(b64, name="BSVIBE_GATEWAY_KMS_PREVIOUS_KEYS_B64")
        for b64 in get_settings().gateway_kms_previous_keys_b64
    ]


def cipher_from_settings() -> CredentialCipher:
    """The cipher every production path uses: current key + every retired key that may still
    be on a stored value."""
    return CredentialCipher(_key_from_settings(), previous=_previous_keys_from_settings())


def encrypt_credentials(plaintext: str) -> str:
    return cipher_from_settings().encrypt(plaintext)


def decrypt_credentials(token: str) -> str:
    return cipher_from_settings().decrypt(token)


__all__ = [
    "CredentialCipher",
    "UnknownCredentialKey",
    "cipher_from_settings",
    "decrypt_credentials",
    "encrypt_credentials",
    "key_id",
]
