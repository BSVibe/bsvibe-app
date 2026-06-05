"""Per-connector OAuth2Provider configs (slack / notion / discord, Lift 2-4).

Thin: the generic exchange/refresh/PKCE behaviour is proven in
``test_oauth2_provider.py``. Here we pin each connector's knobs + endpoints and
smoke one real-shaped token exchange (respx-mocked) so a wrong endpoint or
label path is caught.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import httpx
import respx

from backend.connectors.auth.discord import build_discord_provider
from backend.connectors.auth.notion import build_notion_provider
from backend.connectors.auth.providers import OAuthProvider
from backend.connectors.auth.slack import build_slack_provider

# ── Slack ──────────────────────────────────────────────────────────────


def test_slack_knobs() -> None:
    p = build_slack_provider(client_id="cid", client_secret="sec")
    assert isinstance(p, OAuthProvider)
    assert p.name == "slack"
    assert p.token_exchange_auth == "body"
    assert p.refreshable is False
    assert p.supports_pkce is False
    assert p.supports_service_token is False
    assert p.authorize_endpoint == "https://slack.com/oauth/v2/authorize"


@respx.mock
async def test_slack_exchange_label_is_team_name() -> None:
    respx.post("https://slack.com/api/oauth.v2.access").mock(
        return_value=httpx.Response(
            200,
            json={
                "ok": True,
                "access_token": "xoxb-tok",
                "scope": "chat:write",
                "team": {"id": "T1", "name": "Acme Inc"},
            },
        )
    )
    tok = await build_slack_provider(client_id="c", client_secret="s").exchange_code(
        code="c", code_verifier="v", redirect_uri="https://cb"
    )
    assert tok.access_token == "xoxb-tok"
    assert tok.account_label == "Acme Inc"


# ── Notion ─────────────────────────────────────────────────────────────


def test_notion_knobs_and_owner_param() -> None:
    p = build_notion_provider(client_id="cid", client_secret="sec")
    assert p.name == "notion"
    assert p.token_exchange_auth == "basic"
    assert p.refreshable is False
    url = p.authorize_url(state="s", code_challenge="c", redirect_uri="https://cb")
    assert parse_qs(urlsplit(url).query)["owner"] == ["user"]


@respx.mock
async def test_notion_exchange_label_is_workspace_name() -> None:
    respx.post("https://api.notion.com/v1/oauth/token").mock(
        return_value=httpx.Response(200, json={"access_token": "ntn", "workspace_name": "Docs HQ"})
    )
    tok = await build_notion_provider(client_id="c", client_secret="s").exchange_code(
        code="c", code_verifier="v", redirect_uri="https://cb"
    )
    assert tok.access_token == "ntn"
    assert tok.account_label == "Docs HQ"


# ── Discord ────────────────────────────────────────────────────────────


def test_discord_knobs() -> None:
    p = build_discord_provider(client_id="cid", client_secret="sec")
    assert p.name == "discord"
    assert p.token_exchange_auth == "basic"
    assert p.refreshable is True
    assert p.userinfo_endpoint == "https://discord.com/api/users/@me"


@respx.mock
async def test_discord_exchange_label_from_userinfo() -> None:
    respx.post("https://discord.com/api/oauth2/token").mock(
        return_value=httpx.Response(
            200, json={"access_token": "dtok", "refresh_token": "dref", "expires_in": 604800}
        )
    )
    respx.get("https://discord.com/api/users/@me").mock(
        return_value=httpx.Response(200, json={"username": "trinity"})
    )
    tok = await build_discord_provider(client_id="c", client_secret="s").exchange_code(
        code="c", code_verifier="v", redirect_uri="https://cb"
    )
    assert tok.access_token == "dtok"
    assert tok.refresh_token == "dref"
    assert tok.account_label == "trinity"
