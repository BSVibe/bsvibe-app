"""Extensions context — domain layer (Protocol publication surface).

Lift G keeps a single ``protocols.py`` module here as the staging
location; Lift I will subdivide it into proper layered modules.
"""

from __future__ import annotations

from backend.extensions.domain.protocols import (
    Action,
    ActionDispatchInterceptor,
    ActionInvocation,
    DispatchDecision,
    Event,
    EventBus,
    EventBusSubscriber,
    Plugin,
    SettlementOutcome,
    SettlementSubscriber,
    Skill,
)

__all__ = [
    "Action",
    "ActionDispatchInterceptor",
    "ActionInvocation",
    "DispatchDecision",
    "Event",
    "EventBus",
    "EventBusSubscriber",
    "Plugin",
    "SettlementOutcome",
    "SettlementSubscriber",
    "Skill",
]
