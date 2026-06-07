"""Connector OAuth service layer — shared by the REST routes and MCP tools.

The browser-redirect callbacks stay in the route module (they're inherently
HTTP), but the *initiation* + *status* logic is identical whether a founder
clicks a PWA button or an MCP agent calls a tool, so it lives here once to
avoid drift. bsvibe acts as an OAuth client of the third party throughout
(redirect_uri is always the CONFIGURED issuer base, never a request host).
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import get_settings
from backend.connectors.auth import bootstrap, store
from backend.connectors.auth.app_credentials import get_app_credentials, upsert_app_credentials
from backend.connectors.auth.github_manifest import build_manifest, manifest_post_url
from backend.connectors.auth.providers import get_provider
from backend.connectors.auth.sentry import SentryProvider
from backend.router.accounts.crypto import CredentialCipher, _key_from_settings


def build_credential_cipher() -> CredentialCipher:
    """Construct the credential cipher from settings (one place callers reuse)."""
    return CredentialCipher(_key_from_settings())


# Pending-row marker for the App Manifest flow's CSRF state — distinct from the
# user-OAuth ``github`` pending rows so the two never collide.
MANIFEST_PENDING_PROVIDER = "github:app-manifest"  # noqa: S105 — marker, not a secret

_STATE_BYTES = 32
_VERIFIER_BYTES = 48


class UnknownProviderError(Exception):
    """Raised when an OAuth connect is requested for an unregistered provider."""


async def set_app_credentials(
    session: AsyncSession,
    *,
    provider: str,
    client_id: str,
    client_secret: str,
    app_slug: str | None = None,
    cipher: CredentialCipher,
) -> None:
    """Operator: store a paste-creds provider's OAuth App creds + register it.

    For slack / notion / discord — the operator creates the OAuth app in that
    provider's console (no programmatic creation API) and pastes client_id /
    client_secret here. sentry additionally needs ``app_slug`` (its integration
    slug, used to build the external-install URL). Stored instance-global +
    encrypted in ``connector_oauth_app_credentials`` (app_id / private-key are
    github-only, left empty), then the provider is registered so workspaces can
    connect. github uses the manifest flow, not this.
    """
    if provider == "sentry":
        if not app_slug:
            raise ValueError("sentry requires app_slug (its integration slug)")
        slug: str | None = app_slug
    elif provider in bootstrap.VANILLA_DB_PROVIDERS:
        slug = None
    elif provider == "github":
        raise ValueError("github uses the App Manifest flow, not paste-creds")
    else:
        raise ValueError(f"provider does not support paste-creds setup: {provider}")
    await upsert_app_credentials(
        session,
        provider=provider,
        app_id="",
        app_slug=slug,
        client_id=client_id,
        client_secret=client_secret,
        private_key_pem="",
        webhook_secret=None,
        html_url=None,
        cipher=cipher,
    )
    await session.commit()
    creds = await get_app_credentials(session, provider=provider, cipher=cipher)
    if creds is not None:
        bootstrap.register_provider_from_credentials(provider, creds)


def _issuer() -> str:
    return get_settings().oauth_issuer.rstrip("/")


def callback_redirect_uri(provider: str) -> str:
    """The CONFIGURED callback URL the provider redirects back to."""
    return f"{_issuer()}/api/v1/connectors/oauth/{provider}/callback"


def manifest_redirect_uri() -> str:
    return f"{_issuer()}/api/v1/connectors/oauth/github/app-manifest/callback"


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(_VERIFIER_BYTES)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


async def begin_oauth_connect(
    session: AsyncSession, *, provider: str, workspace_id: uuid.UUID
) -> str:
    """Stash CSRF state + PKCE, return the provider authorize URL.

    Raises :class:`UnknownProviderError` when the provider isn't registered
    (its App credentials aren't configured).
    """
    prov = get_provider(provider)
    if prov is None:
        raise UnknownProviderError(provider)
    state = secrets.token_urlsafe(_STATE_BYTES)
    verifier, challenge = _pkce_pair()
    redirect_uri = callback_redirect_uri(provider)
    await store.create_pending(
        session,
        state=state,
        provider=provider,
        workspace_id=workspace_id,
        code_verifier=verifier,
        redirect_uri=redirect_uri,
    )
    await session.commit()
    return prov.authorize_url(state=state, code_challenge=challenge, redirect_uri=redirect_uri)


async def begin_github_app_manifest(
    session: AsyncSession, *, workspace_id: uuid.UUID
) -> dict[str, Any]:
    """Stash CSRF state, return the GitHub POST target + manifest body."""
    settings = get_settings()
    state = secrets.token_urlsafe(_STATE_BYTES)
    redirect_uri = manifest_redirect_uri()
    await store.create_pending(
        session,
        state=state,
        provider=MANIFEST_PENDING_PROVIDER,
        workspace_id=workspace_id,
        code_verifier="-",
        redirect_uri=redirect_uri,
    )
    await session.commit()
    manifest = build_manifest(
        homepage_url=settings.pwa_url.rstrip("/"),
        redirect_url=redirect_uri,
        oauth_callback_url=callback_redirect_uri("github"),
        webhook_url=f"{_issuer()}/api/webhooks/github",
    )
    return {"post_url": manifest_post_url(state), "manifest": manifest}


async def compute_github_app_status(
    session: AsyncSession, *, cipher: CredentialCipher
) -> dict[str, Any]:
    """Whether the GitHub App is set up + (DB-minted) slug/url."""
    creds = await get_app_credentials(session, provider="github", cipher=cipher)
    configured = get_provider("github") is not None or creds is not None
    return {
        "configured": configured,
        "app_slug": creds.app_slug if creds else None,
        "html_url": creds.html_url if creds else None,
    }


async def sentry_install_url(session: AsyncSession, *, cipher: CredentialCipher) -> str | None:
    """The Sentry external-install URL for the configured integration, or None.

    Sentry connect is NOT an authorize redirect — the user installs the public
    integration at this fixed URL; Sentry then redirects to our install callback
    with ``code`` + ``installationId`` (design §11).
    """
    creds = await get_app_credentials(session, provider="sentry", cipher=cipher)
    if creds is None or not creds.app_slug:
        return None
    return f"https://sentry.io/sentry-apps/{creds.app_slug}/external-install/"


async def list_unclaimed_installs(session: AsyncSession) -> list[dict[str, Any]]:
    """Unclaimed installs awaiting a workspace claim (no secrets returned)."""
    rows = await store.list_unclaimed(session)
    return [
        {
            "id": str(r.id),
            "provider": r.provider,
            "installation_ref": r.installation_ref,
            "account_label": r.account_label,
            "created_at": r.created_at.isoformat(),
        }
        for r in rows
    ]


async def claim_install(
    session: AsyncSession,
    *,
    unclaimed_id: uuid.UUID,
    workspace_id: uuid.UUID,
    cipher: CredentialCipher,
) -> str:
    """Bind an unclaimed install to ``workspace_id``; return the connector name.

    Mints (or reuses) the workspace's connector_account, stores the token, and
    records the installation ref on the account (``external_ref``) so the
    provider's refresh can find it later. Raises :class:`ValueError` if absent.
    """
    claimed = await store.claim_unclaimed(session, unclaimed_id=unclaimed_id, cipher=cipher)
    if claimed is None:
        raise ValueError("unclaimed install not found")
    provider, installation_ref, token = claimed
    account = await store.get_or_create_account(
        session, workspace_id=workspace_id, connector=provider, cipher=cipher
    )
    account.external_ref = installation_ref
    await store.upsert_token(
        session,
        connector_account_id=account.id,
        provider=provider,
        token=token,
        cipher=cipher,
    )
    await session.commit()
    return provider


async def complete_sentry_install(
    session: AsyncSession, *, code: str, installation_id: str, cipher: CredentialCipher
) -> None:
    """Exchange a Sentry install grant → park the token as an unclaimed install.

    No workspace binding here (Sentry passes no state); the founder claims it
    afterwards. Raises :class:`UnknownProviderError` if sentry isn't configured.
    """
    creds = await get_app_credentials(session, provider="sentry", cipher=cipher)
    if creds is None:
        raise UnknownProviderError("sentry")
    provider = SentryProvider(client_id=creds.client_id, client_secret=creds.client_secret)
    token = await provider.exchange_installation(installation_id=installation_id, code=code)
    await store.create_unclaimed(
        session,
        provider="sentry",
        installation_ref=installation_id,
        account_label=token.account_label,
        token=token,
        cipher=cipher,
    )
    await session.commit()


__all__ = [
    "MANIFEST_PENDING_PROVIDER",
    "UnknownProviderError",
    "begin_github_app_manifest",
    "begin_oauth_connect",
    "build_credential_cipher",
    "callback_redirect_uri",
    "claim_install",
    "complete_sentry_install",
    "compute_github_app_status",
    "list_unclaimed_installs",
    "manifest_redirect_uri",
    "sentry_install_url",
    "set_app_credentials",
]
