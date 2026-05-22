"""Tests for confidence decay engine."""

import math
from datetime import UTC, datetime, timedelta

import pytest

from backend.knowledge.retrieval.confidence import DecayConfig, decay_factor, effective_confidence


class TestDecayFactor:
    def test_zero_days(self):
        assert decay_factor(0, 365) == 1.0

    def test_negative_days(self):
        assert decay_factor(-5, 365) == 1.0

    def test_one_halflife(self):
        assert decay_factor(365, 365) == pytest.approx(0.5)

    def test_two_halflives(self):
        assert decay_factor(730, 365) == pytest.approx(0.25)

    def test_partial_halflife(self):
        result = decay_factor(182.5, 365)
        assert result == pytest.approx(math.pow(0.5, 0.5), rel=1e-6)

    def test_zero_halflife(self):
        assert decay_factor(10, 0) == 1.0


class TestDecayConfig:
    def test_defaults(self):
        config = DecayConfig()
        assert config.halflife_for("semantic") == 365
        assert config.halflife_for("episodic") == 30
        assert config.halflife_for("procedural") == 90
        assert config.halflife_for("affective") == 60

    def test_unknown_layer_falls_back_to_semantic(self):
        config = DecayConfig()
        assert config.halflife_for("unknown") == 365

    def test_custom_values(self):
        config = DecayConfig(semantic=100, episodic=10)
        assert config.halflife_for("semantic") == 100
        assert config.halflife_for("episodic") == 10


class TestEffectiveConfidence:
    def test_no_last_confirmed_returns_base(self):
        assert effective_confidence(0.9, None) == 0.9

    def test_fresh_confirmation(self):
        now = datetime.now(tz=UTC)
        result = effective_confidence(0.9, now.isoformat(), now=now)
        assert result == pytest.approx(0.9)

    def test_semantic_one_year(self):
        now = datetime.now(tz=UTC)
        one_year_ago = (now - timedelta(days=365)).isoformat()
        result = effective_confidence(1.0, one_year_ago, "semantic", now=now)
        assert result == pytest.approx(0.5)

    def test_episodic_one_month(self):
        now = datetime.now(tz=UTC)
        one_month_ago = (now - timedelta(days=30)).isoformat()
        result = effective_confidence(1.0, one_month_ago, "episodic", now=now)
        assert result == pytest.approx(0.5)

    def test_episodic_decays_faster_than_semantic(self):
        now = datetime.now(tz=UTC)
        sixty_days_ago = (now - timedelta(days=60)).isoformat()
        semantic = effective_confidence(1.0, sixty_days_ago, "semantic", now=now)
        episodic = effective_confidence(1.0, sixty_days_ago, "episodic", now=now)
        assert semantic > episodic

    def test_custom_config(self):
        now = datetime.now(tz=UTC)
        ten_days_ago = (now - timedelta(days=10)).isoformat()
        config = DecayConfig(semantic=10)
        result = effective_confidence(1.0, ten_days_ago, "semantic", config=config, now=now)
        assert result == pytest.approx(0.5)

    def test_iso_date_string(self):
        now = datetime(2026, 3, 16, tzinfo=UTC)
        result = effective_confidence(0.8, "2026-03-16", "semantic", now=now)
        assert result == pytest.approx(0.8)

    def test_invalid_date_returns_base(self):
        result = effective_confidence(0.9, "not-a-date")
        assert result == 0.9

    def test_datetime_input(self):
        now = datetime.now(tz=UTC)
        confirmed = now - timedelta(days=365)
        result = effective_confidence(1.0, confirmed, "semantic", now=now)
        assert result == pytest.approx(0.5)
