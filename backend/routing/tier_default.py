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
(the gateway :class:`~backend.gateway.classifier.base.Classifier` needs
per-request token counts the run doesn't carry):

* ``pipeline == "single"``          → ``simple``       → the LOCAL account
* ``pipeline == "design_then_impl"`` → ``substantial``  → the EXECUTOR account

Account classes
---------------
* **local** — a native account whose ``provider`` is a host-local inference
  engine (:data:`backend.accounts.service.LOCAL_INFERENCE_PROVIDERS`).
* **executor** — a ``provider == "executor"`` account, i.e. the cloud/opencode
  CLI baseline (claude_code / codex / opencode capabilities).

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

from backend.accounts.service import LOCAL_INFERENCE_PROVIDERS

if TYPE_CHECKING:
    from backend.accounts.models import ModelAccount
    from backend.routing.engine import RoutingContext

logger = structlog.get_logger(__name__)

Tier = Literal["simple", "substantial"]

# Provider marking the executor (cloud/opencode CLI) account class.
_EXECUTOR_PROVIDER = "executor"


def tier_from_context(ctx: RoutingContext) -> Tier:
    """The §12 tier verdict for a run, from the frame's complexity judgment.

    ``design_then_impl`` is the only substantial verdict; everything else
    (``single``, or an absent / non-build pipeline) is a simple chore."""
    return "substantial" if ctx.pipeline == "design_then_impl" else "simple"


def _local_accounts(accounts: list[ModelAccount]) -> list[ModelAccount]:
    return [a for a in accounts if a.provider in LOCAL_INFERENCE_PROVIDERS]


def _executor_accounts(accounts: list[ModelAccount]) -> list[ModelAccount]:
    return [a for a in accounts if a.provider == _EXECUTOR_PROVIDER]


def select_tier_default_account(tier: Tier, accounts: list[ModelAccount]) -> ModelAccount | None:
    """Pick the account for ``tier`` from the workspace's ACTIVE accounts.

    Returns ``None`` (caller falls back to single-active) when the desired class
    is absent or ambiguous — degrade loudly, never guess (gotcha #200)."""
    class_accounts = _local_accounts(accounts) if tier == "simple" else _executor_accounts(accounts)
    if len(class_accounts) == 1:
        return class_accounts[0]
    desired = "local" if tier == "simple" else "executor"
    logger.info(
        "routing_tier_default_unavailable",
        tier=tier,
        desired_class=desired,
        candidate_count=len(class_accounts),
        hint="zero or ambiguous accounts of the desired class — degrading to single-active",
    )
    return None


__all__ = ["Tier", "select_tier_default_account", "tier_from_context"]
