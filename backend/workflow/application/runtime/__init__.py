"""Workflow runtime layer — agent / worker / lifecycle construction.

Decomposed out of the legacy
``backend.workflow.infrastructure.workers.run`` god-file:

* :mod:`.dispatcher` — resolver-backed ``CompileLlm`` / ``FrameLlm`` adapters.
* :mod:`.account_resolution` — workspace ModelAccount resolution policy
  via :class:`backend.dispatch.resolver.ModelAccountResolver`.
* :mod:`.agent_runtime` — :func:`build_agent_execution_deps` factory.
* :mod:`.settle_runtime` — settle entity extractor + note embed hook factories.
* :mod:`.delivery_runtime` — plugin dispatch + connector loaders.
* :mod:`.worker_runtime` — :class:`WorkerRuntime`, Redis-Streams wiring.
* :mod:`.lifecycle` — :func:`run_workers` process entrypoint.

The legacy ``backend.workflow.infrastructure.workers.run`` path remains a
thin re-export shim for back-compat.
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
from backend.workflow.application.runtime.lifecycle import run_workers
from backend.workflow.application.runtime.settle_runtime import (
    build_concept_framer,
    build_note_embed_hook,
    build_reconcile_hook,
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
    "build_concept_framer",
    "build_delivery_adapter",
    "build_note_embed_hook",
    "build_reconcile_hook",
    "build_settle_entity_extractor_factory",
    "build_stream_consumers",
    "build_worker_runtime",
    "check_executor_dispatch_health",
    "load_connector_plugins",
    "resolve_workspace_model_account",
    "run_stream_consumers",
    "run_workers",
]
