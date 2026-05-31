"""Event bus Protocol surface for plugin subscribers.

A plugin that wants to react to engine events (e.g. audit, settlement,
canonicalization) implements the :class:`EventBusSubscriber` Protocol,
typically with the :func:`on_event` decorator marker::

    from bsvibe_sdk import Event, on_event, plugin

    p = plugin(name="audit-sink", credentials=[], data_jurisdiction="local")

    @on_event(kind_prefix="audit.")
    async def record(event: Event) -> None:
        ...

Lift S publishes the Protocol + decorator marker. Lift N wires the
in-process bus and registers subscribers; until then, ``@on_event``
attaches metadata to the function but the runtime does not yet route
events to it.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

# Attribute name the engine (Lift N) will introspect to discover handlers.
_ON_EVENT_ATTR = "__bsvibe_on_event_prefix__"


@dataclass(frozen=True)
class Event:
    """Engine event envelope. ``kind`` is dotted (``audit.action.dispatched``).

    Mirrors :class:`backend.extensions.domain.protocols.Event`. Re-exported
    here so plugin authors can type-annotate without importing backend.
    """

    kind: str
    payload: dict[str, Any]


@runtime_checkable
class EventBusSubscriber(Protocol):
    """A bus subscriber. Concrete impls implement ``on_event``."""

    async def on_event(self, event: Event) -> None: ...


def on_event(
    *, kind_prefix: str
) -> Callable[[Callable[..., Awaitable[None]]], Callable[..., Awaitable[None]]]:
    """Mark a free async function as an event subscriber for ``kind_prefix``.

    The engine (Lift N) discovers decorated functions via the
    ``__bsvibe_on_event_prefix__`` attribute and registers them with the
    in-process bus. In Lift S this is metadata-only — the attribute is set
    but no runtime wiring activates it.
    """
    if not kind_prefix:
        raise ValueError("on_event: kind_prefix must be non-empty")

    def decorator(
        fn: Callable[..., Awaitable[None]],
    ) -> Callable[..., Awaitable[None]]:
        setattr(fn, _ON_EVENT_ATTR, kind_prefix)
        return fn

    return decorator


__all__ = ["Event", "EventBusSubscriber", "on_event"]
