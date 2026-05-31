"""Priority-first-match rule engine.

The engine is purely synchronous against an already-loaded rule set —
the repository is responsible for filtering by ``(workspace_id,
account_id)`` and ordering. The engine handles:

* skipping inactive rules
* deferring the default rule until specifics are exhausted
* AND-evaluating conditions within a rule
* lazy intent classification (only when at least one rule has an
  ``intent`` condition)
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any, Protocol, runtime_checkable

import structlog

from backend.router.rules.conditions import evaluate_condition
from backend.router.rules.models import (
    EvaluationContext,
    RoutingRule,
    RuleMatch,
)

logger = structlog.get_logger(__name__)


@runtime_checkable
class IntentClassifierProtocol(Protocol):
    """Embedding-based intent classifier supplied by ``backend.router.rules.intent`` (1.5b)."""

    async def classify(self, text: str) -> str | None: ...


class RuleEngine:
    """Stateless evaluator — instantiate once, reuse across requests."""

    async def evaluate(
        self,
        data: dict[str, Any],
        *,
        rules: list[RoutingRule],
        workspace_id: uuid.UUID,
        account_id: uuid.UUID,
        intent_classifier: IntentClassifierProtocol | None = None,
    ) -> RuleMatch | None:
        if not rules:
            return None

        ctx = EvaluationContext.from_request(
            data,
            workspace_id=workspace_id,
            account_id=account_id,
        )

        if intent_classifier is not None and self._needs_intent(rules):
            ctx.classified_intent = await intent_classifier.classify(ctx.user_text)

        return self._match(rules, ctx)

    async def evaluate_batch(
        self,
        requests: list[dict[str, Any]],
        *,
        rules: list[RoutingRule],
        workspace_id: uuid.UUID,
        account_id: uuid.UUID,
        intent_classifier: IntentClassifierProtocol | None = None,
    ) -> list[RuleMatch | None]:
        """Evaluate many requests, batching intent classification.

        Cuts intent-classifier round-trips when many parallel requests
        share the same user text.
        """
        if not rules:
            return [None] * len(requests)

        intent_cache: dict[str, str | None] = {}
        if intent_classifier is not None and self._needs_intent(rules):
            unique_texts = list({_last_user(req) for req in requests})
            results = await asyncio.gather(*(intent_classifier.classify(t) for t in unique_texts))
            intent_cache = dict(zip(unique_texts, results, strict=True))

        out: list[RuleMatch | None] = []
        for req in requests:
            ctx = EvaluationContext.from_request(
                req,
                workspace_id=workspace_id,
                account_id=account_id,
            )
            text = _last_user(req)
            if text in intent_cache:
                ctx.classified_intent = intent_cache[text]
            out.append(self._match(rules, ctx))
        return out

    @staticmethod
    def _needs_intent(rules: list[RoutingRule]) -> bool:
        for rule in rules:
            if not rule.is_active:
                continue
            for cond in rule.conditions:
                if cond.condition_type == "intent":
                    return True
        return False

    def _match(
        self,
        rules: list[RoutingRule],
        ctx: EvaluationContext,
    ) -> RuleMatch | None:
        trace: list[dict[str, Any]] = []
        default_rule: RoutingRule | None = None

        for rule in sorted(rules, key=lambda r: r.priority):
            if not rule.is_active:
                continue
            if rule.is_default:
                # Hold default aside; only used if no specifics match.
                default_rule = rule
                continue
            if self._rule_matches(rule, ctx, trace):
                return RuleMatch(rule=rule, target_model=rule.target_model, trace=trace)

        if default_rule is not None:
            return RuleMatch(rule=default_rule, target_model=default_rule.target_model, trace=trace)
        return None

    @staticmethod
    def _rule_matches(
        rule: RoutingRule,
        ctx: EvaluationContext,
        trace: list[dict[str, Any]],
    ) -> bool:
        for cond in rule.conditions:
            if not evaluate_condition(cond, ctx):
                trace.append(
                    {
                        "rule": rule.name,
                        "priority": rule.priority,
                        "matched": False,
                        "failed_condition": {
                            "type": cond.condition_type,
                            "field": cond.field,
                            "operator": cond.operator,
                        },
                    }
                )
                return False
        trace.append({"rule": rule.name, "priority": rule.priority, "matched": True})
        return True


def _last_user(req: dict[str, Any]) -> str:
    for m in reversed(req.get("messages", [])):
        if m.get("role") == "user":
            return str(m.get("content", ""))
    return ""
