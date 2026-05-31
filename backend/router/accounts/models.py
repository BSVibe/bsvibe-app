"""SQLAlchemy schema for ModelAccount (per-workspace, per-account).

Supersedes BSGateway's single-tenant ``tenant_models`` table: each row
is scoped to ``(workspace_id, account_id)``. ``data_jurisdiction`` is
declared by the worker SDK at registration (Workflow §8.4) — never
inferred, never user-typed; we just store + index it.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from backend.data import Base

AccountsBase = Base


class ModelAccount(AccountsBase):
    __tablename__ = "model_accounts"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    account_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    label: Mapped[str] = mapped_column(String(128), nullable=False)
    litellm_model: Mapped[str] = mapped_column(String(255), nullable=False)
    api_base: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # NULLABLE since executor-pool Lift 5a: a ``provider='executor'`` account
    # routes to a CLI worker capability and carries NO api key. Real LLM
    # accounts always populate it (the encrypting create path requires one).
    api_key_encrypted: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    data_jurisdiction: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    extra_params: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=datetime.utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    __table_args__ = (
        UniqueConstraint("workspace_id", "account_id", "label", name="uq_model_account_label"),
        Index("ix_model_accounts_workspace_account", "workspace_id", "account_id"),
    )
