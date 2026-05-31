"""Tests for backend.router.classifier — static + 2-tier LocalVsCloud."""

from __future__ import annotations

import pytest

from backend.router.classifier.base import (
    ClassificationFeatures,
    ClassificationResult,
)
from backend.router.classifier.local_vs_cloud import LocalVsCloudClassifier
from backend.router.classifier.static import StaticClassifier


def _features(**overrides) -> ClassificationFeatures:
    base = {
        "token_count": 0,
        "system_prompt_chars": 0,
        "conversation_turns": 0,
        "code_block_count": 0,
        "tool_count": 0,
    }
    base.update(overrides)
    return ClassificationFeatures(**base)


class TestStatic:
    async def test_trivial_request_goes_local(self):
        c = StaticClassifier(local_score_max=40, cloud_score_min=60)
        result = await c.classify(_features(token_count=50))
        assert result.tier == "local"
        assert result.score <= 40
        assert result.strategy == "static"

    async def test_heavy_code_goes_cloud(self):
        c = StaticClassifier(local_score_max=40, cloud_score_min=60)
        result = await c.classify(_features(token_count=8_000, code_block_count=4, tool_count=4))
        assert result.tier == "cloud"
        assert result.score >= 60

    async def test_rejects_overlapping_thresholds(self):
        with pytest.raises(ValueError, match="local_score_max"):
            StaticClassifier(local_score_max=60, cloud_score_min=40)

    async def test_score_is_capped(self):
        c = StaticClassifier(local_score_max=40, cloud_score_min=60)
        result = await c.classify(
            _features(
                token_count=1_000_000,
                code_block_count=1_000,
                tool_count=1_000,
                conversation_turns=1_000,
                system_prompt_chars=1_000_000,
            )
        )
        assert 0 <= result.score <= 100


class _StubSecondary:
    def __init__(self, tier: str) -> None:
        self.tier = tier
        self.called_with: ClassificationFeatures | None = None

    async def classify(self, features: ClassificationFeatures) -> ClassificationResult:
        self.called_with = features
        return ClassificationResult(tier=self.tier, score=50, strategy="stub")  # type: ignore[arg-type]


class TestLocalVsCloud:
    async def test_low_score_routes_local_without_secondary(self):
        sec = _StubSecondary("cloud")
        c = LocalVsCloudClassifier(local_score_max=40, cloud_score_min=60, secondary=sec)
        result = await c.classify(_features(token_count=50))
        assert result.tier == "local"
        assert sec.called_with is None

    async def test_high_score_routes_cloud_without_secondary(self):
        sec = _StubSecondary("local")
        c = LocalVsCloudClassifier(local_score_max=40, cloud_score_min=60, secondary=sec)
        result = await c.classify(_features(token_count=10_000, code_block_count=10, tool_count=10))
        assert result.tier == "cloud"
        assert sec.called_with is None

    async def test_gray_band_defers_to_secondary(self):
        sec = _StubSecondary("local")
        c = LocalVsCloudClassifier(local_score_max=40, cloud_score_min=60, secondary=sec)
        # Tune feature mix so static score lands in (40, 60).
        result = await c.classify(_features(token_count=4_500, code_block_count=1, tool_count=2))
        assert sec.called_with is not None
        assert result.strategy == "two_tier"
        assert result.tier == "local"

    async def test_gray_band_no_secondary_biases_cloud(self):
        c = LocalVsCloudClassifier(local_score_max=40, cloud_score_min=60)
        result = await c.classify(_features(token_count=4_500, code_block_count=1, tool_count=2))
        assert result.tier == "cloud"
        assert result.reason == "gray_band_no_secondary"

    async def test_rejects_overlapping_thresholds(self):
        with pytest.raises(ValueError, match="local_score_max"):
            LocalVsCloudClassifier(local_score_max=70, cloud_score_min=50)
