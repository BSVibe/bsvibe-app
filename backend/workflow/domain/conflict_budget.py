"""Run-wide budget for merge-conflict re-dispatches.

``MergeWatchRow.conflict_attempts`` resets to 1 on every new conflicting head, so it cannot
bound the TOTAL work a run spends on conflicts: an agent that pushes a fresh head each retry
never hits it. This budget lives on ``run.payload`` instead and only ever accumulates.

Pure and leaf-level: the re-dispatch callback (application) and the after-turn token check
(``token_budget``) both read it.
"""

from __future__ import annotations

from typing import Any

#: ``run.payload`` key holding the conflict-resolution budget state.
CONFLICT_BUDGET_KEY = "merge_conflict_budget"
CONFLICT_REVIEW_DECISION_KIND = "merge_conflict_review"


def read_budget(payload: Any) -> dict[str, Any]:
    raw = payload.get(CONFLICT_BUDGET_KEY) if isinstance(payload, dict) else None
    return dict(raw) if isinstance(raw, dict) else {}


def exhausted_reason(
    budget: dict[str, Any], *, used_tokens: int, max_redispatch: int
) -> str | None:
    """Why the NEXT re-dispatch must not happen, or ``None`` when it may."""
    if max_redispatch > 0 and int(budget.get("redispatches", 0)) >= max_redispatch:
        return "redispatch_limit"
    ceiling = budget.get("token_ceiling")
    if isinstance(ceiling, int) and ceiling > 0 and used_tokens >= ceiling:
        return "token_limit"
    return None


def record_redispatch(
    budget: dict[str, Any],
    *,
    used_tokens: int,
    token_budget: int,
    pr_number: int,
    conflict_paths: list[str],
) -> dict[str, Any]:
    """The budget after one more re-dispatch (first one fixes the token baseline/ceiling)."""
    out = dict(budget)
    if "tokens_at_start" not in out:
        out["tokens_at_start"] = used_tokens
        if token_budget > 0:
            out["token_ceiling"] = used_tokens + token_budget
    out["redispatches"] = int(out.get("redispatches", 0)) + 1
    history = list(out.get("history", []))
    history.append(
        {
            "attempt": out["redispatches"],
            "pr_number": pr_number,
            "conflict_paths": list(conflict_paths),
            "tokens_total": used_tokens,
            "tokens_since_start": used_tokens - int(out["tokens_at_start"]),
        }
    )
    out["history"] = history
    return out


def summarize(budget: dict[str, Any], *, used_tokens: int, reason: str) -> dict[str, Any]:
    """Escalation payload fragment: what was tried so far."""
    return {
        "budget_reason": reason,
        "redispatches": int(budget.get("redispatches", 0)),
        "tokens_spent_on_conflict": used_tokens - int(budget.get("tokens_at_start", used_tokens)),
        "attempts": list(budget.get("history", [])),
    }


__all__ = [
    "CONFLICT_BUDGET_KEY",
    "CONFLICT_REVIEW_DECISION_KIND",
    "exhausted_reason",
    "read_budget",
    "record_redispatch",
    "summarize",
]
