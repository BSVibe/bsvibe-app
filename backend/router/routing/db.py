"""ORM rows for routing — model_catalog_entries + routing_logs.

``model_catalog_entries`` replaces BSGateway's ``tenant_models`` table.
Renamed to disambiguate from :class:`ModelAccount` (credentials) vs.
catalog entries (which model names are available to dispatch).

``routing_logs.bsvibe_task_*`` mirror BSGateway's
``nexus_task_*`` columns (task taxonomy carried alongside the
routing decision). The prefix was renamed: a sweep across the
monorepo (bsvibe-app, BSNexus, BSupervisor) + ``~/Docs`` found zero
external readers, so retaining the BSNexus-era prefix would be
dead semantics. Fresh deploy → rename happens directly in the
1.5b migration; no compat layer needed.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from backend.data import Base
from backend.router.embedding.column import EmbeddingVector

GatewayRoutingBase = Base


class ModelCatalogEntryRow(GatewayRoutingBase):
    """One entry in the per-account model catalog.

    ``origin='custom'`` adds (or overrides) a model name for the account;
    ``origin='hide_system'`` subtracts a name from the yaml-sourced
    system catalog so that account can't use it.
    """

    __tablename__ = "model_catalog_entries"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    account_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    origin: Mapped[str] = mapped_column(String(20), nullable=False)
    litellm_model: Mapped[str | None] = mapped_column(String(255), nullable=True)
    litellm_params: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    is_passthrough: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
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
        UniqueConstraint(
            "workspace_id",
            "account_id",
            "name",
            name="uq_model_catalog_entries_acct_name",
        ),
    )


class RoutingLogRow(GatewayRoutingBase):
    """One routing-decision audit row.

    ``embedding`` uses :class:`EmbeddingVector` so it lives in pgvector
    on Postgres (`vector` column, future `<=>` analytics) and in
    LargeBinary on the SQLite test path.
    """

    __tablename__ = "routing_logs"

    # UUID primary key — append-only, no need for sequential id; uniform
    # with the other domain rows in this codebase.
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=datetime.utcnow, index=True
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    account_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    rule_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    user_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    system_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    token_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    conversation_turns: Mapped[int | None] = mapped_column(Integer, nullable=True)
    code_block_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    code_lines: Mapped[int | None] = mapped_column(Integer, nullable=True)
    has_error_trace: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    tool_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tier: Mapped[str | None] = mapped_column(String(20), nullable=True)
    strategy: Mapped[str | None] = mapped_column(String(40), nullable=True)
    score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    original_model: Mapped[str | None] = mapped_column(String(200), nullable=True)
    resolved_model: Mapped[str | None] = mapped_column(String(200), nullable=True)
    embedding: Mapped[list[float] | None] = mapped_column(EmbeddingVector(), nullable=True)
    bsvibe_task_type: Mapped[str | None] = mapped_column(String(80), nullable=True)
    bsvibe_priority: Mapped[str | None] = mapped_column(String(20), nullable=True)
    bsvibe_complexity_hint: Mapped[int | None] = mapped_column(Integer, nullable=True)
    decision_source: Mapped[str | None] = mapped_column(String(40), nullable=True)
