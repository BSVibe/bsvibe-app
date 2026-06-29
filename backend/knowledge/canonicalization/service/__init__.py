"""CanonicalizationService facade — composes 5 concern-specific mixins.

Per v8 §17.4 (Lift L2) the original 1158-LOC ``service.py`` god-file is split
into a package of five concern modules:

- ``_validators.py`` — per-kind Hard Block validation (§13 step 4)
- ``_effects.py`` — per-kind persistent mutations (§13 step 12)
- ``_safe_mode.py`` — auto-apply vs. pending_approval gate (§13 steps 10-11)
- ``_proposal_lifecycle.py`` — proposal accept/reject + expire sweep
- ``_apply_pipeline.py`` — apply/approve/reject + locked pipeline + emit

This facade module owns:
- The dataclass-shaped construction surface (``__init__``)
- The drafts API (``create_action_draft`` + slug derivation)
- The ingest resolve API (``resolve_and_canonicalize``)
- The module-level constants formerly at the top of ``service.py``

Public import path is preserved:

    from backend.knowledge.canonicalization.service import CanonicalizationService

Spec invariants honored (Handoff §0):
- §0.1 vault is SoT — only ``StorageBackend`` writes happen here
- §0.2 path/frontmatter different jobs — kind/role come from path
- §0.5 typed action mutation — every concept/garden mutation is a typed action
- §0.11 single-writer per action_path — apply pipeline holds the lock

CRITICAL: the per-action_path mutex is acquired in ``_ApplyPipelineMixin``
and held across the entire validate → score → safe-mode → persist sequence.
Every helper that runs inside that block is in the SAME function body
(or directly awaited by ``_apply_locked``). No async-with split spans
files — see ``tests/knowledge/canonicalization/test_service_package_smoke.py``.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from backend.knowledge._internal.events import EventBus
from backend.knowledge.canonicalization import models, paths
from backend.knowledge.canonicalization.decisions import DecisionMemory
from backend.knowledge.canonicalization.index import CanonicalizationIndex
from backend.knowledge.canonicalization.lock import AsyncIOMutationLock
from backend.knowledge.canonicalization.policies import PolicyResolver
from backend.knowledge.canonicalization.resolver import TagResolver
from backend.knowledge.canonicalization.scoring import CanonicalizationScorer
from backend.knowledge.canonicalization.service._apply_pipeline import _ApplyPipelineMixin
from backend.knowledge.canonicalization.service._effects import _EffectsMixin
from backend.knowledge.canonicalization.service._proposal_lifecycle import _ProposalLifecycleMixin
from backend.knowledge.canonicalization.service._safe_mode import _SafeModeMixin
from backend.knowledge.canonicalization.service._validators import _ValidatorsMixin
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


class CanonicalizationService(
    _ApplyPipelineMixin,
    _ProposalLifecycleMixin,
    _SafeModeMixin,
    _ValidatorsMixin,
    _EffectsMixin,
):
    """Slice 1+2 canonicalization facade.

    Slice 2 adds ``index`` + ``resolver`` for tag resolution and
    ``resolve_and_canonicalize`` for the IngestCompiler hook (Handoff §11).
    The index is kept fresh by invalidating affected paths after each
    successful apply.

    The mixin order above defines the MRO. ``_ApplyPipelineMixin`` comes
    first because it owns the lock and the emit helpers that the lifecycle
    + safe-mode mixins call into; downstream mixins resolve those via MRO.
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

    # ---------------------------------------------------------------- resolve

    async def resolve_and_canonicalize(
        self,
        raw_tag: str,
        *,
        raw_source: str | None = None,
        auto_apply: bool = True,
        note_type: str | None = None,
        initial_body: str | None = None,
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
            # Lift E26 — record the dominant seedling type in the action so
            # ``_effect_create_concept`` can stamp it onto the concept.
            params: dict[str, Any] = {
                "concept": normalized,
                "title": _title_from_raw(raw_tag),
            }
            if note_type:
                params["type"] = note_type
            # KG Lift 1 — the promoter passes a synthesized hub body (member
            # seedling [[links]] + excerpts) so the concept is substantive, not
            # an empty ``# Title`` shell. ``_effect_create_concept`` reads it.
            if initial_body:
                params["initial_body"] = initial_body
            draft = await self.create_action_draft(
                kind="create-concept",
                params=params,
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


__all__ = ["CanonicalizationService"]
