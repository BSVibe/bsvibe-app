"""ORM rows for embedding settings + intent definitions + examples.

Per-domain :class:`GatewayEmbeddingBase` (Bundle 1 pattern).
``intent_examples.embedding`` is the :class:`EmbeddingVector` column —
``vector`` on Postgres (pgvector), packed-float32 BLOB on SQLite.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.data import Base
from backend.gateway.embedding.column import EmbeddingVector

GatewayEmbeddingBase = Base


class AccountEmbeddingSettingsRow(GatewayEmbeddingBase):
    """One row per ``(workspace_id, account_id)`` — config JSONB."""

    __tablename__ = "account_embedding_settings"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    account_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    # Parsed by :class:`EmbeddingSettings.from_account_settings`.
    config: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
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
        UniqueConstraint("workspace_id", "account_id", name="uq_account_embedding_settings"),
    )


class IntentDefinitionRow(GatewayEmbeddingBase):
    """A named intent. Examples (with embeddings) live in
    :class:`IntentExampleRow`."""

    __tablename__ = "intent_definitions"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    account_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # Cosine similarity threshold — match returns ``None`` below this.
    # 0.65 default per plan §1.5b; BSGateway prod uses 0.7.
    threshold: Mapped[float] = mapped_column(nullable=False, default=0.65)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=datetime.utcnow
    )

    examples: Mapped[list[IntentExampleRow]] = relationship(
        back_populates="intent",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "account_id", "name", name="uq_intent_definitions_acct_name"
        ),
    )


class IntentExampleRow(GatewayEmbeddingBase):
    """One example phrase + its embedding. Stale when ``embedding_model``
    no longer matches the account's current setting."""

    __tablename__ = "intent_examples"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    intent_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("intent_definitions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Denormalized so search backends can filter without an extra join.
    workspace_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    account_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list[float] | None] = mapped_column(EmbeddingVector(), nullable=True)
    embedding_model: Mapped[str | None] = mapped_column(String(120), nullable=True)
    dimension: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=datetime.utcnow
    )

    intent: Mapped[IntentDefinitionRow] = relationship(back_populates="examples")
