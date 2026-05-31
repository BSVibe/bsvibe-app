"""PresetRegistry — built-in templates list + lookup."""

from __future__ import annotations

import pytest

from backend.router.presets.registry import PresetRegistry


@pytest.fixture
def registry() -> PresetRegistry:
    return PresetRegistry()


class TestBuiltins:
    def test_lists_all_builtins(self, registry):
        names = {p.name for p in registry.list_all()}
        # Four built-in templates per BSGateway parity.
        assert names == {
            "coding-assistant",
            "customer-support",
            "translation-summary",
            "general",
        }

    def test_get_by_name(self, registry):
        preset = registry.get("coding-assistant")
        assert preset is not None
        assert preset.name == "coding-assistant"
        # Coding preset must have at least one intent + a default rule.
        assert preset.intents
        assert any(r.is_default for r in preset.rules)

    def test_get_unknown_returns_none(self, registry):
        assert registry.get("does-not-exist") is None


class TestRuleStructure:
    def test_every_preset_has_default_rule(self, registry):
        for preset in registry.list_all():
            defaults = [r for r in preset.rules if r.is_default]
            assert len(defaults) == 1, f"{preset.name} must have exactly one default rule"

    def test_target_levels_are_canonical(self, registry):
        canonical = {"economy", "balanced", "premium"}
        for preset in registry.list_all():
            for rule in preset.rules:
                assert rule.target_level in canonical


class TestModelMapping:
    def test_resolve_levels(self):
        from backend.router.presets.models import ModelMapping

        mapping = ModelMapping(
            economy="ollama/llama3.2",
            balanced="claude-3-haiku",
            premium="claude-3-5-sonnet",
        )
        assert mapping.resolve("economy") == "ollama/llama3.2"
        assert mapping.resolve("balanced") == "claude-3-haiku"
        assert mapping.resolve("premium") == "claude-3-5-sonnet"

    def test_unknown_level_defaults_to_balanced(self):
        from backend.router.presets.models import ModelMapping

        mapping = ModelMapping(economy="e", balanced="b", premium="p")
        assert mapping.resolve("hippopotamus") == "b"
