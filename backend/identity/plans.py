"""Which plan a workspace is on, and the limits each plan sets (#928).

Founder decision (2026-10-16): **capacity subscription**. Model tokens stay BYO — a workspace
pays its own provider, or runs its own worker on its own subscription — so BSVibe sells the
platform's capacity, not tokens. The capacity levers already existed as per-workspace columns
(``max_concurrent_runs``, ``verify_stack_slots``); what was missing was a name for the set and a
single place that moves a workspace between sets. Before this, leaving the free plan was
operator SQL.

:func:`apply_plan` is that place. It writes the plan AND every limit the plan sets in the
caller's transaction, so the rules that read the columns (``run_caps``, ``verify_slots``,
``workspace_token_budget``) need no change and cannot see a half-applied plan. The payment
provider's webhook calls exactly this; so does an operator.

``monthly_token_budget`` is a SAFETY quota, not a price lever (``workspaces_db``), so the paid
plan does not raise it — a paying workspace is no less able to run away.

A downgrade does not touch what a workspace already holds: runs over the new cap keep running,
the cap only refuses the next submission (``run_caps`` counts at admission).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.identity.workspaces_db import (
    DEFAULT_MAX_CONCURRENT_RUNS,
    DEFAULT_MONTHLY_TOKEN_BUDGET,
    DEFAULT_PLAN,
    WorkspaceRow,
)


class Plan(StrEnum):
    FREE = DEFAULT_PLAN
    PRO = "pro"
    #: The operator's own workspace — grandfathered off every cap by the migrations that
    #: introduced them. Never sold; named so "uncapped" is a plan, not an accident.
    OPERATOR = "operator"


@dataclass(frozen=True)
class PlanLimits:
    """The columns a plan sets. ``None`` = unlimited, as each column documents."""

    max_concurrent_runs: int | None
    verify_stack_slots: int
    monthly_token_budget: int | None


PLAN_LIMITS: dict[Plan, PlanLimits] = {
    # The column defaults ARE the free plan — a new workspace is born on it.
    Plan.FREE: PlanLimits(
        max_concurrent_runs=DEFAULT_MAX_CONCURRENT_RUNS,
        verify_stack_slots=1,
        monthly_token_budget=DEFAULT_MONTHLY_TOKEN_BUDGET,
    ),
    # Founder decision 2026-10-16: 10 runs · 2 verification stacks — what one host carries
    # before horizontal scale-out (#929).
    Plan.PRO: PlanLimits(
        max_concurrent_runs=10,
        verify_stack_slots=2,
        monthly_token_budget=DEFAULT_MONTHLY_TOKEN_BUDGET,
    ),
    Plan.OPERATOR: PlanLimits(
        max_concurrent_runs=None,
        verify_stack_slots=2,
        monthly_token_budget=None,
    ),
}


async def apply_plan(session: AsyncSession, *, workspace_id: uuid.UUID, plan: Plan) -> None:
    """Put ``workspace_id`` on ``plan``: the plan name and every limit it sets, one UPDATE.

    Flushes, does not commit — the caller owns the transaction (a webhook records its event
    in the same one). Raises :class:`LookupError` for an unknown workspace: a payment for a
    workspace that does not exist must fail loudly, not report success.
    """
    limits = PLAN_LIMITS[plan]
    result = await session.execute(
        update(WorkspaceRow)
        .where(WorkspaceRow.id == workspace_id)
        .values(
            plan=plan.value,
            max_concurrent_runs=limits.max_concurrent_runs,
            verify_stack_slots=limits.verify_stack_slots,
            monthly_token_budget=limits.monthly_token_budget,
        )
    )
    if result.rowcount != 1:  # type: ignore[attr-defined]
        raise LookupError(f"no workspace {workspace_id} to put on plan {plan.value!r}")
    await session.flush()


__all__ = ["PLAN_LIMITS", "Plan", "PlanLimits", "apply_plan"]
