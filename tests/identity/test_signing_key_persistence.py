"""The OAuth signing key must be persisted — and its absence must be loud (T2b-1).

``build_signing_key(pem or None)`` generates a fresh key when no PEM is configured. That is
fine for a test and catastrophic anywhere else, because the key is what tokens are signed AND
verified with:

* **Every restart invalidates every token.** Prod runs with no PEM today, so each of the
  several deploys on 2026-07-13/14 silently broke the founder's MCP connection. Nobody was
  told; the tokens simply stopped verifying.
* **Two processes cannot agree.** The run-scoped task token (T2) is minted on the dispatch
  path — inside the *worker* container — and verified by the MCP API in the *backend*
  container. With a per-process ephemeral key the worker's token can never verify, which is
  exactly the ``401 invalid_token`` measured against prod. The executor redesign is
  structurally impossible until backend and worker share one key.

So an ephemeral key is not a degraded mode to fall back into quietly. Outside dev it is a
misconfiguration, and the process says so.
"""

from __future__ import annotations

import pytest

from backend.identity.oauth_keys import build_signing_key, ensure_signing_key_is_shareable

_PEM = None  # generated per test — see _generated_pem()


def _generated_pem() -> str:
    """An EC P-256 private key PEM, the shape the setting expects."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    key = ec.generate_private_key(ec.SECP256R1())
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()


def test_a_configured_pem_gives_a_stable_key() -> None:
    """Same PEM in two processes → same kid → a token minted by one verifies in the other.
    This is the property the whole executor redesign rests on."""
    pem = _generated_pem()

    first = build_signing_key(pem)
    second = build_signing_key(pem)

    assert first.kid == second.kid


def test_no_pem_gives_a_different_key_every_time() -> None:
    """The failure mode, pinned: two processes (or two boots) disagree. Tokens minted before a
    restart stop verifying after it, and a worker's token never verifies at the backend."""
    assert build_signing_key(None).kid != build_signing_key(None).kid


def test_production_refuses_to_run_on_an_ephemeral_key() -> None:
    """A misconfiguration that silently expires every token on every deploy must not be a
    silent degradation. It fails at startup, where it is cheap to see."""
    with pytest.raises(RuntimeError, match="OAUTH_PRIVATE_KEY_PEM"):
        ensure_signing_key_is_shareable(pem="", environment="prod")


def test_dev_may_run_on_an_ephemeral_key() -> None:
    """A developer starting one process on a laptop signs and verifies with the same key —
    nothing to share, nothing to break."""
    ensure_signing_key_is_shareable(pem="", environment="dev")


def test_production_with_a_pem_is_fine() -> None:
    ensure_signing_key_is_shareable(pem=_generated_pem(), environment="prod")
