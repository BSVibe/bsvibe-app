"""Tests for backend.identity.oauth_pkce — Lift D1."""

from __future__ import annotations

import base64
import hashlib

from backend.identity.oauth_pkce import (
    is_valid_verifier,
    match_redirect_uri,
    redirect_uris_equivalent,
    verify_pkce,
)


def _challenge_for(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


class TestIsValidVerifier:
    def test_min_length_43(self) -> None:
        assert is_valid_verifier("A" * 43)

    def test_max_length_128(self) -> None:
        assert is_valid_verifier("A" * 128)

    def test_too_short(self) -> None:
        assert not is_valid_verifier("A" * 42)

    def test_too_long(self) -> None:
        assert not is_valid_verifier("A" * 129)

    def test_alphabet(self) -> None:
        # URL-safe + unreserved per RFC 3986: [A-Z][a-z][0-9]-._~
        assert is_valid_verifier("abcDEF123-._~" + "A" * 30)

    def test_disallowed_char_space(self) -> None:
        assert not is_valid_verifier("A" * 42 + " ")

    def test_disallowed_char_slash(self) -> None:
        assert not is_valid_verifier("/" + "A" * 42)


class TestVerifyPkce:
    def test_s256_match(self) -> None:
        verifier = "abcDEF123-._~abcDEF123-._~abcDEF123-._~xyzAB"
        challenge = _challenge_for(verifier)
        assert verify_pkce(challenge, "S256", verifier)

    def test_plain_method_rejected(self) -> None:
        verifier = "A" * 43
        assert not verify_pkce(verifier, "plain", verifier)

    def test_invalid_verifier_rejected(self) -> None:
        # Too short → still must return False without crashing.
        assert not verify_pkce("anything", "S256", "tiny")

    def test_wrong_verifier(self) -> None:
        right = "abcDEF123-._~abcDEF123-._~abcDEF123-._~xyzAB"
        wrong = "abcDEF123-._~abcDEF123-._~abcDEF123-._~XXXAB"
        challenge = _challenge_for(right)
        assert not verify_pkce(challenge, "S256", wrong)

    def test_unknown_method(self) -> None:
        assert not verify_pkce("anything", "weird", "A" * 43)


class TestMatchRedirectUri:
    def test_exact_match(self) -> None:
        assert match_redirect_uri(["https://app.example.com/cb"], "https://app.example.com/cb")

    def test_unregistered_https(self) -> None:
        assert not match_redirect_uri(["https://app.example.com/cb"], "https://evil.com/cb")

    def test_loopback_port_flex(self) -> None:
        assert match_redirect_uri(["http://127.0.0.1/callback"], "http://127.0.0.1:54321/callback")

    def test_loopback_localhost_alias(self) -> None:
        assert match_redirect_uri(["http://127.0.0.1/callback"], "http://localhost:8080/callback")

    def test_loopback_path_must_match(self) -> None:
        assert not match_redirect_uri(["http://127.0.0.1/callback"], "http://127.0.0.1:8080/other")

    def test_loopback_rejects_https(self) -> None:
        # Loopback rule is HTTP-only (RFC 8252 §7.3).
        assert not match_redirect_uri(
            ["http://127.0.0.1/callback"], "https://127.0.0.1:8080/callback"
        )

    def test_empty_registered_list(self) -> None:
        assert not match_redirect_uri([], "http://127.0.0.1:8080/callback")


class TestRedirectUrisEquivalent:
    def test_exact(self) -> None:
        assert redirect_uris_equivalent("http://127.0.0.1:54321/cb", "http://127.0.0.1:54321/cb")

    def test_loopback_alias(self) -> None:
        assert redirect_uris_equivalent("http://127.0.0.1:54321/cb", "http://localhost:54321/cb")

    def test_port_mismatch_in_loopback(self) -> None:
        # Both loopback but different ports — the request port on /token
        # MUST match the port the code was issued with.
        assert not redirect_uris_equivalent(
            "http://127.0.0.1:54321/cb", "http://localhost:54322/cb"
        )

    def test_non_loopback_must_be_exact(self) -> None:
        assert not redirect_uris_equivalent("https://a.example.com/cb", "https://b.example.com/cb")
