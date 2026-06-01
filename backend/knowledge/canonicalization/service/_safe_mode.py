"""Safe Mode gating — Handoff §13 steps 10-11.

Mixin extracted from the original ``service.py`` god-file per v8 §17.4.
Holds the Safe Mode permission policy (auto-apply vs. queue) and the
``pending_approval`` persistence path.

Mixin contract:
- Depends on ``self._store`` (write_action)
- Depends on ``self._policies`` (merge-auto-apply policy)
- Depends on ``self._scorer`` (scoring availability gate)
- Depends on ``self._safe_mode()`` (RuntimeConfig flag)
- Depends on ``self._clock`` (status timestamps)
- Depends on ``self._invalidate_index`` + ``self._emit_action_status``
  (provided by the apply pipeline mixin)

Mutex note: ``_handle_safe_mode`` runs INSIDE the per-action_path lock
held by ``_apply_locked``. It MUST NOT re-acquire ``self._lock`` —
that would deadlock under a non-reentrant ``AsyncIOMutationLock``
(see skill ``asyncio-lock-non-reentrant-deadlock``).
"""

from __future__ import annotations

from backend.knowledge.canonicalization import models
from backend.knowledge.canonicalization.policies import PolicyConflictError
from backend.knowledge.canonicalization.service._base import _ServiceBase


class _SafeModeMixin(_ServiceBase):
    """Safe Mode permission gate + pending_approval persistence."""

    async def _safe_mode_permits_auto_apply(self, entry: models.ActionEntry) -> bool:
        """Decide whether a Safe-Mode action is low-risk enough to auto-apply.

        Consults the active ``merge-auto-apply`` policy's ``safe_mode_on`` block
        (the risk model the scorer already feeds) — auto-apply requires ALL of:

        * a risk signal exists (scorer + policy wired, scoring completed); when
          no signal is available we fall back to the conservative queue;
        * the action kind is in ``auto_action_kinds`` (decisions/policies are
          never auto-applied — they always require approval under Safe Mode);
        * ``stability_score >= auto_apply_threshold`` — each deterministic risk
          reason (knowledge conflict, oversized blast radius) drops the score,
          so a conflicting merge falls below the bar and is queued.

        Returns ``False`` (→ queue for approval) on any missing signal or any
        risk — Safe Mode stays strict about *genuine* risk while letting routine
        knowledge accrual settle automatically.
        """
        scoring = entry.scoring
        if self._scorer is None or self._policies is None or scoring is None:
            return False
        if scoring.status != "completed":
            return False
        try:
            policy = await self._policies.select(kind="merge-auto-apply", scope={})
        except PolicyConflictError:
            return False
        if policy is None:
            return False
        safe_on = policy.params.get("safe_mode_on", {})
        if entry.kind not in safe_on.get("auto_action_kinds", []):
            return False
        threshold = float(safe_on.get("auto_apply_threshold", 1.0))
        score = scoring.stability_score
        return score is not None and score >= threshold

    async def _handle_safe_mode(
        self,
        entry: models.ActionEntry,
        validation: models.ValidationResult,
        previous_status: str,
        actor: str,  # noqa: ARG002 — kept for §13 signature stability
    ) -> models.ApplyResult:
        """Safe Mode ON — persist as ``pending_approval`` for the queue.

        Canonicalization typed actions are durable: the action file lives
        in the vault, so we never need a synchronous approver round-trip.
        Approve via :meth:`approve_action` (called by the queue UI's
        Approve & apply button); reject via :meth:`reject_action`.
        """
        now = self._clock()
        entry.validation = validation
        entry.status = "pending_approval"
        entry.permission.safe_mode = True
        entry.permission.decision = "require_approval"
        entry.updated_at = now
        await self._store.write_action(entry)
        await self._invalidate_index([entry.path])
        await self._emit_action_status(entry, previous_status)
        return models.ApplyResult(
            action_path=entry.path,
            final_status="pending_approval",
            affected_paths=[entry.path],
        )
