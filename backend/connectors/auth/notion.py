"""Notion OAuth provider config (Lift 3).

Notion uses authorization_code with HTTP **Basic** client auth at the token
endpoint and requires ``owner=user`` on the authorize URL. The access token is
long-lived (non-expiring), so ``refreshable=False``. The connected workspace
name (``workspace_name`` in the token response) is the account label.
"""

from __future__ import annotations

from backend.connectors.auth.oauth2 import OAuth2Provider


def build_notion_provider(*, client_id: str, client_secret: str) -> OAuth2Provider:
    return OAuth2Provider(
        name="notion",
        authorize_endpoint="https://api.notion.com/v1/oauth/authorize",
        token_endpoint="https://api.notion.com/v1/oauth/token",  # noqa: S106 — endpoint URL
        token_exchange_auth="basic",  # noqa: S106 — auth-style label, not a secret
        refreshable=False,
        supports_pkce=False,
        scopes=(),  # Notion scopes are configured on the integration, not requested
        client_id=client_id,
        client_secret=client_secret,
        label_path=("workspace_name",),
        extra_authorize_params={"owner": "user"},
    )


__all__ = ["build_notion_provider"]
