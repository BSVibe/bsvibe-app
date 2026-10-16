"""What plan a workspace is on and how much of it is in use (#928 step 4).

One function for every surface (REST ``GET /api/v1/workspace/plan``, MCP
``bsvibe_workspace_plan``, the PWA's plan card), and it counts use with the SAME functions the
gates refuse with — :func:`~backend.workflow.application.run_caps.count_held_runs` and
:func:`~backend.workflow.application.workspace_token_budget.sum_workspace_tokens_since` — so the
number a founder reads is the number that will say 429.

A limit of ``None`` is "unlimited" and stays ``None`` on the wire; rendering it as 0 would tell
an uncapped workspace it has nothing.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from backend.identity.workspaces_db import WorkspaceRow
from backend.workflow.application.run_caps import count_held_runs
from backend.workflow.application.workspace_token_budget import (
    budget_window_start,
    sum_workspace_tokens_since,
)


@dataclass(frozen=True)
class PlanSummary:
    plan: str
    runs_held: int
    runs_limit: int | None
    tokens_used: int
    tokens_limit: int | None
    period_start: datetime
    verify_stack_slots: int
    billing_status: str | None

    def to_wire(self) -> dict[str, object]:
        """The JSON shape both REST and MCP return — one spelling, so they cannot drift."""
        return {
            "plan": self.plan,
            "runs": {"held": self.runs_held, "limit": self.runs_limit},
            "tokens": {
                "used": self.tokens_used,
                "limit": self.tokens_limit,
                "period_start": self.period_start.isoformat(),
            },
            "verify_stack_slots": self.verify_stack_slots,
            "billing_status": self.billing_status,
        }


async def load_plan_summary(session: AsyncSession, workspace_id: uuid.UUID) -> PlanSummary | None:
    """The plan summary of ``workspace_id``, or ``None`` when no such workspace."""
    row = await session.get(WorkspaceRow, workspace_id)
    if row is None:
        return None
    period_start = budget_window_start()
    return PlanSummary(
        plan=row.plan,
        runs_held=await count_held_runs(session, workspace_id),
        runs_limit=row.max_concurrent_runs,
        tokens_used=await sum_workspace_tokens_since(session, workspace_id, period_start),
        tokens_limit=row.monthly_token_budget,
        period_start=period_start,
        verify_stack_slots=row.verify_stack_slots,
        billing_status=row.billing_status,
    )


__all__ = ["PlanSummary", "load_plan_summary"]
