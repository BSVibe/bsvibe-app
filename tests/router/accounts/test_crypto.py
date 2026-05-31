"""Tests for backend.router.accounts.crypto — AES-256-GCM round-trip."""

from __future__ import annotations

import base64

import pytest

from backend.router.accounts.crypto import (
    CredentialCipher,
    decrypt_credentials,
    encrypt_credentials,
)


class TestCipherRoundTrip:
    def test_round_trip_yields_plaintext(self, cipher: CredentialCipher):
        ct = cipher.encrypt("super-secret-key")
        assert cipher.decrypt(ct) == "super-secret-key"

    def test_different_nonces_produce_different_ciphertexts(self, cipher: CredentialCipher):
        a = cipher.encrypt("same-input")
        b = cipher.encrypt("same-input")
        assert a != b

    def test_rejects_wrong_key_length(self):
        with pytest.raises(ValueError, match="32-byte"):
            CredentialCipher(b"short")

    def test_decrypt_with_wrong_key_fails(self):
        from cryptography.exceptions import InvalidTag

        c1 = CredentialCipher(b"a" * 32)
        c2 = CredentialCipher(b"b" * 32)
        ct = c1.encrypt("secret")
        with pytest.raises(InvalidTag):
            c2.decrypt(ct)


class TestModuleLevelHelpers:
    def test_module_helpers_round_trip_via_env_key(self):
        ct = encrypt_credentials("hello")
        assert decrypt_credentials(ct) == "hello"

    def test_missing_env_key_raises(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("BSVIBE_GATEWAY_KMS_KEY_B64", raising=False)
        from backend.config import get_settings

        get_settings.cache_clear()
        with pytest.raises(RuntimeError, match="not set"):
            encrypt_credentials("x")
        get_settings.cache_clear()

    def test_bad_key_length_raises(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv(
            "BSVIBE_GATEWAY_KMS_KEY_B64",
            base64.urlsafe_b64encode(b"a" * 16).decode("ascii"),
        )
        from backend.config import get_settings

        get_settings.cache_clear()
        with pytest.raises(RuntimeError, match="32 bytes"):
            encrypt_credentials("x")
        get_settings.cache_clear()
