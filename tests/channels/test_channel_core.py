"""Unit tests for the generic :class:`Channel` guard wrapper (INV-1)."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, dataclass

import pytest

from backend.channels import (
    Channel,
    UndeclaredConsumerError,
    UndeclaredProducerError,
)


@dataclass
class _Row:
    value: str


class _Recorder:
    """Minimal :class:`SupportsAdd` — records what was added."""

    def __init__(self) -> None:
        self.added: list[_Row] = []

    def add(self, row: _Row) -> None:
        self.added.append(row)


def _channel() -> Channel[_Row]:
    return Channel(
        name="widgets",
        row=_Row,
        producers=("svc:producer",),
        consumers=("worker:consumer",),
    )


def test_emit_delegates_to_repo_add_for_declared_producer() -> None:
    channel = _channel()
    repo = _Recorder()
    row = _Row(value="x")

    channel.emit(repo, row, producer_id="svc:producer")

    assert repo.added == [row]


def test_emit_rejects_undeclared_producer_without_adding() -> None:
    channel = _channel()
    repo = _Recorder()

    with pytest.raises(UndeclaredProducerError):
        channel.emit(repo, _Row(value="x"), producer_id="svc:intruder")

    assert repo.added == []


@pytest.mark.asyncio
async def test_consume_delegates_to_claim_for_declared_consumer() -> None:
    channel = _channel()
    rows = [_Row(value="a"), _Row(value="b")]

    async def _claim() -> list[_Row]:
        return rows

    result = await channel.consume(consumer_id="worker:consumer", claim=_claim)

    assert result == rows


@pytest.mark.asyncio
async def test_consume_rejects_undeclared_consumer_without_claiming() -> None:
    channel = _channel()
    called = False

    async def _claim() -> list[_Row]:
        nonlocal called
        called = True
        return []

    with pytest.raises(UndeclaredConsumerError):
        await channel.consume(consumer_id="worker:intruder", claim=_claim)

    assert called is False


def test_assert_helpers_pass_for_declared_ids() -> None:
    channel = _channel()
    channel.assert_producer("svc:producer")
    channel.assert_consumer("worker:consumer")


def test_channel_is_frozen() -> None:
    channel = _channel()
    with pytest.raises(FrozenInstanceError):
        channel.name = "renamed"  # type: ignore[misc]


def test_human_origin_defaults_and_authoring_surface() -> None:
    machine = _channel()
    assert machine.human_origin is False
    assert machine.authoring_surface is None

    human: Channel[_Row] = Channel(
        name="typed_asks",
        row=_Row,
        producers=("api:submit",),
        consumers=("worker:consumer",),
        human_origin=True,
        authoring_surface="POST /api/v1/asks",
    )
    assert human.human_origin is True
    assert human.authoring_surface == "POST /api/v1/asks"
