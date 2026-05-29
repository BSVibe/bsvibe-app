"""D4 — deterministic within-class selection among 2+ active accounts.

D2 (``tier_default``) picks the account **class** for a run (simple→local
provider, substantial→executor provider) but deliberately returns ``None`` when
the desired class has ZERO **or 2+** active accounts — degrading to the legacy
single-active resolver (gotcha #200,
``single-active-resolver-degrades-on-new-account-class``). The legacy resolver
then raises an ``ambiguous_model_account`` founder :class:`Decision` for the 2+
case — a **stall**: a workspace that registers two equivalent executor workers
(or two local engines) can no longer route a run without founder intervention.

D4 closes that gap: **the class is D2's job; picking WITHIN the class is D4's.**
Given 2+ eligible same-class active accounts, this module returns a *specific*
account by a deterministic, glass-box policy — never a stall.

Policy signals (real :class:`~backend.accounts.models.ModelAccount` fields only)
-------------------------------------------------------------------------------
The recommended ordering is *budget headroom → health/last-success → explicit
priority → stable tiebreak*. The first two signals have **no backing field** on
``ModelAccount`` (budget is tracked per ``(workspace_id, account_id)`` in the
gateway, NOT per model-account, and there is no health/last-success column — see
the PR body's signal-absent note). So D4 uses what exists, with the same intent:

1. **Explicit priority** — ``extra_params["routing_priority"]`` (an integer in
   the model's existing freeform JSON; NO new schema). HIGHER wins, mirroring a
   founder's "prefer this account" knob. Absent / non-numeric → priority 0.
2. **Stable tiebreak** — oldest ``created_at`` first, then ascending ``id``. Two
   accounts at equal priority always resolve to the SAME one, deterministically,
   so a run never flaps between equivalent workers.

The choice is logged (``routing_multi_account_selected``) with the candidate
count, the winner, and WHY (its priority) — glass-box, never a silent pick.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from backend.accounts.models import ModelAccount

logger = structlog.get_logger(__name__)

#: ``extra_params`` key carrying a founder's explicit per-account preference.
#: Higher wins. Lives in the model's existing freeform JSON — no schema change.
ROUTING_PRIORITY_KEY = "routing_priority"


def _priority(account: ModelAccount) -> int:
    """The account's explicit routing priority (HIGHER wins).

    Read from ``extra_params[ROUTING_PRIORITY_KEY]``; a missing / non-integer
    value is priority 0 (the unconfigured default), so an un-tuned workspace
    still resolves deterministically by the stable tiebreak below."""
    raw = (account.extra_params or {}).get(ROUTING_PRIORITY_KEY)
    if isinstance(raw, bool):  # bool is an int subclass — exclude explicitly
        return 0
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str):
        try:
            return int(raw)
        except ValueError:
            return 0
    return 0


def _sort_key(account: ModelAccount) -> tuple[int, float, str]:
    """Deterministic ordering key. Sorted ASCENDING, so we negate priority to
    make HIGHER priority sort FIRST, then oldest ``created_at``, then ``id``."""
    created = account.created_at.timestamp() if account.created_at is not None else 0.0
    return (-_priority(account), created, str(account.id))


def select_within_class(candidates: list[ModelAccount]) -> ModelAccount | None:
    """Pick ONE account from 2+ eligible same-class active accounts.

    The caller (``resolve_route``) has already narrowed ``candidates`` to the
    active accounts of the tier's desired class (D2 picked the class). This
    applies the within-class policy:

    * 0 candidates → ``None`` (nothing to pick; caller falls through).
    * exactly 1 → that account (the policy is a no-op for the single case, which
      D2 already handles — kept here so the function is total).
    * 2+ → the deterministic winner: highest ``routing_priority``, tiebroken by
      oldest ``created_at`` then ascending ``id``. NEVER a stall.
    """
    if not candidates:
        return None
    winner = sorted(candidates, key=_sort_key)[0]
    if len(candidates) > 1:
        logger.info(
            "routing_multi_account_selected",
            candidate_count=len(candidates),
            winner_id=str(winner.id),
            winner_model=winner.litellm_model,
            winner_priority=_priority(winner),
            reason="highest routing_priority, tiebroken by created_at then id",
        )
    return winner


__all__ = ["ROUTING_PRIORITY_KEY", "select_within_class"]
