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
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import get_settings
from backend.connectors.auth import store
from backend.connectors.auth.app_credentials import (
    app_credentials_exist,
    get_app_credentials,
)
from backend.connectors.auth.github_manifest import build_manifest, manifest_post_url
from backend.connectors.auth.providers import get_provider
from backend.connectors.auth.sentry import SentryProvider
from backend.data.rls import workspace_session_scope
from backend.identity.infrastructure.repositories.membership_repository_sql import (
    SqlAlchemyMembershipRepository,
)
from backend.identity.plans import Plan
from backend.identity.roles import role_satisfies
from backend.identity.workspaces_db import WorkspaceRow
from backend.router.accounts.crypto import (
    CredentialCipher,
    cipher_from_settings,
)


def build_credential_cipher() -> CredentialCipher:
    """Construct the credential cipher from settings (one place callers reuse)."""
    return cipher_from_settings()


# Pending-row marker for the App Manifest flow's CSRF state — distinct from the
# user-OAuth ``github`` pending rows so the two never collide.
MANIFEST_PENDING_PROVIDER = "github:app-manifest"  # noqa: S105 — marker, not a secret

MANIFEST_STATE_TTL = timedelta(minutes=10)
_STATE_BYTES = 32
_VERIFIER_BYTES = 48


class UnknownProviderError(Exception):
    """Raised when an OAuth connect is requested for an unregistered provider."""


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


class NotOperatorError(Exception):
    """The caller is not an instance operator (workspace plan is not ``operator``)."""


class AppAlreadyConfiguredError(Exception):
    """GitHub App credentials already exist and no explicit replace was requested."""


async def is_operator(
    session: AsyncSession, *, workspace_id: uuid.UUID, user_id: uuid.UUID | None
) -> bool:
    """Instance operator = the caller is admin/owner of a workspace on the ``operator`` plan.

    The plan alone is not enough: every member (viewer included) of an
    operator-plan workspace would otherwise control the instance-wide App.
    ``user_id`` is the caller's ``users.id`` (membership key).
    """
    if user_id is None:
        return False
    # Scoped explicitly: the public callback has no request-resolved tenant (RLS).
    async with workspace_session_scope(session, workspace_id):
        row = await session.get(WorkspaceRow, workspace_id)
        if row is None or row.plan != Plan.OPERATOR.value:
            return False
        membership = await SqlAlchemyMembershipRepository(session).active_for_user_in_workspace(
            user_id, workspace_id
        )
    return membership is not None and role_satisfies(membership.role, "admin")


def _encode_manifest_binding(*, user_id: str, replace: bool) -> str:
    # The manifest flow has no PKCE verifier, so the (otherwise unused) column
    # carries who started the flow and whether they asked to replace.
    return f"manifest:{int(replace)}:{user_id}"


def decode_manifest_binding(value: str) -> tuple[str, bool] | None:
    """``(user_id, replace)`` of a bound state, or ``None`` for an unbound one."""
    parts = value.split(":", 2)
    if len(parts) != 3 or parts[0] != "manifest" or parts[1] not in ("0", "1") or not parts[2]:
        return None
    return parts[2], parts[1] == "1"


def manifest_state_expired(created_at: datetime) -> bool:
    """True once a manifest state is older than :data:`MANIFEST_STATE_TTL`."""
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)
    return datetime.now(tz=UTC) - created_at > MANIFEST_STATE_TTL


async def begin_github_app_manifest(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID | None,
    replace: bool = False,
) -> dict[str, Any]:
    """Stash CSRF state, return the GitHub POST target + manifest body.

    Operator-only (the App credentials are instance-global); refuses to start
    over existing credentials unless ``replace`` is explicit.
    """
    if user_id is None or not await is_operator(
        session, workspace_id=workspace_id, user_id=user_id
    ):
        raise NotOperatorError(str(workspace_id))
    if not replace and (
        await app_credentials_exist(session, provider="github") or get_provider("github")
    ):
        raise AppAlreadyConfiguredError("github")
    settings = get_settings()
    state = secrets.token_urlsafe(_STATE_BYTES)
    redirect_uri = manifest_redirect_uri()
    await store.create_pending(
        session,
        state=state,
        provider=MANIFEST_PENDING_PROVIDER,
        workspace_id=workspace_id,
        code_verifier=_encode_manifest_binding(user_id=str(user_id), replace=replace),
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


async def claim_install(
    session: AsyncSession,
    *,
    provider: str,
    installation_ref: str,
    workspace_id: uuid.UUID,
    cipher: CredentialCipher,
) -> str:
    """Bind an unclaimed install to ``workspace_id``; return the connector name.

    H2 (2026-09-10 audit) — the caller PROVES possession of ``installation_ref``
    (visible only in their own Sentry org), which is the workspace binding the
    provider's stateless callback could not carry. Previously any tenant could
    claim any pending install by picking its row id from a global list.

    Mints (or reuses) the workspace's connector_account, stores the token, and
    records the installation ref on the account (``external_ref``) so the
    provider's refresh can find it later. Raises :class:`ValueError` if no
    pending install matches ``(provider, installation_ref)``.
    """
    claimed = await store.claim_by_installation(
        session, provider=provider, installation_ref=installation_ref, cipher=cipher
    )
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
    "AppAlreadyConfiguredError",
    "NotOperatorError",
    "decode_manifest_binding",
    "is_operator",
    "manifest_state_expired",
    "MANIFEST_PENDING_PROVIDER",
    "UnknownProviderError",
    "begin_github_app_manifest",
    "begin_oauth_connect",
    "build_credential_cipher",
    "callback_redirect_uri",
    "claim_install",
    "complete_sentry_install",
    "compute_github_app_status",
    "manifest_redirect_uri",
    "sentry_install_url",
]
