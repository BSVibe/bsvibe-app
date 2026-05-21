"""Per-account budget enforcement.

A new ``account_budget_policies`` row captures one cap (daily or monthly)
per ``(workspace_id, account_id)``. A Redis-backed :class:`BudgetTracker`
keeps running cost totals; :class:`BudgetPolicyService` checks the
current spend against a projected request cost and decides whether to
block, warn, or just log.
"""

from __future__ import annotations

from backend.gateway.budget.errors import BudgetExceeded
from backend.gateway.budget.models import (
    AccountBudgetPolicy,
    BudgetEnforcement,
    BudgetScope,
)
from backend.gateway.budget.policy import BudgetCheckResult, BudgetPolicyService
from backend.gateway.budget.repository import BudgetPolicyRepository
from backend.gateway.budget.tracker import BudgetTracker

__all__ = [
    "AccountBudgetPolicy",
    "BudgetCheckResult",
    "BudgetEnforcement",
    "BudgetExceeded",
    "BudgetPolicyRepository",
    "BudgetPolicyService",
    "BudgetScope",
    "BudgetTracker",
]
