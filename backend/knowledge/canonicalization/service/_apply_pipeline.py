"""Action apply pipeline — Handoff §13 (the full step ladder).

Mixin extracted from the original ``service.py`` god-file per v8 §17.4.
Holds the apply / approve / reject entrypoints, the locked apply pipeline
core, the index-invalidation helper, the blocked-persist short-circuit,
and the event-emit helpers.

Mixin contract:
- Depends on ``self._store`` (read_action, write_action)
- Depends on ``self._lock`` (per-action_path mutex — single-writer invariant)
- Depends on ``self._index`` (invalidate)
- Depends on ``self._event_bus`` (emit_event)
- Depends on ``self._scorer``, ``self._safe_mode`` (Safe Mode gate inputs)
- Depends on ``self._clock`` (status timestamps)
- Depends on ``self._validate`` (provided by ``_ValidatorsMixin``)
- Depends on ``self._persist_effects`` (provided by ``_EffectsMixin``)
- Depends on ``self._safe_mode_permits_auto_apply`` + ``self._handle_safe_mode``
  (provided by ``_SafeModeMixin``)

CRITICAL invariants (BSage canon spec):
1. **action_path mutex** — every mutation of an action note happens
   inside ``self._lock.guard(action_path)``. The guard is acquired in
   ``apply_action``/``approve_action``/``reject_action`` and the entire
   pipeline runs inside that one ``async with`` block. The pipeline
   helpers (``_apply_locked``, ``_persist_blocked``) NEVER re-acquire
   the lock — that would deadlock under non-reentrant ``AsyncIOMutationLock``.
2. **validate-before-persist** — ``_validate`` runs BEFORE
   ``_persist_effects``. Reorder = silent corruption.
3. **idempotent re-apply** — applied/rejected status is terminal;
   re-entering the pipeline returns the existing result without
   re-running effects.
"""

from __future__ import annotations

from typing import Any

from backend.knowledge.canonicalization import models
from backend.knowledge.canonicalization.service._base import _ServiceBase


