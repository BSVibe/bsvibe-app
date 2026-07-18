"""``InProcessEventBus`` — synchronous prefix-routed in-process dispatcher.

Concrete impl of :class:`backend.extensions.domain.protocols.EventBus`. See
the package docstring for the synchronous-publish rationale (transactional
outbox).

Subscriber registration is by ``kind_prefix`` (dotted, e.g. ``audit.``).
A subscriber registered for ``audit.`` receives every event whose ``kind``
starts with ``audit.`` — ``audit.emit``, ``audit.action.dispatched``, etc.

A process-wide singleton is exposed through :func:`get_event_bus`. Tests
that need an isolated bus call :func:`reset_event_bus_for_testing`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import structlog

from backend.channels import Delivered, NoSubscriber, PublishOutcome, SubscriberRaised
from bsvibe_sdk import Event, EventBusSubscriber

logger = structlog.get_logger(__name__)


class InProcessEventBus:
    """Synchronous in-process EventBus.

    ``publish`` awaits every matching subscriber in registration order.
    Subscriber failures are caught — never re-raised, so a misbehaving sink
    can't break the producer's domain write — but they are no longer silently
    swallowed: ``publish`` returns a typed :data:`PublishOutcome` so the caller
    can tell ``Delivered`` from ``NoSubscriber`` from ``SubscriberRaised``.
    """

    def __init__(self) -> None:
        # Maintain insertion order so test assertions are deterministic.
        self._subscribers: list[tuple[str, EventBusSubscriber]] = []
        self._lock = asyncio.Lock()

    async def publish(self, event: Event) -> PublishOutcome:
        # Snapshot the subscriber list to avoid mutation-during-iteration
        # races if a subscriber registers another subscriber.
        snapshot = list(self._subscribers)
        matched = 0
        errors: list[Exception] = []
        for prefix, sub in snapshot:
            if not event.kind.startswith(prefix):
                continue
            matched += 1
            try:
                await sub.on_event(event)
            except Exception as exc:  # noqa: BLE001 — sink failures never propagate
                errors.append(exc)
                logger.error(
                    "event_bus_subscriber_failed",
                    kind=event.kind,
                    prefix=prefix,
                    subscriber=type(sub).__name__,
                    exc_info=True,
                )
        if errors:
            return SubscriberRaised(errors=tuple(errors))
        if matched == 0:
            logger.error("event_bus_no_subscriber", kind=event.kind)
            return NoSubscriber()
        return Delivered(count=matched)

    def subscribe(
        self,
        kind_prefix: str,
        subscriber: EventBusSubscriber,
    ) -> Callable[[], Awaitable[None]]:
        if not kind_prefix:
            raise ValueError("InProcessEventBus.subscribe: kind_prefix must be non-empty")
        self._subscribers.append((kind_prefix, subscriber))

        async def _unsubscribe() -> None:
            try:
                self._subscribers.remove((kind_prefix, subscriber))
            except ValueError:
                pass

        return _unsubscribe

    def registered_prefixes(self) -> list[str]:
        """Debug surface — returns the unique prefixes currently registered."""
        seen: list[str] = []
        for prefix, _ in self._subscribers:
            if prefix not in seen:
                seen.append(prefix)
        return seen


_BUS_SINGLETON: InProcessEventBus | None = None


def get_event_bus() -> InProcessEventBus:
    """Return the process-wide in-process bus singleton (lazy init)."""
    global _BUS_SINGLETON  # noqa: PLW0603 — process-wide singleton pattern
    if _BUS_SINGLETON is None:
        _BUS_SINGLETON = InProcessEventBus()
    return _BUS_SINGLETON


def reset_event_bus_for_testing() -> None:
    """Drop the singleton — next ``get_event_bus`` returns a fresh bus.

    Test helper only. Production code never calls this.
    """
    global _BUS_SINGLETON  # noqa: PLW0603 — process-wide singleton pattern
    _BUS_SINGLETON = None
