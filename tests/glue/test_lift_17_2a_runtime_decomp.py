"""Lift §17.2a — workers/run.py runtime/ decomposition.

Per v8 §17.2 (the 1445 LOC integration god-file). §17.2a extracts the
runtime construction layer to ``backend.workflow.application.runtime`` —
the per-context wiring/ slice is deferred to §17.2b.

Decomposition map:

* ``runtime/dispatcher.py`` — ``build_gateway_dispatcher`` + the
  ``_GatewayCompileLlm`` / ``_GatewayFrameLlm`` adapter seams.
* ``runtime/account_resolution.py`` — workspace ModelAccount resolution
  policy (``resolve_workspace_model_account`` + helpers + Decision
  kinds).
* ``runtime/agent_runtime.py`` — ``build_agent_execution_deps`` factory
  (the big agent-side wiring).
* ``runtime/settle_runtime.py`` — settle entity-extractor + note embed
  hook factories.
* ``runtime/delivery_runtime.py`` — ``build_delivery_adapter``,
  ``load_connector_plugins``, ``RealPluginDispatchAdapter``,
  ``LoggingRelay``.
* ``runtime/worker_runtime.py`` — ``WorkerRuntime``, ``build_worker_runtime``,
  Redis-Streams consumer wiring, ``check_executor_dispatch_health``.
* ``runtime/lifecycle.py`` — ``run_workers`` process entrypoint.

Behavior preservation: the old ``backend.workflow.infrastructure.workers.run``
path still re-exports every public symbol (callers + tests + the
``backend.workers.__main__`` entry untouched).
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Delta 1: runtime/ package exists with the planned modules
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "module_name,symbol",
    [
        ("backend.workflow.application.runtime", "build_agent_execution_deps"),
        ("backend.workflow.application.runtime", "build_worker_runtime"),
        ("backend.workflow.application.runtime", "run_workers"),
        ("backend.workflow.application.runtime.dispatcher", "_ResolverCompileLlm"),
        (
            "backend.workflow.application.runtime.account_resolution",
            "resolve_workspace_model_account",
        ),
        (
            "backend.workflow.application.runtime.account_resolution",
            "DECISION_NO_MODEL_ACCOUNT",
        ),
        ("backend.workflow.application.runtime.agent_runtime", "build_agent_execution_deps"),
        (
            "backend.workflow.application.runtime.settle_runtime",
            "build_settle_entity_extractor_factory",
        ),
        ("backend.workflow.application.runtime.settle_runtime", "build_note_embed_hook"),
        ("backend.workflow.application.runtime.delivery_runtime", "build_delivery_adapter"),
        ("backend.workflow.application.runtime.delivery_runtime", "load_connector_plugins"),
        ("backend.workflow.application.runtime.delivery_runtime", "RealPluginDispatchAdapter"),
        ("backend.workflow.application.runtime.delivery_runtime", "LoggingRelay"),
        ("backend.workflow.application.runtime.worker_runtime", "WorkerRuntime"),
        ("backend.workflow.application.runtime.worker_runtime", "build_worker_runtime"),
        (
            "backend.workflow.application.runtime.worker_runtime",
            "check_executor_dispatch_health",
        ),
        ("backend.workflow.application.runtime.worker_runtime", "build_stream_consumers"),
        ("backend.workflow.application.runtime.worker_runtime", "run_stream_consumers"),
        ("backend.workflow.application.runtime.worker_runtime", "StreamConsumerBinding"),
        ("backend.workflow.application.runtime.lifecycle", "run_workers"),
    ],
)
def test_runtime_module_exposes_symbol(module_name: str, symbol: str) -> None:
    """Each runtime/ module exposes its planned public symbol."""
    mod = importlib.import_module(module_name)
    assert hasattr(mod, symbol), f"{module_name} missing {symbol}"


# ---------------------------------------------------------------------------
# Delta 2: old workers/run.py is a thin re-export shim (back-compat)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "symbol",
    [
        "build_agent_execution_deps",
        "build_worker_runtime",
        "run_workers",
        "build_settle_entity_extractor_factory",
        "build_note_embed_hook",
        "build_delivery_adapter",
        "load_connector_plugins",
        "check_executor_dispatch_health",
        "resolve_workspace_model_account",
        "RealPluginDispatchAdapter",
        "LoggingRelay",
        "WorkerRuntime",
        "StreamConsumerBinding",
        "build_stream_consumers",
        "run_stream_consumers",
        "DECISION_NO_MODEL_ACCOUNT",
        "DECISION_AMBIGUOUS_MODEL_ACCOUNT",
        "AuditOutboxRecord",
    ],
)
def test_legacy_run_module_re_exports(symbol: str) -> None:
    """``backend.workflow.infrastructure.workers.run`` keeps every public symbol
    importable so external callers, tests, and ``backend.workers.__main__``
    continue to work without source edits during §17.2a."""
    mod = importlib.import_module("backend.workflow.infrastructure.workers.run")
    assert hasattr(mod, symbol), f"legacy run.py missing {symbol}"


# ---------------------------------------------------------------------------
# Delta 3: workers/run.py is now thin (≤200 LOC)
# ---------------------------------------------------------------------------


def test_legacy_run_module_is_thin_shim() -> None:
    """After §17.2a, ``workers/run.py`` is a thin shim ≤200 LOC — no
    construction logic remains; it only re-exports the runtime/ modules."""
    path = (
        Path(__file__).resolve().parents[2]
        / "backend"
        / "workflow"
        / "infrastructure"
        / "workers"
        / "run.py"
    )
    loc = path.read_text(encoding="utf-8").count("\n")
    assert loc <= 200, f"workers/run.py is {loc} LOC (must be ≤200 after §17.2a)"


# ---------------------------------------------------------------------------
# Delta 4: each new runtime/ module ≤400 LOC
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "module_filename",
    [
        "dispatcher.py",
        "account_resolution.py",
        "agent_runtime.py",
        "settle_runtime.py",
        "delivery_runtime.py",
        "worker_runtime.py",
        "lifecycle.py",
    ],
)
def test_runtime_module_under_400_loc(module_filename: str) -> None:
    """Each new runtime/ module is ≤400 LOC (audit §17.2 invariant)."""
    path = (
        Path(__file__).resolve().parents[2]
        / "backend"
        / "workflow"
        / "application"
        / "runtime"
        / module_filename
    )
    assert path.exists(), f"{module_filename} not created"
    loc = path.read_text(encoding="utf-8").count("\n")
    assert loc <= 400, f"runtime/{module_filename} is {loc} LOC (must be ≤400)"


# ---------------------------------------------------------------------------
# Delta 5: worker boot path still resolves the same callable
# ---------------------------------------------------------------------------


def test_workers_main_entrypoint_still_wires() -> None:
    """``python -m backend.workers`` still imports a ``run_workers`` callable —
    the shim preserves the legacy path so ``__main__`` is untouched."""
    main_mod = importlib.import_module("backend.workers.__main__")
    assert hasattr(main_mod, "run_workers")
    assert callable(main_mod.run_workers)


# ---------------------------------------------------------------------------
# Delta 6: build_worker_runtime constructs the same worker set
# ---------------------------------------------------------------------------


def test_build_worker_runtime_constructs_expected_workers() -> None:
    """Smoke — ``build_worker_runtime`` returns a ``WorkerRuntime`` whose
    ``workers`` list contains the 7 expected workers (intake, agent, delivery,
    settle, relay, schedule, safe_mode_expiry). Behavior identical to pre-lift.
    """
    from unittest.mock import MagicMock

    from backend.workflow.application.runtime import (
        WorkerRuntime,
        build_worker_runtime,
    )

    session_factory = MagicMock()
    execution = MagicMock()
    delivery_adapter = MagicMock()
    notify_sender = MagicMock()

    runtime = build_worker_runtime(
        session_factory=session_factory,
        execution=execution,
        delivery_adapter=delivery_adapter,
        notify_sender=notify_sender,
    )
    assert isinstance(runtime, WorkerRuntime)
    assert len(runtime.workers) == 10
    names = {getattr(w, "_name", None) for w in runtime.workers}
    expected = {
        "intake_worker",
        "agent_worker",
        "delivery_worker",
        # Notifier N2 — drains notification_outbox, delivers needs_you pushes.
        "notify_worker",
        # Notifier daily_brief — per-workspace once-a-day digest producer.
        "daily_brief_worker",
        "settle_worker",
        "relay_worker",
        "schedule_worker",
        "safe_mode_expiry_worker",
        # Lift Q1 — third ScheduleWorker driving the audit_outbox
        # retention sweep on a daily cadence.
        "audit_retention_sweep_worker",
    }
    assert names == expected, f"got {names}, expected {expected}"
