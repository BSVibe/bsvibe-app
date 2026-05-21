"""Routing-rule engine + repository + cache (Bundle 1.5a)."""

from backend.gateway.rules.cache import InMemoryRulesCache, RulesCache
from backend.gateway.rules.conditions import ALLOWED_FIELDS, evaluate_condition
from backend.gateway.rules.db import (
    GatewayRulesBase,
    RoutingRuleRow,
    RuleConditionRow,
)
from backend.gateway.rules.engine import IntentClassifierProtocol, RuleEngine
from backend.gateway.rules.models import (
    EvaluationContext,
    RoutingRule,
    RuleCondition,
    RuleMatch,
)
from backend.gateway.rules.repository import RuleDuplicateError, RulesRepository

__all__ = [
    "ALLOWED_FIELDS",
    "EvaluationContext",
    "GatewayRulesBase",
    "InMemoryRulesCache",
    "IntentClassifierProtocol",
    "RoutingRule",
    "RoutingRuleRow",
    "RuleCondition",
    "RuleConditionRow",
    "RuleDuplicateError",
    "RuleEngine",
    "RuleMatch",
    "RulesCache",
    "RulesRepository",
    "evaluate_condition",
]
