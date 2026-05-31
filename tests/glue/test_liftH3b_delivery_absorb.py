"""Lift H3b — assert ``backend/delivery/`` absorbed into Workflow context.

Per v8 §13 Lift H3b + D29 (Intake + Delivery absorption into Workflow), every
file under ``backend/delivery/`` moves to a target inside ``backend/workflow/``:

* ``schema.py``             → ``backend/workflow/domain/delivery.py``
* ``db.py``                 → ``backend/workflow/infrastructure/delivery/db.py``
* ``git_ops.py``            → ``backend/workflow/infrastructure/delivery/git_ops.py``
* ``dispatcher.py``         → ``backend/workflow/application/delivery/dispatcher.py``
* ``connector_dispatch.py`` → ``backend/workflow/application/delivery/connector_dispatch.py``
* ``safe_mode_queue.py``    → ``backend/workflow/application/safe_mode_queue.py``
* ``safe_mode_expiry.py``   → ``backend/workflow/application/safe_mode_expiry.py``

The 887-LOC ``connector_dispatch.py`` god-file is moved AS-IS (no decomp);
decomposition is a separate Lift M candidate. The 745-LOC
``backend/api/v1/deliverables.py`` god-file is OUT OF SCOPE (its imports get
updated only).

These tests are the delta-asserting RED-first proof. They check:

1. New canonical Workflow-side imports work.
2. Old ``backend.delivery.*`` imports FAIL (ModuleNotFoundError).
3. ``backend/delivery/`` directory does not exist.
4. Workers + endpoints + cross-context callers still wire.
5. REST endpoint (``api/v1/deliverables.py``) continues to import.
6. D3a sweep runner still exposes its ScheduleRunnerProtocol-shaped entry.
7. R2a audit-EventBus integration — workflow safe_mode docstring references
   stay consistent.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Delta 1: NEW canonical Workflow-side imports work
# ---------------------------------------------------------------------------


def test_workflow_domain_delivery_importable() -> None:
    """``backend.workflow.domain.delivery`` exposes ActionResult / DeliveryResult."""
    mod = importlib.import_module("backend.workflow.domain.delivery")
    for sym in ("ActionResult", "ArtifactType", "DeliveryResult"):
        assert hasattr(mod, sym), sym


def test_workflow_infrastructure_delivery_db_importable() -> None:
    mod = importlib.import_module("backend.workflow.infrastructure.delivery.db")
    for sym in (
        "DeliveryBase",
        "DeliveryEventRow",
        "SafeModeQueueItemRow",
        "SafeModeStatus",
    ):
        assert hasattr(mod, sym), sym


def test_workflow_infrastructure_delivery_git_ops_importable() -> None:
    mod = importlib.import_module("backend.workflow.infrastructure.delivery.git_ops")
    for sym in ("GitOps", "scrub_token"):
        assert hasattr(mod, sym), sym


def test_workflow_application_delivery_dispatcher_importable() -> None:
    mod = importlib.import_module("backend.workflow.application.delivery.dispatcher")
    assert hasattr(mod, "DeliveryDispatcher")


def test_workflow_application_delivery_connector_dispatch_importable() -> None:
    mod = importlib.import_module("backend.workflow.application.delivery.connector_dispatch")
    for sym in ("OUTBOUND_EVENT_BUILDERS", "build_connector_delivery_adapter"):
        assert hasattr(mod, sym), sym


def test_workflow_application_safe_mode_queue_importable() -> None:
    mod = importlib.import_module("backend.workflow.application.safe_mode_queue")
    assert hasattr(mod, "SafeModeQueue")


def test_workflow_application_safe_mode_expiry_importable() -> None:
    mod = importlib.import_module("backend.workflow.application.safe_mode_expiry")
    for sym in (
        "SafeModeExpirySweepRunner",
        "SAFE_MODE_EXPIRED_EVENT_TYPE",
        "SAFE_MODE_EXPIRY_SOURCE",
    ):
        assert hasattr(mod, sym), sym


# ---------------------------------------------------------------------------
# Delta 2: OLD delivery module paths are GONE
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "module_name",
    [
        # NOTE — these are spelled with string concatenation so the bulk
        # import-graph rewriter (and future ones) cannot rewrite them out
        # from under the assertion. The whole point of this delta is to
        # assert that the OLD ``backend.delivery.*`` paths no longer resolve.
        "backend" + ".delivery",
        "backend" + ".delivery.schema",
        "backend" + ".delivery.db",
        "backend" + ".delivery.git_ops",
        "backend" + ".delivery.dispatcher",
        "backend" + ".delivery.connector_dispatch",
        "backend" + ".delivery.safe_mode_queue",
        "backend" + ".delivery.safe_mode_expiry",
    ],
)
def test_old_delivery_module_gone(module_name: str) -> None:
    """All absorbed modules + the package itself can no longer be imported."""
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(module_name)


# ---------------------------------------------------------------------------
# Delta 3: ``backend/delivery/`` directory does NOT exist
# ---------------------------------------------------------------------------


def test_delivery_directory_deleted() -> None:
    """The whole ``backend/delivery/`` directory is gone post-absorb."""
    import backend

    backend_root = Path(next(iter(backend.__path__)))
    assert not (backend_root / "delivery").exists(), (
        "backend/delivery/ should be deleted by Lift H3b"
    )


# ---------------------------------------------------------------------------
# Delta 4: workers / endpoints / cross-context callers still wire
# ---------------------------------------------------------------------------


def test_delivery_worker_imports_resolve() -> None:
    """``backend.workflow.infrastructure.workers.delivery_worker`` imports from new Workflow paths."""
    mod = importlib.import_module("backend.workflow.infrastructure.workers.delivery_worker")
    assert hasattr(mod, "DeliveryWorker")


def test_run_worker_imports_resolve() -> None:
    """``backend.workflow.infrastructure.workers.run`` consumes DeliveryEventRow + queue."""
    mod = importlib.import_module("backend.workflow.infrastructure.workers.run")
    assert mod is not None


def test_safemode_endpoint_imports_resolve() -> None:
    mod = importlib.import_module("backend.api.v1.safemode")
    assert hasattr(mod, "router")


def test_connectors_endpoint_imports_resolve() -> None:
    mod = importlib.import_module("backend.api.v1.connectors")
    assert hasattr(mod, "router")


def test_deliverables_endpoint_imports_resolve() -> None:
    """``backend/api/v1/deliverables.py`` (745-LOC god-file) stays put."""
    mod = importlib.import_module("backend.api.v1.deliverables")
    assert hasattr(mod, "router")


def test_verified_deliverable_imports_resolve() -> None:
    mod = importlib.import_module("backend.execution.verified_deliverable")
    assert mod is not None


def test_workflow_application_safe_mode_imports_resolve() -> None:
    """The consumer of SafeModeQueue inside Workflow context."""
    mod = importlib.import_module("backend.workflow.application.safe_mode")
    assert mod is not None


# ---------------------------------------------------------------------------
# Delta 5: REST endpoint does not reference moved old paths
# ---------------------------------------------------------------------------


_OLD_FROM = "from backend" + ".delivery."
_OLD_IMPORT = "import backend" + ".delivery."
_OLD_FROM_BARE = "from backend" + ".delivery"


def test_deliverables_endpoint_does_not_import_old_delivery_paths() -> None:
    """``api/v1/deliverables.py`` only references the new Workflow paths
    (none required if it only imports types — but no old-path refs left).
    """
    import inspect

    import backend.api.v1.deliverables as mod

    src = inspect.getsource(mod)
    # No old-path imports.
    assert _OLD_FROM not in src
    assert _OLD_IMPORT not in src


def test_safemode_endpoint_uses_new_delivery_path() -> None:
    import inspect

    import backend.api.v1.safemode as mod

    src = inspect.getsource(mod)
    assert _OLD_FROM_BARE not in src


def test_connectors_endpoint_uses_new_delivery_path() -> None:
    import inspect

    import backend.api.v1.connectors as mod

    src = inspect.getsource(mod)
    assert _OLD_FROM_BARE not in src


def test_delivery_worker_uses_new_delivery_path() -> None:
    """The worker must reference the new Workflow paths."""
    import inspect

    import backend.workflow.infrastructure.workers.delivery_worker as mod

    src = inspect.getsource(mod)
    assert _OLD_FROM not in src
    # At minimum one of the lifted symbols' new location appears.
    assert "backend.workflow" in src


# ---------------------------------------------------------------------------
# Delta 6: D3a sweep runner exposes ScheduleRunnerProtocol shape
# ---------------------------------------------------------------------------


def test_safe_mode_expiry_sweep_runner_protocol_shape() -> None:
    """The sweep runner must still be a ScheduleRunnerProtocol-compatible
    callable. The protocol entry is ``async def fire_due(now)``.
    """
    mod = importlib.import_module("backend.workflow.application.safe_mode_expiry")
    runner_cls = mod.SafeModeExpirySweepRunner
    assert hasattr(runner_cls, "fire_due"), (
        "SafeModeExpirySweepRunner must keep its ScheduleRunnerProtocol entry"
    )


# ---------------------------------------------------------------------------
# Delta 7: alembic env still discovers the delivery tables
# ---------------------------------------------------------------------------


def test_alembic_env_imports_resolve() -> None:
    """``backend/data/migrations/env.py`` registers table metadata; the
    delivery_events + safe_mode_queue_items registration must be wired off
    the new path. Read the source file directly — the env script can't be
    imported standalone (it expects an active alembic ``context``).
    """
    import backend.data.migrations as migrations_pkg

    pkg_root = Path(next(iter(migrations_pkg.__path__)))
    src = (pkg_root / "env.py").read_text()
    # Old path is gone.
    assert ("import backend" + ".delivery.db") not in src
    # New path appears.
    assert "backend.workflow.infrastructure.delivery.db" in src
