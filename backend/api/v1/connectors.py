"""/api/v1/connectors — founder-facing ConnectorAccount CRUD (Workflow §11.2).

Lets a founder register, list, and revoke a per-workspace inbound connector
binding and learn the webhook URL to paste into the external service
(GitHub / Slack / Telegram / Discord). This is the missing front door for the
PUBLIC ingress added in §11.2 (``POST /api/webhooks/{connector}/{token}``):
without a way to CREATE a ``connector_accounts`` row there is no token for an
external provider to call.

Capability handling (mirrors :mod:`backend.api.v1.accounts`' encrypt-on-write /
never-return-secret pattern):

* On create the server mints an unguessable ``webhook_token``
  (``secrets.token_urlsafe(32)``) and encrypts ``signing_secret`` via
  :class:`backend.router.accounts.crypto.CredentialCipher` — the plaintext secret
  never touches disk and is never returned over the API.
* The ``webhook_token`` (and the full webhook URL built from it) is returned
  ONLY in the create response, exactly once, like an API key. List responses
  expose a masked hint (last 4 chars) — never the full token, which is itself
  a capability (it is half of the ingress auth).

The allowed connector set is the set of built-in inbound parsers; it is read
from the engine's process-wide
:func:`backend.extensions.plugin.webhook_registry.get_default_registry` so
the CRUD and the ingress agree on exactly which connectors exist (the same
registry the :class:`ConnectorInboundResolver` dispatches through).
"""

from __future__ import annotations

import secrets
import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.deps import get_db_session, get_workspace_id

# Reuse the ingress's cipher dependency so the create-side encrypt and the
# webhook-side decrypt share one (test-overridable) cipher.
from backend.api.webhooks import get_credential_cipher
from backend.connectors.db import ConnectorAccountRow
from backend.extensions.plugin.webhook_registry import get_default_registry
from backend.router.accounts.crypto import CredentialCipher
from backend.workflow.application.delivery.connector_dispatch import OUTBOUND_EVENT_BUILDERS

router = APIRouter()

# Length of the minted capability. token_urlsafe(32) yields ~43 base64url chars.
_TOKEN_BYTES = 32


def _webhook_url(connector: str, webhook_token: str) -> str:
    """The path an external provider POSTs to (mounted under ``/api``)."""
    return f"/api/webhooks/{connector}/{webhook_token}"


class ConnectorCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connector: str = Field(min_length=1, max_length=64)
    signing_secret: str = Field(min_length=1, max_length=1024)
    external_ref: str | None = Field(default=None, max_length=255)
    # Outbound delivery target binding (Workflow §12.5 #8). For a connector
    # with an ``@p.outbound`` this carries the STABLE routing fields it needs to
    # deliver a verified Deliverable OUT (e.g. notion ``{"parent_page_id": …}``).
    # Routing is founder-set config — never derived from LLM/work output.
    delivery_config: dict[str, Any] = Field(default_factory=dict)

    @field_validator("connector")
    @classmethod
    def _known_connector(cls, v: str) -> str:
        # A connector is registerable when it has an inbound parser (webhook
        # ingress) OR an outbound delivery binding (a v1 event-shaping mapper).
        # notion is outbound-only — it has no inbound parser but is a valid
        # delivery target, so the inbound-known check alone would reject it.
        if not get_default_registry().is_known(v) and v not in OUTBOUND_EVENT_BUILDERS:
            raise ValueError(f"unknown connector {v!r}")
        return v


class ConnectorCreated(BaseModel):
    """Create response — the ONLY place the webhook_token + URL are shown."""

    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    connector: str
    external_ref: str | None
    is_active: bool
    created_at: datetime
    delivery_config: dict[str, Any]
    webhook_token: str
    webhook_url: str


class ConnectorOut(BaseModel):
    """List response — never the secret, never the full token."""

    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    connector: str
    external_ref: str | None
    is_active: bool
    created_at: datetime
    delivery_config: dict[str, Any]
    token_hint: str


def _token_hint(webhook_token: str) -> str:
    """Last 4 chars only — enough to recognise, not enough to use."""
    return f"...{webhook_token[-4:]}"


@router.get("")
async def list_connectors(
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> list[ConnectorOut]:
    rows = (
        (
            await session.execute(
                select(ConnectorAccountRow)
                .where(ConnectorAccountRow.workspace_id == workspace_id)
                .order_by(ConnectorAccountRow.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    return [
        ConnectorOut(
            id=r.id,
            connector=r.connector,
            external_ref=r.external_ref,
            is_active=r.is_active,
            created_at=r.created_at,
            delivery_config=r.delivery_config,
            token_hint=_token_hint(r.webhook_token),
        )
        for r in rows
    ]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_connector(
    payload: ConnectorCreate,
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    cipher: Annotated[CredentialCipher, Depends(get_credential_cipher)],
) -> ConnectorCreated:
    webhook_token = secrets.token_urlsafe(_TOKEN_BYTES)
    row = ConnectorAccountRow(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        connector=payload.connector,
        webhook_token=webhook_token,
        signing_secret_ciphertext=cipher.encrypt(payload.signing_secret),
        external_ref=payload.external_ref,
        delivery_config=payload.delivery_config,
        is_active=True,
    )
    session.add(row)
    await session.commit()
    return ConnectorCreated(
        id=row.id,
        connector=row.connector,
        external_ref=row.external_ref,
        is_active=row.is_active,
        created_at=row.created_at,
        delivery_config=row.delivery_config,
        webhook_token=webhook_token,
        webhook_url=_webhook_url(row.connector, webhook_token),
    )


@router.delete("/{connector_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_connector(
    connector_id: uuid.UUID,
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> None:
    """Soft-revoke: flip ``is_active`` False. The ingress already 404s on it."""
    row = await session.get(ConnectorAccountRow, connector_id)
    if row is None or row.workspace_id != workspace_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"connector {connector_id} not found",
        )
    row.is_active = False
    await session.commit()


__all__ = [
    "ConnectorCreate",
    "ConnectorCreated",
    "ConnectorOut",
    "create_connector",
    "list_connectors",
    "revoke_connector",
    "router",
]
