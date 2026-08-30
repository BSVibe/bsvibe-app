"""Proposal lifecycle + staleness sweep — Handoff §5 / §15.3.

Mixin extracted from the original ``service.py`` god-file per v8 §17.4.
Holds proposal accept/reject and the cron-driven expire sweep for both
actions and proposals.

Mixin contract:
- Depends on ``self._store`` (read_proposal, write_proposal)
- Depends on ``self._index`` (list_actions, list_proposals)
- Depends on ``self._lock`` (per-action_path mutex)
- Depends on ``self._clock`` (status timestamps)
- Depends on ``self.apply_action`` (chained per-action apply during accept)
- Depends on ``self._invalidate_index`` + ``self._emit`` + ``self._emit_action_status``
  (provided by the apply pipeline mixin)

Mutex note: ``accept_proposal`` does NOT acquire the lock itself; it
delegates to ``apply_action`` which takes the lock per individual action
draft, so the lock is NEVER held across drafts.
"""

from __future__ import annotations

from datetime import datetime

from backend.knowledge.canonicalization import models
from backend.knowledge.canonicalization.service._base import _ServiceBase


def _aware_dt(dt: datetime) -> datetime:
    """Coerce naive datetimes to UTC so comparisons across naive/aware work."""
    from datetime import UTC

    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


class _ProposalLifecycleMixin(_ServiceBase):
    """Proposal accept/reject + the cron expire sweep."""

    async def accept_proposal(self, proposal_path: str, *, actor: str) -> list[models.ApplyResult]:
        """Accept a pending proposal — apply every linked action draft.

        Per Handoff §5: proposal apply is impossible. Only actions apply.
        ``accept_proposal`` is a convenience wrapper that applies the
        proposal's ``action_drafts`` in order, records resulting paths in
        ``result_actions``, and marks the proposal ``accepted`` only when
        every linked action ends in ``applied``.
        """
        proposal = await self._store.read_proposal(proposal_path)
        if proposal is None:
            msg = f"proposal not found: {proposal_path!r}"
            raise FileNotFoundError(msg)
        if proposal.status != "pending":
            msg = f"proposal not pending (status={proposal.status!r})"
            raise ValueError(msg)

        results: list[models.ApplyResult] = []
        for action_path in proposal.action_drafts:
            results.append(await self.apply_action(action_path, actor=actor))

        all_applied = all(r.final_status == "applied" for r in results)
        now = self._clock()
        previous_status = "pending"
        proposal.status = "accepted" if all_applied else "pending"
        proposal.updated_at = now
        proposal.result_actions = list(
            dict.fromkeys([*proposal.result_actions, *(r.action_path for r in results)])
        )
        await self._store.write_proposal(proposal)
        await self._invalidate_index([proposal_path])
        if proposal.status != previous_status:
            await self._emit(
                "CANONICALIZATION_PROPOSAL_STATUS_CHANGED",
                {
                    "schema_version": "canonicalization-event-v1",
                    "path": proposal_path,
                    "kind": proposal.kind,
                    "status": proposal.status,
                    "previous_status": previous_status,
                    "actor": actor,
                    "result_actions": list(proposal.result_actions),
                },
            )
        return results

    async def reject_proposal(
        self, proposal_path: str, *, actor: str, reason: str | None = None
    ) -> None:
        """Mark a proposal rejected. Linked drafts are not auto-rejected."""
        proposal = await self._store.read_proposal(proposal_path)
        if proposal is None:
            msg = f"proposal not found: {proposal_path!r}"
            raise FileNotFoundError(msg)
        if proposal.status != "pending":
            msg = f"proposal not pending (status={proposal.status!r})"
            raise ValueError(msg)
        now = self._clock()
        proposal.status = "rejected"
        proposal.updated_at = now
        # Audit trail evidence — actor + reason
        proposal.evidence = [
            *proposal.evidence,
            {
                "kind": "human_review",
                "schema_version": "human-review-v1",
                "source": "human",
                "observed_at": now.isoformat(),
                "producer": f"human-{actor}",
                "payload": {"decision": "rejected", "reason": reason},
            },
        ]
        await self._store.write_proposal(proposal)
        await self._invalidate_index([proposal_path])
        await self._emit(
            "CANONICALIZATION_PROPOSAL_STATUS_CHANGED",
            {
                "schema_version": "canonicalization-event-v1",
                "path": proposal_path,
                "kind": proposal.kind,
                "status": "rejected",
                "previous_status": "pending",
                "actor": actor,
                "reason": reason,
            },
        )
