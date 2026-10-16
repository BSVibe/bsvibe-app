"""#957 — the credential key can be rotated: ciphertext names its key, old keys still read.

Before: ``base64(nonce ‖ ct)`` under ONE key from ``BSVIBE_GATEWAY_KMS_KEY_B64``. Nothing in a
ciphertext said which key made it, and there was no second key to read with — so changing the
key made every stored OAuth token, bot token and API key unreadable at once, and the failure
surfaced only as a generic ``InvalidTag`` deep in whichever path decrypted first.

After:

* new ciphertext is ``kms1.<kid>.<base64(nonce ‖ ct)>`` — ``kid`` is derived from the key, so
  it needs no registry and cannot drift from it;
* ``BSVIBE_GATEWAY_KMS_PREVIOUS_KEYS_B64`` holds retired keys that still DECRYPT; encryption
  always uses the current key;
* a legacy (unprefixed) ciphertext still decrypts — current key first, then the previous ones;
* a ciphertext whose key is not configured fails LOUDLY, naming the kid — the operator learns
  *which* key is missing instead of chasing an ``InvalidTag``;
* :meth:`needs_rewrap` says which values the rewrap command must rewrite.
"""

from __future__ import annotations

import ast
import base64
import os
from pathlib import Path

import pytest

from backend.router.accounts.crypto import CredentialCipher, UnknownCredentialKey, key_id

_OLD = b"\x01" * 32
_NEW = b"\x02" * 32
_OTHER = b"\x03" * 32


def _legacy(key: bytes, plaintext: str) -> str:
    """The pre-#957 format, byte for byte: urlsafe-b64(nonce ‖ ct), no prefix."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    nonce = os.urandom(12)
    ct = AESGCM(key).encrypt(nonce, plaintext.encode(), None)
    return base64.urlsafe_b64encode(nonce + ct).decode()


def test_new_ciphertext_names_its_key() -> None:
    token = CredentialCipher(_NEW).encrypt("sk-secret")

    assert token.startswith(f"kms1.{key_id(_NEW)}.")
    assert CredentialCipher(_NEW).decrypt(token) == "sk-secret"


def test_the_key_id_is_stable_and_distinguishes_keys() -> None:
    assert key_id(_NEW) == key_id(bytes(_NEW))
    assert key_id(_NEW) != key_id(_OLD)
    assert len(key_id(_NEW)) == 8


def test_a_legacy_ciphertext_still_decrypts() -> None:
    assert CredentialCipher(_NEW).decrypt(_legacy(_NEW, "tok")) == "tok"


def test_after_rotation_the_old_keys_ciphertext_still_reads() -> None:
    before = CredentialCipher(_OLD).encrypt("bot-token")
    legacy = _legacy(_OLD, "pat")

    rotated = CredentialCipher(_NEW, previous=[_OLD])

    assert rotated.decrypt(before) == "bot-token"
    assert rotated.decrypt(legacy) == "pat"
    assert rotated.encrypt("x").startswith(f"kms1.{key_id(_NEW)}.")


def test_a_missing_key_fails_loudly_naming_it() -> None:
    token = CredentialCipher(_OTHER).encrypt("x")

    with pytest.raises(UnknownCredentialKey, match=key_id(_OTHER)):
        CredentialCipher(_NEW, previous=[_OLD]).decrypt(token)


def test_a_legacy_ciphertext_no_configured_key_opens_fails_loudly() -> None:
    with pytest.raises(UnknownCredentialKey, match="no configured key"):
        CredentialCipher(_NEW, previous=[_OLD]).decrypt(_legacy(_OTHER, "x"))


def test_needs_rewrap_marks_everything_not_under_the_current_key() -> None:
    cipher = CredentialCipher(_NEW, previous=[_OLD])

    assert cipher.needs_rewrap(_legacy(_NEW, "a")) is True  # right key, old format
    assert cipher.needs_rewrap(CredentialCipher(_OLD).encrypt("b")) is True
    assert cipher.needs_rewrap(cipher.encrypt("c")) is False


def test_a_tampered_ciphertext_is_still_an_authentication_failure() -> None:
    """The key lookup must not turn a forged value into a 'missing key' story."""
    from cryptography.exceptions import InvalidTag

    token = CredentialCipher(_NEW).encrypt("x")
    head, body = token.rsplit(".", 1)
    raw = bytearray(base64.urlsafe_b64decode(body))
    raw[-1] ^= 1
    forged = f"{head}.{base64.urlsafe_b64encode(bytes(raw)).decode()}"

    with pytest.raises(InvalidTag):
        CredentialCipher(_NEW).decrypt(forged)


def test_the_settings_factory_reads_the_previous_keys(monkeypatch) -> None:
    from backend.config import get_settings
    from backend.router.accounts.crypto import cipher_from_settings

    b64 = lambda k: base64.urlsafe_b64encode(k).decode()  # noqa: E731
    monkeypatch.setenv("BSVIBE_GATEWAY_KMS_KEY_B64", b64(_NEW))
    monkeypatch.setenv("BSVIBE_GATEWAY_KMS_PREVIOUS_KEYS_B64", f"{b64(_OLD)},{b64(_OTHER)}")
    get_settings.cache_clear()
    try:
        cipher = cipher_from_settings()
        assert cipher.decrypt(CredentialCipher(_OTHER).encrypt("x")) == "x"
        assert cipher.encrypt("y").startswith(f"kms1.{key_id(_NEW)}.")
    finally:
        get_settings.cache_clear()


def _constructions(root: Path) -> set[str]:
    """Every ``backend`` module (relative path) that CALLS ``CredentialCipher(...)``."""
    sites: set[str] = set()
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and (
                (isinstance(node.func, ast.Name) and node.func.id == "CredentialCipher")
                or (isinstance(node.func, ast.Attribute) and node.func.attr == "CredentialCipher")
            ):
                sites.add(str(path.relative_to(root.parent)))
    return sites


def test_production_code_builds_the_cipher_only_through_the_factory() -> None:
    """A cipher built with the current key alone cannot read a value under a retired key — so a
    path that bypassed :func:`cipher_from_settings` would break, alone and silently, the moment
    a rotation began. Only ``crypto.py`` (the factory itself) may construct one."""
    backend = Path(__file__).resolve().parents[2] / "backend"

    sites = _constructions(backend)

    assert "backend/router/accounts/crypto.py" in sites, "control: the factory must be seen"
    assert sites == {"backend/router/accounts/crypto.py"}, sorted(sites)
