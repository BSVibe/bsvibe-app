"""JWT verification — user session JWTs and service-to-service JWTs.

Two distinct flows
------------------
1. **User JWT** (`verify_user_jwt`): tokens issued by BSVibe-Auth's session
   endpoint. Phase 0 dev uses HS256 with a shared secret; Phase 0.4-후속
   will swap to JWKS (RS256/ES256/EdDSA) — the verifier accepts a public
   key via settings to make that mechanical.
2. **Service JWT** (`verify_service_jwt`): tokens issued by BSVibe-Auth's
   `POST /api/service-tokens/issue` endpoint. Audience-scoped (Lockin §3
   decision #16) and `scope`-claim guarded. The decoded payload must satisfy
   the `ServiceTokenPayload` Pydantic model (mirrors BSVibe-Auth PR #3).
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any

import jwt
import structlog

from .settings import Settings
from .types import ServiceAudience, ServiceTokenPayload, User

if TYPE_CHECKING:
    from .cache import IntrospectionCache
    from .introspection import IntrospectionClient

logger = structlog.get_logger(__name__)


class AuthError(Exception):
    """Authentication failed (invalid signature, expired, wrong audience, ...)."""


_jwks_client_cache: dict[str, jwt.PyJWKClient] = {}


def _resolve_user_signing_key(token: str, settings: Settings) -> Any:
    """Resolve the verification key for ``token``.

    Priority order: JWKS URL → static public key → symmetric secret.
    The JWKS client is cached per-process and per-URL; ``PyJWKClient``
    handles its own LRU cache for kid → key resolution.
    """
    if settings.user_jwt_jwks_url:
        client = _jwks_client_cache.get(settings.user_jwt_jwks_url)
        if client is None:
            client = jwt.PyJWKClient(settings.user_jwt_jwks_url)
            _jwks_client_cache[settings.user_jwt_jwks_url] = client
        try:
            return client.get_signing_key_from_jwt(token).key
        except jwt.PyJWKClientError as exc:
            raise AuthError(f"JWKS resolution failed: {exc}") from exc

    if settings.user_jwt_algorithm == "HS256":
        if not settings.user_jwt_secret:
            raise AuthError("user_jwt_secret not configured")
        return settings.user_jwt_secret

    if not settings.user_jwt_public_key:
        raise AuthError("user_jwt_public_key or user_jwt_jwks_url not configured")
    return settings.user_jwt_public_key


def reset_jwks_cache() -> None:
    """Drop the per-process JWKS client cache — used by tests."""
    _jwks_client_cache.clear()


def verify_user_jwt(token: str, settings: Settings) -> dict[str, Any]:
    """Verify a Supabase / BSVibe-Auth user session JWT, return decoded claims.

    Validates: signature, expiration (exp), audience, issuer (if configured).
    Raises `AuthError` on any failure.
    """
    options = {"require": ["exp", "iat", "sub"]}
    try:
        payload = jwt.decode(
            token,
            _resolve_user_signing_key(token, settings),
            algorithms=[settings.user_jwt_algorithm],
            audience=settings.user_jwt_audience,
            issuer=settings.user_jwt_issuer,
            options=options,
        )
    except jwt.PyJWTError as exc:
        logger.warning("user_jwt_invalid", error=str(exc))
        raise AuthError(f"user JWT verification failed: {exc}") from exc
    return payload


def verify_service_jwt(
    token: str,
    settings: Settings,
    expected_audience: ServiceAudience,
) -> ServiceTokenPayload:
    """Verify a service-to-service JWT for `expected_audience`.

    Strict checks (Lockin §3 #16 + Auth_Design §6.4):
    - audience matches `expected_audience`
    - `token_type == "service"`
    - all scopes in the token are prefixed with the audience
    - signature, exp, iat all valid
    """
    if not settings.service_token_signing_secret:
        raise AuthError("service_token_signing_secret not configured")

    try:
        raw = jwt.decode(
            token,
            settings.service_token_signing_secret,
            algorithms=["HS256"],
            audience=expected_audience,
            issuer=settings.service_token_issuer or settings.bsvibe_auth_url,
            options={"require": ["exp", "iat", "sub", "aud"]},
        )
    except jwt.PyJWTError as exc:
        logger.warning("service_jwt_invalid", error=str(exc))
        raise AuthError(f"service JWT verification failed: {exc}") from exc

    try:
        payload = ServiceTokenPayload(**raw)
    except (ValueError, TypeError) as exc:
        logger.warning("service_jwt_payload_invalid", error=str(exc))
        raise AuthError(f"service JWT payload invalid: {exc}") from exc

    if payload.aud != expected_audience:
        raise AuthError(
            f"service JWT audience mismatch: expected {expected_audience}, got {payload.aud}",
        )

    # Defense-in-depth: re-check scope-audience binding even though the issuer
    # already enforces it. A misconfigured issuer must never silently widen
    # privilege. Round 5 final: MCP grammar only (``<aud>:<resource>``).
    audience_prefix = f"{payload.aud}:"
    for scope in payload.scopes:
        if not scope.startswith(audience_prefix):
            raise AuthError(
                f"service JWT scope {scope!r} does not match audience {payload.aud!r}",
            )

    return payload


async def verify_via_introspection(
    token: str,
    client: IntrospectionClient,
    cache: IntrospectionCache,
) -> User:
    """Verify a token via RFC 7662 introspection.

    Used as the PAT-JWT fallback in :func:`get_current_user` — PAT JWTs
    from the device-authorization grant are signed with
    ``SERVICE_TOKEN_SIGNING_SECRET`` (not ``USER_JWT_SECRET``), so they
    fail ``verify_user_jwt``; ``/oauth/introspect`` accepts them by ``jti``.

    Caches both active and inactive responses keyed by sha256(token) so that
    revoked tokens cannot stampede the auth server. Raises :class:`AuthError`
    if the token is inactive — error message MUST NOT contain the token.

    (Formerly ``verify_opaque_token``; renamed in Tier 2 of the 2026-05
    auth cleanup, which retired the ``bsv_sk_*`` opaque token dispatch.)
    """
    token_sha256 = hashlib.sha256(token.encode()).hexdigest()
    response = await cache.get(token_sha256)
    if response is None:
        response = await client.introspect(token)
        await cache.set(token_sha256, response)

    if not response.active:
        logger.info("introspection_token_inactive", token_sha256=token_sha256)
        raise AuthError("token is not active")

    # Tier 5: surface the introspected role into app_metadata so a
    # PAT-authenticated caller drives require_permission's lazy
    # tuple-provisioning and passes require_admin — both read
    # ``app_metadata.role``. Without this, PAT (CLI/MCP) requests carry no
    # role and every OpenFGA-backed route 403s.
    app_metadata = {"role": response.role} if response.role else {}

    return User(
        id=response.sub or "",
        active_tenant_id=response.tenant,
        scope=list(response.scope or []),
        is_service=False,
        email=None,
        app_metadata=app_metadata,
    )


def parse_user_token(payload: dict[str, Any]) -> User:
    """Translate verified user-JWT claims into a `User`.

    The Phase 0 user JWT is intentionally thin (Auth_Design §4.1) — tenants
    list comes from a separate `/api/session` call, not from claims.

    ``app_metadata`` and ``user_metadata`` are lifted off the payload
    (Supabase convention) so consumers can read role / custom claims
    without decoding the token a second time. ``active_tenant_id`` falls
    back to ``app_metadata.tenant_id`` when the top-level claim is absent
    — Supabase JWTs nest tenant under app_metadata.
    """
    sub = payload.get("sub")
    if not isinstance(sub, str) or not sub:
        raise AuthError("user JWT missing sub")
    is_service = sub.startswith("service:")

    app_metadata = payload.get("app_metadata") or {}
    user_metadata = payload.get("user_metadata") or {}
    if not isinstance(app_metadata, dict):
        app_metadata = {}
    if not isinstance(user_metadata, dict):
        user_metadata = {}

    active_tenant_id = payload.get("active_tenant_id")
    if not active_tenant_id:
        nested = app_metadata.get("tenant_id")
        if isinstance(nested, str) and nested:
            active_tenant_id = nested

    return User(
        id=sub,
        email=payload.get("email"),
        active_tenant_id=active_tenant_id,
        tenants=[],
        is_service=is_service,
        app_metadata=app_metadata,
        user_metadata=user_metadata,
    )
