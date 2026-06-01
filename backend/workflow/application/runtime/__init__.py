"""Workflow runtime layer — agent / worker / lifecycle construction.

Decomposed out of the legacy
``backend.workflow.infrastructure.workers.run`` god-file per v8 §17.2a:

* :mod:`.dispatcher` — gateway dispatcher build + ``CompileLlm`` /
  ``FrameLlm`` adapter seams.
* :mod:`.account_resolution` — workspace ModelAccount resolution policy.
* :mod:`.agent_runtime` — :func:`build_agent_execution_deps` factory.
* :mod:`.settle_runtime` — settle entity extractor + note embed hook
  factories.
* :mod:`.delivery_runtime` — :class:`RealPluginDispatchAdapter`,
  :func:`build_delivery_adapter`, :func:`load_connector_plugins`,
  :class:`LoggingRelay`.
* :mod:`.worker_runtime` — :class:`WorkerRuntime`,
  :func:`build_worker_runtime`, Redis-Streams consumer wiring,
  :func:`check_executor_dispatch_health`.
* :mod:`.lifecycle` — :func:`run_workers` process entrypoint.

The legacy ``backend.workflow.infrastructure.workers.run`` path remains a
thin re-export shim during §17.2a for back-compat — every caller +
test + ``backend.workers.__main__`` keeps working without source
edits. The per-context wiring/ slice split is deferred to §17.2b.
"""

from __future__ import annotations

from backend.workflow.application.runtime.account_resolution import (
    DECISION_AMBIGUOUS_MODEL_ACCOUNT,
    DECISION_NO_MODEL_ACCOUNT,
    resolve_workspace_model_account,
)
from backend.workflow.application.runtime.agent_runtime import build_agent_execution_deps
from backend.workflow.application.runtime.delivery_runtime import (
    LoggingRelay,
    RealPluginDispatchAdapter,
    build_delivery_adapter,
    load_connector_plugins,
)
from backend.workflow.application.runtime.dispatcher import build_gateway_dispatcher
from backend.workflow.application.runtime.lifecycle import run_workers
from backend.workflow.application.runtime.settle_runtime import (
    build_note_embed_hook,
    build_settle_entity_extractor_factory,
)
from backend.workflow.application.runtime.worker_runtime import (
    StreamConsumerBinding,
    WorkerRuntime,
    build_stream_consumers,
    build_worker_runtime,
    check_executor_dispatch_health,
    run_stream_consumers,
)

__all__ = [
    "DECISION_AMBIGUOUS_MODEL_ACCOUNT",
    "DECISION_NO_MODEL_ACCOUNT",
    "LoggingRelay",
    "RealPluginDispatchAdapter",
    "StreamConsumerBinding",
    "WorkerRuntime",
    "build_agent_execution_deps",
    "build_delivery_adapter",
    "build_gateway_dispatcher",
    "build_note_embed_hook",
    "build_settle_entity_extractor_factory",
    "build_stream_consumers",
    "build_worker_runtime",
    "check_executor_dispatch_health",
    "load_connector_plugins",
    "resolve_workspace_model_account",
    "run_stream_consumers",
    "run_workers",
]
