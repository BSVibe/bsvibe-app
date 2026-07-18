"""Unit tests for the :class:`EventChannel` bus-coupling sibling (INV-1)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import FrozenInstanceError

import pytest

from backend.channels import (
    Delivered,
    EventChannel,
    NoSubscriber,
    PublishOutcome,
    SubscriberRaised,
    UndeclaredPublisherError,
    UndeclaredSubscriberError,
)
from bsvibe_sdk import Event, EventBusSubscriber


class _StubBus:
    """Minimal publish+subscribe bus for EventChannel unit tests."""

    def __init__(self, outcome: PublishOutcome) -> None:
        self._outcome = outcome
        self.published: list[Event] = []
        self.subscriptions: list[tuple[str, EventBusSubscriber]] = []

    async def publish(self, event: Event) -> PublishOutcome:
        self.published.append(event)
        return self._outcome

    def subscribe(
        self, kind_prefix: str, subscriber: EventBusSubscriber
    ) -> Callable[[], Awaitable[None]]:
        self.subscriptions.append((kind_prefix, subscriber))

        async def _unsub() -> None:
            self.subscriptions.remove((kind_prefix, subscriber))

        return _unsub


class _Sink:
    async def on_event(self, event: Event) -> None:  # pragma: no cover - trivial
        return None


def _channel() -> EventChannel[Event]:
    return EventChannel(
        kind="audit.emit",
        event_type=Event,
        publishers=("audit:safe_emit",),
        subscribers=("audit:outbox_subscriber",),
        subscribe_prefix="audit.",
    )


def test_channel_is_frozen() -> None:
    channel = _channel()
    with pytest.raises(FrozenInstanceError):
        channel.kind = "renamed"  # type: ignore[misc]


def test_assert_publisher_passes_for_declared_id() -> None:
    _channel().assert_publisher("audit:safe_emit")


def test_assert_publisher_rejects_undeclared_id() -> None:
    with pytest.raises(UndeclaredPublisherError):
        _channel().assert_publisher("audit:intruder")


async def test_publish_asserts_publisher_and_forwards_to_bus() -> None:
    channel = _channel()
    bus = _StubBus(Delivered(count=1))
    event = Event(kind="audit.emit", payload={})

    outcome = await channel.publish(bus, event, publisher_id="audit:safe_emit")

    assert outcome == Delivered(count=1)
    assert bus.published == [event]


async def test_publish_rejects_undeclared_publisher_without_forwarding() -> None:
    channel = _channel()
    bus = _StubBus(Delivered(count=1))

    with pytest.raises(UndeclaredPublisherError):
        await channel.publish(
            bus, Event(kind="audit.emit", payload={}), publisher_id="audit:intruder"
        )

    assert bus.published == []


def test_subscribe_asserts_subscriber_and_registers_prefix() -> None:
    channel = _channel()
    bus = _StubBus(NoSubscriber())
    sink = _Sink()

    channel.subscribe(bus, sink, subscriber_id="audit:outbox_subscriber")

    assert bus.subscriptions == [("audit.", sink)]


def test_subscribe_rejects_undeclared_subscriber_without_registering() -> None:
    channel = _channel()
    bus = _StubBus(NoSubscriber())

    with pytest.raises(UndeclaredSubscriberError):
        channel.subscribe(bus, _Sink(), subscriber_id="audit:intruder")

    assert bus.subscriptions == []


def test_subscribe_prefix_must_cover_kind() -> None:
    with pytest.raises(ValueError, match="subscribe_prefix"):
        EventChannel(
            kind="audit.emit",
            event_type=Event,
            publishers=("p",),
            subscribers=("s",),
            subscribe_prefix="other.",
        )


def test_publish_outcomes_are_frozen_dataclasses() -> None:
    assert Delivered(count=2).count == 2
    assert isinstance(NoSubscriber(), NoSubscriber)
    err = RuntimeError("boom")
    raised = SubscriberRaised(errors=(err,))
    assert raised.errors == (err,)


def test_delivered_is_immutable() -> None:
    with pytest.raises(FrozenInstanceError):
        Delivered(count=1).count = 9  # type: ignore[misc]
