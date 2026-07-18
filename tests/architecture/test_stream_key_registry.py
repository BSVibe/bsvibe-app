"""INV-1 enforcement — the Redis stream-key registry meta-tests.

The four Redis stream keys are a WAKE-UP notification coupling: the producer
that lands a DB row also ``XADD``s onto the matching key so the consuming
worker wakes immediately instead of waiting for the next poll (the DB row stays
the source of truth). Before this registry the key strings lived in two
independent places — the producer-side constants in ``backend/workers/emit.py``
and the consumer-side worker→stream map in ``worker_runtime.py`` — with NOTHING
enforcing that the two sets agreed. This is the classic INV-1 "coupling is a
string nobody can see."

These checks make drift a build failure:

(a) **No silent disagreement** — the emit-side constant set, the consumer-side
    map's stream set, and ``{sk.key for sk in ALL_STREAM_KEYS}`` are identical.
    A key added on one side but not the other fails here.

(b) **Backing channel resolves** — every non-exception ``StreamKey`` names a
    ``backing_channel`` that is a real declared Channel in ``ALL_CHANNELS``
    (the stream key shadows a DB channel 1:1).

(c) **Settle is the annotated exception** — ``settle`` is deliberately OUT of
    INV-1 Channel scope (the ``execution_run_activities`` boundary note in
    ``docs/architecture/INVARIANTS.md``); it carries an explicit
    ``out_of_channel_scope`` annotation instead of a ``backing_channel`` and is
    the ONLY allow-listed exception.
"""

from __future__ import annotations

import pytest

import backend.workers.emit as emit
from backend.channels.registry import ALL_CHANNELS
from backend.workers.stream_keys import (
    ALL_STREAM_KEYS,
    STREAM_KEY_BY_CONSUMER,
    StreamKey,
)

# The named producer-side constants that emit.py exposes (and every producer
# imports). The SET of them must cover every declared key — a new StreamKey
# without a matching exported constant fails test (a).
_EMIT_CONSTANTS = {
    emit.STREAM_INTAKE,
    emit.STREAM_AGENT,
    emit.STREAM_DELIVER,
    emit.STREAM_SETTLE,
}

# The single allow-listed out-of-Channel-scope exception.
_OUT_OF_SCOPE_KEYS = {"settle"}


def test_emit_and_consumer_sides_match_declaration() -> None:
    declared = {sk.key for sk in ALL_STREAM_KEYS}
    assert declared, "ALL_STREAM_KEYS is empty — declare at least one stream key"

    # (a) emit-side == declaration == consumer-side, no key on one side missing
    # from the other.
    assert _EMIT_CONSTANTS == declared, (
        "emit.py stream constants drifted from ALL_STREAM_KEYS: "
        f"emit={_EMIT_CONSTANTS} declared={declared}"
    )
    assert set(STREAM_KEY_BY_CONSUMER.values()) == declared, (
        "worker_runtime consumer→stream map drifted from ALL_STREAM_KEYS: "
        f"map={set(STREAM_KEY_BY_CONSUMER.values())} declared={declared}"
    )


def test_consumer_map_keys_are_the_declared_consumers() -> None:
    # The consumer side is keyed by the consuming worker's group id, which must
    # be exactly the StreamKey.consumer (the worker ``_name``).
    assert set(STREAM_KEY_BY_CONSUMER) == {sk.consumer for sk in ALL_STREAM_KEYS}
    for sk in ALL_STREAM_KEYS:
        assert STREAM_KEY_BY_CONSUMER[sk.consumer] == sk.key


def test_stream_key_is_a_frozen_declaration() -> None:
    sk = ALL_STREAM_KEYS[0]
    assert isinstance(sk, StreamKey)
    with pytest.raises((AttributeError, TypeError)):
        sk.key = "mutated"  # type: ignore[misc]


def test_each_stream_key_binds_exactly_one_backing() -> None:
    # Well-formedness: a stream key backs EITHER a Channel OR is the annotated
    # out-of-scope exception, never both and never neither.
    for sk in ALL_STREAM_KEYS:
        has_channel = sk.backing_channel is not None
        has_exception = sk.out_of_channel_scope is not None
        assert has_channel != has_exception, (
            f"stream key {sk.key!r} must bind exactly one of backing_channel / out_of_channel_scope"
        )
        assert sk.producer, f"stream key {sk.key!r} declares no producer"
        assert sk.consumer, f"stream key {sk.key!r} declares no consumer"


def test_non_exception_backing_channels_resolve_to_a_declared_channel() -> None:
    # (b) every non-exception StreamKey's backing_channel is a real Channel.
    channel_names = {ch.name for ch in ALL_CHANNELS}
    for sk in ALL_STREAM_KEYS:
        if sk.out_of_channel_scope is not None:
            continue
        assert sk.backing_channel in channel_names, (
            f"stream key {sk.key!r} names backing_channel {sk.backing_channel!r} "
            f"that is not a declared Channel (declared: {sorted(channel_names)})"
        )


def test_settle_is_the_only_out_of_channel_scope_exception() -> None:
    # (c) settle — and only settle — is the annotated exception.
    out_of_scope = {sk.key for sk in ALL_STREAM_KEYS if sk.out_of_channel_scope is not None}
    assert out_of_scope == _OUT_OF_SCOPE_KEYS, (
        f"out-of-Channel-scope stream keys drifted: {out_of_scope} != {_OUT_OF_SCOPE_KEYS}"
    )
    settle = next(sk for sk in ALL_STREAM_KEYS if sk.key == "settle")
    assert settle.backing_channel is None
    assert settle.out_of_channel_scope is not None
    # The annotation must name the row it references instead of a Channel.
    assert "SettleDrainRow" in settle.out_of_channel_scope
