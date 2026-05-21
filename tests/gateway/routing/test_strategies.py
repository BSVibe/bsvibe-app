"""RegionSelector / CostOptimizer / ABTester strategies — pure logic."""

from __future__ import annotations

from backend.gateway.routing.strategies import (
    ABTestConfig,
    ABTester,
    CostOptimizationConfig,
    CostOptimizer,
    RegionConfig,
    RegionSelector,
)


class TestRegionSelector:
    def test_returns_none_for_empty(self):
        assert RegionSelector([]).select_region() is None

    def test_picks_lowest_priority(self):
        regions = [
            RegionConfig(region="eu", priority=10, api_base="eu", latency_ms=50),
            RegionConfig(region="us", priority=1, api_base="us", latency_ms=80),
        ]
        sel = RegionSelector(regions)
        assert sel.select_region().region == "us"

    def test_user_preference_wins(self):
        regions = [
            RegionConfig(region="eu", priority=10, api_base="eu"),
            RegionConfig(region="us", priority=1, api_base="us"),
        ]
        sel = RegionSelector(regions)
        assert sel.select_region(user_region="eu").region == "eu"

    def test_user_preference_ignored_when_not_available(self):
        regions = [RegionConfig(region="us", priority=1, api_base="us")]
        sel = RegionSelector(regions)
        assert sel.select_region(user_region="ap").region == "us"

    def test_api_base_falls_back_to_default(self):
        sel = RegionSelector([])
        region = RegionConfig(region="us", priority=1, api_base=None)
        assert sel.get_api_base(region, default="https://default") == "https://default"
        region2 = RegionConfig(region="us", priority=1, api_base="https://us")
        assert sel.get_api_base(region2, default="https://default") == "https://us"


class TestCostOptimizer:
    def test_disabled_returns_zero(self):
        opt = CostOptimizer(CostOptimizationConfig(enabled=False))
        assert opt.calculate_cost("m", 1000, 500) == 0.0

    def test_enabled_computes_cost(self):
        cfg = CostOptimizationConfig(
            enabled=True, cost_per_1k_input=0.002, cost_per_1k_output=0.006
        )
        opt = CostOptimizer(cfg)
        cost = opt.calculate_cost("m", 2000, 1000)
        # 2 * 0.002 + 1 * 0.006 = 0.010
        assert abs(cost - 0.010) < 1e-9

    def test_fallback_under_threshold(self):
        cfg = CostOptimizationConfig(enabled=True, fallback_cost_multiplier=0.5)
        opt = CostOptimizer(cfg)
        assert opt.should_use_fallback(primary_cost=1.0, fallback_cost=0.4) is True
        assert opt.should_use_fallback(primary_cost=1.0, fallback_cost=0.6) is False

    def test_disabled_never_fallbacks(self):
        opt = CostOptimizer(CostOptimizationConfig(enabled=False))
        assert opt.should_use_fallback(primary_cost=1.0, fallback_cost=0.0) is False


class TestABTester:
    def test_no_variants_returns_none(self):
        ab = ABTester({})
        assert ab.select_variant("nope") is None

    def test_deterministic_by_user_id(self):
        cfgs = [
            ABTestConfig(variant_id="A", traffic_percentage=50, model="m1"),
            ABTestConfig(variant_id="B", traffic_percentage=50, model="m2"),
        ]
        ab = ABTester({"t": cfgs})
        # Same user → same variant across calls.
        v1 = ab.select_variant("t", user_id="alice")
        v2 = ab.select_variant("t", user_id="alice")
        assert v1.variant_id == v2.variant_id

    def test_different_users_can_differ(self):
        # 100% to A makes deterministic regardless of user; let's check 50/50.
        cfgs = [
            ABTestConfig(variant_id="A", traffic_percentage=50, model="m1"),
            ABTestConfig(variant_id="B", traffic_percentage=50, model="m2"),
        ]
        ab = ABTester({"t": cfgs})
        verdicts = {ab.select_variant("t", user_id=str(i)).variant_id for i in range(100)}
        # Across 100 users we should see both variants.
        assert verdicts == {"A", "B"}

    def test_random_when_no_user_id(self):
        cfgs = [ABTestConfig(variant_id="A", traffic_percentage=100, model="m1")]
        ab = ABTester({"t": cfgs})
        assert ab.select_variant("t").variant_id == "A"
