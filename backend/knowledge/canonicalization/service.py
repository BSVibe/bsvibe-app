"""CanonicalizationService — slice-1 minimal facade (Class_Diagram §5).

Slice 1 exposes only ``create_action_draft`` + ``apply_action`` for the two
in-scope action kinds (``create-concept``, ``retag-notes``). All other kinds
raise ``NotImplementedError``. Proposals, decisions, policies, scoring, Safe
Mode, REST, MCP, watcher, and cron come in slices 2-6.

Spec invariants honored from slice 1 (Handoff §0):
- §0.1 vault is SoT — only ``StorageBackend`` writes happen here
- §0.2 path/frontmatter different jobs — kind/role come from path
- §0.5 typed action mutation — every concept/garden mutation is a typed action
- §0.11 single-writer per action_path — apply pipeline holds the lock
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from backend.knowledge._internal.events import EventBus, emit_event
from backend.knowledge.canonicalization import evidence as evidence_module
from backend.knowledge.canonicalization import models, paths
from backend.knowledge.canonicalization.decisions import DecisionMemory
from backend.knowledge.canonicalization.index import CanonicalizationIndex
from backend.knowledge.canonicalization.lock import AsyncIOMutationLock
from backend.knowledge.canonicalization.policies import PolicyResolver
from backend.knowledge.canonicalization.resolver import TagResolver
from backend.knowledge.canonicalization.scoring import CanonicalizationScorer
from backend.knowledge.canonicalization.store import NoteStore

_DEFAULT_EXPIRY = timedelta(days=1)
# Action kinds available for ``create_action_draft`` + ``apply_action``.
# Expands as later slices add kinds (split-concept, deprecate-concept, etc.).
_SUPPORTED_KINDS: frozenset[str] = frozenset(
    {"create-concept", "retag-notes", "merge-concepts", "create-decision"}
)

_ACTION_SCHEMA_VERSIONS: dict[str, str] = {
    "create-concept": "create-concept-v1",
    "retag-notes": "retag-notes-v1",
    "merge-concepts": "merge-concepts-v1",
    "create-decision": "create-decision-v1",
}

_VALID_DECISION_MATURITIES: frozenset[str] = frozenset({"seedling", "budding", "evergreen"})

# Per Handoff §6 — these statuses are eligible for expire_stale rewrite.
# applied / rejected / expired / superseded / failed / blocked are terminal
# from the staleness perspective.
_NON_TERMINAL_ACTION_STATUSES: frozenset[str] = frozenset({"draft", "pending_approval"})


def _aware_dt(dt: datetime) -> datetime:
    """Coerce naive datetimes to UTC so comparisons across naive/aware work."""
    from datetime import UTC

    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


# Default policies for MergeConcepts when params omit them (Handoff §7.2).
_DEFAULT_MERGE_ALIAS_POLICY = {
    "add_merged_ids_as_aliases": True,
    "preserve_existing_aliases": True,
}
_DEFAULT_MERGE_TOMBSTONE_POLICY = {"create_merged_notes": True}
_DEFAULT_MERGE_RETAG_POLICY = {"update_garden_tags": True}


def _title_from_raw(raw_tag: str) -> str:
    """Best-effort human title from a raw tag for auto-applied CreateConcept.

    Used only when ingest auto-creates a new concept. The vault user can
    rename the H1 later through normal markdown editing; the concept id
    (file stem) is the stable handle.
    """
    cleaned = raw_tag.strip()
    if not cleaned:
        return "Untitled Concept"
    return " ".join(part.capitalize() for part in cleaned.replace("_", " ").split())


def _evidence(reason: str, **payload: Any) -> dict[str, Any]:
    """Hard Block evidence envelope. Thin wrapper over evidence.hard_block."""
    return evidence_module.hard_block(reason, **payload)


class CanonicalizationService:
    """Slice 1+2 canonicalization facade.

    Slice 2 adds ``index`` + ``resolver`` for tag resolution and
    ``resolve_and_canonicalize`` for the IngestCompiler hook (Handoff §11).
    The index is kept fresh by invalidating affected paths after each
    successful apply.
    """

    def __init__(
        self,
        store: NoteStore,
        lock: AsyncIOMutationLock,
        *,
        index: CanonicalizationIndex | None = None,
        resolver: TagResolver | None = None,
        decisions: DecisionMemory | None = None,
        policies: PolicyResolver | None = None,
        clock: Callable[[], datetime] | None = None,
        event_bus: EventBus | None = None,
        safe_mode: Callable[[], bool] | None = None,
    ) -> None:
        self._store = store
        self._lock = lock
        self._index = index
        self._resolver = resolver
        self._decisions = decisions
        self._policies = policies
        self._clock = clock or datetime.now
        self._event_bus = event_bus
        # Safe Mode is a callable so a mutable RuntimeConfig flag is read
        # at apply-time, not at service construction (per existing pattern
        # in bsage.core.safe_mode.SafeModeGuard).
        # Canonicalization deliberately does NOT take an ApprovalInterface
        # — typed actions are persisted as ``pending_approval`` and
        # reviewed via the pull-based queue UI. Push-based round-trips
        # silently auto-rejected whenever the operator was offline.
        self._safe_mode = safe_mode or (lambda: False)
        # Slice 4 scorer is also wired here so the apply pipeline can
        # populate action.scoring before Safe Mode permission check.
        if decisions is not None and policies is not None:
            self._scorer: CanonicalizationScorer | None = CanonicalizationScorer(
                decisions=decisions, policies=policies, clock=self._clock
            )
        else:
            self._scorer = None

    # ---------------------------------------------------------------- drafts

    async def create_action_draft(
        self,
        kind: str,
        params: dict[str, Any],
        *,
        slug: str | None = None,
        source_proposal: str | None = None,
        expires_in: timedelta = _DEFAULT_EXPIRY,
    ) -> str:
        if kind not in _SUPPORTED_KINDS:
            msg = f"action kind {kind!r} not yet supported (only {sorted(_SUPPORTED_KINDS)})"
            raise NotImplementedError(msg)

        if slug is None:
            slug = self._derive_slug(kind, params)

        now = self._clock()
        # CreateDecision uses an extra path segment for the decision kind
        # (Handoff §7.8: actions/create-decision/<decision-kind>/<filename>).
        if kind == "create-decision":
            decision_kind = self._infer_decision_kind(params)
            candidate = paths.build_create_decision_action_path(decision_kind, now, slug)
        else:
            candidate = paths.build_action_path(kind, now, slug)
        existing = await self._store.list_existing_action_paths(kind)
        action_path = paths.with_collision_suffix(candidate, existing)

        entry = models.ActionEntry(
            path=action_path,
            kind=kind,
            status="draft",
            action_schema_version=_ACTION_SCHEMA_VERSIONS[kind],
            params=dict(params),
            created_at=now,
            updated_at=now,
            expires_at=now + expires_in,
            source_proposal=source_proposal,
        )
        await self._store.write_action(entry)
        await self._invalidate_index([action_path])
        await self._emit(
            "CANONICALIZATION_ACTION_DRAFTED",
            {
                "schema_version": "canonicalization-event-v1",
                "path": action_path,
                "kind": kind,
                "status": "draft",
                "source_proposal": source_proposal,
            },
        )
        return action_path

    @staticmethod
    def _derive_slug(kind: str, params: dict[str, Any]) -> str:
        if kind == "create-concept":
            concept = params.get("concept", "")
            if not paths.is_valid_concept_id(concept):
                msg = f"create-concept needs valid 'concept' param: {concept!r}"
                raise ValueError(msg)
            return str(concept)
        if kind == "merge-concepts":
            canonical = params.get("canonical", "")
            if not paths.is_valid_concept_id(canonical):
                msg = f"merge-concepts needs valid 'canonical' param: {canonical!r}"
                raise ValueError(msg)
            return str(canonical)
        if kind == "create-decision":
            # Slug derived from decision_path stem (after the timestamp).
            dp = params.get("decision_path", "")
            stem = dp.rsplit("/", 1)[-1].removesuffix(".md")
            # Strip leading YYYYMMDD-HHMMSS- if present
            if (
                len(stem) > 16
                and stem[8] == "-"
                and stem[15] == "-"
                and stem[:8].isdigit()
                and stem[9:15].isdigit()
            ):
                stem = stem[16:]
            if not paths.is_valid_concept_id(stem):
                # Fallback: use first subject if extraction fails
                subjects = params.get("subjects") or []
                stem = "-".join(s for s in subjects if isinstance(s, str)) or "decision"
            return str(stem)
        # retag-notes / other kinds: caller must supply slug
        msg = f"slug required for action kind {kind!r}"
        raise ValueError(msg)

    @staticmethod
    def _infer_decision_kind(params: dict[str, Any]) -> str:
        dp = params.get("decision_path", "")
        if not isinstance(dp, str):
            msg = "create-decision params.decision_path must be a string"
            raise ValueError(msg)
        # decision_path = decisions/<kind>/<filename>
        parts = dp.split("/")
        if len(parts) >= 3 and parts[0] == "decisions":
            kind = parts[1]
            if kind in paths.DECISION_KINDS:
                return kind
        msg = f"create-decision params.decision_path must start with decisions/<kind>/: {dp!r}"
        raise ValueError(msg)

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

    # ---------------------------------------------------------- expiry

    async def expire_stale(self, *, now: datetime | None = None) -> models.ExpireResult:
        """Flip non-terminal actions/proposals past their expires_at to expired.

        Per Handoff §15.3 (canon-expire plugin) and §13 step 3 — staleness
        gating happens before apply. This sweep is safe to call from cron;
        it acquires the per-action lock before mutating each action note.
        Proposals don't have a lock (no apply-via-proposal), so they're
        updated directly through the store.
        """
        result = models.ExpireResult()
        cutoff = _aware_dt(now or self._clock())
        if self._index is None:
            return result

        # Actions
        for entry in await self._index.list_actions():
            if entry.status not in _NON_TERMINAL_ACTION_STATUSES:
                continue
            if _aware_dt(entry.expires_at) > cutoff:
                continue
            previous_status = entry.status
            async with self._lock.guard(entry.path):
                # Re-read under lock — another writer may have already applied/rejected.
                fresh = await self._store.read_action(entry.path)
                if fresh is None or fresh.status not in _NON_TERMINAL_ACTION_STATUSES:
                    continue
                if _aware_dt(fresh.expires_at) > cutoff:
                    continue
                fresh.status = "expired"
                fresh.updated_at = self._clock()
                await self._store.write_action(fresh)
                await self._invalidate_index([fresh.path])
                await self._emit_action_status(fresh, previous_status)
                result.expired_actions.append(fresh.path)

        # Proposals
        for prop in await self._index.list_proposals(status="pending"):
            if _aware_dt(prop.expires_at) > cutoff:
                continue
            previous_status = prop.status
            prop.status = "expired"
            prop.updated_at = self._clock()
            await self._store.write_proposal(prop)
            await self._invalidate_index([prop.path])
            await self._emit(
                "CANONICALIZATION_PROPOSAL_STATUS_CHANGED",
                {
                    "schema_version": "canonicalization-event-v1",
                    "path": prop.path,
                    "kind": prop.kind,
                    "status": "expired",
                    "previous_status": previous_status,
                },
            )
            result.expired_proposals.append(prop.path)

        return result

    async def _apply_locked(
        self,
        action_path: str,
        *,
        actor: str,
        force_approved: bool = False,
    ) -> models.ApplyResult:
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

        # Safe Mode permission policy (Handoff §13 steps 10-11)
        if not force_approved and self._safe_mode():
            return await self._handle_safe_mode(entry, validation, previous_status, actor)

        # Slice 1 has no scoring/Safe Mode/policy — go straight to effects
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
            entry.permission.safe_mode = False
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

    # ---------------------------------------------------------- safe mode

    async def _handle_safe_mode(
        self,
        entry: models.ActionEntry,
        validation: models.ValidationResult,
        previous_status: str,
        actor: str,
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

    # ----------------------------------------------------------- emit helpers

    async def _emit(self, event_name: str, payload: dict[str, Any]) -> None:
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

    # ---------------------------------------------------------------- resolve

    async def resolve_and_canonicalize(
        self,
        raw_tag: str,
        *,
        raw_source: str | None = None,
        auto_apply: bool = True,
    ) -> str | None:
        """Tag → canonical concept id (Handoff §11 ingest write policy).

        Returns the canonical id when the tag resolves (or auto-creates)
        cleanly. Returns None for ``ambiguous`` / ``blocked`` /
        ``pending_candidate``, and for ``new_candidate`` when ``auto_apply``
        is False — in those cases the caller MUST drop the raw tag from
        any final garden ``tags`` list (per spec).
        """
        if self._resolver is None:
            msg = "service has no resolver wired"
            raise RuntimeError(msg)

        result = await self._resolver.resolve(raw_tag)
        normalized = result.concept_id

        if result.status == "resolved":
            return result.concept_id

        if result.status == "pending_candidate":
            if normalized is not None and result.pending_draft is not None:
                await self._append_pending_evidence(
                    draft_path=result.pending_draft,
                    raw_tag=raw_tag,
                    normalized_tag=normalized,
                    raw_source=raw_source,
                )
            return None

        if result.status == "new_candidate" and normalized is not None:
            draft = await self.create_action_draft(
                kind="create-concept",
                params={"concept": normalized, "title": _title_from_raw(raw_tag)},
            )
            if not auto_apply:
                return None
            applied = await self.apply_action(draft, actor="ingest")
            if applied.final_status == "applied":
                return normalized
            return None

        # ambiguous / blocked
        return None

    async def _append_pending_evidence(
        self,
        *,
        draft_path: str,
        raw_tag: str,
        normalized_tag: str,
        raw_source: str | None,
    ) -> None:
        async with self._lock.guard(draft_path):
            entry = await self._store.read_action(draft_path)
            if entry is None or entry.status not in {"draft", "pending_approval"}:
                return
            evidence_item = {
                "kind": "ingest_pending_candidate",
                "schema_version": "ingest-pending-candidate-v1",
                "source": "system",
                "observed_at": self._clock().isoformat(),
                "producer": "canonicalization.ingest-v1",
                "payload": {
                    "raw_tag": raw_tag,
                    "normalized_tag": normalized_tag,
                    "raw_source": raw_source,
                },
            }
            entry.evidence = [*entry.evidence, evidence_item]
            entry.updated_at = self._clock()
            await self._store.write_action(entry)
            await self._invalidate_index([draft_path])

    # ------------------------------------------------------------- proposals

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

    # -------------------------------------------------------------- validate

    async def _validate(self, entry: models.ActionEntry) -> models.ValidationResult:
        result = models.ValidationResult(status="passed", hard_blocks=[])
        if entry.kind == "create-concept":
            await self._validate_create_concept(entry, result)
        elif entry.kind == "retag-notes":
            await self._validate_retag_notes(entry, result)
        elif entry.kind == "merge-concepts":
            await self._validate_merge_concepts(entry, result)
        elif entry.kind == "create-decision":
            await self._validate_create_decision(entry, result)
        else:  # pragma: no cover — guarded by create_action_draft
            result.hard_blocks.append(_evidence("unsupported_action_kind", kind=entry.kind))
        if result.hard_blocks:
            result.status = "failed"
        return result

    async def _validate_create_concept(
        self,
        entry: models.ActionEntry,
        result: models.ValidationResult,
    ) -> None:
        concept = entry.params.get("concept")
        title = entry.params.get("title")
        if not isinstance(concept, str) or not paths.is_valid_concept_id(concept):
            result.hard_blocks.append(_evidence("invalid_concept_id", concept=concept))
            return
        if not isinstance(title, str) or not title.strip():
            result.hard_blocks.append(_evidence("missing_title"))
            return
        if await self._store.concept_exists(concept):
            result.hard_blocks.append(_evidence("concept_already_exists", concept=concept))

    async def _validate_retag_notes(
        self,
        entry: models.ActionEntry,
        result: models.ValidationResult,
    ) -> None:
        changes = entry.params.get("changes")
        if not isinstance(changes, list) or not changes:
            result.hard_blocks.append(_evidence("missing_changes"))
            return
        for change in changes:
            if not isinstance(change, dict):
                result.hard_blocks.append(_evidence("malformed_change_entry"))
                continue
            path = change.get("path")
            if not isinstance(path, str) or not path.startswith("garden/"):
                result.hard_blocks.append(_evidence("retag_outside_garden", path=path))
                continue
            for tag in change.get("add_tags", []) or []:
                if not isinstance(tag, str) or not paths.is_valid_concept_id(tag):
                    result.hard_blocks.append(_evidence("invalid_tag_id", tag=tag))
                    continue
                if not await self._store.concept_exists(tag):
                    result.hard_blocks.append(_evidence("tag_not_active_concept", tag=tag))

    async def _validate_merge_concepts(
        self,
        entry: models.ActionEntry,
        result: models.ValidationResult,
    ) -> None:
        canonical = entry.params.get("canonical")
        merge = entry.params.get("merge")
        if not isinstance(canonical, str) or not paths.is_valid_concept_id(canonical):
            result.hard_blocks.append(_evidence("invalid_canonical_id", canonical=canonical))
            return
        if not isinstance(merge, list) or not merge:
            result.hard_blocks.append(_evidence("missing_merge_list"))
            return
        for old_id in merge:
            if not isinstance(old_id, str) or not paths.is_valid_concept_id(old_id):
                result.hard_blocks.append(_evidence("invalid_merge_id", merge=old_id))
                return
            if old_id == canonical:
                result.hard_blocks.append(_evidence("canonical_in_merge_list", canonical=canonical))
                return
        if not await self._store.concept_exists(canonical):
            result.hard_blocks.append(_evidence("canonical_not_active", canonical=canonical))
            return
        for old_id in merge:
            if not await self._store.concept_exists(old_id):
                result.hard_blocks.append(_evidence("merge_target_not_active", merge=old_id))
                return

        # Cannot-link Hard Block (Handoff §7.2 + §8.5)
        if self._decisions is not None:
            threshold = await self._cannot_link_threshold()
            for old_id in merge:
                strength = await self._decisions.max_cannot_link_strength(
                    (canonical, old_id), now=self._clock()
                )
                if strength >= threshold:
                    result.hard_blocks.append(
                        _evidence(
                            "cannot_link_hard_block",
                            canonical=canonical,
                            merge=old_id,
                            effective_strength=strength,
                            threshold=threshold,
                        )
                    )

    async def _cannot_link_threshold(self, default: float = 0.85) -> float:
        if self._policies is None:
            return default
        try:
            policy = await self._policies.select(kind="merge-auto-apply", scope={})
        except Exception:  # noqa: BLE001 — policy_conflict bubbles up later
            return default
        if policy is None:
            return default
        return float(policy.params.get("hard_blocks", {}).get("cannot_link_threshold", default))

    async def _validate_create_decision(
        self,
        entry: models.ActionEntry,
        result: models.ValidationResult,
    ) -> None:
        params = entry.params
        decision_path = params.get("decision_path")
        if not isinstance(decision_path, str) or not decision_path.startswith("decisions/"):
            result.hard_blocks.append(
                _evidence("invalid_decision_path", decision_path=decision_path)
            )
            return
        parts = decision_path.split("/")
        if len(parts) < 3 or parts[1] not in paths.DECISION_KINDS:
            result.hard_blocks.append(
                _evidence("invalid_decision_kind", decision_path=decision_path)
            )
            return

        subjects = params.get("subjects")
        if not isinstance(subjects, list) or not subjects:
            result.hard_blocks.append(_evidence("missing_subjects"))
            return
        for s in subjects:
            if not isinstance(s, str) or not s.strip():
                result.hard_blocks.append(_evidence("invalid_subject", subject=s))
                return

        base_confidence = params.get("base_confidence")
        if not isinstance(base_confidence, (int, float)) or not (
            0.0 <= float(base_confidence) <= 1.0
        ):
            result.hard_blocks.append(
                _evidence("invalid_base_confidence", base_confidence=base_confidence)
            )
            return

        maturity = params.get("maturity")
        if maturity not in _VALID_DECISION_MATURITIES:
            result.hard_blocks.append(_evidence("invalid_maturity", maturity=maturity))
            return

        decay_profile = params.get("decay_profile")
        if decay_profile is not None and decay_profile not in models.DECAY_PROFILES:
            result.hard_blocks.append(
                _evidence("invalid_decay_profile", decay_profile=decay_profile)
            )
            return

        # Supersede targets must exist
        supersedes = params.get("supersedes") or []
        if not isinstance(supersedes, list):
            result.hard_blocks.append(_evidence("malformed_supersedes"))
            return
        for sup_path in supersedes:
            if not isinstance(sup_path, str):
                result.hard_blocks.append(_evidence("malformed_supersede_entry"))
                return
            existing = await self._store.read_decision(sup_path)
            if existing is None:
                result.hard_blocks.append(_evidence("supersede_target_missing", path=sup_path))

    # -------------------------------------------------------------- effects

    async def _persist_effects(self, entry: models.ActionEntry) -> list[str]:
        if entry.kind == "create-concept":
            return await self._effect_create_concept(entry)
        if entry.kind == "retag-notes":
            return await self._effect_retag_notes(entry)
        if entry.kind == "merge-concepts":
            return await self._effect_merge_concepts(entry)
        if entry.kind == "create-decision":
            return await self._effect_create_decision(entry)
        msg = f"unsupported kind: {entry.kind!r}"  # pragma: no cover
        raise NotImplementedError(msg)

    async def _effect_create_concept(self, entry: models.ActionEntry) -> list[str]:
        concept = entry.params["concept"]
        title = entry.params["title"]
        aliases = list(entry.params.get("aliases") or [])
        initial_body = entry.params.get("initial_body")

        now = self._clock()
        path = paths.active_concept_path(concept)
        await self._store.write_concept(
            models.ConceptEntry(
                concept_id=concept,
                path=path,
                display=title,
                aliases=aliases,
                created_at=now,
                updated_at=now,
                source_action=entry.path,
            ),
            initial_body=initial_body,
        )
        return [path]

    async def _effect_retag_notes(self, entry: models.ActionEntry) -> list[str]:
        affected: list[str] = []
        for change in entry.params["changes"]:
            path = change["path"]
            current = await self._store.read_garden_tags(path)
            remove = set(change.get("remove_tags") or [])
            add = list(change.get("add_tags") or [])
            kept = [t for t in current if t not in remove]
            merged: list[str] = []
            seen: set[str] = set()
            for tag in [*kept, *add]:
                if tag not in seen:
                    seen.add(tag)
                    merged.append(tag)
            merged.sort()
            await self._store.set_garden_tags(path, merged)
            affected.append(path)
        return affected

    async def _effect_merge_concepts(self, entry: models.ActionEntry) -> list[str]:
        params = entry.params
        canonical_id: str = params["canonical"]
        merge_ids: list[str] = list(params["merge"])
        alias_policy = {**_DEFAULT_MERGE_ALIAS_POLICY, **(params.get("alias_policy") or {})}
        tombstone_policy = {
            **_DEFAULT_MERGE_TOMBSTONE_POLICY,
            **(params.get("tombstone_policy") or {}),
        }
        retag_policy = {
            **_DEFAULT_MERGE_RETAG_POLICY,
            **(params.get("retag_policy") or {}),
        }

        affected: list[str] = []
        now = self._clock()

        canonical = await self._store.read_concept(canonical_id)
        if canonical is None:
            msg = f"canonical concept disappeared mid-apply: {canonical_id!r}"
            raise RuntimeError(msg)

        # 1. Read all merge sources before touching anything
        sources: dict[str, models.ConceptEntry] = {}
        for old_id in merge_ids:
            entry_old = await self._store.read_concept(old_id)
            if entry_old is None:
                msg = f"merge source disappeared mid-apply: {old_id!r}"
                raise RuntimeError(msg)
            sources[old_id] = entry_old

        # 2. Update canonical aliases
        new_aliases: list[str] = (
            list(canonical.aliases) if alias_policy.get("preserve_existing_aliases", True) else []
        )
        if alias_policy.get("add_merged_ids_as_aliases", True):
            for old_id, src in sources.items():
                if old_id not in new_aliases:
                    new_aliases.append(old_id)
                for alias in src.aliases:
                    if alias not in new_aliases:
                        new_aliases.append(alias)
        canonical.aliases = new_aliases
        canonical.updated_at = now
        await self._store.write_concept(canonical)
        affected.append(canonical.path)

        # 3. Tombstones + delete old active notes
        for old_id, src in sources.items():
            old_active_path = f"concepts/active/{old_id}.md"
            await self._store.delete_active_concept(old_id)
            affected.append(old_active_path)
            if tombstone_policy.get("create_merged_notes", True):
                tombstone_path = await self._store.write_tombstone(
                    old_id=old_id,
                    merged_into=canonical_id,
                    merged_at=now,
                    source_action=entry.path,
                    display=src.display or old_id,
                )
                affected.append(tombstone_path)

        # 4. Garden retag
        if retag_policy.get("update_garden_tags", True):
            merge_set = set(merge_ids)
            for garden_path in await self._store.list_garden_paths():
                tags = await self._store.read_garden_tags(garden_path)
                if not any(t in merge_set for t in tags):
                    continue
                rewritten: list[str] = []
                seen: set[str] = set()
                for tag in tags:
                    new = canonical_id if tag in merge_set else tag
                    if new not in seen:
                        seen.add(new)
                        rewritten.append(new)
                await self._store.set_garden_tags(garden_path, rewritten)
                affected.append(garden_path)

        return affected

    async def _effect_create_decision(self, entry: models.ActionEntry) -> list[str]:
        params = entry.params
        decision_path: str = params["decision_path"]
        decision_kind = decision_path.split("/")[1]
        subjects = tuple(s for s in params["subjects"] if isinstance(s, str))
        base_confidence = float(params["base_confidence"])
        maturity = params["maturity"]
        decay_profile = params.get("decay_profile") or (
            "definitional" if decision_kind == "cannot-link" else "semantic"
        )
        decay_halflife_days = params.get("decay_halflife_days")
        supersedes = list(params.get("supersedes") or [])

        now = self._clock()
        affected: list[str] = []

        # 1. Write the new decision note
        decision = models.DecisionEntry(
            path=decision_path,
            kind=decision_kind,
            status="active",
            maturity=maturity,
            decision_schema_version=f"{decision_kind}-v1",
            subjects=subjects,
            base_confidence=base_confidence,
            last_confirmed_at=now,
            decay_profile=decay_profile,
            decay_halflife_days=decay_halflife_days,
            valid_from=now,
            created_at=now,
            updated_at=now,
            policy_profile_path=params.get("policy_profile_path"),
            source_proposal=params.get("source_proposal"),
            source_action=entry.path,
            supersedes=supersedes,
        )
        await self._store.write_decision(decision)
        affected.append(decision_path)

        # 2. Mark each superseded decision (Handoff §7.8 — both paths in affected)
        for sup_path in supersedes:
            existing = await self._store.read_decision(sup_path)
            if existing is None:
                continue
            existing.status = "superseded"
            existing.superseded_by = decision_path
            existing.updated_at = now
            await self._store.write_decision(existing)
            affected.append(sup_path)

        return affected
