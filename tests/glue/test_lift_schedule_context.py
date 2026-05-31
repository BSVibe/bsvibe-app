"""Lift Schedule — Schedule bounded context creation.

Per v8 §3.5 + D30 (6th bounded context: Schedule), lift the 3 files split
across ``backend/intake/`` + ``backend/workers/`` into a unified
``backend/schedule/`` 3-layer bounded context:

* ``backend/intake/schedule.py`` →
  ``backend/schedule/application/emitter.py`` (``ScheduleTrigger``).
* ``backend/intake/schedule_db.py`` →
  ``backend/schedule/infrastructure/schedule_db.py`` (``WorkspaceScheduleRow``).
* ``backend/workers/schedule_runner.py`` is split:
    * ``ScheduleRunnerProtocol`` →
      ``backend/schedule/domain/runner_protocol.py``.
    * ``ScheduleAdvancer`` Protocol + ``OneShotScheduleAdvancer`` +
      ``FixedIntervalScheduleAdvancer`` →
      ``backend/schedule/domain/advancer.py``.
    * ``DbPollScheduleRunner`` + ``build_db_poll_schedule_runner`` →
      ``backend/schedule/infrastructure/db_poll_runner.py``.
    * ``ScheduleWorker`` + ``ScheduleWorkerConfig`` →
      ``backend/schedule/infrastructure/workers/schedule_worker.py``.

This is the RED-first delta proof.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Delta 1: backend/schedule/ exists with 3-layer convention
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "module_name",
    [
        "backend.schedule",
        "backend.schedule.domain",
        "backend.schedule.application",
        "backend.schedule.infrastructure",
        "backend.schedule.infrastructure.workers",
    ],
)
def test_schedule_context_packages_exist(module_name: str) -> None:
    """The Schedule bounded context's 3 layers are importable packages."""
    mod = importlib.import_module(module_name)
    assert mod is not None


# ---------------------------------------------------------------------------
# Delta 2: Schedule domain symbols at new locations
# ---------------------------------------------------------------------------


def test_schedule_runner_protocol_at_new_location() -> None:
    mod = importlib.import_module("backend.schedule.domain.runner_protocol")
    assert hasattr(mod, "ScheduleRunnerProtocol")


def test_schedule_advancer_at_new_location() -> None:
    mod = importlib.import_module("backend.schedule.domain.advancer")
    assert hasattr(mod, "ScheduleAdvancer")
    assert hasattr(mod, "OneShotScheduleAdvancer")
    assert hasattr(mod, "FixedIntervalScheduleAdvancer")


# ---------------------------------------------------------------------------
# Delta 3: Schedule application symbols at new locations
# ---------------------------------------------------------------------------


def test_schedule_emitter_at_new_location() -> None:
    mod = importlib.import_module("backend.schedule.application.emitter")
    assert hasattr(mod, "ScheduleTrigger")


# ---------------------------------------------------------------------------
# Delta 4: Schedule infrastructure symbols at new locations
# ---------------------------------------------------------------------------


def test_schedule_db_at_new_location() -> None:
    mod = importlib.import_module("backend.schedule.infrastructure.schedule_db")
    assert hasattr(mod, "WorkspaceScheduleRow")


def test_db_poll_runner_at_new_location() -> None:
    mod = importlib.import_module("backend.schedule.infrastructure.db_poll_runner")
    assert hasattr(mod, "DbPollScheduleRunner")
    assert hasattr(mod, "build_db_poll_schedule_runner")


def test_schedule_worker_at_new_location() -> None:
    mod = importlib.import_module("backend.schedule.infrastructure.workers.schedule_worker")
    assert hasattr(mod, "ScheduleWorker")
    assert hasattr(mod, "ScheduleWorkerConfig")


# ---------------------------------------------------------------------------
# Delta 5: Old paths GONE
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "module_name",
    [
        # String-concatenated so the bulk import rewriter cannot rewrite
        # them out — the whole point is to assert the old path is gone.
        "backend.intake" + ".schedule",
        "backend.intake" + ".schedule_db",
        "backend.workers" + ".schedule_runner",
    ],
)
def test_old_schedule_module_gone(module_name: str) -> None:
    """Each Schedule source no longer resolves at its old path."""
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(module_name)


def test_old_intake_package_gone() -> None:
    """``backend/intake/`` is now empty and the package is removed (H3a left
    only schedule carry-overs; the Schedule lift moves those out)."""
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("backend.intake")


# ---------------------------------------------------------------------------
# Delta 6: No consumer imports old paths
# ---------------------------------------------------------------------------


