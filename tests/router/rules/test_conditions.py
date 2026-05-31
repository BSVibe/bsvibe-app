"""evaluate_condition — every operator + every field type."""

from __future__ import annotations

import uuid

import pytest

from backend.router.rules.conditions import ALLOWED_FIELDS, evaluate_condition
from backend.router.rules.models import EvaluationContext, RuleCondition


def _ctx(**overrides) -> EvaluationContext:
    base = dict(
        workspace_id=uuid.uuid4(),
        account_id=uuid.uuid4(),
        user_text="",
        system_prompt="",
        all_text="",
        estimated_tokens=0,
        conversation_turns=0,
        has_code_blocks=False,
        has_error_trace=False,
        tool_count=0,
        tool_names=[],
        original_model="",
    )
    base.update(overrides)
    return EvaluationContext(**base)


class TestStringOperators:
    def test_eq_matches_exact(self):
        ctx = _ctx(original_model="gpt-4o")
        cond = RuleCondition("text_pattern", "original_model", "eq", "gpt-4o")
        assert evaluate_condition(cond, ctx) is True

    def test_eq_negate_inverts(self):
        ctx = _ctx(original_model="gpt-4o")
        cond = RuleCondition("text_pattern", "original_model", "eq", "gpt-4o", negate=True)
        assert evaluate_condition(cond, ctx) is False

    def test_contains_case_insensitive(self):
        ctx = _ctx(user_text="Please URGENT respond")
        cond = RuleCondition("text_pattern", "user_text", "contains", "urgent")
        assert evaluate_condition(cond, ctx) is True

    def test_regex_with_ignorecase(self):
        ctx = _ctx(user_text="error: failed")
        cond = RuleCondition("text_pattern", "user_text", "regex", r"^Error")
        assert evaluate_condition(cond, ctx) is True

    def test_regex_invalid_returns_false(self):
        ctx = _ctx(user_text="x")
        cond = RuleCondition("text_pattern", "user_text", "regex", r"[unclosed")
        assert evaluate_condition(cond, ctx) is False

    def test_regex_redos_pattern_rejected(self):
        ctx = _ctx(user_text="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa!")
        cond = RuleCondition("text_pattern", "user_text", "regex", r"(a+)+$")
        assert evaluate_condition(cond, ctx) is False

    def test_regex_overlong_pattern_rejected(self):
        ctx = _ctx(user_text="x")
        cond = RuleCondition("text_pattern", "user_text", "regex", "a" * 501)
        assert evaluate_condition(cond, ctx) is False


class TestNumericOperators:
    @pytest.mark.parametrize(
        "op,expected",
        [("gt", True), ("gte", True), ("lt", False), ("lte", False)],
    )
    def test_numeric_compare(self, op, expected):
        ctx = _ctx(estimated_tokens=100)
        cond = RuleCondition("token_count", "estimated_tokens", op, 50)
        assert evaluate_condition(cond, ctx) is expected

    def test_between_inclusive(self):
        ctx = _ctx(estimated_tokens=100)
        cond = RuleCondition("token_count", "estimated_tokens", "between", [50, 200])
        assert evaluate_condition(cond, ctx) is True

    def test_between_outside(self):
        ctx = _ctx(estimated_tokens=400)
        cond = RuleCondition("token_count", "estimated_tokens", "between", [50, 200])
        assert evaluate_condition(cond, ctx) is False

    def test_between_malformed_returns_false(self):
        ctx = _ctx(estimated_tokens=100)
        cond = RuleCondition("token_count", "estimated_tokens", "between", [50])
        assert evaluate_condition(cond, ctx) is False

    def test_none_field_treated_as_zero(self):
        ctx = _ctx(daily_cost=None)
        cond = RuleCondition("budget", "daily_cost", "lt", 10)
        assert evaluate_condition(cond, ctx) is True


class TestListOperators:
    def test_in_string_match(self):
        ctx = _ctx(original_model="gpt-4o")
        cond = RuleCondition("text_pattern", "original_model", "in", ["gpt-4o", "claude-3"])
        assert evaluate_condition(cond, ctx) is True

    def test_in_intersects_list_field(self):
        ctx = _ctx(tool_names=["search", "fetch"])
        cond = RuleCondition("tool", "tool_names", "in", ["fetch"])
        assert evaluate_condition(cond, ctx) is True

    def test_not_in(self):
        ctx = _ctx(original_model="haiku")
        cond = RuleCondition("text_pattern", "original_model", "not_in", ["gpt-4o", "claude-3"])
        assert evaluate_condition(cond, ctx) is True

    def test_in_expects_list(self):
        ctx = _ctx(original_model="gpt-4o")
        cond = RuleCondition("text_pattern", "original_model", "in", "gpt-4o")
        assert evaluate_condition(cond, ctx) is False


class TestFieldSafety:
    def test_unknown_field_returns_false(self):
        ctx = _ctx()
        cond = RuleCondition("text_pattern", "__class__", "eq", "anything")
        assert evaluate_condition(cond, ctx) is False

    def test_allowed_fields_covers_documented_set(self):
        # Doc-test: matches the documented evaluation surface.
        expected = {
            "user_text",
            "system_prompt",
            "all_text",
            "estimated_tokens",
            "conversation_turns",
            "has_code_blocks",
            "has_error_trace",
            "tool_count",
            "tool_names",
            "original_model",
            "classified_intent",
            "detected_language",
            "hour_of_day",
            "day_of_week",
            "daily_cost",
            "monthly_cost",
            "request_count_hourly",
        }
        assert ALLOWED_FIELDS == frozenset(expected)


class TestUnknownOperator:
    def test_unknown_operator_returns_false(self):
        ctx = _ctx()
        cond = RuleCondition("text_pattern", "user_text", "wat", "x")
        assert evaluate_condition(cond, ctx) is False
