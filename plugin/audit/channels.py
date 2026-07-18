"""Audit-context channel declaration (INV-1).

Declares the ``audit_outbox`` channel next to its row
(:class:`~plugin.audit.models.AuditOutboxRecord`). Imports only the audit-context
row + the generic :class:`~backend.channels.Channel` core — no cross-context
imports, so a producer/consumer that depends on this module does not
transitively pull in another bounded context. ``plugin.audit`` is the in-tree
transactional-outbox subscriber (not a connector plugin), and is explicitly
carved out of the strict connector import-linter contract, so importing the
``Channel`` core here is sanctioned.

``audit_outbox`` is the transactional-outbox queue: every audit event lands a
row in ``audit_outbox`` inside the caller's request transaction, and a relay
drains it on its own schedule to the central audit sink. It has three machine
producers:

* :meth:`~plugin.audit.emitter.AuditEmitter.emit` (id ``audit:emitter``) — the
  bus-subscriber path: turns a typed :class:`~plugin.audit.events.AuditEventBase`
  into an outbox row inside the caller's session.
* :class:`~backend.workflow.application.safe_mode_expiry.SafeModeExpirySweepRunner`
  (id ``worker:safe_mode_expiry_sweep``) — emits ONE ``safe_mode.expired`` row
  per non-empty expiry batch, directly via :class:`~plugin.audit.store.OutboxStore`.
* :class:`~plugin.audit.retention_sweep.AuditRetentionSweepRunner`
  (id ``worker:audit_retention_sweep``) — emits ONE ``audit.retention.swept`` row
  per workspace per non-empty delete batch, directly via
  :class:`~plugin.audit.store.OutboxStore`.

Its sole worker-claim consumer is the
:class:`~backend.workflow.infrastructure.workers.relay_worker.RelayWorker`, which
claims a batch of undelivered rows under ``FOR UPDATE SKIP LOCKED``
(:meth:`~plugin.audit.store.OutboxStore.select_undelivered`), ships them via the
caller-supplied :class:`~backend.workflow.infrastructure.workers.relay_worker.Relay`
adapter, then acks with ``mark_delivered`` (or ``record_failure`` on rejection).
The retention sweep's DELETE and the multi-server-safe SELECT are not consumer
claims of new rows — the sole reader of undelivered rows is the RelayWorker, so
it is the single declared consumer.

An audit row is machine-emitted (a subscriber hook or a system sweep), so
``human_origin=False`` and there is no authoring surface.

This module ALSO declares the ``audit.emit`` :class:`~backend.channels.EventChannel`
— the in-process bus topic that fronts the outbox row. ``safe_emit`` (the sole
publisher) publishes an :class:`~bsvibe_sdk.Event` of kind ``audit.emit``; the
:class:`~plugin.audit.subscriber.AuditEventSubscriber` (the sole subscriber,
id ``audit:outbox_subscriber``) receives it and stages the ``audit_outbox``
row inside the producer's session. The bus is prefix-routed and best-effort:
the subscriber registers under the ``audit.`` family prefix (so future
``audit.*`` variants fan into the same sink) and a sink failure never rolls
back the producer's domain write — the :data:`~backend.channels.PublishOutcome`
returned by :meth:`~backend.channels.EventChannel.publish` is what makes that
best-effort delivery observable instead of silent. The canonical ``audit.emit``
kind + ``audit.`` prefix are defined here (the topic declaration) and
re-exported by :mod:`plugin.audit.subscriber` for the runtime guard.
"""

from __future__ import annotations

from backend.channels import Channel, EventChannel
from bsvibe_sdk import Event
from plugin.audit.models import AuditOutboxRecord

# Canonical bus identifiers for the audit topic (single source of truth).
AUDIT_EMIT_KIND = "audit.emit"
AUDIT_KIND_PREFIX = "audit."

AUDIT_OUTBOX: Channel[AuditOutboxRecord] = Channel(
    name="audit_outbox",
    row=AuditOutboxRecord,
    producers=(
        "audit:emitter",
        "worker:safe_mode_expiry_sweep",
        "worker:audit_retention_sweep",
    ),
    consumers=("worker:relay_worker",),
    human_origin=False,
)

AUDIT_EMIT: EventChannel[Event] = EventChannel(
    kind=AUDIT_EMIT_KIND,
    event_type=Event,
    publishers=("audit:safe_emit",),
    subscribers=("audit:outbox_subscriber",),
    subscribe_prefix=AUDIT_KIND_PREFIX,
)

__all__ = ["AUDIT_EMIT", "AUDIT_EMIT_KIND", "AUDIT_KIND_PREFIX", "AUDIT_OUTBOX"]
