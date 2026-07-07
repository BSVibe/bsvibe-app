"""Back-compat shim — the production worker runtime moved to
``backend.workflow.application.runtime`` per v8 §17.2a.

Behavior is byte-identical — this file is now a pure re-export.
"""

from __future__ import annotations

from backend.router.accounts.crypto import CredentialCipher, _key_from_settings
from backend.workflow.application.runtime.account_resolution import (
    DECISION_AMBIGUOUS_MODEL_ACCOUNT,
    DECISION_NO_MODEL_ACCOUNT,
    _list_active_workspace_accounts,
    _resolve_judge_llm,
    _resolve_via_caller,
    _single_native_account,
    resolve_workspace_model_account,
)
from backend.workflow.application.runtime.agent_runtime import (
    _build_composite_workspace_provisioner,
    _frame_skill_hint,
    _is_knowledge_only,
    _product_workspace_provisioner,
    build_agent_execution_deps,
)
from backend.workflow.application.runtime.delivery_runtime import (
    _PLUGINS_IMPLEMENTATIONS_DIR,
    LoggingRelay,
    RealPluginDispatchAdapter,
    build_delivery_adapter,
    load_connector_plugins,
)
from backend.workflow.application.runtime.dispatcher import (
    _ResolverCompileLlm,
    _ResolverFrameLlm,
)
from backend.workflow.application.runtime.lifecycle import run_workers
from backend.workflow.application.runtime.settle_runtime import (
    _relative_note_path,
    build_concept_framer,
    build_note_embed_hook,
    build_reconcile_hook,
    build_settle_entity_extractor_factory,
)
from backend.workflow.application.runtime.worker_runtime import (
    StreamConsumerBinding,
    WorkerRuntime,
    _tick_handler,
    build_stream_consumers,
    build_worker_runtime,
    check_executor_dispatch_health,
    run_stream_consumers,
)
from plugin.audit.models import AuditOutboxRecord

__all__ = [
    "AuditOutboxRecord",
    "CredentialCipher",
    "DECISION_AMBIGUOUS_MODEL_ACCOUNT",
    "DECISION_NO_MODEL_ACCOUNT",
    "LoggingRelay",
    "RealPluginDispatchAdapter",
    "StreamConsumerBinding",
    "WorkerRuntime",
    "_PLUGINS_IMPLEMENTATIONS_DIR",
    "_ResolverCompileLlm",
    "_ResolverFrameLlm",
    "_build_composite_workspace_provisioner",
    "_frame_skill_hint",
    "_is_knowledge_only",
    "_key_from_settings",
    "_list_active_workspace_accounts",
    "_product_workspace_provisioner",
    "_relative_note_path",
    "_resolve_judge_llm",
    "_resolve_via_caller",
    "_single_native_account",
    "_tick_handler",
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
