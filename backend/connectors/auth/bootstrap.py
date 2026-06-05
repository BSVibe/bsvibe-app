"""Register the credential-gated OAuth providers from settings (Lift 1).

The Lift 0 skeleton seeds only the StubProvider at import time (so the
storage / endpoint / resolution layers are exercisable without secrets). Real
providers are registered HERE, at app startup, and ONLY when their App
credentials are configured — a deployment without GitHub creds keeps a github
connector working via the legacy signing-secret path (resolve_connector_credentials
falls back), it just can't offer "Connect with GitHub".

Called once from :func:`backend.api.main.create_app` (alongside
``discover_webhook_parsers``). Idempotent: re-registering overwrites the same
``github`` slot.
"""

from __future__ import annotations

from backend.config import Settings, get_settings
from backend.connectors.auth.github import GitHubAppProvider
from backend.connectors.auth.providers import register_provider


def register_configured_providers(settings: Settings | None = None) -> list[str]:
    """Register every provider whose credentials are present; return their names."""
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


__all__ = ["register_configured_providers"]