class _ApplyPipelineMixin(_ServiceBase):
    """Apply/approve/reject + the locked pipeline core + emit helpers."""

    # ----------------------------------------------------------------- apply

    async def apply_action(self, action_path: str, *, actor: str) -> models.ApplyResult:
        async with self._lock.guard(action_path):
            return await self._apply_locked(action_path, actor=actor)

    async def approve_action(self, action_path: str, *, actor: str) -> models.ApplyResult:
        """Resume a pending_approval action and run the apply pipeline.

        Per Handoff §13: approval MUST rerun freshness, validation, scoring,
        and policy checks before applying. If the action became stale or
        newly blocked while waiting, approval fails with ``expired`` or
        ``blocked``.
        """
        async with self._lock.guard(action_path):
            entry = await self._store.read_action(action_path)
            if entry is None:
                return models.ApplyResult(
                    action_path=action_path,
                    final_status="failed",
                    affected_paths=[],
                    error="action_note_not_found",
                )
            if entry.status not in {"pending_approval", "draft"}:
                return models.ApplyResult(
                    action_path=action_path,
                    final_status=entry.status,
                    affected_paths=list(entry.affected_paths),
                )
            return await self._apply_locked(action_path, actor=actor, force_approved=True)

    async def reject_action(
        self, action_path: str, *, actor: str, reason: str | None = None
    ) -> None:
        """Reject a pending_approval action without applying it (§13)."""
        async with self._lock.guard(action_path):
            entry = await self._store.read_action(action_path)
            if entry is None:
                msg = f"action not found: {action_path!r}"
                raise FileNotFoundError(msg)
            if entry.status not in {"pending_approval", "draft"}:
                msg = f"action not approvable (status={entry.status!r})"
                raise ValueError(msg)
            previous_status = entry.status
            now = self._clock()
            entry.status = "rejected"
            entry.permission.safe_mode = self._safe_mode()
            entry.permission.decision = "rejected"
            entry.permission.actor = actor
            entry.permission.decided_at = now
            entry.execution.error = reason
            entry.updated_at = now
            await self._store.write_action(entry)
            await self._invalidate_index([action_path])
            await self._emit_action_status(entry, previous_status)

    async def _apply_locked(
        self,
        action_path: str,
        *,
        actor: str,
        force_approved: bool = False,
    ) -> models.ApplyResult:
        # NOTE: caller already holds ``self._lock.guard(action_path)``.
        # Do NOT re-acquire the lock here — non-reentrant deadlock risk.
        entry = await self._store.read_action(action_path)
        if entry is None:
            return models.ApplyResult(
                action_path=action_path,
                final_status="failed",
                affected_paths=[],
                error="action_note_not_found",
            )

        previous_status = entry.status

        # Idempotency: terminal statuses are no-op
        if entry.status == "applied":
            return models.ApplyResult(
                action_path=action_path,
                final_status="applied",
                affected_paths=list(entry.affected_paths),
            )
        if entry.status == "rejected":
            return models.ApplyResult(
                action_path=action_path,
                final_status="rejected",
                affected_paths=[action_path],
            )

        # Validate (deterministic Hard Blocks, Handoff §13)
        validation = await self._validate(entry)
        if validation.hard_blocks:
            return await self._persist_blocked(entry, validation, previous_status)

        # Score (Handoff §13 step 9). Safe even when scorer not wired.
        if self._scorer is not None:
            entry.scoring = await self._scorer.score(entry)

        # Safe Mode permission policy (Handoff §13 steps 10-11). Safe Mode is
        # NOT "all changes are risky" — it gates GENUINE risk. A low-risk action
        # (allow-listed kind, score at/above the policy's auto_apply_threshold,
        # no deterministic risk reason) auto-applies even under Safe Mode; only
        # risky actions (knowledge conflicts, oversized blast radius, or kinds
        # outside the allow-list) are queued for founder approval.
        if (
            not force_approved
            and self._safe_mode()
            and not await self._safe_mode_permits_auto_apply(entry)
        ):
            return await self._handle_safe_mode(entry, validation, previous_status, actor)

        # validate-before-persist: validation passed above, now run effects.
        try:
            affected = await self._persist_effects(entry)
        except Exception as exc:  # noqa: BLE001 — runtime failure logged into action
            entry.execution.status = "failed"
            entry.execution.error = repr(exc)
            entry.status = "failed"
            entry.updated_at = self._clock()
            await self._store.write_action(entry)
            await self._emit_action_status(entry, previous_status)
            return models.ApplyResult(
                action_path=action_path,
                final_status="failed",
                affected_paths=[],
                error=repr(exc),
            )

        now = self._clock()
        entry.validation = validation
        entry.execution.status = "ok"
        entry.execution.applied_at = now
        entry.execution.error = None
        # Distinguish reviewer-approved (Safe Mode was on, queue picked it
        # up) from straight auto-apply (Safe Mode was off). The persisted
        # permission record is the audit trail for §13.
        if force_approved:
            entry.permission.safe_mode = True
            entry.permission.decision = "approved"
        else:
            # Record whether Safe Mode was on when this auto-applied — a low-risk
            # action can auto-apply *under* Safe Mode (decision stays auto_apply,
            # but the audit trail shows Safe Mode was in effect).
            entry.permission.safe_mode = self._safe_mode()
            entry.permission.decision = "auto_apply"
        entry.permission.actor = actor
        entry.permission.decided_at = now
        entry.affected_paths = sorted({action_path, *affected})
        entry.status = "applied"
        entry.updated_at = now
        await self._store.write_action(entry)
        await self._invalidate_index(entry.affected_paths)
        await self._emit_action_status(entry, previous_status)
        await self._emit_action_applied(entry)
        await self._emit_kind_specific_applied(entry)

        return models.ApplyResult(
            action_path=action_path,
            final_status="applied",
            affected_paths=list(entry.affected_paths),
        )

    async def _invalidate_index(self, paths_: list[str]) -> None:
        if self._index is None:
            return
        for p in paths_:
            await self._index.invalidate(p)

    async def _persist_blocked(
        self,
        entry: models.ActionEntry,
        validation: models.ValidationResult,
        previous_status: str = "draft",
    ) -> models.ApplyResult:
        now = self._clock()
        entry.validation = validation
        entry.status = "blocked"
        entry.updated_at = now
        await self._store.write_action(entry)
        await self._invalidate_index([entry.path])
        await self._emit_action_status(entry, previous_status)
        return models.ApplyResult(
            action_path=entry.path,
            final_status="blocked",
            affected_paths=[entry.path],
            error="hard_block",
        )

    # ----------------------------------------------------------- emit helpers

    async def _emit(self, event_name: str, payload: dict[str, Any]) -> None:
        from backend.knowledge._internal.events import emit_event

        if self._event_bus is None:
            return
        await emit_event(self._event_bus, event_name, payload)

    async def _emit_action_status(self, entry: models.ActionEntry, previous_status: str) -> None:
        if previous_status == entry.status:
            return
        await self._emit(
            "CANONICALIZATION_ACTION_STATUS_CHANGED",
            {
                "schema_version": "canonicalization-event-v1",
                "path": entry.path,
                "kind": entry.kind,
                "status": entry.status,
                "previous_status": previous_status,
            },
        )

    async def _emit_action_applied(self, entry: models.ActionEntry) -> None:
        await self._emit(
            "CANONICALIZATION_ACTION_APPLIED",
            {
                "schema_version": "canonicalization-event-v1",
                "action_path": entry.path,
                "kind": entry.kind,
                "status": "applied",
                "affected_paths": list(entry.affected_paths),
                "source_proposal": entry.source_proposal,
                "safe_mode": bool(entry.permission.safe_mode),
                "actor": entry.permission.actor,
            },
        )

    async def _emit_kind_specific_applied(self, entry: models.ActionEntry) -> None:
        """For decision/policy creates, emit dedicated event types in addition
        to the generic ACTION_APPLIED (Handoff §14)."""
        if entry.kind != "create-decision":
            return
        params = entry.params
        decision_path = params.get("decision_path")
        decision_kind = (
            decision_path.split("/")[1]
            if isinstance(decision_path, str) and decision_path.startswith("decisions/")
            else None
        )
        await self._emit(
            "CANONICALIZATION_DECISION_CREATED",
            {
                "schema_version": "canonicalization-event-v1",
                "path": decision_path,
                "kind": decision_kind,
                "subjects": list(params.get("subjects") or []),
                "base_confidence": params.get("base_confidence"),
                "source_action": entry.path,
            },
        )
        # Each superseded decision also flips status — emit one event per
        for sup in params.get("supersedes") or []:
            await self._emit(
                "CANONICALIZATION_DECISION_SUPERSEDED",
                {
                    "schema_version": "canonicalization-event-v1",
                    "path": sup,
                    "superseded_by": decision_path,
                    "source_action": entry.path,
                },
            )
