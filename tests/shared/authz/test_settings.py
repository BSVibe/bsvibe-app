"""Settings (pydantic-settings) tests — user JWT verification config only."""

from __future__ import annotations

import pytest


def test_settings_loads_from_env(reset_settings_env: pytest.MonkeyPatch) -> None:
    reset_settings_env.setenv("USER_JWT_SECRET", "shhh")
    reset_settings_env.setenv("USER_JWT_ALGORITHM", "ES256")
    reset_settings_env.setenv("USER_JWT_AUDIENCE", "bsvibe")
    reset_settings_env.setenv("USER_JWT_ISSUER", "https://auth.bsvibe.dev")
    reset_settings_env.setenv("USER_JWT_JWKS_URL", "https://auth.bsvibe.dev/.well-known/jwks.json")

    from backend.shared.authz.settings import Settings

    s = Settings()  # type: ignore[call-arg]
    assert s.user_jwt_secret == "shhh"
    assert s.user_jwt_algorithm == "ES256"
    assert s.user_jwt_audience == "bsvibe"
    assert s.user_jwt_issuer == "https://auth.bsvibe.dev"
    assert s.user_jwt_jwks_url == "https://auth.bsvibe.dev/.well-known/jwks.json"


def test_settings_defaults(reset_settings_env: pytest.MonkeyPatch) -> None:
    from backend.shared.authz.settings import Settings

    s = Settings()  # type: ignore[call-arg]
    assert s.user_jwt_algorithm == "HS256"
    assert s.user_jwt_audience == "bsvibe"
    assert s.user_jwt_secret is None
    assert s.user_jwt_public_key is None
    assert s.user_jwt_jwks_url is None
    assert s.user_jwt_issuer is None


def test_settings_constructs_with_no_env(reset_settings_env: pytest.MonkeyPatch) -> None:
    """Settings() constructs at import time without any env configured."""
    from backend.shared.authz.settings import Settings

    Settings()  # type: ignore[call-arg]  # must not raise


def test_settings_ignores_extra_env(reset_settings_env: pytest.MonkeyPatch) -> None:
    """``extra="ignore"`` — stale OpenFGA-era env vars don't break construction."""
    reset_settings_env.setenv("OPENFGA_API_URL", "http://leftover:8080")

    from backend.shared.authz.settings import Settings

    s = Settings()  # type: ignore[call-arg]
    assert not hasattr(s, "openfga_api_url")


def test_settings_get_settings_singleton(reset_settings_env: pytest.MonkeyPatch) -> None:
    reset_settings_env.setenv("USER_JWT_SECRET", "shhh")

    from backend.shared.authz.settings import get_settings, reset_settings_cache

    reset_settings_cache()
    a = get_settings()
    b = get_settings()
    assert a is b
    reset_settings_cache()
