"""StaticClassifier — fast deterministic scoring.

Simple weighted sum of features. Calibrated against the BSGateway
heuristic but slimmed to the smaller feature set the BSVibe gateway
threads through (no embeddings, no learned ML weights).
"""

from __future__ import annotations

from backend.router.classifier.base import (
    ClassificationFeatures,
    ClassificationResult,
)


class StaticClassifier:
    def __init__(
        self,
        *,
        local_score_max: int = 40,
        cloud_score_min: int = 60,
    ) -> None:
        if local_score_max >= cloud_score_min:
            raise ValueError("local_score_max must be < cloud_score_min")
        self._local_max = local_score_max
        self._cloud_min = cloud_score_min

    async def classify(self, features: ClassificationFeatures) -> ClassificationResult:
        score = self._score(features)
        if score <= self._local_max:
            return ClassificationResult(tier="local", score=score, strategy="static")
        # Anything above local_max (gray band or hard-cloud) routes cloud
        # for the standalone static classifier. LocalVsCloud overlays the
        # gray band with a secondary call.
        return ClassificationResult(tier="cloud", score=score, strategy="static")

    @staticmethod
    def _score(f: ClassificationFeatures) -> int:
        raw = (
            min(f.token_count / 4_000, 1.0) * 35
            + min(f.system_prompt_chars / 2_000, 1.0) * 15
            + min(f.conversation_turns / 6, 1.0) * 15
            + min(f.code_block_count / 4, 1.0) * 20
            + min(f.tool_count / 4, 1.0) * 15
        )
        return max(0, min(100, int(round(raw))))
