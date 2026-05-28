"""Rule-based run routing (Phase 1).

A per-workspace, priority-ordered rule engine that selects WHICH ModelAccount
(native LLM or executor CLI) handles a run, based on the run's framed signals
(artifact type, path classification, skill, intent, pipeline stage, product).
Ported from BSGateway's rule engine and adapted to the BSVibe run domain.

When a workspace has no routing rules, resolution falls back to the legacy
"exactly one active account" policy — so existing single-account workspaces
are unaffected.
"""

from backend.routing.db import RunRoutingRuleRow
from backend.routing.engine import (
    ALLOWED_FIELDS,
    RoutingContext,
    evaluate_rules,
    resolve_route,
)

__all__ = [
    "ALLOWED_FIELDS",
    "RoutingContext",
    "RunRoutingRuleRow",
    "evaluate_rules",
    "resolve_route",
]
