"""JWT verification tests — user session JWT (authentication only)."""

from __future__ import annotations

import jwt
import pytest


@pytest.fixture
def auth_settings(user_jwt_secret: str, issuer: str):
    from backend.shared.authz.settings import Settings

    return Settings(  # type: ignore[call-arg]
        user_jwt_secret=user_jwt_secret,
        user_jwt_algorithm="HS256",
        user_jwt_audience="bsvibe",
        user_jwt_issuer=issuer,
    )


async def test_verify_user_jwt_uses_jwks_when_url_set(monkeypatch, issuer, now) -> None:
    """``user_jwt_jwks_url`` takes priority over symmetric/static keys — the prod path."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    from backend.shared.authz.auth import reset_jwks_cache, verify_user_jwt
    from backend.shared.authz.settings import Settings

    private = ec.generate_private_key(ec.SECP256R1())
    pub_pem = private.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    priv_pem = private.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )

    token = jwt.encode(
        {"iss": issuer, "sub": "u-jwks", "iat": now, "exp": now + 60, "aud": "bsvibe"},
        priv_pem,
        algorithm="ES256",
        headers={"kid": "test-key-1"},
    )

    settings = Settings(  # type: ignore[call-arg]
        user_jwt_jwks_url="https://auth.bsvibe.dev/.well-known/jwks.json",
        user_jwt_algorithm="ES256",
        user_jwt_audience="bsvibe",
        user_jwt_issuer=issuer,
    )

    class _StubJWKSKey:
        def __init__(self, key: bytes) -> None:
            self.key = key

    class _StubJWKSClient:
        def __init__(self, *_a, **_kw) -> None: ...

        def get_signing_key_from_jwt(self, _token: str) -> _StubJWKSKey:
            return _StubJWKSKey(pub_pem)

    monkeypatch.setattr(jwt, "PyJWKClient", _StubJWKSClient)
    reset_jwks_cache()

    payload = verify_user_jwt(token, settings)
    assert payload["sub"] == "u-jwks"


async def test_verify_user_jwt_jwks_resolution_failure_raises(monkeypatch, issuer) -> None:
    from backend.shared.authz.auth import AuthError, reset_jwks_cache, verify_user_jwt
    from backend.shared.authz.settings import Settings

    class _BrokenJWKSClient:
        def __init__(self, *_a, **_kw) -> None: ...

        def get_signing_key_from_jwt(self, _token: str):
            raise jwt.PyJWKClientError("could not fetch JWKS")

    monkeypatch.setattr(jwt, "PyJWKClient", _BrokenJWKSClient)
    reset_jwks_cache()

    settings = Settings(  # type: ignore[call-arg]
        user_jwt_jwks_url="https://auth.example/.well-known/jwks.json",
        user_jwt_algorithm="ES256",
        user_jwt_audience="bsvibe",
        user_jwt_issuer=issuer,
    )

    with pytest.raises(AuthError, match="JWKS"):
        verify_user_jwt("a.b.c", settings)


async def test_verify_user_jwt_returns_payload(auth_settings, make_user_jwt) -> None:
    from backend.shared.authz.auth import verify_user_jwt

    token = make_user_jwt(sub="u-1", email="alice@bsvibe.dev")
    payload = verify_user_jwt(token, auth_settings)
    assert payload["sub"] == "u-1"
    assert payload["email"] == "alice@bsvibe.dev"


async def test_verify_user_jwt_expired_token(auth_settings, make_user_jwt) -> None:
    from backend.shared.authz.auth import AuthError, verify_user_jwt

    token = make_user_jwt(exp_offset=-10)
    with pytest.raises(AuthError):
        verify_user_jwt(token, auth_settings)


async def test_verify_user_jwt_wrong_audience(auth_settings, user_jwt_secret, issuer, now) -> None:
    from backend.shared.authz.auth import AuthError, verify_user_jwt

    token = jwt.encode(
        {
            "iss": issuer,
            "sub": "u-1",
            "email": "x@y.z",
            "aud": "wrong-aud",
            "iat": now,
            "exp": now + 60,
        },
        user_jwt_secret,
        algorithm="HS256",
    )
    with pytest.raises(AuthError):
        verify_user_jwt(token, auth_settings)


async def test_verify_user_jwt_wrong_signature(auth_settings, issuer, now) -> None:
    from backend.shared.authz.auth import AuthError, verify_user_jwt

    token = jwt.encode(
        {
            "iss": issuer,
            "sub": "u-1",
            "email": "x@y.z",
            "aud": "bsvibe",
            "iat": now,
            "exp": now + 60,
        },
        "different-secret-but-still-32-bytes-long-x",
        algorithm="HS256",
    )
    with pytest.raises(AuthError):
        verify_user_jwt(token, auth_settings)


async def test_verify_user_jwt_missing_sub_raises(
    auth_settings, user_jwt_secret, issuer, now
) -> None:
    from backend.shared.authz.auth import AuthError, verify_user_jwt

    # ``sub`` is in the required-claims set, so decode itself rejects it.
    token = jwt.encode(
        {"iss": issuer, "aud": "bsvibe", "iat": now, "exp": now + 60},
        user_jwt_secret,
        algorithm="HS256",
    )
    with pytest.raises(AuthError):
        verify_user_jwt(token, auth_settings)


def test_parse_user_token_returns_user() -> None:
    from backend.shared.authz.auth import parse_user_token

    user = parse_user_token({"sub": "u-1", "email": "alice@bsvibe.dev"})
    assert user.id == "u-1"
    assert user.email == "alice@bsvibe.dev"
    assert user.is_service is False


def test_parse_user_token_flags_service_principal() -> None:
    from backend.shared.authz.auth import parse_user_token

    user = parse_user_token({"sub": "service:worker", "email": None})
    assert user.is_service is True


def test_parse_user_token_missing_sub_raises() -> None:
    from backend.shared.authz.auth import AuthError, parse_user_token

    with pytest.raises(AuthError, match="missing sub"):
        parse_user_token({"email": "x@y.z"})
