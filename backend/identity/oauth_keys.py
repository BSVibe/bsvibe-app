"""ES256 keypair management + JWKS serving for the embedded OAuth server.

We mint **self-contained ES256 JWT access tokens** so resource servers
(D2's MCP) can verify with a public-key fetch from ``/.well-known/jwks.json``
— no per-request introspection round-trip.

Key material lifecycle:

* In production the private key is loaded from
  ``BSVIBE_OAUTH_PRIVATE_KEY_PEM`` (PEM-encoded ECDSA P-256 private key).
  The matching public key is derived at startup.
* When the env var is empty (local dev) a process-local ephemeral pair is
  generated. The kid is stable for the process lifetime so locally-issued
  tokens validate within that process. Tokens issued by a dev process
  are NOT portable across restarts — by design; never use dev keys
  outside a single dev session.

JWKS shape:

::

    {
      "keys": [
        {
          "kty": "EC",
          "crv": "P-256",
          "use": "sig",
          "alg": "ES256",
          "kid": "<sha256-of-public-bytes-prefix>",
          "x": "<base64url>",
          "y": "<base64url>"
        }
      ]
    }

The ``kid`` is the first 16 hex chars of ``sha256(public_jwk_canonical)``
— stable across processes that share the same private key, distinct
across rotations.
"""

from __future__ import annotations

import base64
import hashlib
import json
import threading
from dataclasses import dataclass
from typing import Any

import structlog
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _int_to_b64url(value: int, byte_length: int) -> str:
    return _b64url(value.to_bytes(byte_length, "big"))


def _compute_kid(public_jwk: dict[str, str]) -> str:
    """Stable kid = first 16 hex chars of sha256(canonical-jwk-json)."""
    canonical = json.dumps(
        {k: public_jwk[k] for k in ("crv", "kty", "x", "y")},
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()[:16]


@dataclass(frozen=True)
class SigningKey:
    """The active OAuth signing key + its public JWKS counterpart."""

    kid: str
    private_key: ec.EllipticCurvePrivateKey
    public_jwk: dict[str, str]


def _public_jwk_from_private(
    private_key: ec.EllipticCurvePrivateKey,
) -> dict[str, str]:
    pub_numbers = private_key.public_key().public_numbers()
    x = _int_to_b64url(pub_numbers.x, 32)
    y = _int_to_b64url(pub_numbers.y, 32)
    return {
        "kty": "EC",
        "crv": "P-256",
        "x": x,
        "y": y,
    }


def _load_private_key_pem(pem: str) -> ec.EllipticCurvePrivateKey:
    """Load a PEM-encoded EC P-256 private key.

    Accepts both ``-----BEGIN EC PRIVATE KEY-----`` and PKCS#8 forms.
    """
    key = serialization.load_pem_private_key(
        pem.encode("utf-8"),
        password=None,
    )
    if not isinstance(key, ec.EllipticCurvePrivateKey):
        raise ValueError("OAUTH_PRIVATE_KEY_PEM must be an ECDSA key")
    if key.curve.name != "secp256r1":
        raise ValueError(f"OAUTH_PRIVATE_KEY_PEM curve must be P-256 (got {key.curve.name})")
    return key


def build_signing_key(pem: str | None) -> SigningKey:
    """Build a :class:`SigningKey` from the configured PEM, or generate one.

    Pure function (no side effects); the result is cached at the
    :func:`get_signing_key` boundary.
    """
    if pem:
        private_key = _load_private_key_pem(pem)
    else:
        private_key = ec.generate_private_key(ec.SECP256R1())
    public_jwk = _public_jwk_from_private(private_key)
    kid = _compute_kid(public_jwk)
    public_jwk_full: dict[str, str] = {
        **public_jwk,
        "use": "sig",
        "alg": "ES256",
        "kid": kid,
    }
    return SigningKey(kid=kid, private_key=private_key, public_jwk=public_jwk_full)


# Process-wide singleton — built once on first use.
_lock = threading.Lock()
_signing_key: SigningKey | None = None


logger = structlog.get_logger(__name__)


def ensure_signing_key_is_shareable(*, pem: str, environment: str) -> None:
    """Refuse to run outside dev on an EPHEMERAL signing key.

    ``build_signing_key(None)`` mints a fresh key per process. That is fine on a laptop, where
    one process both signs and verifies. Anywhere else it is a misconfiguration with two
    silent, expensive consequences (both measured against prod, 2026-07-14):

    * **Every restart invalidates every token.** Prod had no PEM, so each deploy quietly broke
      the founder's MCP connection — no error, tokens simply stopped verifying.
    * **Two processes cannot agree.** A run-scoped task token is minted on the dispatch path
      (the *worker* container) and verified by the MCP API (the *backend* container). With a
      per-process key the worker's token can never verify — the ``401 invalid_token`` that
      blocks the executor redesign.

    A degradation nobody can see is not a degradation; it is a bug waiting to be diagnosed
    twice. Fail here, at startup, where it is cheap.
    """
    if pem.strip():
        return
    if environment.lower() in {"dev", "test", "local"}:
        logger.warning(
            "oauth_signing_key_ephemeral",
            note=(
                "no OAUTH_PRIVATE_KEY_PEM — every restart invalidates every token. Fine for a "
                "single local process; never for a deployment."
            ),
        )
        return
    raise RuntimeError(
        "BSVIBE_OAUTH_PRIVATE_KEY_PEM is not set. Without it every process mints its own "
        "signing key: tokens die on each restart, and a token minted by the worker can never "
        "verify at the backend. Generate one "
        "(openssl ecparam -genkey -name prime256v1 -noout | openssl pkcs8 -topk8 -nocrypt) "
        "and give the SAME value to every container."
    )


def get_signing_key() -> SigningKey:
    """Return the process-wide signing key, building it on first use."""
    global _signing_key  # noqa: PLW0603 — process-wide singleton intentional
    if _signing_key is not None:
        return _signing_key
    with _lock:
        if _signing_key is not None:
            return _signing_key
        from backend.config import get_settings  # noqa: PLC0415

        settings = get_settings()
        _signing_key = build_signing_key(settings.oauth_private_key_pem or None)
        return _signing_key


def reset_signing_key_for_tests() -> None:
    """Drop the cached signing key — tests use this to inject deterministic
    keys via :func:`build_signing_key` and assert on stable kids."""
    global _signing_key  # noqa: PLW0603
    _signing_key = None


def jwks_payload() -> dict[str, list[dict[str, Any]]]:
    """RFC 7517 JWKS document for ``/.well-known/jwks.json``."""
    key = get_signing_key()
    return {"keys": [dict(key.public_jwk)]}


__all__ = [
    "SigningKey",
    "build_signing_key",
    "get_signing_key",
    "jwks_payload",
    "reset_signing_key_for_tests",
]
