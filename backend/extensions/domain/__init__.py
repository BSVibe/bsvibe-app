"""Extensions context — domain layer (Protocol publication surface).

Lift G keeps a single ``protocols.py`` module here as the staging
location; Lift I will subdivide it into proper layered modules.
"""

from __future__ import annotations

from backend.extensions.domain.protocols import (
    Action,
    Event,
    EventBus,
    EventBusSubscriber,
    Plugin,
    Skill,
)

__all__ = [
    "Action",
    "Event",
    "EventBus",
    "EventBusSubscriber",
    "Plugin",
    "Skill",
]
