"""INV-1 — the in-process bus reports a typed 3-state publish outcome.

The bus still catches every subscriber exception (best-effort — a bad sink
never breaks the producer's domain write), but the swallow is no longer
silent: ``publish`` returns ``Delivered`` / ``NoSubscriber`` / ``SubscriberRaised``
so the caller can distinguish delivered from dropped from failed.
"""

from __future__ import annotations

from structlog.testing import capture_logs

from backend.channels import Delivered, NoSubscriber, SubscriberRaised
from backend.extensions.eventbus import InProcessEventBus
from bsvibe_sdk import Event


class _Sink:
    def __init__(self) -> None:
        self.seen: list[Event] = []

    async def on_event(self, event: Event) -> None:
        self.seen.append(event)


class _Boom:
    async def on_event(self, event: Event) -> None:
        raise RuntimeError("boom")


async def test_publish_with_matching_subscriber_returns_delivered() -> None:
    bus = InProcessEventBus()
    sink = _Sink()
    bus.subscribe("audit.", sink)

    outcome = await bus.publish(Event(kind="audit.emit", payload={}))

    assert outcome == Delivered(count=1)
    assert len(sink.seen) == 1


async def test_publish_counts_every_matching_subscriber() -> None:
    bus = InProcessEventBus()
    bus.subscribe("audit.", _Sink())
    bus.subscribe("audit.", _Sink())

    outcome = await bus.publish(Event(kind="audit.emit", payload={}))

    assert outcome == Delivered(count=2)


async def test_publish_with_no_matching_subscriber_returns_no_subscriber() -> None:
    bus = InProcessEventBus()
    bus.subscribe("other.", _Sink())

    outcome = await bus.publish(Event(kind="audit.emit", payload={}))

    assert outcome == NoSubscriber()


async def test_publish_with_throwing_subscriber_returns_subscriber_raised() -> None:
    bus = InProcessEventBus()
    bus.subscribe("audit.", _Boom())

    # Best-effort: the caller does NOT see the exception.
    outcome = await bus.publish(Event(kind="audit.emit", payload={}))

    assert isinstance(outcome, SubscriberRaised)
    assert len(outcome.errors) == 1
    assert isinstance(outcome.errors[0], RuntimeError)


async def test_a_raising_subscriber_does_not_stop_other_subscribers() -> None:
    bus = InProcessEventBus()
    good = _Sink()
    bus.subscribe("audit.", _Boom())
    bus.subscribe("audit.", good)

    outcome = await bus.publish(Event(kind="audit.emit", payload={}))

    # One raised → SubscriberRaised, but the good sink still received the event.
    assert isinstance(outcome, SubscriberRaised)
    assert len(good.seen) == 1


async def test_publish_raise_is_logged_at_error() -> None:
    bus = InProcessEventBus()
    bus.subscribe("audit.", _Boom())

    with capture_logs() as logs:
        await bus.publish(Event(kind="audit.emit", payload={}))

    assert any(
        log["event"] == "event_bus_subscriber_failed" and log["log_level"] == "error"
        for log in logs
    )


async def test_publish_no_subscriber_is_logged_at_error() -> None:
    bus = InProcessEventBus()

    with capture_logs() as logs:
        await bus.publish(Event(kind="audit.emit", payload={}))

    assert any(
        log["event"] == "event_bus_no_subscriber" and log["log_level"] == "error" for log in logs
    )
