"""Shared fixtures for authz (authentication) tests."""

from __future__ import annotations

import time
from collections.abc import Iterator

import jwt
import pytest


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

    Production verifies ES256 via JWKS; the HS256 path is the local-dev
    equivalent the unit tests exercise.
    """

    def _make(
        sub: str = "00000000-0000-0000-0000-000000000001",
        email: str = "alice@bsvibe.dev",
        exp_offset: int = 900,
        aud: str = "bsvibe",
        extra_claims: dict | None = None,
    ) -> str:
        payload = {
            "iss": issuer,
            "sub": sub,
            "email": email,
            "iat": now,
            "exp": now + exp_offset,
            "aud": aud,
        }
        if extra_claims:
            payload.update(extra_claims)
        return jwt.encode(payload, user_jwt_secret, algorithm="HS256")

    return _make


@pytest.fixture
def reset_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[pytest.MonkeyPatch]:
    """Strip USER_JWT_* env so Settings tests start from a clean slate."""
    for env in [
        "USER_JWT_SECRET",
        "USER_JWT_PUBLIC_KEY",
        "USER_JWT_JWKS_URL",
        "USER_JWT_ALGORITHM",
        "USER_JWT_AUDIENCE",
        "USER_JWT_ISSUER",
    ]:
        monkeypatch.delenv(env, raising=False)
    yield monkeypatch
