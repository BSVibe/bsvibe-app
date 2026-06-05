"""OAuth2Provider — generic authorization_code provider (Lift 2 foundation).

slack / discord / notion all share the vanilla ``authorization_code`` grant;
the design's three knobs (token_exchange_auth body|basic, refreshable, PKCE)
plus where the account label comes from (the token response vs a userinfo call)
are the only variation. One data-driven class covers them, mirroring
GitHubAppProvider's behaviour but without the App-JWT / installation specifics.
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import respx

from backend.connectors.auth.oauth2 import OAuth2Provider
from backend.connectors.auth.providers import OAuthProvider

_AUTHZ = "https://example.test/authorize"
_TOKEN = "https://example.test/token"  # noqa: S105 — endpoint URL
_USERINFO = "https://example.test/me"


def _body_provider() -> OAuth2Provider:
    # slack-shaped: body auth, label from the token response, no PKCE.
    return OAuth2Provider(
        name="slack",
        authorize_endpoint=_AUTHZ,
        token_endpoint=_TOKEN,
        token_exchange_auth="body",
        refreshable=False,
        supports_pkce=False,
        scopes=("chat:write",),
        client_id="cid",
        client_secret="csecret",
        label_path=("team", "name"),
    )


def _basic_pkce_provider() -> OAuth2Provider:
    # discord-shaped: basic auth, PKCE, label from a userinfo call.
    return OAuth2Provider(
        name="discord",
        authorize_endpoint=_AUTHZ,
        token_endpoint=_TOKEN,
        token_exchange_auth="basic",
        refreshable=True,
        supports_pkce=True,
        scopes=("identify",),
        client_id="cid",
        client_secret="csecret",
        userinfo_endpoint=_USERINFO,
        label_path=("username",),
    )


def test_satisfies_protocol() -> None:
    assert isinstance(_body_provider(), OAuthProvider)


def test_authorize_url_basic_fields() -> None:
    url = _body_provider().authorize_url(
        state="st", code_challenge="ch", redirect_uri="https://cb"
    )
    q = parse_qs(urlsplit(url).query)
    assert q["client_id"] == ["cid"]
    assert q["state"] == ["st"]
    assert q["response_type"] == ["code"]
    assert q["redirect_uri"] == ["https://cb"]
    assert q["scope"] == ["chat:write"]
    assert "code_challenge" not in q  # PKCE off


def test_authorize_url_includes_pkce_when_supported() -> None:
    url = _basic_pkce_provider().authorize_url(
        state="st", code_challenge="ch", redirect_uri="https://cb"
    )
    q = parse_qs(urlsplit(url).query)
    assert q["code_challenge"] == ["ch"]
    assert q["code_challenge_method"] == ["S256"]


@respx.mock
async def test_exchange_code_body_auth_label_from_token() -> None:
    route = respx.post(_TOKEN).mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "xoxb-tok",
                "scope": "chat:write",
                "team": {"id": "T1", "name": "Acme"},
            },
        )
    )
    token = await _body_provider().exchange_code(
        code="c", code_verifier="v", redirect_uri="https://cb"
    )
    assert token.access_token == "xoxb-tok"
    assert token.account_label == "Acme"
    assert token.refresh_token is None  # not refreshable

    body = parse_qs(route.calls.last.request.content.decode())
    assert body["client_id"] == ["cid"]
    assert body["client_secret"] == ["csecret"]
    assert body["code"] == ["c"]
    assert "authorization" not in {k.lower() for k in route.calls.last.request.headers}


@respx.mock
async def test_exchange_code_basic_auth_label_from_userinfo() -> None:
    token_route = respx.post(_TOKEN).mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "disc-tok",
                "refresh_token": "disc-ref",
                "expires_in": 604800,
            },
        )
    )
    respx.get(_USERINFO).mock(return_value=httpx.Response(200, json={"username": "neo"}))

    token = await _basic_pkce_provider().exchange_code(
        code="c", code_verifier="ver", redirect_uri="https://cb"
    )
    assert token.access_token == "disc-tok"
    assert token.refresh_token == "disc-ref"
    assert token.account_label == "neo"
    assert token.expires_at is not None
    assert (token.expires_at - datetime.now(tz=UTC)).total_seconds() > 600000

    sent = token_route.calls.last.request
    # Basic auth header carries the client creds; not in the body.
    auth = sent.headers["authorization"]
    assert auth.startswith("Basic ")
    decoded = base64.b64decode(auth.removeprefix("Basic ")).decode()
    assert decoded == "cid:csecret"
    body = parse_qs(sent.content.decode())
    assert "client_secret" not in body
    assert body["code_verifier"] == ["ver"]  # PKCE


@respx.mock
async def test_exchange_code_raises_on_error_payload() -> None:
    respx.post(_TOKEN).mock(
        return_value=httpx.Response(200, json={"ok": False, "error": "invalid_code"})
    )
    with pytest.raises(ValueError, match="invalid_code"):
        await _body_provider().exchange_code(
            code="bad", code_verifier="v", redirect_uri="https://cb"
        )


@respx.mock
async def test_refresh_posts_refresh_grant() -> None:
    route = respx.post(_TOKEN).mock(
        return_value=httpx.Response(
            200, json={"access_token": "new", "refresh_token": "newref", "expires_in": 100}
        )
    )
    token = await _basic_pkce_provider().refresh(refresh_token="old")
    assert token.access_token == "new"
    body = parse_qs(route.calls.last.request.content.decode())
    assert body["grant_type"] == ["refresh_token"]
    assert body["refresh_token"] == ["old"]


async def test_service_token_unsupported() -> None:
    with pytest.raises(NotImplementedError):
        await _body_provider().service_token(install_ref="x")
