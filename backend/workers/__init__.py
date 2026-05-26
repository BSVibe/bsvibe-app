"""Workers — Workflow §12.5 #8 (Bundle G).

Consumer-group workers binding the orchestrator, delivery, execution,
and audit-relay surfaces to Redis Streams.
"""

from __future__ import annotations

from backend.workers.agent_worker import AgentWorker
from backend.workers.db import (
    AuditRelayStateRow,
    SettleDrainRow,
    WorkerInstallTokenRow,
    WorkerRow,
    WorkersBase,
    WorkerStatus,
)
from backend.workers.delivery_worker import DeliveryWorker

# NOTE: ``ExecutorDispatchWorker`` (formerly backend.workers.executor_dispatch)
# was an orphaned alt-dispatch design — never wired into
# :func:`backend.workers.run.build_worker_runtime`. Real executor dispatch lives
# inline in :class:`backend.executors.orchestrator.ExecutorOrchestrator` (B14).
from backend.workers.intake_worker import IntakeWorker
from backend.workers.relay_worker import RelayWorker
from backend.workers.settle_worker import (
    KnowledgeSettleSink,
    Settlement,
    SettleSink,
    SettleWorker,
    SettleWorkerConfig,
)
from backend.workers.streams import RedisStreamConsumer, StreamHandler
from backend.workers.verifier_worker import VerifierWorker

__all__ = [
    "AgentWorker",
    "AuditRelayStateRow",
    "DeliveryWorker",
    "IntakeWorker",
    "KnowledgeSettleSink",
    "RedisStreamConsumer",
    "RelayWorker",
    "SettleDrainRow",
    "SettleSink",
    "SettleWorker",
    "SettleWorkerConfig",
    "Settlement",
    "StreamHandler",
    "VerifierWorker",
    "WorkerInstallTokenRow",
    "WorkerRow",
    "WorkerStatus",
    "WorkersBase",
]
