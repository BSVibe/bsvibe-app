"""Per-account budget policy schema.

One row per ``(workspace_id, account_id, scope)``. Cap is in cents to
avoid float arithmetic; ``enforcement`` selects between hard-block /
warn / log-only behavior at evaluation time.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, Integer, UniqueConstraint
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class GatewayBudgetBase(DeclarativeBase):
    """Declarative base for budget-enforcement tables.

    Separate from :class:`backend.accounts.models.AccountsBase` because
    budget caps are a gateway-domain enforcement policy attached to an
    ``account_id`` scope — the ``ModelAccount`` row itself lives at the
    workspace layer.
    """


class BudgetScope(StrEnum):
    DAILY = "daily"
    MONTHLY = "monthly"


class BudgetEnforcement(StrEnum):
    BLOCK = "block"
    WARN = "warn"
    LOG = "log"


class AccountBudgetPolicy(GatewayBudgetBase):
    __tablename__ = "account_budget_policies"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    account_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    scope: Mapped[BudgetScope] = mapped_column(
        SAEnum(
            BudgetScope, name="budget_scope_enum", values_callable=lambda ec: [m.value for m in ec]
        ),
        nullable=False,
    )
    cost_cap_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    enforcement: Mapped[BudgetEnforcement] = mapped_column(
        SAEnum(
            BudgetEnforcement,
            name="budget_enforcement_enum",
            values_callable=lambda ec: [m.value for m in ec],
        ),
        nullable=False,
        default=BudgetEnforcement.BLOCK,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=datetime.utcnow
    )

    __table_args__ = (
        UniqueConstraint("workspace_id", "account_id", "scope", name="uq_account_budget_scope"),
    )
