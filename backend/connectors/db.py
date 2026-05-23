"""ConnectorAccount persistence — workspace-scoped inbound webhook binding.

Workflow §11.2 (connector-inbound path). One row per external connector a
workspace has registered for inbound webhooks. The ``webhook_token`` is the
unguessable path component an external provider calls
(``/api/webhooks/{connector}/{webhook_token}``); it is UNIQUE so the
``(connector, webhook_token)`` pair resolves to exactly one workspace.

The connector's signing secret is stored encrypted
(``signing_secret_ciphertext``) via the same
:class:`backend.accounts.crypto.CredentialCipher` pattern as
``model_accounts.api_key_encrypted`` — plaintext secrets never touch disk.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from backend.data import Base

ConnectorsBase = Base


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


class ConnectorAccountRow(ConnectorsBase):
    """A workspace's binding of one external connector for inbound webhooks."""

    __tablename__ = "connector_accounts"
    __table_args__ = (
        UniqueConstraint("webhook_token", name="uq_connector_accounts_webhook_token"),
        Index("ix_connector_accounts_lookup", "connector", "webhook_token"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    connector: Mapped[str] = mapped_column(String(64), nullable=False)
    webhook_token: Mapped[str] = mapped_column(String(128), nullable=False)
    signing_secret_ciphertext: Mapped[str] = mapped_column(String(1024), nullable=False)
    external_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


__all__: list[str] = ["ConnectorAccountRow", "ConnectorsBase"]
