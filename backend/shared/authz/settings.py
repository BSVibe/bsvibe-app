"""Pydantic-settings configuration for bsvibe-authz.

Phase 0 uses HS256 for both user and service JWTs (shared secret + signing
secret). Phase 0.4-후속 will introduce JWKS rotation for user JWTs and
Ed25519 for service tokens — the verifier accepts a `user_jwt_public_key`
slot in addition to the secret to make that swap mechanical.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

UserJwtAlgorithm = Literal["HS256", "RS256", "ES256", "EdDSA"]


class Settings(BaseSettings):
    """Configuration loaded from environment variables.

    All `BSVIBE_*` / `OPENFGA_*` / `SERVICE_TOKEN_*` / `USER_JWT_*` env vars
    map to fields below. The model deliberately accepts ``extra="ignore"`` so
    products carrying their own settings can coexist.
    """

    model_config = SettingsConfigDict(
        env_file=None,
        case_sensitive=False,
        extra="ignore",
    )

    # BSVibe-Auth. Empty default so a partially-configured deployment (no
    # OpenFGA bootstrapped yet) can still construct ``Settings()`` at import
    # time — products previously hand-rolled tolerant Settings adapters to
    # work around the required fields.
    bsvibe_auth_url: str = ""

    # OpenFGA. Empty ``openfga_api_url`` means OpenFGA is not deployed: the
    # ``require_permission`` dep then runs in permissive mode (authenticated
    # callers pass, the OpenFGA check is skipped).
    openfga_api_url: str = ""
    openfga_store_id: str = ""
    openfga_auth_model_id: str = ""
    openfga_auth_token: str | None = None
    openfga_request_timeout_s: float = 3.0

    # Service-token verification (matches BSVibe-Auth PR #3 issuance secret).
    service_token_signing_secret: str = ""
    service_token_issuer: str | None = None

    # OAuth2 client_credentials grant (BSVibe-Auth PR #7). One row per
    # backend in `oauth_clients`. Optional so verifier-only services
    # (e.g. BSSupervisor receiver side) need not configure them.
    bsvibe_client_id: str | None = None
    bsvibe_client_secret: str | None = None

    # User session JWT verification. Three signing-key sources, in
    # priority order: (1) ``user_jwt_jwks_url`` — fetch + cache the
    # JWKS document and resolve the signing key from the token's ``kid``
    # header (BSVibe-Auth and Supabase rotation pattern); (2)
    # ``user_jwt_public_key`` — static PEM for asymmetric algos;
    # (3) ``user_jwt_secret`` — symmetric key for HS256.
    user_jwt_secret: str | None = None
    user_jwt_public_key: str | None = None
    user_jwt_jwks_url: str | None = None
    user_jwt_algorithm: UserJwtAlgorithm = "HS256"
    user_jwt_audience: str = "bsvibe"
    user_jwt_issuer: str | None = None

    # Demo session JWT — separate issuer (HS256, DEMO_JWT_SECRET). When
    # configured, tokens carrying ``is_demo: true`` are verified against
    # this secret and resolve to a User with ``is_demo=True``. Permission
    # checks short-circuit to allow for demo principals so the demo
    # backends don't need OpenFGA + a synthetic user graph.
    demo_jwt_secret: str | None = None

    # Permission cache.
    permission_cache_ttl_s: int = 30

    # RFC 7662 OAuth2 token introspection (opaque session tokens issued by
    # BSVibe-Auth). Empty introspection_url disables the opaque-token path —
    # the verifier then only handles JWT tokens.
    #
    # `BSV_*` env aliases match the canonical names used in product docs
    # (`BSV_INTROSPECTION_URL`, `BSV_INTROSPECTION_CLIENT_ID`, …). The
    # unprefixed names remain primary for backwards compatibility with
    # existing deployments.
    introspection_url: str = Field(
        default="",
        validation_alias=AliasChoices("introspection_url", "bsv_introspection_url"),
    )
    introspection_client_id: str = Field(
        default="",
        validation_alias=AliasChoices("introspection_client_id", "bsv_introspection_client_id"),
    )
    introspection_client_secret: str = Field(
        default="",
        validation_alias=AliasChoices(
            "introspection_client_secret", "bsv_introspection_client_secret"
        ),
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide Settings singleton (cached)."""
    return Settings()  # type: ignore[call-arg]


def reset_settings_cache() -> None:
    """Drop the cached Settings — used by tests."""
    get_settings.cache_clear()
