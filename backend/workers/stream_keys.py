"""Redis stream keys as a typed, declared coupling (INV-1).

The four Redis stream keys are a **wake-up notification** transport: when a
producer lands a DB row a worker would otherwise poll for, it also ``XADD``s
onto the matching key so the consuming worker wakes immediately (see
:mod:`backend.workers.emit`). The DB row stays the source of truth — losing a
stream entry only delays a pickup until the next poll tick.

Each key shadows an existing DB Channel 1:1, yet before this module the key
strings lived in two independent string sets — the producer-side constants in
:mod:`backend.workers.emit` and the consumer-side worker→stream map in
``worker_runtime.build_stream_consumers`` — with nothing enforcing that the two
agreed. This module is the single source of truth: it declares each key ONCE,
binding it to its producing worker, its consuming worker/group, and the backing
DB Channel it shadows. ``emit.py`` re-exports the named constants from here and
``worker_runtime`` derives its map from :data:`STREAM_KEY_BY_CONSUMER`, so
neither side can drift; the meta-tests in
``tests/architecture/test_stream_key_registry.py`` make any drift a build
failure.

This is a **declaration only** — the actual ``XADD`` / ``XREADGROUP`` transport
is unchanged. The module is a pure leaf (only string bindings, no context
imports), so a producer/consumer that depends on it pulls in no bounded
context. The backing Channel is referenced by its ``.name`` string rather than
by importing the Channel object, which keeps this leaf free of a
``backend.workflow`` edge (the string is validated against the real Channel
registry in the meta-test).

``settle`` is the one deliberate exception: it is OUT of INV-1 Channel scope
(the ``execution_run_activities`` boundary note in
``docs/architecture/INVARIANTS.md`` — the activity table is a mixed glass-box
log, not a single-purpose queue), so its StreamKey carries an explicit
``out_of_channel_scope`` annotation referencing :class:`SettleDrainRow` instead
of a ``backing_channel``.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class StreamKey:
    """A declared Redis wake-up key binding key ↔ producer ↔ consumer ↔ channel.

    ``key`` is the Redis stream name shared by producer (``XADD``) and consumer
    (``XREADGROUP`` group). ``producer`` names the pipeline stage that lands the
    backing row and emits the wake-up; ``consumer`` is the consuming worker's
    group id (its ``_name``). A key backs EITHER a real DB Channel
    (``backing_channel`` = the Channel ``.name``) OR — for ``settle`` only — is
    the annotated ``out_of_channel_scope`` exception; never both, never neither.
    """

    key: str
    producer: str
    consumer: str
    backing_channel: str | None = None
    out_of_channel_scope: str | None = None

    def __post_init__(self) -> None:
        has_channel = self.backing_channel is not None
        has_exception = self.out_of_channel_scope is not None
        if has_channel == has_exception:
            raise ValueError(
                f"stream key {self.key!r} must bind exactly one of "
                "backing_channel / out_of_channel_scope"
            )


STREAM_INTAKE = "intake"
STREAM_AGENT = "agent"
STREAM_DELIVER = "deliver"
STREAM_SETTLE = "settle"


ALL_STREAM_KEYS: tuple[StreamKey, ...] = (
    # A TriggerEvent landed (webhook / direct / schedule receivers) → the intake
    # worker drains it into a Request. Backing channel: TRIGGER_EVENTS.
    StreamKey(
        key=STREAM_INTAKE,
        producer="workflow:trigger_receivers",
        consumer="intake_worker",
        backing_channel="trigger_events",
    ),
    # The intake worker minted an OPEN Request → the agent worker claims + drives
    # it. Backing channel: REQUESTS.
    StreamKey(
        key=STREAM_AGENT,
        producer="intake_worker",
        consumer="agent_worker",
        backing_channel="requests",
    ),
    # A delivery event landed (verified / partial / answer deliverable writers)
    # → the delivery worker ships it. Backing channel: DELIVERY_EVENTS.
    StreamKey(
        key=STREAM_DELIVER,
        producer="workflow:run_persistence",
        consumer="delivery_worker",
        backing_channel="delivery_events",
    ),
    # A settle activity landed → the settle worker absorbs it. Settle is
    # deliberately OUT of INV-1 Channel scope (the execution_run_activities
    # boundary note in docs/architecture/INVARIANTS.md): the activity table is a
    # mixed glass-box log, not a single-purpose queue, so it has no backing
    # Channel — the coupling is enforced by the existing SettleDrainRow dedupe
    # (backend/workers/db.py), which this annotation references directly.
    StreamKey(
        key=STREAM_SETTLE,
        producer="workflow:run_persistence",
        consumer="settle_worker",
        out_of_channel_scope=(
            "SettleDrainRow (backend/workers/db.py) — settle is deliberately out "
            "of INV-1 Channel scope (execution_run_activities is a mixed "
            "glass-box activity log, not a single-purpose queue); its coupling "
            "is enforced by the existing SettleDrainRow dedupe, not a Channel."
        ),
    ),
)


# Consumer-side derivation: the consuming worker's group id (``_name``) → its
# stream key. ``worker_runtime.build_stream_consumers`` uses this as its single
# source instead of a hand-maintained duplicate map.
STREAM_KEY_BY_CONSUMER: dict[str, str] = {sk.consumer: sk.key for sk in ALL_STREAM_KEYS}


__all__ = [
    "ALL_STREAM_KEYS",
    "STREAM_AGENT",
    "STREAM_DELIVER",
    "STREAM_INTAKE",
    "STREAM_KEY_BY_CONSUMER",
    "STREAM_SETTLE",
    "StreamKey",
]
