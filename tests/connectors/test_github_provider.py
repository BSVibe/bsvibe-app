"""GitHubAppProvider — bsvibe acting as an OAuth *client* of GitHub (Lift 1).

The first real provider behind the Lift 0 skeleton. Exercises all four
:class:`backend.connectors.auth.providers.OAuthProvider` surfaces against
respx-mocked GitHub HTTP (no real network):

* ``authorize_url`` — the user-to-server authorize redirect (GitHub does NOT
  support PKCE, so the ``code_challenge`` is accepted-and-ignored; ``state``
  still binds CSRF).
* ``exchange_code`` — POST the token endpoint (body auth) + GET ``/user`` for
  the ``@login`` account label.
* ``refresh`` — POST the token endpoint with ``grant_type=refresh_token``.
* ``service_token`` — sign an App JWT (RS256) and POST the installation
  access-token endpoint (the GitHub App "act without a user" capability).
"""

from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest
import respx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from backend.connectors.auth.github import GitHubAppProvider
from backend.connectors.auth.providers import OAuthProvider

_AUTHORIZE = "https://github.com/login/oauth/authorize"
_TOKEN = "https://github.com/login/oauth/access_token"
_USER = "https://api.github.com/user"


@pytest.fixture
def rsa_private_key_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")


@pytest.fixture
def provider(rsa_private_key_pem: str) -> GitHubAppProvider:
    return GitHubAppProvider(
        client_id="Iv1.testclientid",
        client_secret="testsecret",  # noqa: S106 — test fixture
        app_id="123456",
        private_key_pem=rsa_private_key_pem,
    )


def test_provider_satisfies_protocol_with_github_knobs(provider: GitHubAppProvider) -> None:
    assert isinstance(provider, OAuthProvider)
    assert provider.name == "github"
    # Design §3: github's three knobs.
    assert provider.token_exchange_auth == "body"
    assert provider.refreshable is True
    assert provider.supports_service_token is True


def test_authorize_url_carries_client_id_redirect_state(provider: GitHubAppProvider) -> None:
    url = provider.authorize_url(
        state="st-123",
        code_challenge="ignored-no-pkce",
        redirect_uri="https://api.bsvibe.dev/api/v1/connectors/oauth/github/callback",
    )
    parts = urlsplit(url)
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == _AUTHORIZE
    q = parse_qs(parts.query)
    assert q["client_id"] == ["Iv1.testclientid"]
    assert q["state"] == ["st-123"]
    assert q["redirect_uri"] == ["https://api.bsvibe.dev/api/v1/connectors/oauth/github/callback"]


@respx.mock
async def test_exchange_code_returns_tokenset_with_login_label(
    provider: GitHubAppProvider,
) -> None:
    respx.post(_TOKEN).mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "ghu_useraccess",
                "refresh_token": "ghr_refresh",
                "expires_in": 28800,
                "scope": "repo,read:user",
                "token_type": "bearer",
            },
        )
    )
    respx.get(_USER).mock(return_value=httpx.Response(200, json={"login": "octocat"}))

    token = await provider.exchange_code(
        code="abc",
        code_verifier="unused",
        redirect_uri="https://api.bsvibe.dev/cb",
    )
    assert token.access_token == "ghu_useraccess"
    assert token.refresh_token == "ghr_refresh"
    assert token.account_label == "@octocat"
    assert token.scopes == ("repo", "read:user")
    assert token.expires_at is not None
    # ~8h out, generous slack for clock + parse.
    delta = token.expires_at - datetime.now(tz=UTC)
    assert 28000 < delta.total_seconds() < 29000


@respx.mock
async def test_exchange_code_sends_credentials_in_body(provider: GitHubAppProvider) -> None:
    route = respx.post(_TOKEN).mock(
        return_value=httpx.Response(200, json={"access_token": "ghu_x", "token_type": "bearer"})
    )
    respx.get(_USER).mock(return_value=httpx.Response(200, json={"login": "u"}))

    await provider.exchange_code(code="thecode", code_verifier="v", redirect_uri="https://cb")

    sent = route.calls.last.request
    body = parse_qs(sent.content.decode())
    assert body["client_id"] == ["Iv1.testclientid"]
    assert body["client_secret"] == ["testsecret"]
    assert body["code"] == ["thecode"]
    # body auth → secret NOT in an Authorization header.
    assert "authorization" not in {k.lower() for k in sent.headers}
    # JSON response negotiated.
    assert sent.headers["accept"] == "application/json"


@respx.mock
async def test_exchange_code_raises_on_oauth_error(provider: GitHubAppProvider) -> None:
    respx.post(_TOKEN).mock(
        return_value=httpx.Response(
            200,
            json={
                "error": "bad_verification_code",
                "error_description": "The code passed is incorrect or expired.",
            },
        )
    )
    with pytest.raises(ValueError, match="bad_verification_code"):
        await provider.exchange_code(code="bad", code_verifier="v", redirect_uri="https://cb")


@respx.mock
async def test_refresh_posts_refresh_grant(provider: GitHubAppProvider) -> None:
    route = respx.post(_TOKEN).mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "ghu_new",
                "refresh_token": "ghr_new",
                "expires_in": 28800,
                "token_type": "bearer",
            },
        )
    )
    token = await provider.refresh(refresh_token="ghr_old")
    assert token.access_token == "ghu_new"
    assert token.refresh_token == "ghr_new"
    body = parse_qs(route.calls.last.request.content.decode())
    assert body["grant_type"] == ["refresh_token"]
    assert body["refresh_token"] == ["ghr_old"]


@respx.mock
async def test_service_token_signs_app_jwt_and_calls_installation(
    provider: GitHubAppProvider, rsa_private_key_pem: str
) -> None:
    install_url = "https://api.github.com/app/installations/9876/access_tokens"
    route = respx.post(install_url).mock(
        return_value=httpx.Response(
            201,
            json={
                "token": "ghs_installationtoken",
                "expires_at": "2099-01-01T00:00:00Z",
            },
        )
    )
    token = await provider.service_token(install_ref="9876")
    assert token.access_token == "ghs_installationtoken"
    assert token.refresh_token is None
    assert token.expires_at is not None

    # The installation call authenticates with a freshly-signed App JWT
    # (RS256, iss=app_id) — verify it round-trips against the public key.
    auth = route.calls.last.request.headers["authorization"]
    assert auth.startswith("Bearer ")
    app_jwt = auth.removeprefix("Bearer ")
    public_key = serialization.load_pem_private_key(
        rsa_private_key_pem.encode(), password=None
    ).public_key()
    claims = jwt.decode(app_jwt, public_key, algorithms=["RS256"])
    assert claims["iss"] == "123456"


def test_service_token_requires_private_key() -> None:
    # No app credentials → service-token capability cannot be used.
    bare = GitHubAppProvider(client_id="x", client_secret="y")  # noqa: S106
    assert bare.supports_service_token is False
