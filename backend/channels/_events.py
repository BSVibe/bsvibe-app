"""EventChannel — a declared, typed bus publish/subscribe coupling (INV-1).

Sibling of :class:`~backend.channels._core.Channel`. Where a ``Channel``
guards a durable *row* seam (staged through a repository ``add``), an
:class:`EventChannel` guards a *bus topic*: the publish→subscribe coupling on
the in-process :class:`~backend.extensions.eventbus.InProcessEventBus`. It is
a declaration plus a guard wrapper — declared publisher and subscriber ids —
so a bus topic is a **typed object** rather than a bare dotted string no tool
can see. An orphaned half (a kind nobody subscribes to, a subscriber with no
declared publisher) is a build failure via the meta-tests in
``tests/architecture/test_channel_registry.py``.

The bus is best-effort by design (a misbehaving sink must never roll back the
producer's domain write — audit is an observer, not a safety gate). The
coupling's job is therefore not to change *when* delivery fails but to make
the outcome **observable**: :meth:`EventChannel.publish` returns a typed
:data:`PublishOutcome` 3-state so ``Delivered`` / ``NoSubscriber`` /
``SubscriberRaised`` are distinguishable at the call site instead of collapsing
into a silent swallow.

Two rules keep the abstraction honest:

* ``publish`` asserts its ``publisher_id`` is declared; ``subscribe`` asserts
  its ``subscriber_id`` is declared. An undeclared id raises rather than
  silently coupling through a string.
* The bus is prefix-routed, so a subscriber registers under a dotted
  ``subscribe_prefix`` that must *cover* the channel's exact ``kind`` (the
  invariant is asserted at construction). One prefix may fan a whole event
  family into a single subscriber; the channel declares the exact kind its
  publisher emits.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Generic, Protocol, TypeVar

from bsvibe_sdk import EventBusSubscriber

TEvent = TypeVar("TEvent")
TEvent_contra = TypeVar("TEvent_contra", contravariant=True)


class UndeclaredPublisherError(RuntimeError):
    """A publish was attempted with a ``publisher_id`` the channel does not declare."""


class UndeclaredSubscriberError(RuntimeError):
    """A subscribe was attempted with a ``subscriber_id`` the channel does not declare."""


@dataclass(frozen=True)
class Delivered:
    """At least one subscriber matched the event's kind and none raised."""

    count: int


@dataclass(frozen=True)
class NoSubscriber:
    """No subscriber matched the event's kind — the event was dropped."""


@dataclass(frozen=True)
class SubscriberRaised:
    """At least one matched subscriber raised.

    The exceptions were caught (best-effort — the producer's domain write is
    never rolled back by a sink failure) and are surfaced here so the outcome
    is observable rather than silently swallowed.
    """

    errors: tuple[Exception, ...]


PublishOutcome = Delivered | NoSubscriber | SubscriberRaised
"""Typed 3-state result of a bus publish (INV-2)."""


class SupportsPublish(Protocol[TEvent_contra]):
    """The publish half of the bus an :class:`EventChannel` guards."""

    async def publish(self, event: TEvent_contra) -> PublishOutcome: ...


class SupportsSubscribe(Protocol):
    """The subscribe half of the bus an :class:`EventChannel` guards."""

    def subscribe(
        self, kind_prefix: str, subscriber: EventBusSubscriber
    ) -> Callable[[], Awaitable[None]]: ...


@dataclass(frozen=True)
class EventChannel(Generic[TEvent]):
    """A declared publisher→subscriber coupling over a single bus ``kind``."""

    kind: str
    event_type: type[TEvent]
    publishers: tuple[str, ...]
    subscribers: tuple[str, ...]
    subscribe_prefix: str

    def __post_init__(self) -> None:
        if not self.kind.startswith(self.subscribe_prefix):
            raise ValueError(
                f"EventChannel {self.kind!r}: subscribe_prefix "
                f"{self.subscribe_prefix!r} does not cover the channel kind"
            )

    def assert_publisher(self, publisher_id: str) -> None:
        if publisher_id not in self.publishers:
            raise UndeclaredPublisherError(
                f"{publisher_id!r} is not a declared publisher of event channel "
                f"{self.kind!r} (declared: {self.publishers})"
            )

    def assert_subscriber(self, subscriber_id: str) -> None:
        if subscriber_id not in self.subscribers:
            raise UndeclaredSubscriberError(
                f"{subscriber_id!r} is not a declared subscriber of event channel "
                f"{self.kind!r} (declared: {self.subscribers})"
            )

    async def publish(
        self,
        bus: SupportsPublish[TEvent],
        event: TEvent,
        *,
        publisher_id: str,
    ) -> PublishOutcome:
        """Assert the publisher, then publish through ``bus``.

        Returns the bus's typed :data:`PublishOutcome`. Delivery is
        best-effort (the bus catches subscriber exceptions), so the return
        value — not a raise — is how the caller learns delivery failed.
        """
        self.assert_publisher(publisher_id)
        return await bus.publish(event)

    def subscribe(
        self,
        bus: SupportsSubscribe,
        subscriber: EventBusSubscriber,
        *,
        subscriber_id: str,
    ) -> Callable[[], Awaitable[None]]:
        """Assert the subscriber, then register it under ``subscribe_prefix``.

        Returns the bus's async unsubscribe handle.
        """
        self.assert_subscriber(subscriber_id)
        return bus.subscribe(self.subscribe_prefix, subscriber)


__all__ = [
    "Delivered",
    "EventChannel",
    "NoSubscriber",
    "PublishOutcome",
    "SubscriberRaised",
    "SupportsPublish",
    "SupportsSubscribe",
    "UndeclaredPublisherError",
    "UndeclaredSubscriberError",
]
