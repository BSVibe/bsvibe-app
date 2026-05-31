"""Extension Protocols — Lift G publication-only surface.

Design source: ``~/Docs/BSVibe_Class_Architecture_Design_2026-05-30.md``
v8 §13 Lift G + D33 + v2 §7 extension hooks.

These Protocols formalize what the (newly-merged) plugin engine + skill
engine already produce, *plus* three forward-looking hook surfaces
(``ActionDispatchInterceptor``, ``SettlementSubscriber``, ``EventBus`` +
``EventBusSubscriber``) that have **zero registered implementations** in
this lift. Lift I subdivides this single file into proper domain layer
modules; Lift S publishes them as part of the external ``bsvibe_sdk``.

NOTE: This is a *staging* location. Importers should treat these as the
authoritative extension surface, not the engine internals under
``backend.extensions.plugin`` / ``backend.extensions.skill``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

# ---------------------------------------------------------------------------
# Extension-shaped value objects (Lift G — minimal, expand in Lift I/S)
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


@dataclass(frozen=True)
class Event:
    """Generic envelope for :class:`EventBus`. ``kind`` is a dotted
    namespace (e.g. ``"audit.action.dispatched"``); ``payload`` is opaque
    to the bus."""

    kind: str
    payload: dict[str, Any]


# ---------------------------------------------------------------------------
# Engine-formalizing Protocols (what the loader/runner already produce)
# ---------------------------------------------------------------------------


@runtime_checkable
class Action(Protocol):
    """An exposed action surface inside a plugin (``@p.action(...)`` decorator).

    The concrete capability dataclass lives at
    ``backend.extensions.plugin.base.ActionCapability``; this Protocol
    publishes only the call shape relied on by the dispatch path."""

    name: str

    async def __call__(self, context: Any, /, **kwargs: Any) -> Any: ...


@runtime_checkable
class Plugin(Protocol):
    """A loaded plugin instance (``PluginMeta``-shaped).

    The concrete carrier is
    ``backend.extensions.plugin.base.PluginMeta``; this Protocol publishes
    the engine-facing surface used outside the loader."""

    name: str

    def list_actions(self) -> list[str]: ...


@runtime_checkable
class Skill(Protocol):
    """A loaded skill manifest (``SkillMeta``-shaped). Concrete carrier at
    ``backend.extensions.skill.meta.SkillMeta``."""

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
class EventBusSubscriber(Protocol):
    """Bus subscriber — receives :class:`Event` instances filtered by
    ``kind`` prefix. Audit is the first *concrete* user surfaced under
    ``backend.extensions.implementations.audit`` but is not registered as
    a subscriber in this lift — Lift I/N wires it."""

    async def on_event(self, event: Event) -> None: ...


@runtime_checkable
class EventBus(Protocol):
    """In-process event bus surface."""

    async def publish(self, event: Event) -> None: ...

    def subscribe(
        self,
        kind_prefix: str,
        subscriber: EventBusSubscriber,
    ) -> Callable[[], Awaitable[None]]:
        """Register ``subscriber`` for events whose ``kind`` starts with
        ``kind_prefix``; returns an async unsubscribe handle."""
        ...


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
