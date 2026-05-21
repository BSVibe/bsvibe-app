"""Two-tier classifier — fast static, then a secondary tier-breaker.

Scores in the gray band ``(local_max, cloud_min)`` defer to a secondary
:class:`Classifier` (typically an Ollama-backed LLM). Outside the band
the static verdict stands. The wrapper records its strategy as
``"two_tier"`` so audit logs surface the decision path.
"""

from __future__ import annotations

from backend.gateway.classifier.base import (
    ClassificationFeatures,
    ClassificationResult,
    Classifier,
)
from backend.gateway.classifier.static import StaticClassifier


class LocalVsCloudClassifier:
    def __init__(
        self,
        *,
        local_score_max: int,
        cloud_score_min: int,
        static: StaticClassifier | None = None,
        secondary: Classifier | None = None,
    ) -> None:
        if local_score_max >= cloud_score_min:
            raise ValueError("local_score_max must be < cloud_score_min")
        self._local_max = local_score_max
        self._cloud_min = cloud_score_min
        self._static = static or StaticClassifier(
            local_score_max=local_score_max,
            cloud_score_min=cloud_score_min,
        )
        self._secondary = secondary

    async def classify(self, features: ClassificationFeatures) -> ClassificationResult:
        first = await self._static.classify(features)
        if first.score <= self._local_max:
            return ClassificationResult(
                tier="local",
                score=first.score,
                strategy="two_tier",
                reason="below_local_max",
            )
        if first.score >= self._cloud_min:
            return ClassificationResult(
                tier="cloud",
                score=first.score,
                strategy="two_tier",
                reason="above_cloud_min",
            )
        # Gray band — defer to secondary classifier if configured.
        if self._secondary is not None:
            second = await self._secondary.classify(features)
            return ClassificationResult(
                tier=second.tier,
                score=first.score,
                strategy="two_tier",
                reason=f"gray_band_secondary={second.strategy}",
            )
        # No secondary available — bias cloud (safer default for unsure
        # requests; mirrors the [parked] guidance of preferring cloud
        # when in doubt).
        return ClassificationResult(
            tier="cloud",
            score=first.score,
            strategy="two_tier",
            reason="gray_band_no_secondary",
        )
