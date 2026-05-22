"""CanonicalizationScorer — slice 4 deterministic-only scorer (Handoff §13).

Produces a ``ScoreResult`` with envelope-shaped, source-separated
``risk_reasons``. Slice 4 emits only ``source: deterministic`` reasons:

- ``blast_radius_exceeded`` — affected_count > policy.max_affected_paths[kind]
- ``prior_decision_conflict`` — cannot-link decision below hard_block but
  above review threshold (review-warning band)

LLM-as-judge / model evidence is reserved for slice 5 (BalancedProposer
verifier integration). Per §13: model evidence MUST live under
``model_evidence``, never as deterministic risk_reasons.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import structlog

from backend.knowledge.canonicalization import evidence, models
from backend.knowledge.canonicalization.decisions import DecisionMemory
from backend.knowledge.canonicalization.policies import PolicyResolver

logger = structlog.get_logger(__name__)

_SCORER_VERSION = "canonicalization.scoring-v1"
_PRODUCER = _SCORER_VERSION


class CanonicalizationScorer:
    """Rule-based scorer (slice 4 deterministic-only)."""

    def __init__(
        self,
        decisions: DecisionMemory,
        policies: PolicyResolver,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._decisions = decisions
        self._policies = policies
        self._clock = clock or (lambda: datetime.now(tz=UTC))

    async def score(self, action: models.ActionEntry) -> models.ScoreResult:
        result = models.ScoreResult(
            status="completed",
            scorer_version=_SCORER_VERSION,
            stability_score=1.0,
        )

        merge_policy = await self._safe_select("merge-auto-apply")
        decision_policy = await self._safe_select("decision-maturity")
        result.policy_profile_path = merge_policy.path if merge_policy is not None else "default"

        # 1. Blast radius (Handoff §8.5 max_affected_paths)
        await self._check_blast_radius(action, merge_policy, result)

        # 2. Prior decision conflict (cannot-link below hard_block)
        if action.kind == "merge-concepts":
            await self._check_decision_conflicts(action, merge_policy, decision_policy, result)

        # Stability score: 1.0 minus weighted penalties
        result.stability_score = self._compute_stability(result)
        return result

    # ------------------------------------------------------ helpers

    async def _safe_select(self, kind: str) -> models.PolicyEntry | None:
        try:
            return await self._policies.select(kind=kind, scope={})
        except Exception:  # noqa: BLE001 — policy_conflict surfaced elsewhere
            return None

    async def _check_blast_radius(
        self,
        action: models.ActionEntry,
        policy: models.PolicyEntry | None,
        result: models.ScoreResult,
    ) -> None:
        affected_count = len(action.affected_paths)
        cap = self._max_affected_paths(action.kind, policy)
        if cap is None or affected_count <= cap:
            return
        envelope = self._envelope(
            kind="blast_radius_exceeded",
            schema_version="blast-radius-v1",
            payload={"affected_count": affected_count, "cap": cap, "kind": action.kind},
        )
        result.risk_reasons.append(envelope)
        result.deterministic_evidence.append(envelope)

    async def _check_decision_conflicts(
        self,
        action: models.ActionEntry,
        merge_policy: models.PolicyEntry | None,
        decision_policy: models.PolicyEntry | None,
        result: models.ScoreResult,
    ) -> None:
        canonical = action.params.get("canonical")
        merge = action.params.get("merge") or []
        if not isinstance(canonical, str) or not isinstance(merge, list):
            return
        hard_block = self._cannot_link_threshold(merge_policy)
        review = self._review_threshold(decision_policy)
        for old_id in merge:
            if not isinstance(old_id, str):
                continue
            decisions = await self._decisions.find_cannot_link((canonical, old_id))
            for d in decisions:
                strength = self._decisions.effective_strength(d, now=self._clock())
                # Hard-block-level conflicts are validation Hard Blocks elsewhere;
                # scorer surfaces only the review-warning band.
                if review <= strength < hard_block:
                    envelope = self._envelope(
                        kind="prior_decision_conflict",
                        schema_version="decision-conflict-v1",
                        payload={
                            "decision_path": d.path,
                            "subjects": list(d.subjects),
                            "effective_strength": strength,
                            "hard_block_threshold": hard_block,
                            "review_threshold": review,
                        },
                    )
                    result.risk_reasons.append(envelope)
                    result.deterministic_evidence.append(envelope)

    @staticmethod
    def _max_affected_paths(action_kind: str, policy: models.PolicyEntry | None) -> int | None:
        if policy is None:
            return None
        caps = policy.params.get("safe_mode_on", {}).get("max_affected_paths", {})
        value = caps.get(action_kind)
        return int(value) if value is not None else None

    @staticmethod
    def _cannot_link_threshold(policy: models.PolicyEntry | None) -> float:
        if policy is None:
            return 0.85
        return float(policy.params.get("hard_blocks", {}).get("cannot_link_threshold", 0.85))

    @staticmethod
    def _review_threshold(policy: models.PolicyEntry | None) -> float:
        if policy is None:
            return 0.60
        return float(policy.params.get("thresholds", {}).get("review", 0.60))

    @staticmethod
    def _compute_stability(result: models.ScoreResult) -> float:
        # Each risk knocks 0.2 off the score, floored at 0.0.
        # Slice 4 is intentionally simple — slice 5 layers model verify
        # signals into a richer formula.
        score = 1.0 - 0.2 * len(result.risk_reasons)
        return round(max(0.0, min(1.0, score)), 3)

    def _envelope(
        self,
        *,
        kind: str,
        schema_version: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        return evidence.envelope(
            kind=kind,
            schema_version=schema_version,
            payload=payload,
            source="deterministic",
            producer=_PRODUCER,
            clock=self._clock,
        )
