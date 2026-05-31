"""Workers — shared infrastructure module.

Per v8 D34 (Lift H3c) the per-context workers moved to their owning
contexts. What stays here is the cross-context common worker infra:

* :class:`~backend.workers.base.BaseWorker` — the poll-loop base.
* :mod:`backend.workers.db` — worker registration + drain marker tables.
* :mod:`backend.workers.emit` — Redis Streams emit helpers + stream names.
* :mod:`backend.workers.streams` — :class:`RedisStreamConsumer` +
  :class:`StreamHandler` (consumer-group plumbing).
* :mod:`backend.workers.relays` — relay-adapter wiring for RelayWorker.
* :mod:`backend.workers.__main__` — production daemon entrypoint.

The per-context workers now live at:

* :mod:`backend.workflow.infrastructure.workers` — agent / verifier /
  relay / intake / delivery + the production ``run`` module.
* :mod:`backend.knowledge.infrastructure.workers` — settle worker.
* :mod:`backend.schedule.infrastructure.workers` — schedule worker
  (Lift Schedule, v8 §3.5 / D30).
"""

from __future__ import annotations

from backend.workers.db import (
    AuditRelayStateRow,
    SettleDrainRow,
    WorkerInstallTokenRow,
    WorkerRow,
    WorkersBase,
    WorkerStatus,
)
from backend.workers.streams import RedisStreamConsumer, StreamHandler

__all__ = [
    "AuditRelayStateRow",
    "RedisStreamConsumer",
    "SettleDrainRow",
    "StreamHandler",
    "WorkerInstallTokenRow",
    "WorkerRow",
    "WorkerStatus",
    "WorkersBase",
]
