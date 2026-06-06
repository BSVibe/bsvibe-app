"""CallerSpec registry — static + skill-namespace lookup."""

from __future__ import annotations

import pytest

from backend.dispatch.caller_registry import (
    CALLER_AGENT_LOOP_PLAN,
    CALLER_FRAME,
    CALLER_KNOWLEDGE_INGEST,
    KNOWN_CALLERS,
    SKILL_CALLER_PREFIX,
    CallerSpec,
    get_caller_spec,
    list_all_callers,
)


class TestStaticRegistry:
    def test_known_callers_include_core_sites(self) -> None:
        assert CALLER_KNOWLEDGE_INGEST in KNOWN_CALLERS
        assert CALLER_AGENT_LOOP_PLAN in KNOWN_CALLERS
        assert CALLER_FRAME in KNOWN_CALLERS

    def test_every_spec_requires_chat(self) -> None:
        for spec in KNOWN_CALLERS.values():
            assert "chat" in spec.required_methods

    def test_specs_are_frozen(self) -> None:
        spec = KNOWN_CALLERS[CALLER_FRAME]
        with pytest.raises(AttributeError):
            spec.caller_id = "tampered"  # type: ignore[misc]

    def test_get_caller_spec_returns_static_entry(self) -> None:
        spec = get_caller_spec(CALLER_KNOWLEDGE_INGEST)
        assert spec.caller_id == CALLER_KNOWLEDGE_INGEST


class TestSkillNamespace:
    def test_unknown_id_raises(self) -> None:
        with pytest.raises(KeyError):
            get_caller_spec("totally-made-up")

    def test_unknown_skill_without_loader_raises(self) -> None:
        # ``skill.X`` lookup with no ``skill_names`` is still a miss — the
        # resolver wants the workspace to have ACTUALLY loaded that skill.
        with pytest.raises(KeyError):
            get_caller_spec(f"{SKILL_CALLER_PREFIX}widget-builder")

    def test_known_skill_via_dynamic_lookup(self) -> None:
        spec = get_caller_spec(
            f"{SKILL_CALLER_PREFIX}widget-builder",
            skill_names=["widget-builder"],
        )
        assert isinstance(spec, CallerSpec)
        assert spec.caller_id == f"{SKILL_CALLER_PREFIX}widget-builder"
        assert "chat" in spec.required_methods

    def test_skill_lookup_misnamed_still_misses(self) -> None:
        with pytest.raises(KeyError):
            get_caller_spec(
                f"{SKILL_CALLER_PREFIX}widget-builder",
                skill_names=["other-skill"],
            )


class TestListAllCallers:
    def test_list_static_only(self) -> None:
        items = list_all_callers()
        assert len(items) == len(KNOWN_CALLERS)
        ids = {s.caller_id for s in items}
        assert CALLER_FRAME in ids
        assert CALLER_KNOWLEDGE_INGEST in ids

    def test_list_merges_skill_namespace(self) -> None:
        items = list_all_callers(skill_names=["alpha", "beta"])
        ids = {s.caller_id for s in items}
        assert f"{SKILL_CALLER_PREFIX}alpha" in ids
        assert f"{SKILL_CALLER_PREFIX}beta" in ids
        # Static still present too.
        assert CALLER_FRAME in ids