def test_no_consumer_imports_old_schedule_paths() -> None:
    """No source file in backend/ tests/ apps/ plugin/ imports from the old
    Schedule paths (excluding this test file's own assertion strings)."""
    import backend

    backend_root = Path(next(iter(backend.__path__))).parent
    needles = [
        "from backend.intake" + ".schedule",
        "import backend.intake" + ".schedule",
        "from backend.workers" + ".schedule_runner",
        "import backend.workers" + ".schedule_runner",
    ]
    offenders: list[str] = []
    for sub in ("backend", "tests", "plugin"):
        root = backend_root / sub
        if not root.exists():
            continue
        for py in root.rglob("*.py"):
            text = py.read_text(encoding="utf-8", errors="ignore")
            for needle in needles:
                if needle in text:
                    if py.name == "test_lift_schedule_context.py":
                        break
                    offenders.append(str(py.relative_to(backend_root)))
                    break
    assert not offenders, f"Found old-path Schedule consumers: {offenders}"


# ---------------------------------------------------------------------------
# Delta 7: WorkspaceScheduleRow still registers on Base.metadata
# ---------------------------------------------------------------------------


def test_workspace_schedules_table_registered_on_metadata() -> None:
    """The ``workspace_schedules`` table is still registered on the shared
    Base.metadata so alembic sees it and the runtime ORM can use it.

    The migration file stays at
    ``backend/data/migrations/versions/20260619_workspace_schedules.py`` —
    only the SQLAlchemy model moves to ``backend.schedule.infrastructure``.
    """
    # Importing the model registers it on the shared Base.metadata.
    import backend.schedule.infrastructure.schedule_db  # noqa: F401
    from backend.data import Base

    assert "workspace_schedules" in Base.metadata.tables


# ---------------------------------------------------------------------------
# Delta 8: SafeModeExpirySweepRunner still satisfies ScheduleRunnerProtocol
# ---------------------------------------------------------------------------


def test_safe_mode_expiry_satisfies_schedule_runner_protocol() -> None:
    """Workflow's :class:`SafeModeExpirySweepRunner` is a structural impl of
    the new :class:`ScheduleRunnerProtocol` — cross-context import via a
    domain Protocol is acceptable per DDD."""
    from backend.schedule.domain.runner_protocol import ScheduleRunnerProtocol
    from backend.workflow.application.safe_mode_expiry import SafeModeExpirySweepRunner

    runner: ScheduleRunnerProtocol = SafeModeExpirySweepRunner()  # structural check
    assert hasattr(runner, "fire_due")


# ---------------------------------------------------------------------------
# Delta 9: Worker registration in build_worker_runtime still works
# ---------------------------------------------------------------------------


def test_run_workers_still_registers_schedule_worker() -> None:
    """``backend.workflow.infrastructure.workers.run`` still imports the
    Schedule symbols (from the new location) and registers ``ScheduleWorker``
    in build_worker_runtime."""
    import inspect

    import backend.workflow.infrastructure.workers.run as run_mod

    src = inspect.getsource(run_mod)
    assert "backend.schedule.infrastructure.workers.schedule_worker" in src or (
        "backend.schedule.infrastructure.db_poll_runner" in src and "ScheduleWorker" in src
    )
    assert "from backend.workers" + ".schedule_runner" not in src


# ---------------------------------------------------------------------------
# Delta 10: workers/__main__.py entrypoint still resolves
# ---------------------------------------------------------------------------


def test_workers_dunder_main_still_boots() -> None:
    mod = importlib.import_module("backend.workers.__main__")
    assert hasattr(mod, "main")


# ---------------------------------------------------------------------------
# Delta 11: ScheduleTrigger still uses Workflow's TriggerEventRow + intake
# ---------------------------------------------------------------------------


def test_schedule_emitter_imports_workflow_intake() -> None:
    """The Schedule emitter writes to the Workflow context's
    ``trigger_events`` table via the new ``backend.workflow.infrastructure``
    locations (H3a moved those there)."""
    import inspect

    import backend.schedule.application.emitter as mod

    src = inspect.getsource(mod)
    assert "backend.workflow.infrastructure.intake.db" in src
    assert "backend.workflow.infrastructure.idempotency" in src
    assert "backend.workflow.domain.incoming" in src
    assert "backend.workflow.application.intake.webhook" in src
    assert "from backend.intake" not in src


# ---------------------------------------------------------------------------
# Delta 12: db_poll_runner / schedule_worker reference new schedule paths
# ---------------------------------------------------------------------------


def test_db_poll_runner_imports_new_paths() -> None:
    import inspect

    import backend.schedule.infrastructure.db_poll_runner as mod

    src = inspect.getsource(mod)
    assert "backend.schedule.application.emitter" in src
    assert "backend.schedule.infrastructure.schedule_db" in src
    assert "backend.schedule.domain.advancer" in src
    assert "from backend.intake" not in src
    assert "from backend.workers" + ".schedule_runner" not in src


def test_schedule_worker_imports_new_paths() -> None:
    import inspect

    import backend.schedule.infrastructure.workers.schedule_worker as mod

    src = inspect.getsource(mod)
    assert "backend.schedule.domain.runner_protocol" in src
    assert "from backend.intake" not in src
    assert "from backend.workers" + ".schedule_runner" not in src
