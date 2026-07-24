"""CallerSpec registry — static + skill-namespace lookup."""

from __future__ import annotations

import pytest

from backend.dispatch.caller_registry import (
    CALLER_AGENT_LOOP_ACT,
    CALLER_AGENT_LOOP_PLAN,
    CALLER_FRAME,
    CALLER_JUDGE,
    CALLER_KNOWLEDGE_CANONICALIZATION,
    CALLER_KNOWLEDGE_INGEST,
    CALLER_KNOWLEDGE_QUERY,
    CALLER_SETTLE_EXTRACT,
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


class TestPerCallerTimeout:
    """Lift E9 — per-caller chat timeout knob on :class:`CallerSpec`.

    Lift E14 dogfood (bsvibe-app big-repo bootstrap, 1134 chunks) found the
    original 180 s ceilings were too aggressive for a single large-file
    chunk (an ``opencode run`` subprocess on a 10–20 KB file takes
    5–16 min wall-clock). Bumped to per-caller targets that match
    observed worker latency on realistic seed sizes.
    """

    def test_callerspec_defaults_to_none(self) -> None:
        """A spec authored without an explicit timeout uses settings default."""
        spec = CallerSpec(caller_id="custom")
        assert spec.default_timeout_s is None

    def test_knowledge_ingest_ten_minutes(self) -> None:
        """Big-file chunks (single 10–20 KB seed) routinely take 5–16 min
        via the executor adapter; 600 s is the post-E14 ceiling."""
        spec = KNOWN_CALLERS[CALLER_KNOWLEDGE_INGEST]
        assert spec.default_timeout_s == 600.0

    def test_knowledge_canonicalization_ten_minutes(self) -> None:
        """Canonicalization passes fan out over the workspace ontology and
        run as long as a heavy ingest chunk."""
        spec = KNOWN_CALLERS[CALLER_KNOWLEDGE_CANONICALIZATION]
        assert spec.default_timeout_s == 600.0

    def test_frame_five_minutes(self) -> None:
        """Frame is bounded reasoning; 5 min is plenty."""
        spec = KNOWN_CALLERS[CALLER_FRAME]
        assert spec.default_timeout_s == 300.0

    def test_judge_five_minutes(self) -> None:
        spec = KNOWN_CALLERS[CALLER_JUDGE]
        assert spec.default_timeout_s == 300.0

    def test_settle_extract_five_minutes(self) -> None:
        spec = KNOWN_CALLERS[CALLER_SETTLE_EXTRACT]
        assert spec.default_timeout_s == 300.0

    def test_knowledge_query_ninety_seconds(self) -> None:
        """Interactive query — founder is waiting; fail fast but small bump
        from 60 s so a slow first-token doesn't cancel a healthy query."""
        spec = KNOWN_CALLERS[CALLER_KNOWLEDGE_QUERY]
        assert spec.default_timeout_s == 90.0

    def test_agent_loop_plan_ten_minutes(self) -> None:
        """Plan turn over a big repo can pull lots of context."""
        spec = KNOWN_CALLERS[CALLER_AGENT_LOOP_PLAN]
        assert spec.default_timeout_s == 600.0

    def test_agent_loop_act_uses_settings_default(self) -> None:
        """Tool-emitting agent turn genuinely runs `claude --print` —
        keeps the 1800 s legacy default by leaving the override None."""
        spec = KNOWN_CALLERS[CALLER_AGENT_LOOP_ACT]
        assert spec.default_timeout_s is None


class TestYieldOnSaturation:
    """Run-drive callers yield-back on saturation; batch callers keep the wait.

    A saturated run-drive executor call (framing / agent-loop) must NOT block
    the shared ``AgentWorker`` — the worker re-polls OPEN runs, so leaving the
    run open and retrying is strictly better than holding the worker slot + DB
    lock for up to 30 min. The ingest / canonicalization fan-out CANNOT yield
    to a poll loop, so its bounded capacity-wait stays.
    """

    def test_default_is_false(self) -> None:
        """A spec authored without the flag never yields — the safe default."""
        spec = CallerSpec(caller_id="custom")
        assert spec.yield_on_saturation is False

    def test_frame_yields_on_saturation(self) -> None:
        assert KNOWN_CALLERS[CALLER_FRAME].yield_on_saturation is True

    def test_agent_loop_plan_yields_on_saturation(self) -> None:
        assert KNOWN_CALLERS[CALLER_AGENT_LOOP_PLAN].yield_on_saturation is True

    def test_agent_loop_act_yields_on_saturation(self) -> None:
        assert KNOWN_CALLERS[CALLER_AGENT_LOOP_ACT].yield_on_saturation is True

    def test_knowledge_ingest_does_not_yield(self) -> None:
        """The batch fan-out legitimately waits for a slot — must NOT yield."""
        assert KNOWN_CALLERS[CALLER_KNOWLEDGE_INGEST].yield_on_saturation is False

    def test_knowledge_canonicalization_does_not_yield(self) -> None:
        assert KNOWN_CALLERS[CALLER_KNOWLEDGE_CANONICALIZATION].yield_on_saturation is False

    def test_only_run_drive_callers_yield(self) -> None:
        """Exactly the three run-drive callers carry the flag; nothing else."""
        yielders = {spec.caller_id for spec in KNOWN_CALLERS.values() if spec.yield_on_saturation}
        assert yielders == {CALLER_FRAME, CALLER_AGENT_LOOP_PLAN, CALLER_AGENT_LOOP_ACT}


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
