"""RuleEngine — priority order + first-match + default fallback + intent."""

from __future__ import annotations

import uuid

from backend.gateway.rules.engine import RuleEngine
from backend.gateway.rules.models import (
    RoutingRule,
    RuleCondition,
)

WS = uuid.uuid4()
ACCT = uuid.uuid4()


def _rule(
    *,
    name: str,
    priority: int,
    target: str,
    conditions: list[RuleCondition] | None = None,
    is_default: bool = False,
    is_active: bool = True,
) -> RoutingRule:
    return RoutingRule(
        id=uuid.uuid4(),
        workspace_id=WS,
        account_id=ACCT,
        name=name,
        priority=priority,
        is_active=is_active,
        is_default=is_default,
        target_model=target,
        conditions=list(conditions or []),
    )


def _data(user_text: str = "hello", **extra) -> dict:
    return {"messages": [{"role": "user", "content": user_text}], **extra}


class TestPriorityOrdering:
    async def test_empty_rules_returns_none(self):
        engine = RuleEngine()
        result = await engine.evaluate(_data(), rules=[], workspace_id=WS, account_id=ACCT)
        assert result is None

    async def test_first_match_wins_by_priority_ascending(self):
        # priority 1 is highest; both rules match the input but priority 1 wins.
        rule_lo = _rule(
            name="lo",
            priority=10,
            target="cheap-model",
            conditions=[RuleCondition("text_pattern", "user_text", "contains", "hi")],
        )
        rule_hi = _rule(
            name="hi",
            priority=1,
            target="premium-model",
            conditions=[RuleCondition("text_pattern", "user_text", "contains", "hi")],
        )
        engine = RuleEngine()
        result = await engine.evaluate(
            _data("hi there"),
            rules=[rule_lo, rule_hi],
            workspace_id=WS,
            account_id=ACCT,
        )
        assert result is not None
        assert result.target_model == "premium-model"
        assert result.rule.name == "hi"

    async def test_inactive_rules_skipped(self):
        rule = _rule(
            name="off",
            priority=1,
            target="never",
            conditions=[RuleCondition("text_pattern", "user_text", "contains", "hi")],
            is_active=False,
        )
        engine = RuleEngine()
        result = await engine.evaluate(
            _data("hi"),
            rules=[rule],
            workspace_id=WS,
            account_id=ACCT,
        )
        assert result is None


class TestDefaultRule:
    async def test_default_used_when_no_specific_match(self):
        specific = _rule(
            name="specific",
            priority=1,
            target="specific-model",
            conditions=[RuleCondition("text_pattern", "user_text", "contains", "urgent")],
        )
        default = _rule(
            name="default",
            priority=99,
            target="default-model",
            is_default=True,
        )
        engine = RuleEngine()
        result = await engine.evaluate(
            _data("just chatting"),
            rules=[specific, default],
            workspace_id=WS,
            account_id=ACCT,
        )
        assert result is not None
        assert result.target_model == "default-model"

    async def test_specific_rule_beats_default(self):
        specific = _rule(
            name="specific",
            priority=1,
            target="specific-model",
            conditions=[RuleCondition("text_pattern", "user_text", "contains", "urgent")],
        )
        default = _rule(
            name="default",
            priority=99,
            target="default-model",
            is_default=True,
        )
        engine = RuleEngine()
        result = await engine.evaluate(
            _data("urgent please"),
            rules=[default, specific],
            workspace_id=WS,
            account_id=ACCT,
        )
        assert result is not None
        assert result.target_model == "specific-model"

    async def test_no_match_no_default_returns_none(self):
        rule = _rule(
            name="never",
            priority=1,
            target="x",
            conditions=[RuleCondition("text_pattern", "user_text", "contains", "nope")],
        )
        engine = RuleEngine()
        result = await engine.evaluate(
            _data("hello"),
            rules=[rule],
            workspace_id=WS,
            account_id=ACCT,
        )
        assert result is None


class TestAndLogic:
    async def test_all_conditions_must_match(self):
        rule = _rule(
            name="both",
            priority=1,
            target="x",
            conditions=[
                RuleCondition("text_pattern", "user_text", "contains", "urgent"),
                RuleCondition("token_count", "estimated_tokens", "gt", 5),
            ],
        )
        engine = RuleEngine()
        # contains "urgent" but short → must NOT match
        result = await engine.evaluate(
            _data("urgent"),
            rules=[rule],
            workspace_id=WS,
            account_id=ACCT,
        )
        assert result is None


