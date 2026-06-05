"""Slack OAuth v2 provider config (Lift 2).

Slack uses the vanilla authorization_code grant with body client auth and no
PKCE; the bot token it returns does not expire by default (rotation is opt-in,
so ``refreshable=False``). The connected workspace name (``team.name`` in the
``oauth.v2.access`` response) is the account label. Just a configured
:class:`backend.connectors.auth.oauth2.OAuth2Provider`.
"""

from __future__ import annotations

from backend.connectors.auth.oauth2 import OAuth2Provider

# Minimal delivery scope: post messages. Founders can broaden on the Slack app.
_SCOPES = ("chat:write",)


def build_slack_provider(*, client_id: str, client_secret: str) -> OAuth2Provider:
    return OAuth2Provider(
        name="slack",
        authorize_endpoint="https://slack.com/oauth/v2/authorize",
        token_endpoint="https://slack.com/api/oauth.v2.access",  # noqa: S106 — endpoint URL
        token_exchange_auth="body",  # noqa: S106 — auth-style label, not a secret
        refreshable=False,
        supports_pkce=False,
        scopes=_SCOPES,
        client_id=client_id,
        client_secret=client_secret,
        label_path=("team", "name"),
    )


__all__ = ["build_slack_provider"]
