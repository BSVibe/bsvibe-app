"""Pydantic-settings configuration for authentication.

Only the user session JWT verification knobs remain — OpenFGA, service-token,
and RFC 7662 introspection settings were retired with the cross-service authz
layer. Production verifies Supabase JWTs via JWKS (``USER_JWT_JWKS_URL`` +
``USER_JWT_ALGORITHM=ES256``); HS256 + a shared secret stays available for
local dev.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

UserJwtAlgorithm = Literal["HS256", "RS256", "ES256", "EdDSA"]


class Settings(BaseSettings):
    """Configuration loaded from environment variables.

    All ``USER_JWT_*`` env vars map to fields below. The model accepts
    ``extra="ignore"`` so products carrying their own settings can coexist.
    """

    model_config = SettingsConfigDict(
        env_file=None,
        case_sensitive=False,
        extra="ignore",
    )

    # User session JWT verification. Three signing-key sources, in
    # priority order: (1) ``user_jwt_jwks_url`` — fetch + cache the
    # JWKS document and resolve the signing key from the token's ``kid``
    # header (Supabase rotation pattern); (2) ``user_jwt_public_key`` —
    # static PEM for asymmetric algos; (3) ``user_jwt_secret`` — symmetric
    # key for HS256 (local dev).
    user_jwt_secret: str | None = None
    user_jwt_public_key: str | None = None
    user_jwt_jwks_url: str | None = None
    user_jwt_algorithm: UserJwtAlgorithm = "HS256"
    user_jwt_audience: str = "bsvibe"
    user_jwt_issuer: str | None = None


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide Settings singleton (cached)."""
    return Settings()


def reset_settings_cache() -> None:
    """Drop the cached Settings — used by tests."""
    get_settings.cache_clear()
