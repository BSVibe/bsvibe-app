"""Tests for backend.identity.oauth_jwt / oauth_keys — Lift D1."""

from __future__ import annotations

import time
import uuid

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from jwt.exceptions import InvalidTokenError

from backend.identity.oauth_jwt import (
    ACCESS_TOKEN_AUDIENCE,
    issue_access_token,
    verify_access_token,
)
from backend.identity.oauth_keys import build_signing_key, jwks_payload


@pytest.fixture
def stable_key() -> tuple[str, object]:
    """A deterministic PEM-encoded ES256 key for tests."""
    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")
    return pem, key


def test_build_signing_key_from_pem_stable_kid(stable_key: tuple[str, object]) -> None:
    pem, _ = stable_key
    sk1 = build_signing_key(pem)
    sk2 = build_signing_key(pem)
    assert sk1.kid == sk2.kid
    assert sk1.public_jwk["kid"] == sk1.kid
    assert sk1.public_jwk["alg"] == "ES256"
    assert sk1.public_jwk["kty"] == "EC"
    assert sk1.public_jwk["crv"] == "P-256"
    assert "x" in sk1.public_jwk and "y" in sk1.public_jwk


def test_build_signing_key_generates_when_pem_empty() -> None:
    sk = build_signing_key(None)
    assert sk.kid
    assert sk.public_jwk["alg"] == "ES256"


def test_issue_then_verify_roundtrip(stable_key: tuple[str, object]) -> None:
    pem, _ = stable_key
    sk = build_signing_key(pem)
    user_id = uuid.uuid4()
    workspace_id = uuid.uuid4()
    jti = uuid.uuid4()
    now = int(time.time())
    token = issue_access_token(
        user_id=user_id,
        workspace_id=workspace_id,
        client_id="dcr-test",
        scope=["mcp:read", "mcp:write"],
        jti=jti,
        issued_at=now,
        expires_at=now + 3600,
        issuer="http://test/issuer",
        signing_key=sk,
    )
    jwks = {"keys": [sk.public_jwk]}
    claims = verify_access_token(token, issuer="http://test/issuer", jwks=jwks)
    assert claims["sub"] == str(user_id)
    assert claims["wsp"] == str(workspace_id)
    assert claims["aud"] == ACCESS_TOKEN_AUDIENCE
    assert claims["scope"] == "mcp:read mcp:write"
    assert claims["client_id"] == "dcr-test"
    assert claims["jti"] == str(jti)


def test_verify_rejects_tampered_payload(stable_key: tuple[str, object]) -> None:
    pem, _ = stable_key
    sk = build_signing_key(pem)
    now = int(time.time())
    token = issue_access_token(
        user_id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        client_id="dcr-test",
        scope=["mcp:read"],
        jti=uuid.uuid4(),
        issued_at=now,
        expires_at=now + 3600,
        issuer="http://test/issuer",
        signing_key=sk,
    )
    # flip a char in the payload segment
    header, payload, sig = token.split(".")
    bad = f"{header}.{payload[:-1]}A.{sig}"
    jwks = {"keys": [sk.public_jwk]}
    with pytest.raises(InvalidTokenError):
        verify_access_token(bad, issuer="http://test/issuer", jwks=jwks)


def test_verify_rejects_wrong_kid(stable_key: tuple[str, object]) -> None:
    pem, _ = stable_key
    sk_a = build_signing_key(pem)
    sk_b = build_signing_key(None)  # different key
    now = int(time.time())
    token = issue_access_token(
        user_id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        client_id="dcr-test",
        scope=["mcp:read"],
        jti=uuid.uuid4(),
        issued_at=now,
        expires_at=now + 3600,
        issuer="http://test/issuer",
        signing_key=sk_a,
    )
    # JWKS only knows about sk_b
    jwks = {"keys": [sk_b.public_jwk]}
    with pytest.raises(InvalidTokenError):
        verify_access_token(token, issuer="http://test/issuer", jwks=jwks)


def test_verify_rejects_expired_token(stable_key: tuple[str, object]) -> None:
    pem, _ = stable_key
    sk = build_signing_key(pem)
    now = int(time.time())
    token = issue_access_token(
        user_id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        client_id="dcr-test",
        scope=["mcp:read"],
        jti=uuid.uuid4(),
        issued_at=now - 7200,
        expires_at=now - 3600,
        issuer="http://test/issuer",
        signing_key=sk,
    )
    jwks = {"keys": [sk.public_jwk]}
    with pytest.raises(InvalidTokenError):
        verify_access_token(token, issuer="http://test/issuer", jwks=jwks)


def test_jwks_payload_round_trip() -> None:
    payload = jwks_payload()
    assert "keys" in payload
    assert len(payload["keys"]) == 1
    k = payload["keys"][0]
    assert k["kty"] == "EC"
    assert k["crv"] == "P-256"
    assert k["use"] == "sig"
    assert k["alg"] == "ES256"
    assert k["kid"]
