"""Action validators — Handoff §13 step 4 (deterministic Hard Blocks).

Mixin extracted from the original ``service.py`` god-file per v8 §17.4.
Holds the per-kind validation logic. Validation runs BEFORE persistence
in ``_apply_locked`` — that ordering is the invariant the apply pipeline
relies on and MUST NOT change.

Mixin contract:
- Depends on ``self._store`` (concept_exists, read_decision)
- Depends on ``self._decisions`` (cannot-link strength)
- Depends on ``self._policies`` (cannot-link threshold)
- Depends on ``self._clock`` (validation timestamps)
"""

from __future__ import annotations

from typing import Any

from backend.knowledge.canonicalization import evidence as evidence_module
from backend.knowledge.canonicalization import models, paths
from backend.knowledge.canonicalization.service._base import _ServiceBase

_VALID_DECISION_MATURITIES: frozenset[str] = frozenset({"seedling", "budding", "evergreen"})


def _evidence(reason: str, **payload: Any) -> dict[str, Any]:
    """Hard Block evidence envelope. Thin wrapper over evidence.hard_block."""
    return evidence_module.hard_block(reason, **payload)


class _ValidatorsMixin(_ServiceBase):
    """Per-kind validation for the apply pipeline.

    The single public-to-the-mixin entry is ``_validate``; it dispatches to
    one of the per-kind private validators. Each validator appends Hard Block
    evidence to ``result.hard_blocks`` — the caller flips ``status`` to
    ``failed`` after the dispatch returns.
    """

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
