"""Per-connector OAuth2Provider configs (slack / notion / discord, Lift 2-4).

Thin: the generic exchange/refresh/PKCE behaviour is proven in
``test_oauth2_provider.py``. Here we pin each connector's knobs + endpoints and
smoke one real-shaped token exchange (respx-mocked) so a wrong endpoint or
label path is caught.
"""

from __future__ import annotations

import httpx
import respx

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
