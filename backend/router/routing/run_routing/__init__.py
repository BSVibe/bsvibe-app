"""Rule-based run routing (Lift E2 — classifier-free).

Per-workspace, priority-ordered rules that select WHICH ModelAccount
handles a run, keyed on the dispatch caller_id + the run's framed
signals. When no rule matches and the workspace has no
``default_account_id``, dispatch fails with
:class:`~backend.dispatch.resolver.NoMatchingRouteError` (never a silent
pick).
"""

from backend.router.routing.run_routing.db import RunRoutingRuleRow
from backend.router.routing.run_routing.engine import (
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
