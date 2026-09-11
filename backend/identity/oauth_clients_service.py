"""OAuth client (RFC 7591 Dynamic Client Registration) service layer.

D1 ships **public clients only** — PKCE-bound, no client_secret. The DCR
endpoint is unauthenticated (per RFC 7591 §3): anyone who can reach the
authorization server can register a new client_id. Founder gating
happens elsewhere (the consent screen + the founder-facing Settings
management UI).

A client always carries a ``workspace_id`` — the workspace whose owner
registered it (for founder-created clients) or the workspace of the
first user to ``/authorize`` against it (NOT YET; v1 requires the
founder to register clients explicitly from Settings, so the workspace
is the founder's at registration time). Without an authenticated DCR
caller, v1 requires the founder to pass ``workspace_id`` explicitly via
the Settings UI; the public ``POST /api/oauth/clients`` endpoint accepts
this in the body.
"""

from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.identity.oauth_db import OAuthClientRow
from backend.shared.oauth_client_ids import DEVICE_CLIENT_ID

#: Clients this server vouches for WITHOUT a registration row.
#:
#: The RFC 8628 device grant deliberately has no registration step — it is a
#: public-client flow whose security rests on the human approving a short code,
#: not on client identity (see ``backend.shared.oauth_client_ids``). That is
#: why prod carries no ``oauth_clients`` row for ``bsvibe-cli`` even though it
#: is the client behind most device sign-ins. Without this allow-list the real
#: first-party CLI would be indistinguishable from a string an attacker typed,
#: and the consent screen could only ever say "unverified".
#:
#: The key is imported from the shared kernel, never re-typed: two copies of
#: this literal is exactly how the CLI and the consent screen would drift into
#: disagreeing about who the first party is. It is NOT imported from the CLI's
#: login module — the server does not depend on its clients.
FIRST_PARTY_DEVICE_CLIENTS: dict[str, str] = {DEVICE_CLIENT_ID: "BSVibe CLI"}


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _gen_client_id() -> str:
    """``dcr-<24 url-safe chars>`` — distinguishable from static seeding."""
    return "dcr-" + secrets.token_urlsafe(18)


async def register_client(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID | None,
    created_by_user_id: uuid.UUID | None,
    client_name: str,
    redirect_uris: list[str],
    allowed_scopes: list[str],
    now: datetime | None = None,
) -> OAuthClientRow:
    """Register a new OAuth client. Returns the persisted row.

    ``workspace_id`` and ``created_by_user_id`` may be ``None`` for
    anonymous DCR rows (Lift D2 followup) — see :mod:`backend.api.oauth`
    for the open ``POST /api/oauth/register`` endpoint that uses this.
    """
    n = now or _utcnow()
    row = OAuthClientRow(
        workspace_id=workspace_id,
        client_id=_gen_client_id(),
        client_name=client_name,
        client_type="public",
        redirect_uris=list(redirect_uris),
        allowed_scopes=list(allowed_scopes),
        created_by_user_id=created_by_user_id,
        created_at=n,
    )
    session.add(row)
    await session.flush()
    return row


async def lookup_client_by_client_id(
    session: AsyncSession, client_id: str
) -> OAuthClientRow | None:
    """Return the client matching ``client_id``, ignoring workspace scope.

    Used by the unauthenticated ``/authorize`` + ``/token`` endpoints —
    these MUST find the client regardless of which workspace it lives in.
    The auto-filter is bypassed via :class:`OAuthClientRow.__exclude...`
    NOT being set; clients ARE workspace-scoped, but the
    ``/authorize`` surface has no contextvar at all (no session), so
    nothing engages the filter. The ``.get`` / ``select`` below run
    outside any workspace context.
    """
    stmt = select(OAuthClientRow).where(OAuthClientRow.client_id == client_id)
    return (await session.execute(stmt)).scalar_one_or_none()


@dataclass(frozen=True)
class ClientIdentity:
    """What this server is willing to SAY about a ``client_id``.

    ``verified`` is the only thing a consent screen may hang a name on.
    When it is ``False`` the caller-supplied string is still worth showing —
    the human should see what was claimed — but never as an identity.
    """

    verified: bool
    label: str | None


async def resolve_client_identity(session: AsyncSession, *, client_id: str) -> ClientIdentity:
    """Resolve a caller-supplied ``client_id`` to a display identity.

    Two sources count as known, and nothing else does:

    1. the first-party allow-list (:data:`FIRST_PARTY_DEVICE_CLIENTS`), and
    2. a live (non-revoked) ``oauth_clients`` registration — the name that
       row carries is one this server issued.

    A revoked registration is deliberately NOT verified: the founder revoking
    a client is precisely the statement "I no longer vouch for this".
    """
    first_party = FIRST_PARTY_DEVICE_CLIENTS.get(client_id)
    if first_party is not None:
        return ClientIdentity(verified=True, label=first_party)
    row = await lookup_client_by_client_id(session, client_id)
    if row is None or row.revoked_at is not None:
        return ClientIdentity(verified=False, label=None)
    return ClientIdentity(verified=True, label=row.client_name)


async def list_clients_for_workspace(
    session: AsyncSession, workspace_id: uuid.UUID
) -> list[OAuthClientRow]:
    """Founder-facing listing — current workspace, active rows only.

    Revoked rows are kept in the table for audit (and so we don't break
    the FK from oauth_codes / oauth_access_tokens already issued under
    them), but the Settings UI doesn't show them — the supersede-on-
    consent path produces one revoked row per ``claude mcp authenticate``
    retry, which is noise the founder doesn't want to see.
    """
    stmt = (
        select(OAuthClientRow)
        .where(
            OAuthClientRow.workspace_id == workspace_id,
            OAuthClientRow.revoked_at.is_(None),
        )
        .order_by(OAuthClientRow.created_at.desc())
    )
    return list((await session.execute(stmt)).scalars().all())


async def revoke_client(
    session: AsyncSession,
    *,
    client_id: str,
    workspace_id: uuid.UUID,
    now: datetime | None = None,
) -> OAuthClientRow | None:
    """Soft-revoke (idempotent). Returns the row, or ``None`` if absent."""
    n = now or _utcnow()
    stmt = select(OAuthClientRow).where(
        OAuthClientRow.client_id == client_id,
        OAuthClientRow.workspace_id == workspace_id,
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        return None
    if row.revoked_at is None:
        row.revoked_at = n
        await session.flush()
    return row


__all__ = [
    "FIRST_PARTY_DEVICE_CLIENTS",
    "ClientIdentity",
    "list_clients_for_workspace",
    "lookup_client_by_client_id",
    "register_client",
    "resolve_client_identity",
    "revoke_client",
]
