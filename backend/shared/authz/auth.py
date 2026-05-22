"""JWT verification — Supabase / BSVibe-Auth user session JWTs.

Authentication only: verify the caller's session JWT (signature, exp, aud,
iss) against the configured key source and translate the claims into a
:class:`User`. Key resolution order: JWKS URL → static public key →
symmetric secret (HS256 dev). Authorization is a separate layer
(:func:`backend.api.deps.require_role` over ``Membership.role``).
"""

from __future__ import annotations

from typing import Any

import jwt
import structlog

from .settings import Settings
from .types import User

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
    try:
        payload = jwt.decode(
            token,
            _resolve_user_signing_key(token, settings),
            algorithms=[settings.user_jwt_algorithm],
            audience=settings.user_jwt_audience,
            issuer=settings.user_jwt_issuer,
            options={"require": ["exp", "iat", "sub"]},
        )
    except jwt.PyJWTError as exc:
        logger.warning("user_jwt_invalid", error=str(exc))
        raise AuthError(f"user JWT verification failed: {exc}") from exc
    return payload


def parse_user_token(payload: dict[str, Any]) -> User:
    """Translate verified user-JWT claims into a :class:`User`.

    The tenant a request operates within is resolved separately from the
    caller's ``Membership`` (Workflow §3), not from token claims.
    """
    sub = payload.get("sub")
    if not isinstance(sub, str) or not sub:
        raise AuthError("user JWT missing sub")
    return User(
        id=sub,
        email=payload.get("email"),
        is_service=sub.startswith("service:"),
    )
