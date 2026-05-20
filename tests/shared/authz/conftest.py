"""Shared fixtures for bsvibe-authz tests."""

from __future__ import annotations

import time
from collections.abc import Iterator

import jwt
import pytest


@pytest.fixture
def service_signing_secret() -> str:
    return "test-service-signing-secret-do-not-use-in-prod"


@pytest.fixture
def user_jwt_secret() -> str:
    return "test-user-jwt-secret-do-not-use-in-prod"


@pytest.fixture
def issuer() -> str:
    return "https://auth.bsvibe.dev"


@pytest.fixture
def now() -> int:
    return int(time.time())


@pytest.fixture
def make_user_jwt(user_jwt_secret: str, issuer: str, now: int):
    """Build a Supabase-style user session JWT with HS256.

    The package supports both HS256 (Phase 0 dev) and ES256/RS256 via JWKS
    (Phase 0.4-후속). Tests cover the HS256 path; production deployments
    inject the public key via settings.
    """

    def _make(
        sub: str = "00000000-0000-0000-0000-000000000001",
        email: str = "alice@bsvibe.dev",
        active_tenant_id: str = "tenant-123",
        exp_offset: int = 900,
        aud: str = "bsvibe",
        extra_claims: dict | None = None,
    ) -> str:
        payload = {
            "iss": issuer,
            "sub": sub,
            "email": email,
            "active_tenant_id": active_tenant_id,
            "iat": now,
            "exp": now + exp_offset,
            "aud": aud,
        }
        if extra_claims:
            payload.update(extra_claims)
        return jwt.encode(payload, user_jwt_secret, algorithm="HS256")

    return _make


@pytest.fixture
def make_service_jwt(service_signing_secret: str, issuer: str, now: int):
    """Build a service JWT matching the BSVibe-Auth PR #3 ServiceTokenPayload contract.

    Reference: BSVibe-Auth/phase0/auth-app/api/_lib/service-token.ts:51-62
    Required fields: iss, sub, aud, scope (space-delimited string), iat, exp, token_type="service"
    Optional: tenant_id
    """

    def _make(
        sub: str = "service:bsnexus",
        aud: str = "bsage",
        scope: str = "bsage:read bsage:write",
        exp_offset: int = 3600,
        tenant_id: str | None = None,
        token_type: str | None = "service",
    ) -> str:
        payload: dict = {
            "iss": issuer,
            "sub": sub,
            "aud": aud,
            "scope": scope,
            "iat": now,
            "exp": now + exp_offset,
        }
        if token_type is not None:
            payload["token_type"] = token_type
        if tenant_id is not None:
            payload["tenant_id"] = tenant_id
        return jwt.encode(payload, service_signing_secret, algorithm="HS256")

    return _make


@pytest.fixture
def reset_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[pytest.MonkeyPatch]:
    """Strip BSVIBE_*/OPENFGA_* env so Settings tests start from a clean slate."""
    for env in [
        "BSVIBE_AUTH_URL",
        "OPENFGA_API_URL",
        "OPENFGA_STORE_ID",
        "OPENFGA_AUTH_MODEL_ID",
        "OPENFGA_AUTH_TOKEN",
        "SERVICE_TOKEN_SIGNING_SECRET",
        "USER_JWT_SECRET",
        "USER_JWT_PUBLIC_KEY",
        "USER_JWT_ALGORITHM",
        "USER_JWT_AUDIENCE",
        "PERMISSION_CACHE_TTL_S",
        "INTROSPECTION_URL",
        "INTROSPECTION_CLIENT_ID",
        "INTROSPECTION_CLIENT_SECRET",
        "BSV_INTROSPECTION_URL",
        "BSV_INTROSPECTION_CLIENT_ID",
        "BSV_INTROSPECTION_CLIENT_SECRET",
    ]:
        monkeypatch.delenv(env, raising=False)
    yield monkeypatch