class TestIntentClassification:
    async def test_no_classifier_intent_condition_fails_silently(self):
        rule = _rule(
            name="intent_one",
            priority=1,
            target="x",
            conditions=[RuleCondition("intent", "classified_intent", "eq", "support")],
        )
        engine = RuleEngine()
        result = await engine.evaluate(
            _data("hi"),
            rules=[rule],
            workspace_id=WS,
            account_id=ACCT,
            intent_classifier=None,
        )
        assert result is None

    async def test_classifier_invoked_lazily(self):
        class Stub:
            calls = 0

            async def classify(self, text: str):
                Stub.calls += 1
                return "support"

        stub = Stub()
        non_intent = _rule(name="other", priority=10, target="z", is_default=True)
        engine = RuleEngine()
        await engine.evaluate(
            _data("hi"),
            rules=[non_intent],
            workspace_id=WS,
            account_id=ACCT,
            intent_classifier=stub,
        )
        # No intent condition in rules → classifier never invoked.
        assert Stub.calls == 0

    async def test_classifier_invoked_when_needed_and_matches(self):
        class Stub:
            async def classify(self, text: str):
                return "support"

        rule = _rule(
            name="intent_rule",
            priority=1,
            target="support-model",
            conditions=[RuleCondition("intent", "classified_intent", "eq", "support")],
        )
        engine = RuleEngine()
        result = await engine.evaluate(
            _data("help me"),
            rules=[rule],
            workspace_id=WS,
            account_id=ACCT,
            intent_classifier=Stub(),
        )
        assert result is not None
        assert result.target_model == "support-model"


class TestBatch:
    async def test_batch_empty_rules_returns_none_per_request(self):
        engine = RuleEngine()
        results = await engine.evaluate_batch(
            [_data("a"), _data("b")], rules=[], workspace_id=WS, account_id=ACCT
        )
        assert results == [None, None]

    async def test_batch_matches_per_request(self):
        rule = _rule(
            name="urgent",
            priority=1,
            target="urgent-model",
            conditions=[RuleCondition("text_pattern", "user_text", "contains", "urgent")],
        )
        default = _rule(name="default", priority=99, target="default-model", is_default=True)
        engine = RuleEngine()
        results = await engine.evaluate_batch(
            [_data("urgent fix"), _data("hello")],
            rules=[rule, default],
            workspace_id=WS,
            account_id=ACCT,
        )
        assert results[0] is not None and results[0].target_model == "urgent-model"
        assert results[1] is not None and results[1].target_model == "default-model"

    async def test_batch_dedupes_intent_classifier_calls(self):
        calls: list[str] = []

        class Stub:
            async def classify(self, text: str):
                calls.append(text)
                return "support"

        rule = _rule(
            name="support",
            priority=1,
            target="support-model",
            conditions=[RuleCondition("intent", "classified_intent", "eq", "support")],
        )
        engine = RuleEngine()
        # Same text twice + one unique.
        await engine.evaluate_batch(
            [_data("help"), _data("help"), _data("other")],
            rules=[rule],
            workspace_id=WS,
            account_id=ACCT,
            intent_classifier=Stub(),
        )
        # Classifier invoked once per unique text (2 unique texts).
        assert sorted(calls) == ["help", "other"]


class TestTrace:
    async def test_trace_records_matched_and_failed(self):
        miss = _rule(
            name="miss",
            priority=1,
            target="x",
            conditions=[RuleCondition("text_pattern", "user_text", "contains", "nope")],
        )
        hit = _rule(
            name="hit",
            priority=2,
            target="y",
            conditions=[RuleCondition("text_pattern", "user_text", "contains", "hi")],
        )
        engine = RuleEngine()
        result = await engine.evaluate(
            _data("hi"),
            rules=[miss, hit],
            workspace_id=WS,
            account_id=ACCT,
        )
        assert result is not None
        names = [t["rule"] for t in (result.trace or [])]
        assert names == ["miss", "hit"]
        assert result.trace[0]["matched"] is False
        assert result.trace[1]["matched"] is True
