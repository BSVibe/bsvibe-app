# bsvibe:stable-internal — modifications require a design doc update.
# Owners: extensions/domain
"""Extension Protocols — Lift G publication surface, Lift S SDK re-export.

Design source: ``~/Docs/BSVibe_Class_Architecture_Design_2026-05-30.md``
v8 §13 Lift G + Lift S + D33 + D39 + D42 + v2 §7 extension hooks.

Lift G formalized the engine-facing Protocols (``Plugin``, ``Skill``,
``Action``) and published forward-looking hook surfaces with zero registered
implementations. Two of them (a pre-dispatch interceptor and a settlement
subscriber) never got one and were deleted 2026-08-21 — a test had even
pinned "zero registered impl" as their contract. ``EventBus`` /
``EventBusSubscriber`` DID get one and stay.

Lift S introduces the external ``bsvibe_sdk`` package. The
plugin-author-facing Protocols (``Plugin``, ``Action``,
``EventBusSubscriber``, ``Event``) now live there and are re-exported
here so existing backend importers keep working unchanged. Per v8 §D42
the SDK is plugin-only — ``Skill`` and ``EventBus`` remain backend-only and
continue to live in this module.

Lift I subdivides this single file into proper domain-layer modules.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol, runtime_checkable

# Re-export the plugin-author-facing Protocols from the external SDK.
# Identity equality is preserved — ``backend.extensions.domain.protocols.Plugin``
# IS the same class as ``bsvibe_sdk.Plugin``.
from backend.channels import PublishOutcome
from bsvibe_sdk import (
    Action,
    Event,
    EventBusSubscriber,
    Plugin,
)

# ---------------------------------------------------------------------------
# Backend-only value objects (Lift G — minimal, expand in Lift I)
# ---------------------------------------------------------------------------


@runtime_checkable
class Skill(Protocol):
    """A loaded skill manifest (``SkillMeta``-shaped). Concrete carrier at
    ``backend.extensions.skill.meta.SkillMeta``.

    Skills are yaml + md *data*, not an SDK author contract (v8 §D42), so
    this Protocol stays backend-internal — it formalizes what the engine
    loader produces, not what an external author writes.
    """

    name: str
    version: str


# ---------------------------------------------------------------------------
# Forward-looking hook Protocols (Lift G publishes; ZERO registered impl)
# ---------------------------------------------------------------------------


@runtime_checkable
class EventBus(Protocol):
    """In-process event bus surface."""

    async def publish(self, event: Event) -> PublishOutcome: ...

    def subscribe(
        self,
        kind_prefix: str,
        subscriber: EventBusSubscriber,
    ) -> Callable[[], Awaitable[None]]:
        """Register ``subscriber`` for events whose ``kind`` starts with
        ``kind_prefix``; returns an async unsubscribe handle."""
        ...


__all__ = [
    # SDK re-exports (Lift S)
    "Action",
    "Event",
    "EventBusSubscriber",
    "Plugin",
    # Backend-only
    "EventBus",
    "Skill",
]
