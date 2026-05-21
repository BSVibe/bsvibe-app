"""Condition evaluators used by :class:`RuleEngine`."""

from __future__ import annotations

import re
from typing import Any

import structlog

from backend.gateway.rules.models import EvaluationContext, RuleCondition

logger = structlog.get_logger(__name__)

# Reject patterns with nested quantifiers — they can ReDoS the worker.
_REDOS_PATTERN = re.compile(r"\(.+[*+]\)[*+?]|\[.+[*+]\][*+?]")

# Hard cap on regex length to bound worst-case compile time.
_MAX_PATTERN_LEN = 500

# Whitelist of evaluable fields on :class:`EvaluationContext`. Anything
# outside this set short-circuits to False — protects against access to
# internal / dunder attributes via crafted rule payloads.
ALLOWED_FIELDS: frozenset[str] = frozenset(
    {
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
)


def evaluate_condition(condition: RuleCondition, ctx: EvaluationContext) -> bool:
    """Return True if ``condition`` matches ``ctx`` (after negate)."""
    if condition.field not in ALLOWED_FIELDS:
        logger.warning(
            "rules.invalid_field",
            field=condition.field,
            hint="condition will never match — check for typos",
        )
        return False
    result = _evaluate_raw(condition, ctx)
    return (not result) if condition.negate else result


def _evaluate_raw(condition: RuleCondition, ctx: EvaluationContext) -> bool:  # noqa: PLR0911 — operator dispatch
    field_value = getattr(ctx, condition.field, None)
    op = condition.operator
    expected = condition.value

    if op == "eq":
        return bool(field_value == expected)
    if op == "contains":
        return _str_contains(field_value, expected)
    if op == "regex":
        return _regex_match(field_value, expected)
    if op == "gt":
        return _numeric(field_value) > _numeric(expected)
    if op == "lt":
        return _numeric(field_value) < _numeric(expected)
    if op == "gte":
        return _numeric(field_value) >= _numeric(expected)
    if op == "lte":
        return _numeric(field_value) <= _numeric(expected)
    if op == "between":
        if not isinstance(expected, list) or len(expected) != 2:
            return False
        v = _numeric(field_value)
        return _numeric(expected[0]) <= v <= _numeric(expected[1])
    if op == "in":
        return _check_in(field_value, expected)
    if op == "not_in":
        return not _check_in(field_value, expected)
    return False


def _regex_match(field_value: Any, expected: Any) -> bool:
    pattern = str(expected)
    if len(pattern) > _MAX_PATTERN_LEN:
        return False
    if _REDOS_PATTERN.search(pattern):
        return False
    try:
        return bool(re.search(pattern, str(field_value), re.IGNORECASE))
    except re.error:
        return False


def _str_contains(haystack: Any, needle: Any) -> bool:
    if haystack is None:
        return False
    return str(needle).lower() in str(haystack).lower()


def _numeric(value: Any) -> float:
    # None is treated as 0.0 so "daily_cost < 10" still fires when the
    # cost feed hasn't populated yet — matches BSGateway prod semantics.
    if value is None:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _check_in(field_value: Any, expected_list: Any) -> bool:
    if not isinstance(expected_list, list):
        return False
    if isinstance(field_value, list):
        return bool(set(field_value) & set(expected_list))
    return field_value in expected_list
