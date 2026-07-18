# bsvibe:stable-internal — modifications require a design doc update.
# Owners: extensions/domain
"""Extension Protocols — Lift G publication surface, Lift S SDK re-export.

Design source: ``~/Docs/BSVibe_Class_Architecture_Design_2026-05-30.md``
v8 §13 Lift G + Lift S + D33 + D39 + D42 + v2 §7 extension hooks.

Lift G formalized the engine-facing Protocols (``Plugin``, ``Skill``,
``Action``) and published three forward-looking hook surfaces
(``ActionDispatchInterceptor``, ``SettlementSubscriber``, ``EventBus`` +
``EventBusSubscriber``) with zero registered implementations.

Lift S introduces the external ``bsvibe_sdk`` package. The
plugin-author-facing Protocols (``Plugin``, ``Action``,
``EventBusSubscriber``, ``Event``) now live there and are re-exported
here so existing backend importers keep working unchanged. Per v8 §D42
the SDK is plugin-only — ``Skill`` and the hook Protocols
(``ActionDispatchInterceptor``, ``SettlementSubscriber``, ``EventBus``,
plus their dataclass payloads) remain backend-only and continue to live
in this module.

Lift I subdivides this single file into proper domain-layer modules.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

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


@dataclass(frozen=True)
class ActionInvocation:
    """Pre-dispatch payload handed to an :class:`ActionDispatchInterceptor`.

    Kept tiny on purpose — the interceptor surface is publication-only in
    Lift G and the production gate (the per-call DangerAnalyzer rolled
    back in Lift 0a) is no longer wired. Concrete fields stabilize in
    Lift I once the dispatch layer is folded under Router.
    """

    workspace_id: str
    plugin_name: str
    action_name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class DispatchDecision:
    """Allow / deny verdict from an interceptor. ``reason`` is human-readable
    and shown to the caller on deny."""

    allowed: bool
    reason: str = ""


@dataclass(frozen=True)
class SettlementOutcome:
    """Post-dispatch settlement signal for :class:`SettlementSubscriber`.

    The hook is intentionally observer-shaped — subscribers cannot reverse
    a settlement decision, only react (notify, audit, schedule a
    compensation through the existing delivery layer).
    """

    workspace_id: str
    action_path: str
    status: str  # one of: "settled", "rolled_back", "expired"
    detail: dict[str, Any]


# ---------------------------------------------------------------------------
# Backend-only engine-formalizing Protocols
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
class ActionDispatchInterceptor(Protocol):
    """Pre-action gate — invoked before the runner dispatches an action.

    Replaces the rolled-back per-call DangerAnalyzer wiring (Lift 0a) with
    a Protocol surface that *future* impls can register against. Lift G
    registers no impls; the production code path does not call
    interceptors yet."""

    async def before_dispatch(self, invocation: ActionInvocation) -> DispatchDecision: ...


@runtime_checkable
class SettlementSubscriber(Protocol):
    """Settlement / rollback observer — invoked after Safe Mode queue
    decisions or compensation fires (Lift 0b rolled back the
    auto-compensation default; this Protocol becomes the formal seam when
    settlement re-lands behind an explicit subscriber)."""

    async def on_settlement(self, outcome: SettlementOutcome) -> None: ...


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
    "ActionDispatchInterceptor",
    "ActionInvocation",
    "DispatchDecision",
    "EventBus",
    "SettlementOutcome",
    "SettlementSubscriber",
    "Skill",
]
