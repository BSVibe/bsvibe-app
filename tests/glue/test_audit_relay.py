"""HttpRelay — configurable HTTP audit sink + config-driven relay selection.

The production worker runtime drains the ``audit_outbox`` via a
:class:`~backend.workflow.infrastructure.workers.relay_worker.Relay`. By default that is the
:class:`~backend.workflow.infrastructure.workers.run.LoggingRelay` (log + ack, no remote sink). When
``settings.audit_relay_url`` is set, the runtime instead wires an
:class:`~backend.workers.relays.HttpRelay` that POSTs the batch to that URL.

Contract proven here (with respx-mocked httpx — no real network):

* 2xx → the whole batch's ids are acked (delivered).
* non-2xx → ``[]`` acked (nothing lost), no raise — rows stay in the outbox
  and are retried next tick.
* network error → ``[]``, no raise (soft-fail, the worker loop never crashes).
* empty batch → ``[]`` and NO POST is issued.

Plus the config-driven selection helper: ``HttpRelay`` when the URL is set,
``LoggingRelay`` when it is empty.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import httpx
import pytest
import respx

from backend.config import Settings
from backend.workers.relays import HttpRelay, build_relay
from backend.workflow.infrastructure.workers.run import LoggingRelay
from plugin.audit.models import AuditOutboxRecord

pytestmark = pytest.mark.asyncio

_SINK_URL = "https://audit.example.test/ingest"


def _records(ids: Sequence[int]) -> list[AuditOutboxRecord]:
    """A batch of outbox rows with explicit ids (the relay serializes these)."""
    return [
        AuditOutboxRecord(
            id=rid,
            event_id=f"event-{rid}",
            event_type="gateway.completion.dispatched",
            occurred_at=datetime(2026, 5, 23, 12, 0, 0, tzinfo=UTC),
            payload={"i": rid},
        )
        for rid in ids
    ]


@respx.mock
async def test_http_relay_2xx_acks_whole_batch() -> None:
    route = respx.post(_SINK_URL).mock(return_value=httpx.Response(200, json={"ok": True}))
    relay = HttpRelay(url=_SINK_URL)

    delivered = await relay.send(_records([1, 2, 3]))

    assert sorted(delivered) == [1, 2, 3]
    assert route.called
    # The batch is POSTed as JSON carrying each record's id + payload.
    sent = route.calls.last.request
    body = httpx.Response(200, content=sent.content).json()
    assert {r["id"] for r in body["records"]} == {1, 2, 3}
    assert {r["event_id"] for r in body["records"]} == {"event-1", "event-2", "event-3"}


@respx.mock
async def test_http_relay_non_2xx_acks_nothing_no_raise() -> None:
    respx.post(_SINK_URL).mock(return_value=httpx.Response(503))
    relay = HttpRelay(url=_SINK_URL)

    # No raise — soft-fail. Nothing acked → rows stay in the outbox for retry.
    delivered = await relay.send(_records([10, 11]))

    assert delivered == []


@respx.mock
async def test_http_relay_network_error_acks_nothing_no_raise() -> None:
    respx.post(_SINK_URL).mock(side_effect=httpx.ConnectError("sink down"))
    relay = HttpRelay(url=_SINK_URL)

    delivered = await relay.send(_records([20, 21]))

    assert delivered == []


@respx.mock
async def test_http_relay_empty_batch_no_post() -> None:
    route = respx.post(_SINK_URL).mock(return_value=httpx.Response(200))
    relay = HttpRelay(url=_SINK_URL)

    delivered = await relay.send([])

    assert delivered == []
    assert not route.called


async def test_http_relay_injected_client_is_used() -> None:
    """A caller-supplied httpx.AsyncClient is honored (no implicit network)."""
    captured: dict[str, object] = {}

    async def _handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200)

    transport = httpx.MockTransport(_handler)
    async with httpx.AsyncClient(transport=transport) as client:
        relay = HttpRelay(url=_SINK_URL, client=client)
        delivered = await relay.send(_records([5]))

    assert delivered == [5]
    assert captured["url"] == _SINK_URL


async def test_build_relay_picks_http_when_url_set() -> None:
    # ``async`` only to satisfy the module's ``pytestmark`` — body is sync.
    settings = Settings(audit_relay_url=_SINK_URL)
    relay = build_relay(settings)
    assert isinstance(relay, HttpRelay)


async def test_build_relay_defaults_to_logging_when_empty() -> None:
    settings = Settings(audit_relay_url="")
    relay = build_relay(settings)
    assert isinstance(relay, LoggingRelay)
