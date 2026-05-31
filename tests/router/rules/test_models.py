"""EvaluationContext extraction + RoutingRule/RuleCondition dataclasses."""

from __future__ import annotations

import uuid

from backend.router.rules.models import (
    EvaluationContext,
    RoutingRule,
    RuleCondition,
)


class TestEvaluationContextFromRequest:
    def test_extracts_user_text_from_messages(self):
        ctx = EvaluationContext.from_request(
            {
                "messages": [
                    {"role": "user", "content": "hello"},
                    {"role": "assistant", "content": "hi"},
                    {"role": "user", "content": "follow up"},
                ],
                "model": "gpt-4o-mini",
            },
            workspace_id=uuid.uuid4(),
            account_id=uuid.uuid4(),
        )
        assert "follow up" in ctx.user_text
        assert ctx.conversation_turns == 2
        assert ctx.original_model == "gpt-4o-mini"

    def test_detects_code_blocks(self):
        ctx = EvaluationContext.from_request(
            {"messages": [{"role": "user", "content": "look:\n```py\nprint(1)\n```"}]},
            workspace_id=uuid.uuid4(),
            account_id=uuid.uuid4(),
        )
        assert ctx.has_code_blocks is True

    def test_detects_error_trace(self):
        ctx = EvaluationContext.from_request(
            {"messages": [{"role": "user", "content": "got Traceback (most recent call last)"}]},
            workspace_id=uuid.uuid4(),
            account_id=uuid.uuid4(),
        )
        assert ctx.has_error_trace is True

    def test_extracts_tool_names(self):
        ctx = EvaluationContext.from_request(
            {
                "messages": [],
                "tools": [
                    {"function": {"name": "search"}},
                    {"function": {"name": "fetch"}},
                ],
            },
            workspace_id=uuid.uuid4(),
            account_id=uuid.uuid4(),
        )
        assert ctx.tool_count == 2
        assert set(ctx.tool_names) == {"search", "fetch"}

    def test_estimates_tokens_nonzero_for_text(self):
        ctx = EvaluationContext.from_request(
            {"messages": [{"role": "user", "content": "the quick brown fox jumps"}]},
            workspace_id=uuid.uuid4(),
            account_id=uuid.uuid4(),
        )
        assert ctx.estimated_tokens > 0

    def test_carries_workspace_and_account_ids(self):
        ws = uuid.uuid4()
        acct = uuid.uuid4()
        ctx = EvaluationContext.from_request(
            {"messages": [{"role": "user", "content": "x"}]},
            workspace_id=ws,
            account_id=acct,
        )
        assert ctx.workspace_id == ws
        assert ctx.account_id == acct


class TestDataclasses:
    def test_routing_rule_defaults_empty_conditions(self):
        rule = RoutingRule(
            id=uuid.uuid4(),
            workspace_id=uuid.uuid4(),
            account_id=uuid.uuid4(),
            name="default",
            priority=10,
            is_active=True,
            is_default=False,
            target_model="ollama/llama3.2",
        )
        assert rule.conditions == []

    def test_rule_condition_negate_defaults_false(self):
        cond = RuleCondition(
            condition_type="text_pattern",
            field="user_text",
            operator="contains",
            value="urgent",
        )
        assert cond.negate is False
