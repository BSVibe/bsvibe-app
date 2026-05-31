"""Routing-rule engine + repository + cache (Bundle 1.5a)."""

from backend.router.rules.cache import InMemoryRulesCache, RulesCache
from backend.router.rules.conditions import ALLOWED_FIELDS, evaluate_condition
from backend.router.rules.db import (
    GatewayRulesBase,
    RoutingRuleRow,
    RuleConditionRow,
)
from backend.router.rules.engine import IntentClassifierProtocol, RuleEngine
from backend.router.rules.models import (
    EvaluationContext,
    RoutingRule,
    RuleCondition,
    RuleMatch,
)
from backend.router.rules.repository import RuleDuplicateError, RulesRepository

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
