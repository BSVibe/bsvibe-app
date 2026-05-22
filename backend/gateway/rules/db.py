"""SQLAlchemy ORM rows for ``routing_rules`` + ``rule_conditions``.

Owns its own :class:`GatewayRulesBase` to keep alembic's per-base
metadata-merge pattern (see :mod:`backend.gateway.budget.models`). The
runtime dataclasses in :mod:`backend.gateway.rules.models` are the
evaluator's API surface; these ORM rows are only what crosses the wire
to Postgres.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.data import Base

GatewayRulesBase = Base


class RoutingRuleRow(GatewayRulesBase):
    __tablename__ = "routing_rules"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    account_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    target_model: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=datetime.utcnow
    )

    conditions: Mapped[list[RuleConditionRow]] = relationship(
        back_populates="rule",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    __table_args__ = (
        UniqueConstraint("workspace_id", "account_id", "name", name="uq_routing_rules_acct_name"),
        # NOTE: This constraint is DEFERRABLE INITIALLY DEFERRED in the
        # alembic migration (so :meth:`RulesRepository.reorder_rules`
        # can swap priorities in one tx). SQLite has no DEFERRABLE
        # support, so we keep the SQLAlchemy model side simple.
        UniqueConstraint(
            "workspace_id",
            "account_id",
            "priority",
            name="uq_routing_rules_acct_priority",
        ),
    )


class RuleConditionRow(GatewayRulesBase):
    __tablename__ = "rule_conditions"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    rule_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("routing_rules.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    condition_type: Mapped[str] = mapped_column(String(40), nullable=False)
    operator: Mapped[str] = mapped_column(String(20), nullable=False)
    field: Mapped[str] = mapped_column(String(60), nullable=False)
    # JSON (not JSONB) — portable to SQLite for tests; Postgres uses JSONB
    # under the hood via the dialect's JSON type adapter.
    value: Mapped[object] = mapped_column(JSON, nullable=False)
    negate: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    rule: Mapped[RoutingRuleRow] = relationship(back_populates="conditions")
