"""ES256 JWT issuance + verification for embedded OAuth (Lift D1).

The access token format is a JWS-compact ES256 JWT. Resource servers
(D2's MCP) verify with the JWKS public key — no round-trip per request.

Claim shape::

    {
      "iss": "<oauth_issuer>",
      "sub": "<user_id>",          # UUID
      "wsp": "<workspace_id>",     # UUID — workspace the token operates within
      "aud": "bsvibe-app",
      "scope": "mcp:read mcp:write",
      "client_id": "<dcr-...>",
      "jti": "<access_token row id>",
      "iat": <unix-seconds>,
      "exp": <unix-seconds>
    }

We compose the JWT with PyJWT (already in deps as ``pyjwt[crypto]``).
``verify_access_token`` is included for D2's reference — backend MCP
will live in a separate process and reuse this module.
"""

from __future__ import annotations

import uuid
from typing import Any

import jwt
from jwt.algorithms import ECAlgorithm
from jwt.exceptions import InvalidTokenError

from backend.identity.oauth_keys import SigningKey, get_signing_key, jwks_payload

ACCESS_TOKEN_AUDIENCE = "bsvibe-app"  # noqa: S105 — OAuth audience, not a secret


def issue_access_token(
    *,
    user_id: uuid.UUID,
    workspace_id: uuid.UUID,
    client_id: str,
    scope: list[str],
    jti: uuid.UUID,
    issued_at: int,
    expires_at: int,
    issuer: str,
    signing_key: SigningKey | None = None,
) -> str:
    """Return a signed ES256 JWT access token.

    ``signing_key`` defaults to the process singleton; tests pass an
    explicit key to assert on deterministic kids.
    """
    key = signing_key or get_signing_key()
    payload: dict[str, Any] = {
        "iss": issuer,
        "sub": str(user_id),
        "wsp": str(workspace_id),
        "aud": ACCESS_TOKEN_AUDIENCE,
        "scope": " ".join(scope),
        "client_id": client_id,
        "jti": str(jti),
        "iat": issued_at,
        "exp": expires_at,
    }
    return jwt.encode(
        payload,
        key.private_key,
        algorithm="ES256",
        headers={"kid": key.kid, "typ": "JWT"},
    )


def verify_access_token(
    token: str,
    *,
    issuer: str,
    jwks: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Verify an ES256 access token and return its claims.

    Resource-server-side helper. Looks up the kid in ``jwks`` (defaults
    to this process's own JWKS — useful for in-process tests and the
    /introspect endpoint). Raises :class:`InvalidTokenError` on any
    verification failure.
    """
    headers = jwt.get_unverified_header(token)
    kid = headers.get("kid")
    if not isinstance(kid, str) or not kid:
        raise InvalidTokenError("missing kid header")
    keyset = jwks if jwks is not None else jwks_payload()
    match: dict[str, Any] | None = None
    for k in keyset.get("keys", []):
        if k.get("kid") == kid:
            match = k
            break
    if match is None:
        raise InvalidTokenError(f"unknown kid: {kid}")
    public_key = ECAlgorithm.from_jwk(match)
    return jwt.decode(
        token,
        public_key,
        algorithms=["ES256"],
        audience=ACCESS_TOKEN_AUDIENCE,
        issuer=issuer,
        options={"require": ["iss", "sub", "aud", "exp", "iat", "jti"]},
    )


__all__ = [
    "ACCESS_TOKEN_AUDIENCE",
    "issue_access_token",
    "verify_access_token",
]
