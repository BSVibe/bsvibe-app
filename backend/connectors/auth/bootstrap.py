"""Register the credential-gated OAuth providers (Lift 1 env + Lift 1.5 DB).

The Lift 0 skeleton seeds only the StubProvider at import time (so the
storage / endpoint / resolution layers are exercisable without secrets). Real
providers are registered ONLY when their App credentials exist — a deployment
with none keeps a github connector working via the legacy signing-secret path
(resolve_connector_credentials falls back), it just can't offer "Connect with
GitHub".

Two credential sources, loaded in this order so the later one WINS:

1. :func:`register_configured_providers` — env settings, sync, called from
   :func:`backend.api.main.create_app` (alongside ``discover_webhook_parsers``).
2. :func:`load_app_credential_providers` — the DB ``connector_oauth_app_credentials``
   table (populated by the GitHub App Manifest flow), async, called at lifespan
   startup AND right after a manifest callback. DB takes precedence over env
   because it is the App the founder just set up through the UI.

Idempotent: re-registering overwrites the same provider slot.
"""

from __future__ import annotations

from collections.abc import Callable

from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import Settings, get_settings
from backend.connectors.auth.app_credentials import AppCredentials, get_app_credentials
from backend.connectors.auth.discord import build_discord_provider
from backend.connectors.auth.github import GitHubAppProvider
from backend.connectors.auth.notion import build_notion_provider
from backend.connectors.auth.providers import OAuthProvider, register_provider
from backend.connectors.auth.sentry import SentryProvider
from backend.connectors.auth.slack import build_slack_provider
from backend.router.accounts.crypto import CredentialCipher

# Vanilla OAuth2 providers configurable via DB app-credentials (operator pastes
# client_id/secret — they have no GitHub-App-style manifest). Builders take
# client_id/client_secret only (app_id/private-key are github-specific).
VANILLA_DB_PROVIDERS: dict[str, Callable[..., OAuthProvider]] = {
    "slack": build_slack_provider,
    "notion": build_notion_provider,
    "discord": build_discord_provider,
}

# Providers loadable from the DB app-credentials table at startup: github (via
# the manifest flow), the vanilla providers (paste-creds), and sentry
# (install→grant; paste-creds + integration slug).
_DB_PROVIDERS = ("github", *VANILLA_DB_PROVIDERS, "sentry")

# Vanilla OAuth2 connectors registered from env: (name, builder, id_attr,
# secret_attr). Each builder takes client_id/client_secret and returns a
# configured provider. github (App args) + sentry (install→grant) are handled
# separately below.
_ENV_PROVIDERS: tuple[tuple[str, Callable[..., OAuthProvider], str, str], ...] = (
    ("slack", build_slack_provider, "slack_client_id", "slack_client_secret"),
    ("notion", build_notion_provider, "notion_client_id", "notion_client_secret"),
    ("discord", build_discord_provider, "discord_client_id", "discord_client_secret"),
)


def register_configured_providers(settings: Settings | None = None) -> list[str]:
    """Register every provider whose ENV credentials are present; return names.

    github is intentionally NOT here — its App credentials are the single
    source of truth in the DB (``connector_oauth_app_credentials``, set up via
    the manifest flow) and are loaded by :func:`load_app_credential_providers`
    at startup. slack / notion / discord remain env-registered until their own
    DB-credential setup lift.
    """
    settings = settings or get_settings()
    registered: list[str] = []

    for name, build, id_attr, secret_attr in _ENV_PROVIDERS:
        client_id = getattr(settings, id_attr)
        client_secret = getattr(settings, secret_attr)
        if client_id and client_secret:
            register_provider(build(client_id=client_id, client_secret=client_secret))
            registered.append(name)

    return registered


def register_provider_from_credentials(provider: str, creds: AppCredentials) -> None:
    """Build + register the provider for ``provider`` from its DB credentials.

    github → GitHubAppProvider (app_id + private key); slack / notion / discord
    → their vanilla builder (client_id/secret only).
    """
    if provider == "github":
        register_provider(
            GitHubAppProvider(
                client_id=creds.client_id,
                client_secret=creds.client_secret,
                app_id=creds.app_id,
                private_key_pem=creds.private_key_pem,
            )
        )
    elif provider in VANILLA_DB_PROVIDERS:
        register_provider(
            VANILLA_DB_PROVIDERS[provider](
                client_id=creds.client_id, client_secret=creds.client_secret
            )
        )
    elif provider == "sentry":
        register_provider(
            SentryProvider(client_id=creds.client_id, client_secret=creds.client_secret)
        )


async def load_app_credential_providers(
    session: AsyncSession, cipher: CredentialCipher
) -> list[str]:
    """Register providers from the DB app-credentials table; return their names.

    Overrides any env-registered provider of the same name (DB = the App the
    operator set up via the manifest flow / paste-creds form).
    """
    registered: list[str] = []
    for provider in _DB_PROVIDERS:
        creds = await get_app_credentials(session, provider=provider, cipher=cipher)
        if creds is None:
            continue
        register_provider_from_credentials(provider, creds)
        registered.append(provider)
    return registered


__all__ = [
    "VANILLA_DB_PROVIDERS",
    "load_app_credential_providers",
    "register_configured_providers",
    "register_provider_from_credentials",
]
