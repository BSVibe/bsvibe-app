"""AES-256-GCM credential encryption.

Tiny helper using ``cryptography`` so plaintext API keys never live on
disk. Key is sourced from settings; for tests a deterministic key is
fine.
"""

from __future__ import annotations

import base64
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class CredentialCipher:
    """AES-256-GCM with a stable 32-byte key + per-call random nonce."""

    NONCE_BYTES = 12

    def __init__(self, key: bytes) -> None:
        if len(key) != 32:
            raise ValueError("CredentialCipher requires a 32-byte key")
        self._aesgcm = AESGCM(key)

    def encrypt(self, plaintext: str) -> str:
        nonce = os.urandom(self.NONCE_BYTES)
        ct = self._aesgcm.encrypt(nonce, plaintext.encode("utf-8"), associated_data=None)
        return base64.urlsafe_b64encode(nonce + ct).decode("ascii")

    def decrypt(self, token: str) -> str:
        raw = base64.urlsafe_b64decode(token.encode("ascii"))
        nonce, ct = raw[: self.NONCE_BYTES], raw[self.NONCE_BYTES :]
        return self._aesgcm.decrypt(nonce, ct, associated_data=None).decode("utf-8")


def _key_from_settings() -> bytes:
    from backend.config import get_settings  # noqa: PLC0415 — lazy to avoid circular import

    s = get_settings()
    if not s.gateway_kms_key_b64:
        raise RuntimeError("BSVIBE_GATEWAY_KMS_KEY_B64 is not set; cannot encrypt credentials")
    key = base64.urlsafe_b64decode(s.gateway_kms_key_b64)
    if len(key) != 32:
        raise RuntimeError("BSVIBE_GATEWAY_KMS_KEY_B64 must decode to 32 bytes")
    return key


def encrypt_credentials(plaintext: str) -> str:
    return CredentialCipher(_key_from_settings()).encrypt(plaintext)


def decrypt_credentials(token: str) -> str:
    return CredentialCipher(_key_from_settings()).decrypt(token)
