"""Discord OAuth2 provider config (Lift 4).

Discord uses authorization_code with HTTP Basic client auth; tokens expire
(``expires_in``) and come with a refresh token, so ``refreshable=True``. The
account label is the username from ``GET /users/@me`` (the token response
itself carries no human name), so it uses a userinfo call.
"""

from __future__ import annotations

from backend.connectors.auth.oauth2 import OAuth2Provider


def build_discord_provider(*, client_id: str, client_secret: str) -> OAuth2Provider:
    return OAuth2Provider(
        name="discord",
        authorize_endpoint="https://discord.com/oauth2/authorize",
        token_endpoint="https://discord.com/api/oauth2/token",  # noqa: S106 — endpoint URL
        token_exchange_auth="basic",  # noqa: S106 — auth-style label, not a secret
        refreshable=True,
        supports_pkce=False,
        scopes=("identify",),
        client_id=client_id,
        client_secret=client_secret,
        userinfo_endpoint="https://discord.com/api/users/@me",
        label_path=("username",),
    )


__all__ = ["build_discord_provider"]
