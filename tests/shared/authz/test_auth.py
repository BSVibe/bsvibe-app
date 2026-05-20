"""JWT verification tests — user JWT + service JWT."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock

import jwt
import pytest


@pytest.fixture
def auth_settings(user_jwt_secret: str, service_signing_secret: str, issuer: str):
    from backend.shared.authz.settings import Settings

    return Settings(  # type: ignore[call-arg]
        bsvibe_auth_url=issuer,
        openfga_api_url="http://openfga.local:8080",
        openfga_store_id="store-1",
        openfga_auth_model_id="model-1",
        service_token_signing_secret=service_signing_secret,
        user_jwt_secret=user_jwt_secret,
        user_jwt_algorithm="HS256",
        user_jwt_audience="bsvibe",
        user_jwt_issuer=issuer,
    )


async def test_verify_user_jwt_uses_jwks_when_url_set(monkeypatch, issuer, now) -> None:
    """``user_jwt_jwks_url`` takes priority over symmetric/static keys."""
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
        {
            "iss": issuer,
            "sub": "u-jwks",
            "iat": now,
            "exp": now + 60,
            "aud": "bsvibe",
        },
        priv_pem,
        algorithm="ES256",
        headers={"kid": "test-key-1"},
    )

    settings = Settings(  # type: ignore[call-arg]
        bsvibe_auth_url=issuer,
        openfga_api_url="http://openfga.local:8080",
        openfga_store_id="store-1",
        openfga_auth_model_id="model-1",
        service_token_signing_secret="x",
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
        bsvibe_auth_url=issuer,
        openfga_api_url="http://openfga.local:8080",
        openfga_store_id="store-1",
        openfga_auth_model_id="model-1",
        service_token_signing_secret="x",
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


async def test_verify_service_jwt_passes_for_valid_payload(auth_settings, make_service_jwt) -> None:
    from backend.shared.authz.auth import verify_service_jwt

    token = make_service_jwt(
        sub="service:bsnexus",
        aud="bsage",
        scope="bsage:read bsage:write",
        tenant_id="t-1",
    )
    payload = verify_service_jwt(token, auth_settings, expected_audience="bsage")
    assert payload.aud == "bsage"
    assert payload.sub == "service:bsnexus"
    assert payload.has_scope("bsage:read")
    assert payload.tenant_id == "t-1"
    assert payload.token_type == "service"


async def test_verify_service_jwt_uses_service_token_issuer(
    user_jwt_secret: str,
    service_signing_secret: str,
    now: int,
) -> None:
    from backend.shared.authz.auth import verify_service_jwt
    from backend.shared.authz.settings import Settings

    settings = Settings(  # type: ignore[call-arg]
        bsvibe_auth_url="http://auth-app:5179",
        openfga_api_url="http://openfga.local:8080",
        openfga_store_id="store-1",
        openfga_auth_model_id="model-1",
        service_token_signing_secret=service_signing_secret,
        service_token_issuer="http://auth-app:5179",
        user_jwt_secret=user_jwt_secret,
        user_jwt_algorithm="HS256",
        user_jwt_audience="bsvibe",
        user_jwt_issuer="http://localhost:54321/auth/v1",
    )
    token = jwt.encode(
        {
            "iss": "http://auth-app:5179",
            "sub": "user:u-1",
            "aud": "bsupervisor",
            "scope": "bsupervisor:events",
            "iat": now,
            "exp": now + 60,
            "token_type": "service",
            "tenant_id": "t-1",
        },
        service_signing_secret,
        algorithm="HS256",
    )

    payload = verify_service_jwt(token, settings, expected_audience="bsupervisor")

    assert payload.aud == "bsupervisor"
    assert payload.has_scope("bsupervisor:events")


async def test_verify_service_jwt_rejects_wrong_audience(auth_settings, make_service_jwt) -> None:
    from backend.shared.authz.auth import AuthError, verify_service_jwt

    token = make_service_jwt(aud="bsage")
    with pytest.raises(AuthError):
        verify_service_jwt(token, auth_settings, expected_audience="bsgateway")


async def test_verify_service_jwt_rejects_wrong_token_type(
    auth_settings, service_signing_secret, issuer
) -> None:
    from backend.shared.authz.auth import AuthError, verify_service_jwt

    now = int(time.time())
    token = jwt.encode(
        {
            "iss": issuer,
            "sub": "service:x",
            "aud": "bsage",
            "scope": "bsage:read",
            "iat": now,
            "exp": now + 60,
            "token_type": "user",  # wrong
        },
        service_signing_secret,
        algorithm="HS256",
    )
    with pytest.raises(AuthError):
        verify_service_jwt(token, auth_settings, expected_audience="bsage")


async def test_verify_service_jwt_rejects_expired(auth_settings, make_service_jwt) -> None:
    from backend.shared.authz.auth import AuthError, verify_service_jwt

    token = make_service_jwt(exp_offset=-10)
    with pytest.raises(AuthError):
        verify_service_jwt(token, auth_settings, expected_audience="bsage")


async def test_verify_service_jwt_rejects_scope_audience_mismatch(
    auth_settings, service_signing_secret, issuer
) -> None:
    from backend.shared.authz.auth import AuthError, verify_service_jwt

    now = int(time.time())
    token = jwt.encode(
        {
            "iss": issuer,
            "sub": "service:x",
            "aud": "bsage",
            "scope": "bsgateway:read",  # scope doesn't match aud
            "iat": now,
            "exp": now + 60,
            "token_type": "service",
        },
        service_signing_secret,
        algorithm="HS256",
    )
    with pytest.raises(AuthError):
        verify_service_jwt(token, auth_settings, expected_audience="bsage")


async def test_parse_user_token_returns_user(auth_settings, make_user_jwt) -> None:
    from backend.shared.authz.auth import parse_user_token, verify_user_jwt

    token = make_user_jwt(sub="u-1", email="alice@bsvibe.dev", active_tenant_id="t-1")
    payload = verify_user_jwt(token, auth_settings)
    user = parse_user_token(payload)
    assert user.id == "u-1"
    assert user.email == "alice@bsvibe.dev"
    assert user.active_tenant_id == "t-1"
    assert user.is_service is False
    # New: empty metadata when claims don't provide them.
    assert user.app_metadata == {}
    assert user.user_metadata == {}


async def test_parse_user_token_lifts_app_metadata(auth_settings, make_user_jwt) -> None:
    """Supabase claims under ``app_metadata`` / ``user_metadata`` flow through."""
    from backend.shared.authz.auth import parse_user_token, verify_user_jwt

    token = make_user_jwt(
        sub="u-1",
        active_tenant_id="t-1",
        extra_claims={
            "app_metadata": {"role": "admin", "tenant_id": "t-1"},
            "user_metadata": {"name": "Alice"},
        },
    )
    payload = verify_user_jwt(token, auth_settings)
    user = parse_user_token(payload)
    assert user.app_metadata == {"role": "admin", "tenant_id": "t-1"}
    assert user.user_metadata == {"name": "Alice"}


async def test_parse_user_token_falls_back_to_app_metadata_tenant_id(
    auth_settings, make_user_jwt
) -> None:
    """Supabase JWTs nest tenant_id under app_metadata — lift it as fallback."""
    from backend.shared.authz.auth import parse_user_token, verify_user_jwt

    # No top-level active_tenant_id — only the nested one.
    token = make_user_jwt(
        sub="u-1",
        active_tenant_id="",  # cleared below by extra_claims override
        extra_claims={
            "active_tenant_id": None,
            "app_metadata": {"role": "viewer", "tenant_id": "t-nested"},
        },
    )
    payload = verify_user_jwt(token, auth_settings)
    user = parse_user_token(payload)
    assert user.active_tenant_id == "t-nested"
    assert user.app_metadata["tenant_id"] == "t-nested"


async def test_parse_user_token_top_level_tenant_id_wins(auth_settings, make_user_jwt) -> None:
    """Top-level active_tenant_id takes precedence over app_metadata.tenant_id."""
    from backend.shared.authz.auth import parse_user_token, verify_user_jwt

    token = make_user_jwt(
        sub="u-1",
        active_tenant_id="t-top",
        extra_claims={"app_metadata": {"tenant_id": "t-nested"}},
    )
    payload = verify_user_jwt(token, auth_settings)
    user = parse_user_token(payload)
    assert user.active_tenant_id == "t-top"


# ---------------------------------------------------------------------------
# verify_via_introspection (RFC 7662 introspection + cache)
# ---------------------------------------------------------------------------


@pytest.fixture
def opaque_cache():
    from backend.shared.authz.cache import IntrospectionCache

    return IntrospectionCache(ttl_s=60)


def _make_active_response(**overrides):
    from backend.shared.authz.types import IntrospectionResponse

    payload = {
        "active": True,
        "sub": "u-1",
        "tenant": "t-1",
        "aud": ["bsgateway"],
        "scope": ["bsgateway:models:write"],
        "exp": 9999999999,
        "client_id": "bsgateway-prod",
        "username": "alice",
    }
    payload.update(overrides)
    return IntrospectionResponse(**payload)


async def test_verify_via_introspection_returns_user_on_active(opaque_cache) -> None:
    from backend.shared.authz.auth import verify_via_introspection

    client = AsyncMock()
    client.introspect = AsyncMock(return_value=_make_active_response())

    user = await verify_via_introspection("bsv_sk_abc", client, opaque_cache)

    assert user.id == "u-1"
    assert user.active_tenant_id == "t-1"
    assert user.scope == ["bsgateway:models:write"]
    assert user.is_service is False
    client.introspect.assert_awaited_once_with("bsv_sk_abc")


async def test_verify_via_introspection_propagates_role_into_app_metadata(
    opaque_cache,
) -> None:
    """Tier 5: an introspection response carrying ``role`` surfaces it on
    ``User.app_metadata`` so PAT callers drive require_permission's lazy
    tuple-provision + require_admin."""
    from backend.shared.authz.auth import verify_via_introspection

    client = AsyncMock()
    client.introspect = AsyncMock(
        return_value=_make_active_response(role="admin"),
    )

    user = await verify_via_introspection("bsv_sk_abc", client, opaque_cache)

    assert user.app_metadata == {"role": "admin"}


async def test_verify_via_introspection_no_role_leaves_app_metadata_empty(
    opaque_cache,
) -> None:
    """No ``role`` in the introspection response → empty app_metadata (no
    spurious key)."""
    from backend.shared.authz.auth import verify_via_introspection

    client = AsyncMock()
    client.introspect = AsyncMock(return_value=_make_active_response())

    user = await verify_via_introspection("bsv_sk_abc", client, opaque_cache)

    assert user.app_metadata == {}


async def test_verify_via_introspection_raises_on_inactive(opaque_cache) -> None:
    from backend.shared.authz.auth import AuthError, verify_via_introspection
    from backend.shared.authz.types import IntrospectionResponse

    client = AsyncMock()
    client.introspect = AsyncMock(return_value=IntrospectionResponse(active=False))

    with pytest.raises(AuthError) as exc_info:
        await verify_via_introspection("bsv_sk_revoked", client, opaque_cache)

    # Token contents must NOT leak into the error message.
    assert "bsv_sk_revoked" not in str(exc_info.value)


async def test_verify_via_introspection_uses_cache_on_repeat_calls(opaque_cache) -> None:
    from backend.shared.authz.auth import verify_via_introspection

    client = AsyncMock()
    client.introspect = AsyncMock(return_value=_make_active_response())

    user1 = await verify_via_introspection("bsv_sk_xyz", client, opaque_cache)
    user2 = await verify_via_introspection("bsv_sk_xyz", client, opaque_cache)

    assert user1.id == user2.id == "u-1"
    # Second call should hit cache, not re-introspect.
    client.introspect.assert_awaited_once()


async def test_verify_via_introspection_caches_inactive_response(opaque_cache) -> None:
    from backend.shared.authz.auth import AuthError, verify_via_introspection
    from backend.shared.authz.types import IntrospectionResponse

    client = AsyncMock()
    client.introspect = AsyncMock(return_value=IntrospectionResponse(active=False))

    for _ in range(2):
        with pytest.raises(AuthError):
            await verify_via_introspection("bsv_sk_revoked", client, opaque_cache)

    client.introspect.assert_awaited_once()
