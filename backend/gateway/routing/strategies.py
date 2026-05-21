"""Routing strategies — region selection, cost optimization, A/B testing.

Pure-logic helpers — no DB, no HTTP. Wired into the dispatch path in
Bundle 1.5c when the LiteLLM hook lands. Surfaced here so the data
classes are stable and tests can verify the math up front.
"""

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass

import structlog

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class RegionConfig:
    region: str
    priority: int  # lower = better
    api_base: str | None = None
    latency_ms: float | None = None


@dataclass(frozen=True)
class CostOptimizationConfig:
    enabled: bool = False
    cost_per_1k_input: float = 0.0
    cost_per_1k_output: float = 0.0
    # Fallback fires when fallback_cost < primary_cost * multiplier.
    fallback_cost_multiplier: float = 0.5


@dataclass(frozen=True)
class ABTestConfig:
    variant_id: str
    traffic_percentage: int  # 0..100
    model: str


class RegionSelector:
    def __init__(self, regions: list[RegionConfig]) -> None:
        self.regions = sorted(regions, key=lambda r: r.priority)

    def select_region(self, user_region: str | None = None) -> RegionConfig | None:
        if not self.regions:
            return None
        if user_region:
            for region in self.regions:
                if region.region == user_region:
                    logger.debug("region.selected", region=user_region, reason="user_preference")
                    return region
        selected = self.regions[0]
        logger.debug("region.selected", region=selected.region, latency_ms=selected.latency_ms)
        return selected

    @staticmethod
    def get_api_base(region: RegionConfig, default: str) -> str:
        return region.api_base or default


class CostOptimizer:
    def __init__(self, config: CostOptimizationConfig) -> None:
        self._cfg = config

    def calculate_cost(self, model: str, input_tokens: int, output_tokens: int) -> float:
        if not self._cfg.enabled:
            return 0.0
        return (input_tokens / 1000) * self._cfg.cost_per_1k_input + (
            output_tokens / 1000
        ) * self._cfg.cost_per_1k_output

    def should_use_fallback(self, primary_cost: float, fallback_cost: float) -> bool:
        if not self._cfg.enabled or primary_cost == 0:
            return False
        threshold = primary_cost * self._cfg.fallback_cost_multiplier
        if fallback_cost < threshold:
            logger.debug(
                "cost.fallback_triggered",
                primary_cost=primary_cost,
                fallback_cost=fallback_cost,
                threshold=threshold,
            )
            return True
        return False


class ABTester:
    def __init__(self, tests: dict[str, list[ABTestConfig]]) -> None:
        self._tests = tests

    def select_variant(self, test_id: str, user_id: str | None = None) -> ABTestConfig | None:
        variants = self._tests.get(test_id, [])
        if not variants:
            return None
        if not user_id:
            return self._weighted_choice(variants)
        hash_val = int(hashlib.sha256(user_id.encode()).hexdigest(), 16) % 100
        cumulative = 0
        for variant in variants:
            cumulative += variant.traffic_percentage
            if hash_val < cumulative:
                logger.debug(
                    "ab_test.variant_selected",
                    test_id=test_id,
                    variant=variant.variant_id,
                    reason="hash_assignment",
                )
                return variant
        return variants[0]

    @staticmethod
    def _weighted_choice(variants: list[ABTestConfig]) -> ABTestConfig:
        total = sum(v.traffic_percentage for v in variants)
        rand = random.uniform(0, total)  # noqa: S311 — traffic split, not cryptographic
        cumulative = 0
        for variant in variants:
            cumulative += variant.traffic_percentage
            if rand < cumulative:
                return variant
        return variants[-1]
