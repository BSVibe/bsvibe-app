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

from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import Settings, get_settings
from backend.connectors.auth.app_credentials import AppCredentials, get_app_credentials
from backend.connectors.auth.github import GitHubAppProvider
from backend.connectors.auth.providers import register_provider
from backend.router.accounts.crypto import CredentialCipher

# Providers loadable from the DB app-credentials table (manifest flow). github
# only for now; slack / discord / notion / sentry cascade.
_DB_PROVIDERS = ("github",)


def register_configured_providers(settings: Settings | None = None) -> list[str]:
    """Register every provider whose ENV credentials are present; return names."""
    settings = settings or get_settings()
    registered: list[str] = []

    if settings.github_app_client_id and settings.github_app_client_secret:
        register_provider(
            GitHubAppProvider(
                client_id=settings.github_app_client_id,
                client_secret=settings.github_app_client_secret,
                app_id=settings.github_app_id,
                private_key_pem=settings.github_app_private_key_pem,
            )
        )
        registered.append("github")

    return registered


def _register_github_from_credentials(creds: AppCredentials) -> None:
    register_provider(
        GitHubAppProvider(
            client_id=creds.client_id,
            client_secret=creds.client_secret,
            app_id=creds.app_id,
            private_key_pem=creds.private_key_pem,
        )
    )


async def load_app_credential_providers(
    session: AsyncSession, cipher: CredentialCipher
) -> list[str]:
    """Register providers from the DB app-credentials table; return their names.

    Overrides any env-registered provider of the same name (DB = the App the
    founder set up via the manifest flow).
    """
    registered: list[str] = []
    for provider in _DB_PROVIDERS:
        creds = await get_app_credentials(session, provider=provider, cipher=cipher)
        if creds is None:
            continue
        if provider == "github":
            _register_github_from_credentials(creds)
        registered.append(provider)
    return registered


__all__ = ["load_app_credential_providers", "register_configured_providers"]
