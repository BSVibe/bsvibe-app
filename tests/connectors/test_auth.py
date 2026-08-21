"""Slice 0.1 — connector AuthStrategy core (pure backend, no DB/HTTP).

Covers the common skeleton's foundation:

* :class:`TokenSet` — the provider-agnostic result of any token acquisition.
* :class:`OAuthProvider` Protocol + :class:`StubProvider` — one method, three
  knobs (``token_exchange_auth`` / ``refreshable`` / ``supports_service_token``).
  ``github_app`` is NOT a separate method; the "act without a user" capability
  (GitHub App installation token, Sentry JWT) is the optional ``service_token``.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime

import pytest

from backend.connectors.auth.providers import (
    OAuthProvider,
    StubProvider,
    get_provider,
    register_provider,
)
from backend.connectors.auth.tokenset import TokenSet

# ── TokenSet ──────────────────────────────────────────────────────────


def test_tokenset_minimal_construction() -> None:
    ts = TokenSet(access_token="abc")
    assert ts.access_token == "abc"
    assert ts.refresh_token is None
    assert ts.expires_at is None
    assert ts.scopes == ()
    assert ts.account_label is None


def test_tokenset_is_frozen() -> None:
    ts = TokenSet(access_token="abc")
    with pytest.raises(dataclasses.FrozenInstanceError):
        ts.access_token = "mutated"  # type: ignore[misc]


def test_tokenset_full() -> None:
    exp = datetime(2030, 1, 1, tzinfo=UTC)
    ts = TokenSet(
        access_token="a",
        refresh_token="r",
        expires_at=exp,
        scopes=("repo", "read"),
        account_label="@octocat",
    )
    assert ts.refresh_token == "r"
    assert ts.expires_at == exp
    assert ts.scopes == ("repo", "read")
    assert ts.account_label == "@octocat"


# ── StubProvider / OAuthProvider Protocol ─────────────────────────────


def test_stub_provider_satisfies_protocol() -> None:
    stub = StubProvider()
    assert isinstance(stub, OAuthProvider)
    # Three knobs present.
    assert stub.token_exchange_auth in ("body", "basic", "jwt")
    assert isinstance(stub.refreshable, bool)
    assert isinstance(stub.supports_service_token, bool)


def test_stub_authorize_url_carries_state_and_pkce() -> None:
    stub = StubProvider()
    url = stub.authorize_url(
        state="st-123",
        code_challenge="cc-456",
        redirect_uri="https://app.example/cb",
    )
    assert "st-123" in url
    assert "cc-456" in url
    assert "https://app.example/cb" in url


@pytest.mark.asyncio
async def test_stub_exchange_code_returns_tokenset() -> None:
    stub = StubProvider()
    ts = await stub.exchange_code(
        code="auth-code",
        code_verifier="verifier",
        redirect_uri="https://app.example/cb",
    )
    assert isinstance(ts, TokenSet)
    assert ts.access_token
    assert ts.account_label  # stub reports a connected identity


@pytest.mark.asyncio
async def test_stub_refresh_returns_new_tokenset() -> None:
    stub = StubProvider()
    ts = await stub.refresh(refresh_token="r-1")
    assert isinstance(ts, TokenSet)
    assert ts.access_token


@pytest.mark.asyncio
async def test_stub_service_token_unsupported_by_default() -> None:
    stub = StubProvider()
    assert stub.supports_service_token is False
    with pytest.raises(NotImplementedError):
        await stub.service_token(install_ref="inst-1")


@pytest.mark.asyncio
async def test_stub_can_be_configured_with_service_token() -> None:
    stub = StubProvider(supports_service_token=True)
    ts = await stub.service_token(install_ref="inst-1")
    assert isinstance(ts, TokenSet)
    assert ts.access_token


# ── provider registry ─────────────────────────────────────────────────


def test_registry_register_and_get() -> None:
    stub = StubProvider(name="stub-registry-test")
    register_provider(stub)
    assert get_provider("stub-registry-test") is stub


def test_registry_unknown_returns_none() -> None:
    assert get_provider("does-not-exist-xyz") is None
