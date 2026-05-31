"""Built-in tier default — §12 model-tiering as an automatic routing default.

The §12 model-tiering lock: *simple chores → a local LLM; substantial work +
orchestration → the cloud/opencode (executor) baseline.* Until D2 the system
only **reported** a tier verdict — actual simple→local / substantial→executor
routing happened only if a founder hand-seeded :class:`RunRoutingRuleRow` rules.
D2 makes tiering a sensible automatic DEFAULT, so the OS promise ("intervene
only when needed") holds with zero manual rules.

Run-level tier source
---------------------
The frame already carries the complexity verdict — D1 (#212) had the LLM judge
``pipeline`` by COMPLEXITY and SCOPE (a tiny tweak → ``single``; a
multi-component / cross-cutting build → ``design_then_impl``). That IS the
run-level tier signal; D2 reuses it rather than inventing a parallel classifier
(the gateway :class:`~backend.router.classifier.base.Classifier` needs
per-request token counts the run doesn't carry):

* ``pipeline == "single"``          → ``simple``       → the LOCAL account
* ``pipeline == "design_then_impl"`` → ``substantial``  → the EXECUTOR account

Account classes
---------------
* **local** — a native account whose ``provider`` is a host-local inference
  engine (:data:`backend.router.accounts.service.LOCAL_INFERENCE_PROVIDERS`).
* **executor** — an account whose dispatch strategy is the CLI wrapper, i.e.
  the cloud/opencode CLI baseline (claude_code / codex / opencode
  capabilities). Membership is checked via
  :func:`~backend.router.dispatch.strategies.is_executor_account` — the single
  source of truth for the executor predicate after Lift D.

Degrade-loudly invariant (gotcha #200,
``single-active-resolver-degrades-on-new-account-class``): the tier default only
fires when EXACTLY ONE account of the desired class is active. Zero or ambiguous
→ it returns ``None`` so the caller falls back to the legacy single-active
resolver (which raises a founder :class:`Decision` on zero/ambiguous) — never a
silent wrong pick.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

import structlog

from backend.router.accounts.service import LOCAL_INFERENCE_PROVIDERS
from backend.router.dispatch.strategies import is_executor_account

if TYPE_CHECKING:
    from backend.router.accounts.models import ModelAccount
    from backend.router.routing.run_routing.engine import RoutingContext

logger = structlog.get_logger(__name__)

Tier = Literal["simple", "substantial"]


def tier_from_context(ctx: RoutingContext) -> Tier:
    """The §12 tier verdict for a run, from the frame's complexity judgment.

    ``design_then_impl`` is the only substantial verdict; everything else
    (``single``, or an absent / non-build pipeline) is a simple chore."""
    return "substantial" if ctx.pipeline == "design_then_impl" else "simple"


def _local_accounts(accounts: list[ModelAccount]) -> list[ModelAccount]:
    return [a for a in accounts if a.provider in LOCAL_INFERENCE_PROVIDERS]


def _executor_accounts(accounts: list[ModelAccount]) -> list[ModelAccount]:
    return [a for a in accounts if is_executor_account(a)]


def tier_class_accounts(tier: Tier, accounts: list[ModelAccount]) -> list[ModelAccount]:
    """The active accounts of ``tier``'s desired class (local / executor).

    The single source of truth for D2's class membership, reused by D4
    (``select_within_class``) so within-class selection never re-derives the
    class — the class is D2's job, picking within it is D4's."""
    return _local_accounts(accounts) if tier == "simple" else _executor_accounts(accounts)


def select_tier_default_account(tier: Tier, accounts: list[ModelAccount]) -> ModelAccount | None:
    """Pick the account for ``tier`` from the workspace's ACTIVE accounts.

    Returns ``None`` (caller falls back to D4's within-class policy, then
    single-active) when the desired class is absent or has 2+ accounts — this
    function still fires ONLY on EXACTLY ONE (degrade loudly, gotcha #200). The
    2+ case is D4's (``select_within_class``); the caller (``resolve_route``)
    tries it next so 2+ no longer stalls."""
    class_accounts = tier_class_accounts(tier, accounts)
    if len(class_accounts) == 1:
        return class_accounts[0]
    desired = "local" if tier == "simple" else "executor"
    logger.info(
        "routing_tier_default_unavailable",
        tier=tier,
        desired_class=desired,
        candidate_count=len(class_accounts),
        hint="zero or 2+ accounts of the desired class — D4 within-class policy handles 2+",
    )
    return None


__all__ = ["Tier", "select_tier_default_account", "tier_class_accounts", "tier_from_context"]
