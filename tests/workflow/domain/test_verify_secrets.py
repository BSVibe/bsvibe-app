"""A product declares its OWN verification secrets — the platform only carries them.

Which identity a browser probe logs in as, which API key an HTTP probe presents,
which channel a delivery probe reads: every one of those is a fact about THAT
product, not a decision the platform gets to make. So the platform's whole job
here is a safe place to put them and a path to the check that needs them.

Stored on the product, and stored SEALED. ``products.product_metadata`` is a
plaintext JSON column — it lands in DB dumps, backups, API responses and the
occasional log line — so a password written there in the clear is a password
published. Sealing at the write seam means the plaintext exists only in the
request that set it.

Two failure modes this file exists to prevent, both silent:

* a value read back out and written back in (the PWA's settings round-trip does
  exactly that) must not overwrite the secret with the mask it was shown;
* sealing twice must not double-encrypt, or the value can never be read again.
"""

from __future__ import annotations

import pytest

from backend.router.accounts.crypto import CredentialCipher
from backend.workflow.domain.verify_secrets import (
    MASK,
    METADATA_KEY,
    declared_secret_names,
    redact_secrets,
    seal_secrets,
    unseal_secrets,
)

#: The REAL cipher, not a stand-in. A fake that returned ``f"CT<{plaintext}>"``
#: passes every shape assertion here while carrying the plaintext through — the
#: test would be supplying the very answer it checks for. AES-GCM is pure; there
#: is no reason to substitute it.
_CIPHER = CredentialCipher(b"0123456789abcdef0123456789abcdef")


def _encrypt(plaintext: str) -> str:
    return _CIPHER.encrypt(plaintext)


class TestSealing:
    def test_a_plaintext_value_is_sealed(self) -> None:
        sealed = seal_secrets({METADATA_KEY: {"BSVIBE_TEST_PASSWORD": "hunter2"}}, encrypt=_encrypt)

        stored = sealed[METADATA_KEY]["BSVIBE_TEST_PASSWORD"]
        assert "hunter2" not in stored, f"the plaintext survived into the column: {stored!r}"
        assert stored.startswith("enc:")

    def test_sealing_twice_does_not_double_encrypt(self) -> None:
        """Every metadata write re-seals the whole map. A second pass that
        encrypted the ciphertext again would leave a value nothing can decrypt —
        and nothing would notice until a check needed it."""
        once = seal_secrets({METADATA_KEY: {"K": "v"}}, encrypt=_encrypt)
        twice = seal_secrets(once, encrypt=_encrypt)

        assert twice == once

    def test_the_mask_keeps_whatever_was_there(self) -> None:
        """The round-trip trap. A settings screen reads the product, shows the
        secret as a mask, and PUTs the whole object back. Taking the mask
        literally would wipe the secret on every unrelated edit."""
        prior = seal_secrets({METADATA_KEY: {"K": "real"}}, encrypt=_encrypt)

        after = seal_secrets({METADATA_KEY: {"K": MASK}}, encrypt=_encrypt, prior=prior)

        assert after[METADATA_KEY]["K"] == prior[METADATA_KEY]["K"]

    def test_the_mask_with_nothing_prior_drops_the_key(self) -> None:
        """ "Keep what was there" when nothing was there is not a secret whose
        value is the mask string — that would be a credential of ``***``."""
        after = seal_secrets({METADATA_KEY: {"K": MASK}}, encrypt=_encrypt)

        assert "K" not in after.get(METADATA_KEY, {})

    def test_an_empty_value_removes_the_secret(self) -> None:
        prior = seal_secrets({METADATA_KEY: {"K": "real"}}, encrypt=_encrypt)

        after = seal_secrets({METADATA_KEY: {"K": ""}}, encrypt=_encrypt, prior=prior)

        assert "K" not in after.get(METADATA_KEY, {})

    def test_metadata_without_secrets_is_untouched(self) -> None:
        metadata = {"execution_target": "client_attach", "client_workspace_path": "/x"}

        assert seal_secrets(metadata, encrypt=_encrypt) == metadata

    def test_a_non_mapping_under_the_key_is_refused(self) -> None:
        """Loudly, not silently dropped: a founder who wrote the wrong shape
        would otherwise believe their secret was stored."""
        with pytest.raises(ValueError, match=METADATA_KEY):
            seal_secrets({METADATA_KEY: ["not", "a", "map"]}, encrypt=_encrypt)

    def test_names_are_validated_as_environment_variables(self) -> None:
        """They become ``docker run -e NAME``. A name with a space or an ``=``
        turns into a different flag, or a value assignment carrying the secret
        into the command string — which is the one place it must never be."""
        with pytest.raises(ValueError, match="name"):
            seal_secrets({METADATA_KEY: {"BAD NAME": "v"}}, encrypt=_encrypt)
        with pytest.raises(ValueError, match="name"):
            seal_secrets({METADATA_KEY: {"K=V": "v"}}, encrypt=_encrypt)


class TestReading:
    def test_reads_show_a_mask_not_the_ciphertext(self) -> None:
        """Even sealed, the ciphertext is the secret's only representation — no
        reason to hand it to every reader of the product. The mask also gives
        the round-trip its "keep this" token."""
        sealed = seal_secrets({METADATA_KEY: {"K": "real"}}, encrypt=_encrypt)

        shown = redact_secrets(sealed)

        assert shown[METADATA_KEY] == {"K": MASK}
        assert sealed[METADATA_KEY]["K"] not in str(shown), "the ciphertext leaked to the reader"

    def test_redacting_leaves_the_rest_of_the_metadata_alone(self) -> None:
        sealed = seal_secrets(
            {"execution_target": "client_attach", METADATA_KEY: {"K": "v"}}, encrypt=_encrypt
        )

        shown = redact_secrets(sealed)

        assert shown["execution_target"] == "client_attach"

    def test_declared_names_are_readable_without_decrypting(self) -> None:
        """The stack plan needs the NAMES (to pass ``-e NAME``) and must never
        need the values — that is what keeps values out of command strings."""
        sealed = seal_secrets({METADATA_KEY: {"B": "2", "A": "1"}}, encrypt=_encrypt)

        assert declared_secret_names(sealed) == ["A", "B"], "sorted: commands must be stable"

    def test_no_declaration_means_no_names(self) -> None:
        assert declared_secret_names({}) == []
        assert declared_secret_names({"execution_target": "client_attach"}) == []


class TestUnsealing:
    def test_a_sealed_secret_comes_back(self) -> None:
        sealed = seal_secrets({METADATA_KEY: {"K": "hunter2"}}, encrypt=_encrypt)

        assert unseal_secrets(sealed, decrypt=_CIPHER.decrypt) == {"K": "hunter2"}

    def test_an_unreadable_secret_is_dropped_not_raised(self) -> None:
        """A rotated KMS key must not take down every run of the product. The
        check that needed it fails on its own terms — a login that does not work
        is a verdict, and a verdict is what verification is for."""
        other = CredentialCipher(b"ffffffffffffffffffffffffffffffff")
        sealed = seal_secrets({METADATA_KEY: {"K": "v"}}, encrypt=_encrypt)

        assert unseal_secrets(sealed, decrypt=other.decrypt) == {}

    def test_nothing_declared_unseals_to_nothing(self) -> None:
        assert unseal_secrets({}, decrypt=_CIPHER.decrypt) == {}
